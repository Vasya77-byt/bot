"""AI-инсайты «3 риска которые вы пропустили» — короткий блок от GigaChat.

Принцип: на основе данных о компании + сработавших риск-факторов GigaChat
выделяет 1–3 НАИБОЛЕЕ ВАЖНЫХ риска одним-двумя предложениями каждый.
Это не общий summary, а точечные находки.

Защиты:
* GigaChat не настроен (нет credentials) → return None
* У компании нет факторов риска (чистая, score 100/100) → return None
  (не вызываем GigaChat впустую — нет рисков о которых говорить)
* GigaChat вернул мусор / упал / timeout → return None
* GigaChat вернул < 3 пунктов → возвращаем сколько есть

Промпт устроен так, чтобы модель работала ТОЛЬКО с предоставленными
фактами и не выдумывала «возможные» риски, которых нет в данных.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import List, Optional

from reputation_score import ReputationScore
from schemas import CompanyData
from security_check import SecurityResult

logger = logging.getLogger("financial-architect")


SYSTEM_PROMPT = (
    "Ты — финансовый аналитик. Твоя задача — на основе данных о российской "
    "компании выделить 3 НАИБОЛЕЕ ВАЖНЫХ риска для предпринимателя, "
    "который рассматривает её как контрагента."
    "\n\n"
    "СТРОГИЕ ПРАВИЛА:\n"
    "1. Опирайся ТОЛЬКО на факты ниже. Не выдумывай «возможные» риски.\n"
    "2. Каждый риск = одна короткая фраза, до 120 символов.\n"
    "3. Начинай каждый пункт с эмодзи: 🚨 (критично) / ⚠️ (важно) / 💡 (обратить внимание).\n"
    "4. Без вступления, без выводов, без советов — только 3 пункта подряд.\n"
    "5. Формат строго:\n"
    "1. <эмодзи> <риск>\n"
    "2. <эмодзи> <риск>\n"
    "3. <эмодзи> <риск>\n"
    "6. Если выявленных факторов меньше 3 — назови столько сколько есть."
)


def _fmt_money(value: Optional[float]) -> str:
    if value is None:
        return "нет данных"
    if abs(value) >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f} млрд ₽"
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:.1f} млн ₽"
    if abs(value) >= 1_000:
        return f"{value / 1_000:.0f} тыс ₽"
    return f"{value:.0f} ₽"


def _build_prompt(
    company: CompanyData,
    security: Optional[SecurityResult],
    reputation: ReputationScore,
) -> str:
    parts = [SYSTEM_PROMPT, "", "ДАННЫЕ О КОМПАНИИ:"]
    parts.append(f"• Название: {company.name or '—'}")
    parts.append(f"• ИНН: {company.inn or '—'}")
    if company.status:
        parts.append(f"• Статус: {company.status}")
    if company.age_years is not None:
        parts.append(f"• Возраст: {company.age_years} лет")
    if company.okved_main:
        parts.append(f"• ОКВЭД: {company.okved_main} {company.okved_name or ''}")
    if company.capital is not None:
        parts.append(f"• Уставный капитал: {_fmt_money(company.capital)}")
    if company.revenue_last_year is not None:
        parts.append(f"• Выручка (последний год): {_fmt_money(company.revenue_last_year)}")
    if company.profit_last_year is not None:
        parts.append(f"• Прибыль (последний год): {_fmt_money(company.profit_last_year)}")
    if company.employees_count is not None:
        parts.append(f"• Штат: {company.employees_count} чел.")

    if security:
        if security.enforcement_count:
            parts.append(
                f"• ФССП: {security.enforcement_count} производств "
                f"на сумму {_fmt_money(security.enforcement_total_sum)}"
            )
        v = getattr(security, "inspections_violations_count", 0) or 0
        if v:
            parts.append(f"• Регуляторные проверки: {v} с нарушениями")

    parts.append("")
    parts.append(f"REPUTATION SCORE: {reputation.score}/100")
    parts.append("")
    parts.append("СРАБОТАВШИЕ ФАКТОРЫ РИСКА:")
    has_factors = False
    for cat in reputation.categories:
        for f in cat.factors:
            parts.append(f"• [{cat.name}] {f.label}")
            has_factors = True
    if not has_factors:
        parts.append("• нет")

    parts.append("")
    parts.append("ОТВЕТ (3 пункта строго в формате):")
    return "\n".join(parts)


# Регулярка для строки вида "1. 🚨 ..." или "1) 🚨 ..." или "🚨 ..."
_BULLET_RE = re.compile(
    r"^\s*(?:[\d]+[\.\)]\s*)?([🚨⚠️💡⚡🔴🟠🟡][\s\S]+?)\s*$"
)


def _parse_insights(raw: str) -> List[str]:
    """Из ответа модели вытаскиваем чистые строки риск-пунктов."""
    if not raw:
        return []
    out: List[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _BULLET_RE.match(line)
        if m:
            txt = m.group(1).strip()
            # Обрезаем слишком длинные ответы (модель иногда нарушает 120)
            if len(txt) > 200:
                txt = txt[:197] + "…"
            out.append(txt)
        if len(out) >= 3:
            break
    return out


def _format_block(insights: List[str]) -> str:
    lines = ["🤖 На что обратить внимание:", ""]
    for ins in insights:
        lines.append(f"• {ins}")
    return "\n".join(lines)


async def generate_insights(
    company: Optional[CompanyData],
    security: Optional[SecurityResult],
    reputation: ReputationScore,
    gigachat,
) -> Optional[str]:
    """Возвращает блок «🤖 На что обратить внимание» или None.

    Не вызывает GigaChat если:
    * компания пуста (None)
    * GigaChat не настроен (нет credentials)
    * у компании нет факторов риска (rеputation.score == 100)
    """
    if company is None or gigachat is None:
        return None
    if not getattr(gigachat, "credentials", ""):
        return None

    has_factors = any(cat.factors for cat in reputation.categories)
    if not has_factors:
        return None

    prompt = _build_prompt(company, security, reputation)
    try:
        # _chat — синхронный (requests), оборачиваем чтобы не блокировать loop
        raw = await asyncio.to_thread(gigachat._chat, prompt)
    except Exception as exc:
        logger.warning("AI insights: GigaChat call failed: %s", exc)
        return None

    insights = _parse_insights(raw or "")
    if not insights:
        logger.info("AI insights: parser returned 0 items, raw=%r", (raw or "")[:200])
        return None
    return _format_block(insights)
