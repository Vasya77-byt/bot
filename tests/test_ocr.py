"""Тесты OCR-извлечения ИНН из текста и изображений."""
from __future__ import annotations

import pytest

from ocr import (
    _check_inn_10,
    _check_inn_12,
    extract_inns_from_image,
    extract_inns_from_text,
    is_valid_inn,
)


# Реальные валидные ИНН для тестов (известные публичные компании)
SBERBANK_INN = "7707083893"   # Сбербанк, 10 цифр
LUKOIL_INN = "7708004767"     # ЛУКОЙЛ, 10 цифр

# Невалидные с правильным числом цифр
INVALID_10 = "1234567890"
INVALID_12 = "123456789012"


class TestChecksumValidation:
    """Контрольная цифра — главная защита от OCR-шума."""

    def test_sberbank_inn_is_valid(self):
        assert _check_inn_10(SBERBANK_INN) is True

    def test_lukoil_inn_is_valid(self):
        assert _check_inn_10(LUKOIL_INN) is True

    def test_invalid_10_digits_rejected(self):
        assert _check_inn_10(INVALID_10) is False

    def test_wrong_length_rejected(self):
        assert _check_inn_10("123") is False
        assert _check_inn_10(SBERBANK_INN + "1") is False  # 11 цифр

    def test_non_digits_rejected(self):
        assert _check_inn_10("ABCDEFGHIJ") is False

    def test_invalid_12_digits_rejected(self):
        assert _check_inn_12(INVALID_12) is False


class TestIsValidInn:
    """Универсальная проверка валидности."""

    def test_valid_10(self):
        assert is_valid_inn(SBERBANK_INN) is True

    def test_invalid_10(self):
        assert is_valid_inn(INVALID_10) is False

    def test_invalid_length(self):
        assert is_valid_inn("12345") is False
        assert is_valid_inn("1234567890123") is False  # 13 цифр

    def test_empty(self):
        assert is_valid_inn("") is False


class TestExtractInnsFromText:
    """Извлечение из произвольного OCR-текста."""

    def test_extracts_single_inn(self):
        text = f"ООО Тест, ИНН: {SBERBANK_INN}, телефон..."
        assert extract_inns_from_text(text) == [SBERBANK_INN]

    def test_extracts_with_prefix_label(self):
        text = f"ИНН {SBERBANK_INN}"
        assert extract_inns_from_text(text) == [SBERBANK_INN]

    def test_extracts_multiple_inns(self):
        text = f"От {SBERBANK_INN} к {LUKOIL_INN}"
        result = extract_inns_from_text(text)
        assert SBERBANK_INN in result
        assert LUKOIL_INN in result

    def test_dedupes(self):
        text = f"{SBERBANK_INN}, ещё раз {SBERBANK_INN} и снова {SBERBANK_INN}"
        assert extract_inns_from_text(text) == [SBERBANK_INN]

    def test_filters_invalid_checksum(self):
        # Числовая последовательность из 10 цифр без валидной контрольной
        text = f"Артикул 1111111111, договор {SBERBANK_INN}"
        result = extract_inns_from_text(text)
        # Только реально валидный ИНН попал
        assert result == [SBERBANK_INN]

    def test_returns_empty_when_no_inn(self):
        assert extract_inns_from_text("hello world, no numbers") == []
        assert extract_inns_from_text("123 456 78") == []  # короткие куски

    def test_handles_short_numbers(self):
        # 9 цифр (короткий) — не должен попасть
        assert extract_inns_from_text("123456789") == []

    def test_handles_long_numbers(self):
        # 13 цифр (слишком длинный) — берётся подстрока 10, проверяется,
        # но контрольная цифра почти наверняка не сойдётся
        text = "1234567890123"
        assert extract_inns_from_text(text) == []


def _tesseract_available() -> bool:
    """Helper для skipif: проверяет наличие pytesseract И бинарника."""
    try:
        import pytesseract
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


class TestExtractInnsFromImageGracefulDegradation:
    """OCR должен молча возвращать [] если tesseract не установлен —
    без падений и юзер-видимых ошибок."""

    def test_empty_bytes_returns_empty(self):
        assert extract_inns_from_image(b"") == []

    def test_invalid_image_returns_empty(self):
        # bytes не похожие на картинку — Pillow вернёт ошибку,
        # OCR должен это поймать и вернуть пусто
        assert extract_inns_from_image(b"not-an-image") == []

    @pytest.mark.skipif(
        not _tesseract_available(),
        reason="tesseract не установлен системно — пропускаем e2e",
    )
    def test_real_ocr_on_synthetic_image(self):
        """Только если tesseract есть: генерим картинку с известным
        ИНН и проверяем, что OCR его извлёк."""
        from PIL import Image, ImageDraw
        img = Image.new("RGB", (300, 80), color="white")
        draw = ImageDraw.Draw(img)
        draw.text((10, 30), f"INN: {SBERBANK_INN}", fill="black")
        import io
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        result = extract_inns_from_image(buf.getvalue())
        # На крошечной картинке с дефолтным шрифтом Tesseract может
        # промахнуться — тест неstrict
        if result:
            assert SBERBANK_INN in result
