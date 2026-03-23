"""Валидация ИНН юридического лица (10 цифр) и ИП (12 цифр) с проверкой контрольной суммы."""


def validate_inn(text: str) -> tuple[bool, str]:
    """
    Проверяет ИНН юрлица (10 цифр) или ИП/физлица (12 цифр).
    Возвращает (is_valid, error_message).
    """
    cleaned = text.strip()

    if not cleaned.isdigit():
        return False, "ИНН должен состоять только из цифр."

    if len(cleaned) == 10:
        return _validate_inn_10(cleaned)
    elif len(cleaned) == 12:
        return _validate_inn_12(cleaned)
    else:
        return False, (
            f"ИНН должен содержать 10 цифр (юрлицо) или 12 цифр (ИП). "
            f"Вы ввели {len(cleaned)}."
        )


def _validate_inn_10(inn: str) -> tuple[bool, str]:
    """Проверка 10-значного ИНН (юрлицо)."""
    weights = [2, 4, 10, 3, 5, 9, 4, 6, 8]
    digits = [int(c) for c in inn]
    checksum = sum(w * d for w, d in zip(weights, digits[:9])) % 11 % 10

    if checksum != digits[9]:
        return False, "Некорректный ИНН: контрольная сумма не совпадает."

    return True, ""


def _validate_inn_12(inn: str) -> tuple[bool, str]:
    """Проверка 12-значного ИНН (ИП / физлицо)."""
    digits = [int(c) for c in inn]

    # Проверка 11-й цифры
    weights_11 = [7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
    check_11 = sum(w * d for w, d in zip(weights_11, digits[:10])) % 11 % 10
    if check_11 != digits[10]:
        return False, "Некорректный ИНН: контрольная сумма не совпадает."

    # Проверка 12-й цифры
    weights_12 = [3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
    check_12 = sum(w * d for w, d in zip(weights_12, digits[:11])) % 11 % 10
    if check_12 != digits[11]:
        return False, "Некорректный ИНН: контрольная сумма не совпадает."

    return True, ""
