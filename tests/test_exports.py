"""Тесты exports — генерация КП в PDF/PNG.

Бинарный вывод проверяем по сигнатуре формата (magic bytes) и по
извлекаемому тексту, где это возможно. Структуру шрифтов не тестируем —
зависит от окружения.
"""
from io import BytesIO

import pytest
from PIL import Image

import exports
from exports import (
    _company_block,
    _find_truetype_font,
    _wrap_text,
    build_kp_pdf,
    build_kp_png,
)
from schemas import CompanyData


def _company(**overrides) -> CompanyData:
    base = dict(
        inn="7707083893",
        name="ООО Ромашка",
        ogrn="1027700132195",
        region="Москва",
        okved_main="62.01",
        employees_count=50,
        revenue_last_year=120_000_000.0,
        profit_last_year=18_000_000.0,
    )
    base.update(overrides)
    return CompanyData(**base)


class TestCompanyBlock:
    def test_filled_company_renders_all_fields(self):
        text = _company_block(_company())
        assert "ООО Ромашка" in text
        assert "7707083893" in text
        assert "1027700132195" in text
        assert "Москва" in text
        assert "62.01" in text

    def test_empty_company_uses_dashes(self):
        text = _company_block(CompanyData())
        # Все поля None → каждая строка с "—", не None
        assert "—" in text
        assert "None" not in text

    def test_partial_company_mixes_real_and_dashes(self):
        c = _company(name="ООО Тест", ogrn=None, region=None)
        text = _company_block(c)
        assert "ООО Тест" in text
        assert "—" in text  # вместо None для пустых

    def test_no_zero_substituted_for_none(self):
        # revenue/profit None → "—", не "0"
        c = _company(revenue_last_year=None, profit_last_year=None)
        text = _company_block(c)
        # "—" повторяется в строке выручка/прибыль
        assert "Выручка/прибыль: — / —" in text


class TestWrapText:
    def test_short_line_passes_through(self):
        assert _wrap_text("hi", width=80) == ["hi"]

    def test_long_line_split(self):
        text = "a" * 200
        lines = _wrap_text(text, width=80)
        assert len(lines) >= 2
        assert all(len(line) <= 80 for line in lines)

    def test_paragraphs_preserved(self):
        text = "first paragraph\nsecond paragraph\nthird"
        lines = _wrap_text(text, width=80)
        # Каждый параграф остаётся отдельным
        assert "first paragraph" in lines
        assert "second paragraph" in lines
        assert "third" in lines

    def test_empty_paragraph_yields_empty_line(self):
        # Пустые строки сохраняются как разделители
        text = "first\n\nthird"
        lines = _wrap_text(text, width=80)
        assert "first" in lines
        assert "" in lines
        assert "third" in lines


class TestFindFont:
    def test_returns_path_when_font_exists(self, monkeypatch, tmp_path):
        fake_font = tmp_path / "fake.ttf"
        fake_font.write_bytes(b"")
        monkeypatch.setattr(exports, "_FONT_SEARCH_PATHS", [str(fake_font)])
        assert _find_truetype_font() == str(fake_font)

    def test_returns_none_when_no_font_found(self, monkeypatch):
        monkeypatch.setattr(
            exports, "_FONT_SEARCH_PATHS",
            ["/nonexistent/font.ttf", "/also/missing.ttf"],
        )
        assert _find_truetype_font() is None

    def test_returns_first_match(self, monkeypatch, tmp_path):
        first = tmp_path / "first.ttf"
        first.write_bytes(b"")
        second = tmp_path / "second.ttf"
        second.write_bytes(b"")
        monkeypatch.setattr(exports, "_FONT_SEARCH_PATHS",
                            [str(first), str(second)])
        assert _find_truetype_font() == str(first)


class TestBuildKpPdf:
    def test_returns_pdf_bytes(self):
        data = build_kp_pdf("Title", "Body text")
        # PDF magic bytes
        assert data.startswith(b"%PDF-")
        assert len(data) > 100  # не пустой

    def test_with_company_block(self):
        data = build_kp_pdf("KP", "body", company=_company())
        assert data.startswith(b"%PDF-")

    def test_with_empty_body(self):
        data = build_kp_pdf("KP", "")
        assert data.startswith(b"%PDF-")

    def test_no_company_argument(self):
        data = build_kp_pdf("Title", "Body")
        assert data.startswith(b"%PDF-")

    def test_works_without_truetype_font(self, monkeypatch):
        monkeypatch.setattr(exports, "_FONT_SEARCH_PATHS", [])
        data = build_kp_pdf("Title", "Body")
        # Падать не должен — fallback на Helvetica
        assert data.startswith(b"%PDF-")


class TestBuildKpPng:
    def test_returns_png_bytes(self):
        data = build_kp_png("Title", "Body")
        # PNG magic bytes
        assert data[:8] == b"\x89PNG\r\n\x1a\n"

    def test_image_dimensions_match_args(self):
        data = build_kp_png("T", "B", width=400, height=300)
        img = Image.open(BytesIO(data))
        assert img.size == (400, 300)

    def test_default_dimensions(self):
        data = build_kp_png("T", "B")
        img = Image.open(BytesIO(data))
        assert img.size == (1000, 600)

    def test_with_company_block(self):
        data = build_kp_png("Title", "Body", company=_company())
        assert data[:8] == b"\x89PNG\r\n\x1a\n"

    def test_long_body_does_not_crash(self):
        long_body = "x" * 5000
        data = build_kp_png("Title", long_body)
        assert data[:8] == b"\x89PNG\r\n\x1a\n"

    def test_works_without_truetype_font(self, monkeypatch):
        monkeypatch.setattr(exports, "_FONT_SEARCH_PATHS", [])
        data = build_kp_png("Title", "Body")
        # Должен использовать ImageFont.load_default()
        assert data[:8] == b"\x89PNG\r\n\x1a\n"

    def test_recovers_from_truetype_load_failure(self, monkeypatch, tmp_path):
        # Файл существует, но это не валидный TTF — ImageFont.truetype бросит
        fake = tmp_path / "broken.ttf"
        fake.write_bytes(b"not a real ttf")
        monkeypatch.setattr(exports, "_FONT_SEARCH_PATHS", [str(fake)])
        # Не должен падать — except в _build_kp_png ловит и фоллбэк
        data = build_kp_png("Title", "Body")
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
