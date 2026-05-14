"""Детектор сленга про обнал и серые схемы.

Подход: для каждой леммы явно задан стем (общий префикс всех её
словоформ). Поиск ведётся через границу слова: \\bSTEM\\w*. Это ловит
склонения и спряжения, при этом стемы выбраны достаточно длинными,
чтобы минимизировать false-positives на нейтральных словах.

Где компромисс: стем «обел» сматчит и редкое «обелиск», «прокрут»
сматчит «прокрутка». В контексте Telegram-бота проверки контрагентов
такие false-positives крайне редки, и цена ошибки — лишний legal_note,
а не блокировка пользователя.

Многословные фразы («через ип», «по агентской», «нал ↔ безнал») не
склоняются и матчатся подстрокой.
"""

import re
from typing import Dict, Optional, Pattern, Set


# Мапа: каноническая лемма → стем (или None для многословных фраз).
# Стемы намеренно немного длиннее общего корня, чтобы избежать
# false-positives на словах из других семантических полей.
_SLANG_STEMS: Dict[str, Optional[str]] = {
    # Существительные
    "обнал":      "обнал",      # обнал, обналом, обналичка, обналичу
    "прокладка":  "проклад",    # прокладка, прокладкой, прокладок, прокладывать
    "техничка":   "техничк",    # техничка, технички, техничку («технический» не матчится)
    # Глаголы
    "прокрутить": "прокрут",    # прокрутить, прокрутил, прокрутят, прокрутка
    "обелить":    "обел",       # обелить, обелили, обелим, обелит
    # Многословные фразы — substring без склонений
    "нал ↔ безнал":  None,
    "через ип":      None,
    "по агентской":  None,
}

# Порядок для отчётов и тестов
SLANG_TERMS = list(_SLANG_STEMS.keys())


def _compile_patterns() -> Dict[str, Optional[Pattern[str]]]:
    compiled: Dict[str, Optional[Pattern[str]]] = {}
    for term, stem in _SLANG_STEMS.items():
        if stem is None:
            compiled[term] = None
        else:
            compiled[term] = re.compile(rf"\b{re.escape(stem)}\w*", re.IGNORECASE)
    return compiled


_PATTERNS: Dict[str, Optional[Pattern[str]]] = _compile_patterns()


def assess_risk(text: str) -> Set[str]:
    """Возвращает набор лемм сленга, найденных в тексте."""
    lowered = text.lower()
    found: Set[str] = set()
    for term, pattern in _PATTERNS.items():
        if pattern is None:
            if term in lowered:
                found.add(term)
        else:
            if pattern.search(lowered):
                found.add(term)
    return found


def legal_note(risk_terms: Set[str]) -> str:
    if not risk_terms:
        return ""
    joined = ", ".join(sorted(risk_terms))
    return (
        f"Вижу запрос про: {joined}. "
        "Работаем только в легальном поле: прозрачные договоры, корректные назначения, согласованные лимиты и KYC-профиль."
    )
