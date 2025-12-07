from io import BytesIO
from textwrap import wrap
from typing import Optional

from fpdf import FPDF
from PIL import Image, ImageDraw, ImageFont

from schemas import CompanyData


def build_kp_pdf(title: str, body: str, company: Optional[CompanyData] = None) -> bytes:
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", size=14)
    pdf.cell(0, 10, txt=title, ln=True)
    pdf.set_font("Arial", size=11)

    if company:
        pdf.multi_cell(0, 8, txt=_company_block(company))
        pdf.ln(4)

    pdf.multi_cell(0, 8, txt=body)

    output = BytesIO()
    pdf.output(output)
    return output.getvalue()


def build_kp_png(title: str, body: str, company: Optional[CompanyData] = None, width: int = 1000, height: int = 600) -> bytes:
    img = Image.new("RGB", (width, height), color="white")
    draw = ImageDraw.Draw(img)
    try:
        font_title = ImageFont.truetype("arial.ttf", 24)
        font_body = ImageFont.truetype("arial.ttf", 16)
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

