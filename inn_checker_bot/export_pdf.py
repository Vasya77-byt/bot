"""
Генерация PDF-отчёта по компании.

Использует reportlab для создания красивого PDF с кириллицей.
"""

import io
import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib.colors import HexColor
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    )
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    HAS_REPORTLAB = True
except ImportError:
    HAS_REPORTLAB = False
    logger.warning("reportlab not installed — PDF export disabled")

# Цвета (инициализируются только если reportlab доступен)
GREEN = RED = YELLOW = GRAY = DARK = LIGHT_BG = None
if HAS_REPORTLAB:
    GREEN = HexColor("#27ae60")
    RED = HexColor("#e74c3c")
    YELLOW = HexColor("#f39c12")
    GRAY = HexColor("#7f8c8d")
    DARK = HexColor("#2c3e50")
    LIGHT_BG = HexColor("#ecf0f1")

_FONT_REGISTERED = False


def _register_fonts():
    """Регистрирует шрифт с кириллицей."""
    global _FONT_REGISTERED
    if _FONT_REGISTERED:
        return

    font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/Library/Fonts/Arial Unicode.ttf",
    ]
    for path in font_paths:
        try:
            pdfmetrics.registerFont(TTFont("CyrFont", path))
            _FONT_REGISTERED = True
            return
        except Exception:
            continue

    logger.warning("No Cyrillic font found, PDF may have encoding issues")
    _FONT_REGISTERED = True  # Prevent retrying


def _get_styles():
    """Создаёт стили для PDF."""
    styles = getSampleStyleSheet()
    font_name = "CyrFont" if _FONT_REGISTERED else "Helvetica"

    styles.add(ParagraphStyle(
        "Title_RU", parent=styles["Title"],
        fontName=font_name, fontSize=16, textColor=DARK,
        spaceAfter=6 * mm,
    ))
    styles.add(ParagraphStyle(
        "Heading_RU", parent=styles["Heading2"],
        fontName=font_name, fontSize=12, textColor=DARK,
        spaceBefore=4 * mm, spaceAfter=2 * mm,
    ))
    styles.add(ParagraphStyle(
        "Body_RU", parent=styles["Normal"],
        fontName=font_name, fontSize=10, leading=14,
    ))
    styles.add(ParagraphStyle(
        "Small_RU", parent=styles["Normal"],
        fontName=font_name, fontSize=8, textColor=GRAY,
    ))
    styles.add(ParagraphStyle(
        "Risk_Green", parent=styles["Normal"],
        fontName=font_name, fontSize=11, textColor=GREEN,
    ))
    styles.add(ParagraphStyle(
        "Risk_Red", parent=styles["Normal"],
        fontName=font_name, fontSize=11, textColor=RED,
    ))
    styles.add(ParagraphStyle(
        "Risk_Yellow", parent=styles["Normal"],
        fontName=font_name, fontSize=11, textColor=YELLOW,
    ))
    return styles


def _money(v: float | int | None) -> str:
    if v is None:
        return "н/д"
    v = float(v)
    if abs(v) >= 1_000_000_000:
        return f"{v / 1_000_000_000:.1f} млрд руб."
    elif abs(v) >= 1_000_000:
        return f"{v / 1_000_000:.1f} млн руб."
    elif abs(v) >= 1_000:
        return f"{v / 1_000:.0f} тыс руб."
    else:
        return f"{v:,.0f} руб.".replace(",", " ")


def _status_text(status: str | None) -> str:
    return {
        "ACTIVE": "Действующая",
        "LIQUIDATING": "Ликвидируется",
        "LIQUIDATED": "Ликвидирована",
        "BANKRUPT": "Банкрот",
        "REORGANIZING": "Реорганизация",
    }.get(status or "", status or "н/д")


