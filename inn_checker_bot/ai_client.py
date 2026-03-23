"""
ИИ-рекомендация по компании.

Использует GigaChat API (Сбер) для генерации развёрнутого анализа.
Если API-ключ не задан — выдаёт правило-based рекомендацию.
"""

import logging
import time
import uuid
from typing import Any

import httpx

from config import GIGACHAT_MODEL

logger = logging.getLogger(__name__)

_TIMEOUT = 30.0


def _first_valid(*values):
    """Возвращает первое не-None значение (0 считается валидным, в отличие от `or`)."""
    for v in values:
        if v is not None:
            return v
    return None


_AUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
_CHAT_URL = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"

# Кэш OAuth-токена (живёт 30 мин, обновляем за 5 мин)
_cached_token: str | None = None
_token_expires: float = 0


async def _get_access_token(credentials: str) -> str:
    """Получает OAuth-токен GigaChat (кэшируется на 25 мин)."""
    global _cached_token, _token_expires

    now = time.time()
    if _cached_token and now < _token_expires:
        return _cached_token

    async with httpx.AsyncClient(verify=False, timeout=15.0) as client:
        resp = await client.post(
            _AUTH_URL,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "RqUID": str(uuid.uuid4()),
                "Authorization": f"Basic {credentials}",
            },
            data={"scope": "GIGACHAT_API_PERS"},
        )

        if resp.status_code != 200:
            raise RuntimeError(f"GigaChat auth error {resp.status_code}: {resp.text[:200]}")

        data = resp.json()
        _cached_token = data["access_token"]
        _token_expires = now + 25 * 60  # 25 мин
        logger.info("GigaChat token obtained, expires in 25 min")
        return _cached_token


async def generate_recommendation(
    fields: dict[str, Any],
    zchb_data: dict[str, Any] | None = None,
    zsk_data: dict[str, Any] | None = None,
    rp_data: dict[str, Any] | None = None,
    fin_history: dict[str, Any] | None = None,
    fns_data: dict[str, Any] | None = None,
    api_key: str | None = None,
) -> str:
    """
    Генерирует ИИ-рекомендацию по компании.
    Если api_key задан — GigaChat API, иначе — правило-based.
    """
    zchb = zchb_data or {}
    zsk = zsk_data or {}
    rp = rp_data or {}
    fin = fin_history or {}
    fns = fns_data or {}

    company_summary = _build_company_summary(fields, zchb, zsk, rp, fin, fns)

    if api_key:
        try:
            return await _ask_gigachat(company_summary, api_key)
        except Exception as e:
            logger.warning("GigaChat API error: %s, falling back to rules", e)
            return _rule_based_recommendation(fields, zchb, zsk, rp, fin, fns)
    else:
        return _rule_based_recommendation(fields, zchb, zsk, rp, fin, fns)


