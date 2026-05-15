"""OCR извлечение ИНН из фотографий визиток / договоров / актов.

Локальный pipeline через Tesseract — без внешних API, чтобы не
расходовать квоты. Pillow предобрабатывает изображение для лучшего
качества OCR. Регулярка извлекает кандидатов 10/12 цифр; проверка
контрольной цифры отсеивает false positives (когда OCR случайно
собрал цифры из шума).

Если pytesseract / tesseract не установлены — модуль возвращает
пустой список без падения (graceful degradation). Админ увидит
WARNING в логах.

Возможное расширение в будущем (не реализовано в MVP):
- GigaChat Vision как fallback при low confidence от Tesseract
- Yandex Vision OCR для случаев когда нужна высокая точность

Системные требования:
    apt install tesseract-ocr tesseract-ocr-rus
    pip install pytesseract
"""

from __future__ import annotations

import io
import logging
import re
from typing import List

logger = logging.getLogger("financial-architect")


# Регулярка ИНН: 10 цифр (юрлица) или 12 цифр (ИП). \b границы важны
# чтобы не цеплять подстроки длинных последовательностей цифр.
_INN_RE = re.compile(r"\b(\d{12}|\d{10})\b")

# Часто на фото перед ИНН есть префикс «ИНН» / «INN» / «Н.Н.» — ищем
# и в таких паттернах тоже (без \b до префикса). Используется как
# второй проход.
_INN_PREFIXED_RE = re.compile(
    r"(?:ИНН|инн|INN|inn)[\s:.№#-]*?(\d{12}|\d{10})",
)


def _check_inn_10(inn: str) -> bool:
    """Проверка контрольной цифры 10-значного ИНН (юрлица)."""
    if not inn.isdigit() or len(inn) != 10:
        return False
    coeffs = [2, 4, 10, 3, 5, 9, 4, 6, 8]
    s = sum(int(inn[i]) * coeffs[i] for i in range(9))
    check = (s % 11) % 10
    return check == int(inn[9])


def _check_inn_12(inn: str) -> bool:
    """Проверка двух контрольных цифр 12-значного ИНН (ИП)."""
    if not inn.isdigit() or len(inn) != 12:
        return False
    coeffs_11 = [7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
    s11 = sum(int(inn[i]) * coeffs_11[i] for i in range(10))
    check11 = (s11 % 11) % 10
    if check11 != int(inn[10]):
        return False
    coeffs_12 = [3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
    s12 = sum(int(inn[i]) * coeffs_12[i] for i in range(11))
    check12 = (s12 % 11) % 10
    return check12 == int(inn[11])


def is_valid_inn(inn: str) -> bool:
    """True если строка — валидный ИНН (10 или 12 цифр + контрольная).

    Используется для отсева OCR-шума: случайные 10 цифр почти никогда
    не пройдут проверку контрольной цифры.
    """
    if len(inn) == 10:
        return _check_inn_10(inn)
    if len(inn) == 12:
        return _check_inn_12(inn)
    return False


def extract_inns_from_text(text: str) -> List[str]:
    """Извлекает уникальные валидные ИНН из произвольного текста.

    Сначала пытается найти ИНН по паттерну «ИНН: 1234567890» (выше
    приоритет), затем добавляет остальные совпадения 10/12 цифр.
    Каждый кандидат валидируется через is_valid_inn — фильтрует
    OCR-шум.

    Порядок результата — порядок первого появления.
    """
    seen: set[str] = set()
    out: List[str] = []

    # Сначала с префиксом «ИНН:» — это «уверенные» совпадения.
    for match in _INN_PREFIXED_RE.finditer(text):
        candidate = match.group(1)
        if candidate in seen or not is_valid_inn(candidate):
            continue
        seen.add(candidate)
        out.append(candidate)

    # Потом любые 10/12 цифр в тексте.
    for match in _INN_RE.finditer(text):
        candidate = match.group(1)
        if candidate in seen or not is_valid_inn(candidate):
            continue
        seen.add(candidate)
        out.append(candidate)

    return out


def _preprocess_image(image_bytes: bytes):
    """Pre-process для OCR: grayscale + контраст + threshold.

    Возвращает PIL.Image готовое к Tesseract. Если Pillow недоступен —
    возвращает None (handler должен это обработать).
    """
    try:
        from PIL import Image, ImageEnhance, ImageOps
    except ImportError:
        logger.warning("OCR: Pillow не установлен")
        return None
    try:
        img = Image.open(io.BytesIO(image_bytes))
        # Grayscale
        img = img.convert("L")
        # Auto-contrast + sharpening — помогает на фото с тенями/бликами
        img = ImageOps.autocontrast(img, cutoff=2)
        enhancer = ImageEnhance.Contrast(img)
        img = enhancer.enhance(1.5)
        return img
    except Exception as exc:
        logger.warning("OCR: ошибка предобработки: %s", exc)
        return None


def extract_inns_from_image(image_bytes: bytes) -> List[str]:
    """Главная точка входа: фото → список валидных ИНН.

    Pipeline:
    1. Pillow: grayscale + auto-contrast (помогает на плохих фото)
    2. Tesseract OCR с русскоязычной моделью (langs='rus+eng')
    3. Регулярка + контрольная цифра ИНН

    Пустой результат при:
    - tesseract / pytesseract не установлены
    - Не удалось распарсить изображение
    - На фото нет валидных ИНН
    """
    if not image_bytes:
        return []

    img = _preprocess_image(image_bytes)
    if img is None:
        return []

    try:
        import pytesseract
    except ImportError:
        logger.warning(
            "OCR: pytesseract не установлен. "
            "apt install tesseract-ocr tesseract-ocr-rus && pip install pytesseract",
        )
        return []

    try:
        # rus+eng — на визитках часто английский (статус компании, URL)
        text = pytesseract.image_to_string(img, lang="rus+eng")
    except pytesseract.TesseractNotFoundError:
        logger.warning(
            "OCR: tesseract бинарник не найден. "
            "Установите: apt install tesseract-ocr tesseract-ocr-rus",
        )
        return []
    except Exception as exc:
        logger.warning("OCR: tesseract failed: %s", exc)
        return []

    inns = extract_inns_from_text(text)
    if inns:
        logger.info("OCR: найдено %d ИНН", len(inns))
    else:
        logger.info("OCR: ИНН не найдены в тексте (длина текста: %d)", len(text))
    return inns