def generate_report_pdf(
    fields: dict[str, Any],
    zsk_data: dict[str, Any] | None = None,
    fns_data: dict[str, Any] | None = None,
    fin_history: dict[str, Any] | None = None,
    sanctions_data: dict[str, Any] | None = None,
    cbrf_data: dict[str, Any] | None = None,
) -> bytes:
    """Генерирует PDF-отчёт. Возвращает bytes."""
    if not HAS_REPORTLAB:
        raise RuntimeError("reportlab не установлен. pip install reportlab")

    _register_fonts()
    styles = _get_styles()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=20 * mm, rightMargin=20 * mm,
        topMargin=15 * mm, bottomMargin=15 * mm,
    )

    story = []
    zsk = zsk_data or {}
    fns = fns_data or {}
    fin = fin_history or {}
    sanctions = sanctions_data or {}
    cbrf = cbrf_data or {}

    entity_type = fields.get("entity_type", "ul")
    is_ip = entity_type == "ip"
    name = fields.get("name") or "Неизвестно"

    # ── Заголовок ──
    story.append(Paragraph(f"Отчёт о проверке контрагента", styles["Title_RU"]))
    story.append(Paragraph(name, styles["Heading_RU"]))
    story.append(Paragraph(
        f"Дата: {datetime.now().strftime('%d.%m.%Y %H:%M')} | "
        f"{'ИП' if is_ip else 'ЮЛ'} | Источник: @Fridaycompany_bot",
        styles["Small_RU"],
    ))
    story.append(Spacer(1, 4 * mm))

    # ── Реквизиты ──
    story.append(Paragraph("Реквизиты", styles["Heading_RU"]))
    rekvizity = [
        ["ИНН", fields.get("inn", "—")],
        ["Статус", _status_text(fields.get("status"))],
    ]
    if not is_ip and fields.get("kpp"):
        rekvizity.append(["КПП", fields["kpp"]])
    if fields.get("ogrn"):
        rekvizity.append(["ОГРНИП" if is_ip else "ОГРН", fields["ogrn"]])
    if fields.get("registration_date"):
        age = fields.get("company_age_years")
        age_s = f" ({age} лет)" if age else ""
        rekvizity.append(["Дата регистрации", f"{fields['registration_date']}{age_s}"])
    if not is_ip and fields.get("management_name"):
        post = fields.get("management_post", "")
        rekvizity.append(["Руководитель", f"{fields['management_name']} ({post})" if post else fields["management_name"]])
    if fields.get("city"):
        rekvizity.append(["Город", fields["city"]])
    if fields.get("okved_code"):
        okved_text = fields.get("okved_text", "")
        rekvizity.append(["ОКВЭД", f"{fields['okved_code']} — {okved_text[:60]}"])
    if not is_ip and fields.get("capital_value") is not None:
        rekvizity.append(["Уставный капитал", _money(fields["capital_value"])])
    if fields.get("tax_system"):
        rekvizity.append(["Налогообложение", str(fields["tax_system"])])

    font_name = "CyrFont" if _FONT_REGISTERED else "Helvetica"
    t = Table(rekvizity, colWidths=[45 * mm, 120 * mm])
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), font_name),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("FONTNAME", (0, 0), (0, -1), font_name),
        ("TEXTCOLOR", (0, 0), (0, -1), GRAY),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(t)
    story.append(Spacer(1, 3 * mm))

    # ── Финансы ──
    fns_bo = fns.get("bo") or {}
    rev = fns_bo.get("revenue") if fns_bo.get("revenue") is not None else zsk.get("revenue") if zsk.get("revenue") is not None else fields.get("income")
    profit = fns_bo.get("net_profit") if fns_bo.get("net_profit") is not None else zsk.get("net_profit")

    if rev is not None or profit is not None:
        story.append(Paragraph("Финансы", styles["Heading_RU"]))
        fin_rows = []
        if rev is not None:
            fin_rows.append(["Выручка", _money(rev)])
        if profit is not None:
            fin_rows.append(["Чистая прибыль", _money(profit)])
        if rev and rev > 0 and profit is not None:
            margin = profit / rev * 100
            fin_rows.append(["Рентабельность", f"{margin:.1f}%"])
        emp = zsk.get("employee_count") or fields.get("employee_count")
        if emp:
            fin_rows.append(["Штат", f"{emp} чел."])
        year_label = ""
        if fns_bo.get("latest_year"):
            year_label = f" (за {fns_bo['latest_year']} г., ФНС)"
        fin_rows.append(["Источник", year_label or "—"])
        t = Table(fin_rows, colWidths=[45 * mm, 120 * mm])
        t.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), font_name),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("TEXTCOLOR", (0, 0), (0, -1), GRAY),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ]))
        story.append(t)
        story.append(Spacer(1, 3 * mm))

    # ── Учредители ──
    if not is_ip:
        founders = fields.get("founders") or []
        if founders:
            story.append(Paragraph("Учредители", styles["Heading_RU"]))
            for f in founders[:10]:
                fname = f.get("name", "—")
                share = f.get("share")
                share_s = f" — {share}%" if share is not None else ""
                story.append(Paragraph(f"• {fname}{share_s}", styles["Body_RU"]))
            story.append(Spacer(1, 2 * mm))

    # ── Суды ──
    courts_total = zsk.get("courts_total")
    if courts_total is not None:
        story.append(Paragraph("Суды (арбитраж)", styles["Heading_RU"]))
        if courts_total == 0:
            story.append(Paragraph("Судебные дела не найдены", styles["Body_RU"]))
        else:
            defendant = zsk.get("courts_defendant", 0)
            plaintiff = zsk.get("courts_plaintiff", 0)
            courts_sum = zsk.get("courts_sum")
            story.append(Paragraph(
                f"Всего дел: {courts_total} (ответчик: {defendant}, истец: {plaintiff})",
                styles["Body_RU"],
            ))
            if courts_sum:
                story.append(Paragraph(f"Общая сумма: {_money(courts_sum)}", styles["Body_RU"]))
        story.append(Spacer(1, 2 * mm))

    # ── ФССП ──
    fssp = zsk.get("fssp_total")
    if fssp is not None:
        story.append(Paragraph("ФССП (приставы)", styles["Heading_RU"]))
        if fssp == 0:
            story.append(Paragraph("Исполнительных производств нет", styles["Body_RU"]))
        else:
            fssp_sum = zsk.get("fssp_sum")
            s = f"Производств: {fssp}"
            if fssp_sum:
                s += f", на сумму {_money(fssp_sum)}"
            story.append(Paragraph(s, styles["Body_RU"]))
        story.append(Spacer(1, 2 * mm))

    # ── Проверка ФНС ──
    check = fns.get("check") or {}
    nalogbi = fns.get("nalogbi") or {}
    if check.get("source") or nalogbi.get("source"):
        story.append(Paragraph("Проверка ФНС", styles["Heading_RU"]))
        risks = []
        if check.get("mass_director"):
            risks.append("Массовый руководитель")
        if check.get("mass_address"):
            risks.append("Массовый адрес")
        if check.get("unreliable_address"):
            risks.append("Недостоверный адрес")
        if check.get("unreliable_director"):
            risks.append("Недостоверный руководитель")
        if check.get("disqualified"):
            risks.append("Дисквалификация руководителя")
        if check.get("tax_debt"):
            risks.append("Задолженность по налогам")
        if check.get("no_reports"):
            risks.append("Не сдаёт отчётность")
        if check.get("liquidation_decision"):
            risks.append("Решение о ликвидации")
        if risks:
            for r in risks:
                story.append(Paragraph(f"⚠ {r}", styles["Risk_Red"]))
        elif check.get("clean"):
            story.append(Paragraph("Негативных признаков не выявлено", styles["Risk_Green"]))

        if nalogbi.get("has_blocked_accounts"):
            cnt = nalogbi.get("blocked_accounts_count", 0)
            story.append(Paragraph(f"Блокировка счетов: {cnt} решений", styles["Risk_Red"]))
        elif nalogbi.get("source"):
            story.append(Paragraph("Блокировка счетов: нет", styles["Risk_Green"]))
        story.append(Spacer(1, 2 * mm))

    # ── Отказы банков (ЦБ) ──
    if cbrf.get("source"):
        story.append(Paragraph("Отказы банков (ЦБ 550-П)", styles["Heading_RU"]))
        if cbrf.get("found"):
            cnt = cbrf.get("count", 0)
            story.append(Paragraph(f"Найдено отказов: {cnt}", styles["Risk_Red"]))
        else:
            story.append(Paragraph("Отказов не найдено", styles["Risk_Green"]))
        story.append(Spacer(1, 2 * mm))

    # ── Санкции ──
    if sanctions.get("source"):
        story.append(Paragraph("Санкции", styles["Heading_RU"]))
        if sanctions.get("found"):
            story.append(Paragraph("НАЙДЕН в санкционных списках!", styles["Risk_Red"]))
            for m in sanctions.get("matches", []):
                story.append(Paragraph(
                    f"• {m.get('name', '?')} (совпадение {m.get('score', 0)}%)",
                    styles["Body_RU"],
                ))
        else:
            story.append(Paragraph("Не найден в санкционных списках", styles["Risk_Green"]))
        story.append(Spacer(1, 2 * mm))

    # ── Оценка риска ──
    fns_zsk = fns.get("zsk") or {}
    zsk_color = fns_zsk.get("zsk_color") or zsk.get("reliability_color")
    zsk_label = fns_zsk.get("zsk_level") or zsk.get("reliability_label") or "Нет данных"

    story.append(Paragraph("Оценка риска", styles["Heading_RU"]))
    style_map = {"green": "Risk_Green", "red": "Risk_Red", "yellow": "Risk_Yellow"}
    risk_style = style_map.get(zsk_color, "Body_RU")
    story.append(Paragraph(zsk_label, styles[risk_style]))

    # ── Лицензии ──
    licenses = fields.get("licenses") or []
    if licenses:
        story.append(Spacer(1, 2 * mm))
        story.append(Paragraph("Лицензии", styles["Heading_RU"]))
        for lic in licenses[:10]:
            story.append(Paragraph(f"• {lic[:100]}", styles["Body_RU"]))

    # ── Footer ──
    story.append(Spacer(1, 8 * mm))
    story.append(Paragraph(
        f"Отчёт сформирован ботом @Fridaycompany_bot | {datetime.now().strftime('%d.%m.%Y %H:%M')}",
        styles["Small_RU"],
    ))

    doc.build(story)
    return buf.getvalue()
