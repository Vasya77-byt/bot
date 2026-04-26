"""ИИ-анализ компании через GigaChat API (Сбер)."""

import asyncio
import logging
from typing import Optional

from schemas import CompanyData

logger = logging.getLogger("financial-architect")

_SYSTEM = (
    "Ты — опытный финансовый аналитик, помогаешь бизнесу оценивать контрагентов. "
    "Пиши живо, информативно, на русском языке. "
    "Используй эмодзи в начале каждого пункта для наглядности. "
    "Не используй markdown-форматирование (никаких **, ## и т.п.). "
    "Структура ответа строго следующая:\n"
    "1. Первая строка — только маркер риска и название компании: "
    "🔴 <название> (если много серьёзных рисков), "
    "🟡 <название> (если есть отдельные замечания), "
    "🟢 <название> (если компания надёжная и чистая).\n"
    "2. Далее 4-6 пунктов анализа, каждый с новой строки, с эмодзи.\n"
    "3. Последний блок — строка «Вывод:» и одно-два предложения итогового заключения."
)


def _company_to_text(c: CompanyData) -> str:
    parts = []
    if c.name:
        parts.append(f"Название: {c.name}")
    if c.inn:
        parts.append(f"ИНН: {c.inn}")
    if c.status:
        parts.append(f"Статус: {c.status}")
    if c.reg_date:
        age = f" ({c.age_years} лет)" if c.age_years else ""
        parts.append(f"Дата регистрации: {c.reg_date}{age}")
    if c.okved_main or c.okved_name:
        okved = c.okved_main or ""
        if c.okved_name:
            okved += f" — {c.okved_name}"
        parts.append(f"Основной ОКВЭД: {okved}")
    if c.director:
        parts.append(f"Руководитель: {c.director}")
    if c.address or c.region:
        parts.append(f"Адрес: {c.address or c.region}")
    if c.employees_count:
        parts.append(f"Сотрудники: {c.employees_count} чел.")
    if c.capital:
        parts.append(f"Уставный капитал: {c.capital:,.0f} руб.")
    if c.revenue_last_year:
        parts.append(f"Выручка (последний год): {c.revenue_last_year:,.0f} руб.")
    if c.profit_last_year:
        parts.append(f"Прибыль (последний год): {c.profit_last_year:,.0f} руб.")
    if c.reliability_rating:
        parts.append(f"Рейтинг надёжности: {c.reliability_rating}")
    if c.reliability_obligations:
        parts.append(f"Риски неисполнения обязательств: {c.reliability_obligations}")
    if c.reliability_shell:
        parts.append(f"Признаки однодневки: {c.reliability_shell}")
    if c.reliability_tax:
        parts.append(f"Налоговые риски: {c.reliability_tax}")
    if c.reliability_financial:
        parts.append(f"Финансовое положение: {c.reliability_financial}")
    return "\n".join(parts)


def _call_gigachat(credentials: str, prompt: str) -> str:
    """Синхронный вызов GigaChat (запускается через asyncio.to_thread)."""
    from gigachat import GigaChat
    from gigachat.models import Chat, Messages, MessagesRole

    with GigaChat(credentials=credentials, verify_ssl_certs=False) as giga:
        response = giga.chat(
            Chat(
                messages=[
                    Messages(role=MessagesRole.SYSTEM, content=_SYSTEM),
                    Messages(role=MessagesRole.USER, content=prompt),
                ],
                max_tokens=800,
            )
        )
    return response.choices[0].message.content


async def analyse_company(company: CompanyData, credentials: str) -> Optional[str]:
    """Возвращает краткий ИИ-анализ компании или None при ошибке."""
    if not credentials:
        return None

    data_text = _company_to_text(company)
    if not data_text:
        return None

    prompt = (
        f"Вот данные о компании:\n\n{data_text}\n\n"
        "Проанализируй эту компанию как потенциального контрагента: "
        "оцени надёжность, выяви риски, укажи сильные и слабые стороны. "
        "Соблюдай структуру из системного сообщения: маркер+название, пункты с эмодзи, Вывод."
    )

    try:
        text = await asyncio.to_thread(_call_gigachat, credentials, prompt)
        return text.strip() if text else None
    except Exception as exc:
        logger.warning("GigaChat analysis failed: %s", exc)
        return None
