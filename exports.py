import os
import re
from io import BytesIO
from textwrap import wrap
from typing import Optional

from fpdf import FPDF
from PIL import Image, ImageDraw, ImageFont

from schemas import CompanyData

_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

_FONT_SEARCH_PATHS = [
    # Локально в проекте — самый надёжный путь, не зависит от ОС
    os.path.join(_PROJECT_DIR, "assets", "DejaVuSans.ttf"),
    os.path.join(_PROJECT_DIR, "DejaVuSans.ttf"),
    # Системные пути (Linux / Ubuntu / Debian)
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/local/share/fonts/DejaVuSans.ttf",
    # macOS
    "/Library/Fonts/DejaVuSans.ttf",
    # Windows (fallback)
    "C:/Windows/Fonts/arial.ttf",
]


class FontNotFoundError(RuntimeError):
    """Не найден TrueType-шрифт, поддерживающий кириллицу."""


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
    font_path = _find_truetype_font()
    if not font_path:
        # Helvetica не поддерживает кириллицу — без TTF не получим
        # читаемый PDF. Бросаем явное исключение, обработчик ловит и
        # показывает осмысленное сообщение.
        raise FontNotFoundError(
            "TrueType-шрифт не найден. Установите fonts-dejavu-core "
            "(apt-get install fonts-dejavu-core) или положите "
            "DejaVuSans.ttf в каталог проекта."
        )

    pdf = FPDF()
    pdf.add_page()
    pdf.add_font("CustomFont", "", font_path)
    pdf.set_font("CustomFont", size=14)
    pdf.cell(0, 10, text=_pdf_safe(title), new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("CustomFont", size=11)

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


def build_bulk_xlsx(results: list, filename: str = "bulk_check.xlsx") -> bytes:
    """Сборка Excel-файла из списка BulkResult.

    Использует openpyxl (lazy-import — не подгружаем при обычных
    операциях). Если openpyxl не установлен, fallback'имся на CSV.

    Каждая строка — один ИНН с базовыми данными:
    ИНН / Название / ОГРН / Статус / Директор / Регион / ОКВЭД /
    Дата регистрации / Возраст (лет) / Комментарий.

    Column auto-width, заголовок жирным, ошибочные строки помечены
    в колонке «Комментарий».
    """
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
    except ImportError:
        # Fallback: CSV в bytes — Excel откроет, но без форматирования
        return _build_bulk_csv(results)

    wb = Workbook()
    ws = wb.active
    ws.title = "Контрагенты"

    headers = [
        "ИНН", "Название", "ОГРН", "Статус", "Директор",
        "Регион", "ОКВЭД", "ОКВЭД-описание",
        "Дата регистрации", "Возраст (лет)", "Комментарий",
    ]
    ws.append(headers)
    # Стиль заголовка
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="2481CC")
    for cell in ws[1]:
        cell.font = header_font
        cell.fill = header_fill

    error_fill = PatternFill("solid", fgColor="FFE5E5")
    for r in results:
        comment = ""
        if r.error == "not_found":
            comment = "Не найдена"
        elif r.error == "quota_exhausted":
            comment = "Лимит API — попробуйте позже"
        elif r.error:
            comment = "Ошибка проверки"
        row = [
            r.inn or "",
            r.name or "",
            r.ogrn or "",
            r.status or "",
            r.director or "",
            r.region or "",
            r.okved_main or "",
            r.okved_name or "",
            r.reg_date or "",
            r.age_years if r.age_years is not None else "",
            comment,
        ]
        ws.append(row)
        if r.error:
            # Подсвечиваем строки с ошибкой розовым
            for cell in ws[ws.max_row]:
                cell.fill = error_fill

    # Auto-width: по самой длинной ячейке в колонке (clamp на 50)
    for col_idx, _ in enumerate(headers, start=1):
        max_len = max(
            (
                len(str(ws.cell(row=r, column=col_idx).value or ""))
                for r in range(1, ws.max_row + 1)
            ),
            default=10,
        )
        col_letter = ws.cell(row=1, column=col_idx).column_letter
        ws.column_dimensions[col_letter].width = min(max_len + 2, 50)

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_company_xlsx(company: CompanyData) -> bytes:
    """Excel-карточка по одной компании.

    Двухколоночный лист «Поле | Значение» — подходит для бухгалтерии
    и быстрого сохранения паспорта контрагента. Не таблица, а карточка
    (один контрагент = один файл).

    Если openpyxl недоступен — fallback на CSV-байты.
    """
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
    except ImportError:
        return _build_company_csv(company)

    wb = Workbook()
    ws = wb.active
    ws.title = "Контрагент"

    # Заголовок-шапка
    ws["A1"] = "Карточка контрагента"
    ws["A1"].font = Font(bold=True, size=14, color="FFFFFF")
    ws["A1"].fill = PatternFill("solid", fgColor="2481CC")
    ws.merge_cells("A1:B1")
    ws["A1"].alignment = Alignment(horizontal="center")

    fields = [
        ("ИНН",                  company.inn),
        ("КПП",                  company.kpp),
        ("ОГРН",                 company.ogrn),
        ("Название",             company.name),
        ("Статус",               company.status),
        ("Руководитель",         company.director),
        ("Регион",               company.region),
        ("Адрес",                company.address),
        ("ОКВЭД",                company.okved_main),
        ("Описание ОКВЭД",       company.okved_name),
        ("Дата регистрации",     company.reg_date),
        ("Возраст (лет)",        company.age_years),
        ("Уставный капитал, ₽",  company.capital),
        ("Сотрудников",          company.employees_count),
        ("Выручка за год, ₽",    company.revenue_last_year),
        ("Прибыль за год, ₽",    company.profit_last_year),
        ("Источники данных",     company.source),
    ]

    label_font = Font(bold=True)
    label_fill = PatternFill("solid", fgColor="F0F0F0")
    for row_idx, (label, value) in enumerate(fields, start=2):
        ws.cell(row=row_idx, column=1, value=label).font = label_font
        ws.cell(row=row_idx, column=1).fill = label_fill
        ws.cell(row=row_idx, column=2, value=value if value not in (None, "") else "—")

    ws.column_dimensions["A"].width = 28
    ws.column_dimensions["B"].width = 60

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _build_company_csv(company: CompanyData) -> bytes:
    """Fallback на CSV если openpyxl недоступен."""
    import csv
    import io
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";")
    fields = [
        ("ИНН",            company.inn),
        ("КПП",            company.kpp),
        ("ОГРН",           company.ogrn),
        ("Название",       company.name),
        ("Статус",         company.status),
        ("Руководитель",   company.director),
        ("Регион",         company.region),
        ("Адрес",          company.address),
        ("ОКВЭД",          company.okved_main),
        ("Дата регистрации", company.reg_date),
        ("Уставный капитал", company.capital),
    ]
    for label, value in fields:
        writer.writerow([label, value if value not in (None, "") else ""])
    return ("﻿" + out.getvalue()).encode("utf-8")