def _build_company_summary(
    fields: dict, zchb: dict, zsk: dict, rp: dict, fin: dict, fns: dict | None = None
) -> str:
    """Собирает текстовое описание компании для ИИ."""
    parts: list[str] = []

    name = fields.get("name", "Неизвестно")
    inn = fields.get("inn", "—")
    parts.append(f"Компания: {name} (ИНН: {inn})")

    status = fields.get("status", "н/д")
    parts.append(f"Статус: {status}")

    reg = fields.get("registration_date")
    age = fields.get("company_age_years")
    if reg:
        parts.append(f"Дата регистрации: {reg}" + (f" ({age} лет)" if age else ""))

    city = fields.get("city")
    if city:
        parts.append(f"Город: {city}")

    okved = fields.get("okved_code")
    okved_text = fields.get("okved_text")
    if okved:
        parts.append(f"ОКВЭД: {okved}" + (f" — {okved_text}" if okved_text else ""))

    cap = fields.get("capital_value")
    if cap is not None:
        parts.append(f"Уставный капитал: {cap:,.0f} ₽")

    mgr = fields.get("management_name")
    if mgr:
        post = fields.get("management_post", "")
        parts.append(f"Руководитель: {mgr}" + (f" ({post})" if post else ""))

    # Финансы (приоритет: FNS bo → ZSK → Rusprofile → DaData; 0 — валидное значение)
    _fns = fns or {}
    fns_bo = _fns.get("bo") or {}
    rev = _first_valid(zchb.get("revenue"), fns_bo.get("revenue"), fields.get("income"))
    profit = _first_valid(zchb.get("net_profit"), fns_bo.get("net_profit"), zsk.get("net_profit"))
    if rev is not None:
        parts.append(f"Выручка: {rev:,.0f} ₽")
    if profit is not None:
        parts.append(f"Чистая прибыль: {profit:,.0f} ₽")
    total_assets = fns_bo.get("total_assets")
    if total_assets is not None:
        parts.append(f"Активы: {total_assets:,.0f} ₽")

    # Финансовая история
    fin_years = fin.get("years", [])
    if fin_years:
        trend = fin.get("trend", "н/д")
        parts.append(f"Тренд дохода: {trend}")
        year_strs = []
        for yd in fin_years[:3]:
            y = yd["year"]
            inc = yd.get("income")
            if inc is not None:
                year_strs.append(f"{y}: {inc:,.0f} ₽")
        if year_strs:
            parts.append(f"Доход по годам: {', '.join(year_strs)}")

    # Штат
    emp = _first_valid(zchb.get("employee_count"), zsk.get("employee_count"), fields.get("employee_count"))
    if emp is not None and emp > 0:
        parts.append(f"Штат: {emp} чел.")

    # Суды
    courts_total = _first_valid(zchb.get("courts_total"), zsk.get("courts_total"))
    if courts_total is not None:
        defendant = _first_valid(zchb.get("courts_defendant"), zsk.get("courts_defendant")) or 0
        plaintiff = _first_valid(zchb.get("courts_plaintiff"), zsk.get("courts_plaintiff")) or 0
        courts_sum = _first_valid(zchb.get("courts_plaintiff_sum"), zsk.get("courts_sum"))
        parts.append(f"Суды: {courts_total} дел (ответчик {defendant}, истец {plaintiff})")
        if courts_sum:
            parts.append(f"Общая сумма судов: {courts_sum:,.0f} ₽")
        active_sum = zsk.get("courts_active_sum")
        if active_sum:
            parts.append(f"На рассмотрении: {active_sum:,.0f} ₽")

    # ФССП
    fssp = zsk.get("fssp_total")
    if fssp is not None:
        parts.append(f"ФССП: {fssp} производств")
        fssp_sum = zsk.get("fssp_sum")
        if fssp_sum:
            parts.append(f"Сумма ФССП: {fssp_sum:,.0f} ₽")

    # Надёжность
    color = zsk.get("reliability_color") or rp.get("reliability_color")
    label = zsk.get("reliability_label") or rp.get("reliability_label")
    if color:
        parts.append(f"Оценка надёжности: {label or color}")

    green_f = zsk.get("green_facts", 0)
    yellow_f = zsk.get("yellow_facts", 0)
    red_f = zsk.get("red_facts", 0)
    if green_f or yellow_f or red_f:
        parts.append(f"Факты: зелёных {green_f}, жёлтых {yellow_f}, красных {red_f}")

    # Данные ФНС (API-FNS) — _fns уже определён выше
    check = _fns.get("check") or {}
    nalogbi = _fns.get("nalogbi") or {}
    fns_zsk = _fns.get("zsk") or {}

    fns_risks = []
    if check.get("mass_director"):
        fns_risks.append("массовый руководитель")
    if check.get("mass_address"):
        fns_risks.append("массовый адрес")
    if check.get("unreliable_address"):
        fns_risks.append("недостоверный адрес")
    if check.get("unreliable_director"):
        fns_risks.append("недостоверный руководитель")
    if check.get("disqualified"):
        fns_risks.append("дисквалификация руководителя")
    if check.get("tax_debt"):
        fns_risks.append("задолженность по налогам")
    if check.get("no_reports"):
        fns_risks.append("не сдаёт отчётность")
    if check.get("liquidation_decision"):
        fns_risks.append("решение о ликвидации")

    if fns_risks:
        parts.append(f"Признаки ФНС: {', '.join(fns_risks)}")
    elif check.get("source"):
        parts.append("Проверка ФНС: негативных признаков нет")

    if nalogbi.get("has_blocked_accounts"):
        cnt = nalogbi.get("blocked_accounts_count", 0)
        parts.append(f"Блокировка счетов ФНС: {cnt} решений")
    elif nalogbi.get("source"):
        parts.append("Блокировка счетов: нет")

    if fns_zsk.get("zsk_color"):
        parts.append(f"ЗСК ЦБ: {fns_zsk.get('zsk_level', fns_zsk['zsk_color'])}")

    return "\n".join(parts)


