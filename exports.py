import os
import re
from io import BytesIO
from textwrap import wrap
from typing import Optional

from fpdf import FPDF
from PIL import Image, ImageDraw, ImageFont

from schemas import CompanyData

_FONT_SEARCH_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "C:/Windows/Fonts/arial.ttf",
]


# Эмодзи, которые мы используем в текстовых отчётах. DejaVu/Helvetica их
# почти все не поддерживают — заменяем на текстовые маркеры, чтобы fpdf
# не падал.
_EMOJI_REPLACEMENTS = {
    "🟢": "[LOW]",
    "🟡": "[MED]",
    "🟠": "[HIGH]",
    "🔴": "[CRIT]",
    "⚪": "[?]",
    "✅": "[+]",
    "⚠️": "[!]",
    "❌": "[-]",
    "🚨": "[!!]",
    "❗️": "!",
    "🤖": "",
    "📊": "",
    "📋": "",
    "📜": "",
    "📦": "",
    "📥": "",
    "📄": "",
    "📈": "",
    "📍": "",
    "📅": "",
    "📡": "",
    "📞": "",
    "🏢": "",
    "🏛": "",
    "🏭": "",
    "🏦": "",
    "🎯": "",
    "👤": "",
    "👥": "",
    "💼": "",
    "💰": "",
    "💸": "",
    "💹": "",
    "💡": "",
    "🔒": "",
    "🔎": "",
    "🔗": "",
    "🔄": "",
    "🛑": "",
    "🧑": "",
    "🆘": "",
    "⚖️": "",
    "🤝": "",
    "📲": "",
    "🟫": "",
    "🟪": "",
    "🟦": "",
    "🟨": "",
    # ── разделители/типографика ──
    "—": "-",
    "–": "-",
    "•": "*",
    "↳": "->",
    "→": "->",
    "│": "|",
    "━": "-",
    "┌": "+",
    "┐": "+",
    "└": "+",
    "┘": "+",
    "─": "-",
    "…": "...",
}


# Префикс — символы вне Basic Multilingual Plane (U+10000+) — обычно эмодзи
# (квадратные плитки, скрепки и т.п.). DejaVu их не имеет. Сжимаем в один
# regex, чтобы убрать всё что не вошло в _EMOJI_REPLACEMENTS.
_NON_BMP_RE = re.compile(r"[\U00010000-\U0010ffff]")
# Variation Selector-16 (U+FE0F) — невидимый символ, делает «emoji»-вид
# у текстового знака (⚠️ = ⚠ + U+FE0F). DejaVu его не имеет.
_VS16_RE = re.compile(r"️")


def _pdf_safe(text: str) -> str:
    """Готовит текст к PDF: заменяет эмодзи на текстовые маркеры,
    срезает символы вне BMP (которых нет в DejaVu/Helvetica)."""
    if not text:
        return ""
    for emoji, replacement in _EMOJI_REPLACEMENTS.items():
        text = text.replace(emoji, replacement)
    text = _VS16_RE.sub("", text)
    text = _NON_BMP_RE.sub("", text)
    # Может остаться двойной пробел после удалений — схлопываем
    text = re.sub(r"  +", " ", text)
    return text


def _find_truetype_font() -> Optional[str]:
    for path in _FONT_SEARCH_PATHS:
        if os.path.isfile(path):
            return path
    return None


def build_kp_pdf(title: str, body: str, company: Optional[CompanyData] = None) -> bytes:
    pdf = FPDF()
    pdf.add_page()

    font_path = _find_truetype_font()
    if font_path:
        pdf.add_font("CustomFont", "", font_path)
        pdf.set_font("CustomFont", size=14)
    else:
        pdf.set_font("Helvetica", size=14)

    pdf.cell(0, 10, text=_pdf_safe(title), new_x="LMARGIN", new_y="NEXT")

    if font_path:
        pdf.set_font("CustomFont", size=11)
    else:
        pdf.set_font("Helvetica", size=11)

    if company:
        pdf.multi_cell(0, 8, text=_pdf_safe(_company_block(company)))
        pdf.ln(4)

    pdf.multi_cell(0, 8, text=_pdf_safe(body))

    output = BytesIO()
    pdf.output(output)
    return output.getvalue()


def build_kp_png(title: str, body: str, company: Optional[CompanyData] = None, width: int = 1000, height: int = 600) -> bytes:
    img = Image.new("RGB", (width, height), color="white")
    draw = ImageDraw.Draw(img)
    ttf_path = _find_truetype_font()
    try:
        if ttf_path:
            font_title = ImageFont.truetype(ttf_path, 24)
            font_body = ImageFont.truetype(ttf_path, 16)
        else:
            font_title = ImageFont.load_default()
            font_body = ImageFont.load_default()
    except Exception:
        font_title = ImageFont.load_default()
        font_body = ImageFont.load_default()

    y = 20
    draw.text((20, y), title, font=font_title, fill="black")
    y += 40

    if company:
        company_text = _company_block(company)
        for line in company_text.splitlines():
            draw.text((20, y), line, font=font_body, fill="black")
            y += 20
        y += 10

    for line in _wrap_text(body, width=80):
        draw.text((20, y), line, font=font_body, fill="black")
        y += 20

    buffer = BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


def _company_block(company: CompanyData) -> str:
    return "\n".join(
        [
            f"Компания: {company.name or '—'}",
            f"ИНН: {company.inn or '—'}; ОГРН: {company.ogrn or '—'}",
            f"Регион: {company.region or '—'}; ОКВЭД: {company.okved_main or '—'}",
            f"Штат: {company.employees_count or '—'}; Выручка/прибыль: {company.revenue_last_year or '—'} / {company.profit_last_year or '—'}",
        ]
    )


def _wrap_text(text: str, width: int) -> list[str]:
    lines = []
    for paragraph in text.split("\n"):
        lines.extend(wrap(paragraph, width=width) or [""])
    return lines

