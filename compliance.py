from typing import Set


SLANG_TERMS = [
    "обнал",
    "нал ↔ безнал",
    "прокрутить",
    "обелить",
    "техничка",
    "прокладка",
    "через ип",
    "по агентской",
]


def assess_risk(text: str) -> Set[str]:
    lowered = text.lower()
    return {term for term in SLANG_TERMS if term in lowered}


def legal_note(risk_terms: Set[str]) -> str:
    if not risk_terms:
        return ""
    joined = ", ".join(sorted(risk_terms))
    return (
        f"Вижу запрос про: {joined}. "
        "Работаем только в легальном поле: прозрачные договоры, корректные назначения, согласованные лимиты и KYC-профиль."
    )