async def _ask_gigachat(company_summary: str, credentials: str) -> str:
    """Отправляет запрос к GigaChat API."""
    # 1. Получаем OAuth-токен
    token = await _get_access_token(credentials)

    system_prompt = (
        "Ты — эксперт по проверке контрагентов в России. "
        "На основании данных о компании дай краткую (3-5 предложений) рекомендацию: "
        "стоит ли работать с этой компанией. "
        "Укажи основные риски и положительные стороны. "
        "Используй деловой тон. Отвечай на русском языке. "
        "НЕ используй markdown-разметку. Используй plain text с эмодзи."
    )

    # 2. Отправляем запрос к chat/completions
    async with httpx.AsyncClient(verify=False, timeout=_TIMEOUT) as client:
        resp = await client.post(
            _CHAT_URL,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
            },
            json={
                "model": GIGACHAT_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": f"Проанализируй компанию и дай рекомендацию:\n\n{company_summary}",
                    },
                ],
                "max_tokens": 500,
                "temperature": 0.3,
            },
        )

        if resp.status_code == 401:
            # Токен истёк — сбросим кэш и попробуем ещё раз
            global _cached_token, _token_expires
            _cached_token = None
            _token_expires = 0
            logger.info("GigaChat token expired, retrying...")
            token = await _get_access_token(credentials)
            resp = await client.post(
                _CHAT_URL,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Authorization": f"Bearer {token}",
                },
                json={
                    "model": GIGACHAT_MODEL,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {
                            "role": "user",
                            "content": f"Проанализируй компанию и дай рекомендацию:\n\n{company_summary}",
                        },
                    ],
                    "max_tokens": 500,
                    "temperature": 0.3,
                },
            )

        if resp.status_code != 200:
            logger.warning("GigaChat API HTTP %s: %s", resp.status_code, resp.text[:300])
            raise RuntimeError(f"GigaChat API error {resp.status_code}")

        data = resp.json()
        choices = data.get("choices", [])
        if choices:
            message = choices[0].get("message", {})
            content = message.get("content", "")
            if content:
                return content

        raise RuntimeError("Empty GigaChat response")


