"""Сборка веб-отчёта по компании в HTML.

Использует Jinja2-шаблон templates/report.html. Получает данные из
тех же источников что и обычный отчёт в боте: CompanyData, CardSummary,
SecurityResult, ArbitrationSummary.

Список факторов экспресс-отчёта собирается по всем известным флагам
из card + security. Положительные и отрицательные в раздельных списках.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

from risk_score import LEVEL_LABEL, calculate_risk_score

logger = logging.getLogger("financial-architect")

_TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")

_env = Environment(
    loader=FileSystemLoader(_TEMPLATES_DIR),
    autoescape=select_autoescape(["html", "xml"]),
)


def _fmt_money(v) -> str:
    try:
        v = float(v or 0)
    except (TypeError, ValueError):
        return "0 ₽"
    if v >= 1_000_000_000:
        return f"{v / 1_000_000_000:.1f} млрд ₽"
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f} млн ₽"
    if v >= 1_000:
        return f"{int(v / 1_000)} тыс ₽"
    return f"{int(v)} ₽"


_env.filters["fmt_money"] = _fmt_money


# ────────────────────────────────────────────────────────────────────
# Список факторов экспресс-отчёта.
#
# Каждый элемент:
#   key   — внутренний идентификатор
#   label — отображаемое название (как на скриншоте)
#   resolve(card, security) -> (is_negative, detail_text)
#
# Возвращает кортеж: (is_negative_bool, detail_text). Если фактор
# срабатывает (is_negative=True) — попадает в красный список.
# Если не срабатывает — в зелёный с пометкой 'Не найдено'.
# ────────────────────────────────────────────────────────────────────


def _collect_factors(card, security) -> tuple[list[dict], list[dict]]:
    pos: list[dict] = []
    neg: list[dict] = []

    def add(label: str, is_negative: bool, detail: str) -> None:
        target = neg if is_negative else pos
        target.append({"label": label, "detail": detail})

    has_card = card is not None
    has_sec = security is not None

    # ── Реестры ФНС / ФАС ──
    add(
        "Включена в реестр взыскиваемой задолженности ФНС",
        bool(has_card and card.in_debt_registry),
        "Найдено" if (has_card and card.in_debt_registry) else "Не найдено",
    )
    add(
        "Не предоставляет налоговую отчётность более года",
        bool(has_card and card.in_no_reporting_registry),
        "Найдено" if (has_card and card.in_no_reporting_registry) else "Не найдено",
    )
    add(
        "Адрес ЕГРЮЛ признан недостоверным",
        bool(has_card and card.address_invalid),
        "Найдено" if (has_card and card.address_invalid) else "Не найдено",
    )
    add(
        "В реестре недобросовестных поставщиков (ФАС)",
        bool(has_card and card.is_unreliable_supplier),
        "Найдено" if (has_card and card.is_unreliable_supplier) else "Не найдено",
    )

    # ── Массовость руководителя/учредителя ──
    add(
        "Массовый руководитель",
        bool(has_card and card.director_is_mass_leader),
        "Найдено" if (has_card and card.director_is_mass_leader) else "Не найдено",
    )
    add(
        "Массовый учредитель",
        bool(has_card and card.founder_is_mass),
        "Найдено" if (has_card and card.founder_is_mass) else "Не найдено",
    )

    # ── ФССП ──
    fssp_count = security.enforcement_count if has_sec else 0
    add(
        "Исполнительные производства ФССП",
        bool(fssp_count > 0),
        f"Производств: {fssp_count}" if fssp_count > 0 else "Не найдено",
    )

    # ── Налоговая задолженность ──
    tax_debt = card.tax_debt_sum if has_card else 0
    add(
        "Просроченная задолженность по налогам и сборам",
        bool(tax_debt > 0),
        _fmt_money(tax_debt) if tax_debt > 0 else "Не найдено",
    )

    # ── Статус компании ──
    is_liquidated = False
    is_in_reorg = False
    if has_card and card.status:
        s = card.status.lower()
        is_liquidated = "ликвид" in s
        is_in_reorg = "реорг" in s
    add(
        "Организация ликвидирована или ликвидируется",
        bool(is_liquidated),
        card.status if (has_card and is_liquidated) else "Не найдено",
    )
    add(
        "Организация в процессе реорганизации",
        bool(is_in_reorg),
        card.status if (has_card and is_in_reorg) else "Не найдено",
    )

    # ── Статичные плейсхолдеры (источники подключаются позже) ──
    # Сохраняем структуру отчёта (как на скриншотах) даже если
    # данных пока нет.
    placeholders = [
        "Завершённая процедура банкротства",
        "Подано заявление на ликвидацию",
        "Сообщения о завершённой процедуре банкротства менее 3 лет назад",
        "Санкционный список",
        "Приостановление операций по счетам организации",
        "Сведения о причастности к терроризму и экстремизму",
        "Признаки нелегальной деятельности на финрынке",
        "Субъект находится в реестре иноагентов",
        ("Сведения о лицах, подпадающих под условия "
         "подпункта «ф» пункта 1 статьи 23 Закона о регистрации"),
    ]
    for label in placeholders:
        add(label, False, "Не найдено")

    return neg, pos


def render_report(
    *,
    inn: str,
    company,
    card,
    security,
) -> bytes:
    """Рендерит HTML-отчёт. Возвращает bytes (utf-8)."""
    template = _env.get_template("report.html")

    # Базовые поля
    company_name = ""
    if card is not None and (card.name_short or card.name_full):
        company_name = card.name_short or card.name_full
    elif company is not None and company.name:
        company_name = company.name
    else:
        company_name = f"ИНН {inn}"

    status = ""
    status_active = False
    if card is not None and card.status:
        status = card.status
    elif company is not None and company.status:
        status = company.status
    if status:
        status_active = "действ" in status.lower()

    ogrn = ""
    if card is not None and card.ogrn:
        ogrn = card.ogrn
    elif company is not None and company.ogrn:
        ogrn = company.ogrn

    address = company.address if company is not None else ""

    # Учредители
    founders = []
    if card is not None:
        for f in (card.founders or [])[:8]:
            if not (f.name or f.inn):
                continue
            founders.append({
                "name": f.name or "Без имени",
                "inn": f.inn,
                "share_pct": f"{f.share_pct:g}" if f.share_pct else "",
            })

    # Финансы по годам
    finance_years = []
    if card is not None and card.finance_history:
        for y in card.finance_history[:5]:
            rev = float(y.revenue or y.income or 0)
            prof = float(y.profit or 0)
            if rev <= 0 and prof == 0:
                continue
            finance_years.append({
                "year": y.year,
                "revenue": rev,
                "profit": prof,
            })

    # Арбитраж
    arb = getattr(security, "zchb_arbitration", None) if security else None
    arb_count_p = arb.as_plaintiff_count if arb else 0
    arb_count_d = arb.as_defendant_count if arb else 0
    arb_total_p = arb.plaintiff_claim_sum if arb else 0
    arb_total_d = arb.defendant_claim_sum if arb else 0

    # Факторы экспресс-отчёта
    neg_factors, pos_factors = _collect_factors(card, security)

    # Итоговый риск
    score = calculate_risk_score(company, security)
    risk_level = score.level
    risk_label = LEVEL_LABEL.get(risk_level, "Низкий")
    risk_label_lower = risk_label.lower()

    director_name = (card.director_name if card else "") or ""
    director_inn = (card.director_inn if card else "") or ""

    capital = 0
    if card and card.capital:
        capital = card.capital
    elif company and company.capital:
        capital = company.capital

    tax_violations_sum = card.tax_violations_sum if card else 0

    context = {
        "company_name": company_name,
        "inn": inn,
        "ogrn": ogrn,
        "status": status or "—",
        "status_active": status_active,
        "director_name": director_name,
        "director_inn": director_inn,
        "address": address,
        "founders": founders,
        "finance_years": finance_years,
        "finance_years_json": json.dumps(finance_years, ensure_ascii=False),
        "capital": capital,
        "tax_violations_sum": tax_violations_sum,
        "arb_count_plaintiff": arb_count_p,
        "arb_count_defendant": arb_count_d,
        "arb_total_plaintiff": arb_total_p,
        "arb_total_defendant": arb_total_d,
        "neg_factors": neg_factors,
        "pos_factors": pos_factors,
        "risk_level": risk_level,
        "risk_label": risk_label,
        "risk_label_lower": risk_label_lower,
        "fmt_money": _fmt_money,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }
    html = template.render(**context)
    return html.encode("utf-8")