def build_company_1c_csv(company: CompanyData) -> bytes:
    """1С-совместимый CSV для импорта контрагента.

    Формат: одна строка с заголовками в первой строке, одна строка
    с данными во второй. Заголовки точно соответствуют стандартным
    реквизитам справочника «Контрагенты» 1С 8.3 (УТ/Бухгалтерия).
    Разделитель `;`, кодировка cp1251 — именно так 1С ждёт CSV при
    стандартной загрузке через «Обработки → Загрузка данных из
    табличного документа».

    После выгрузки юзер открывает в 1С нужную обработку и указывает
    этот CSV как источник.
    """
    import csv
    import io

    # Поля, которые 1С понимает «из коробки» по точному совпадению
    # имён колонок с реквизитами справочника. Порядок имеет значение
    # для удобства маппинга в обработке.
    headers = [
        "ИНН",
        "КПП",
        "Наименование",
        "НаименованиеПолное",
        "ЮридическийАдрес",
        "ОГРН",
        "ДатаРегистрации",
        "КодОКВЭД",
        "НаименованиеОКВЭД",
        "Руководитель",
        "СтатусВЕГРЮЛ",
    ]
    row = [
        company.inn or "",
        company.kpp or "",
        company.name or "",
        company.name or "",  # полное = краткое если нет отдельного
        company.address or "",
        company.ogrn or "",
        company.reg_date or "",
        company.okved_main or "",
        company.okved_name or "",
        company.director or "",
        company.status or "",
    ]

    out = io.StringIO()
    writer = csv.writer(out, delimiter=";", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(headers)
    writer.writerow(row)
    # cp1251 — стандарт 1С 8.3 на Windows. Заменяем непредставимые
    # символы на '?' чтобы не уронить экспорт на экзотических OKVED.
    return out.getvalue().encode("cp1251", errors="replace")


def _build_bulk_csv(results: list) -> bytes:
    """Fallback: CSV-байты, если openpyxl не установлен.

    BOM добавляется для совместимости с русским Excel — иначе он
    разваливает кодировку при двойном клике."""
    import csv
    import io
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";")
    writer.writerow([
        "ИНН", "Название", "ОГРН", "Статус", "Директор",
        "Регион", "ОКВЭД", "ОКВЭД-описание",
        "Дата регистрации", "Возраст (лет)", "Комментарий",
    ])
    for r in results:
        comment = ""
        if r.error == "not_found":
            comment = "Не найдена"
        elif r.error == "quota_exhausted":
            comment = "Лимит API — попробуйте позже"
        elif r.error:
            comment = "Ошибка проверки"
        writer.writerow([
            r.inn or "",
            r.name or "",
            r.ogrn or "",
            r.status or "",
            r.director or "",
            r.region or "",
            r.okved_main or "",
            r.okved_name or "",
            r.reg_date or "",
            r.age_years if r.age_years is not None else "",
            comment,
        ])
    return ("﻿" + out.getvalue()).encode("utf-8")


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