def _rule_based_recommendation(
    fields: dict, zchb: dict, zsk: dict, rp: dict, fin: dict, fns: dict | None = None
) -> str:
    """Правило-based рекомендация (без ИИ)."""
    risks: list[str] = []
    positives: list[str] = []
    score = 50  # Базовый балл

    # Статус
    _STATUS_RU = {
        "ACTIVE": "Действующая", "LIQUIDATING": "Ликвидируется",
        "LIQUIDATED": "Ликвидирована", "BANKRUPT": "Банкрот",
        "REORGANIZING": "Реорганизация",
    }
    status = fields.get("status")
    if status == "ACTIVE":
        positives.append("компания действующая")
        score += 10
    elif status in ("LIQUIDATING", "LIQUIDATED", "BANKRUPT"):
        status_ru = _STATUS_RU.get(status, status)
        risks.append(f"компания в статусе «{status_ru}» — высокий риск")
        score -= 40

    # Возраст
    age = fields.get("company_age_years")
    if age is not None:
        if age >= 5:
            positives.append(f"работает {age} лет")
            score += 10
        elif age >= 3:
            positives.append(f"на рынке {age} года")
            score += 5
        elif age < 1:
            risks.append("компания моложе 1 года")
            score -= 15

    # Уставный капитал
    cap = fields.get("capital_value")
    if cap is not None:
        if cap <= 10_000:
            risks.append(f"минимальный уставный капитал ({cap:,.0f} ₽)")
            score -= 5
        elif cap >= 1_000_000:
            positives.append(f"уставный капитал {cap:,.0f} ₽")
            score += 5

    # Выручка
    rev = _first_valid(zchb.get("revenue"), fields.get("income"))
    if rev is not None:
        if rev > 100_000_000:
            positives.append("выручка свыше 100 млн ₽")
            score += 10
        elif rev > 10_000_000:
            positives.append("выручка свыше 10 млн ₽")
            score += 5
        elif rev < 1_000_000 and rev > 0:
            risks.append("низкая выручка (менее 1 млн ₽)")
            score -= 5

    # Тренд
    trend = fin.get("trend")
    if trend == "up":
        positives.append("доход растёт")
        score += 5
    elif trend == "down":
        risks.append("доход падает")
        score -= 10

    # Суды (ЗЧБ API → scraping)
    courts_total = _first_valid(zchb.get("courts_total"), zsk.get("courts_total"))
    defendant = _first_valid(zchb.get("courts_defendant"), zsk.get("courts_defendant")) or 0
    if courts_total is not None:
        if courts_total == 0:
            positives.append("нет судебных дел")
            score += 10
        elif defendant > 50:
            risks.append(f"большое число судов как ответчик ({defendant})")
            score -= 15
        elif defendant > 10:
            risks.append(f"есть суды как ответчик ({defendant})")
            score -= 5

    courts_sum = _first_valid(zchb.get("courts_defendant_sum"), zsk.get("courts_sum"))
    if courts_sum and courts_sum > 100_000_000:
        risks.append(f"сумма судов превышает 100 млн ₽")
        score -= 10

    # ФССП (ЗЧБ API → scraping)
    fssp = _first_valid(zchb.get("fssp_count"), zsk.get("fssp_total"))
    if fssp is not None:
        if fssp == 0:
            positives.append("нет исполнительных производств")
            score += 5
        elif fssp > 10:
            risks.append(f"много исп. производств ({fssp})")
            score -= 15
        elif fssp > 0:
            risks.append(f"есть исп. производства ({fssp})")
            score -= 5

    # Террорист/стоп-лист (ЗЧБ API)
    if zchb.get("terrorist"):
        risks.append("ВНИМАНИЕ: в реестре террористов/экстремистов!")
        score -= 50

    # Светофор (scraping — только для надёжности)
    color = zsk.get("reliability_color") or rp.get("reliability_color")
    if color == "green":
        positives.append("оценка надёжности — зелёная")
        score += 10
    elif color == "red":
        risks.append("оценка надёжности — красная")
        score -= 20
    elif color == "yellow":
        risks.append("оценка надёжности — жёлтая")
        score -= 5

    # Красные факты
    red_f = zsk.get("red_facts", 0)
    if red_f and red_f > 5:
        risks.append(f"много красных фактов ({red_f})")
        score -= 10

    # Данные ФНС (API-FNS)
    _fns = fns or {}
    check = _fns.get("check") or {}
    nalogbi = _fns.get("nalogbi") or {}
    fns_zsk = _fns.get("zsk") or {}

    if check.get("mass_director"):
        risks.append("массовый руководитель (ФНС)")
        score -= 15
    if check.get("mass_address"):
        risks.append("массовый адрес регистрации (ФНС)")
        score -= 10
    if check.get("unreliable_address"):
        risks.append("недостоверный адрес (ФНС)")
        score -= 15
    if check.get("unreliable_director"):
        risks.append("недостоверный руководитель (ФНС)")
        score -= 20
    if check.get("disqualified"):
        risks.append("дисквалификация руководителя (ФНС)")
        score -= 30
    if check.get("tax_debt"):
        risks.append("задолженность по налогам (ФНС)")
        score -= 15
    if check.get("no_reports"):
        risks.append("не сдаёт отчётность (ФНС)")
        score -= 20
    if check.get("liquidation_decision"):
        risks.append("решение о ликвидации (ФНС)")
        score -= 30

    if nalogbi.get("has_blocked_accounts"):
        cnt = nalogbi.get("blocked_accounts_count", 0)
        risks.append(f"блокировка счетов ФНС ({cnt} решений)")
        score -= 25
    elif nalogbi.get("source") and not nalogbi.get("has_blocked_accounts"):
        positives.append("нет блокировок счетов (ФНС)")
        score += 5

    if check.get("source") and not any(
        check.get(k) for k in [
            "mass_director", "mass_address", "unreliable_address",
            "unreliable_director", "disqualified", "tax_debt",
            "no_reports", "liquidation_decision",
        ]
    ):
        positives.append("проверка ФНС пройдена без замечаний")
        score += 10

    # Формируем текст
    score = max(0, min(100, score))
    lines: list[str] = []

    if score >= 70:
        lines.append("🟢 <b>Рекомендация: МОЖНО работать</b>")
    elif score >= 40:
        lines.append("🟡 <b>Рекомендация: ОСТОРОЖНО</b>")
    else:
        lines.append("🔴 <b>Рекомендация: НЕ РЕКОМЕНДУЕТСЯ</b>")

    lines.append(f"Балл надёжности: {score}/100")
    lines.append("")

    if positives:
        lines.append("✅ Плюсы:")
        for p in positives:
            lines.append(f"  • {p}")

    if risks:
        lines.append("⚠️ Риски:")
        for r in risks:
            lines.append(f"  • {r}")

    if not risks and not positives:
        lines.append("ℹ️ Недостаточно данных для полной оценки.")

    return "\n".join(lines)
