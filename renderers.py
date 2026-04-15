from typing import Optional, Set

from compliance import legal_note
from parsers import ParseResult
from schemas import CompanyData, empty_company


def render_response(parsed: ParseResult, company: Optional[CompanyData], risk: Set[str]) -> str:
    company = company or empty_company(parsed.inn)

    if parsed.is_request:
        return render_request(company)
    if parsed.is_proposal:
        return render_proposal(company)

    mode = (parsed.mode or "").lower()
    if mode == "internal_analysis":
        return render_internal_analysis(company, risk)
    if mode == "client_proposal":
        return render_client_proposal(company, risk)

    return render_mixed(company, risk)


def render_internal_analysis(company: CompanyData, risk: Set[str]) -> str:
    parts = [
        "📊 Внутренний разбор: оцениваем риски по банку/115-ФЗ, структуру платежей и соответствие ОКВЭД.",
        _company_table(company),
        _recommendations(company),
    ]
    note = legal_note(risk)
    if note:
        parts.append(note)
    if company.source:
        parts.append(f"📡 Источники: {company.source}")
    return "\n\n".join(parts)


def render_client_proposal(company: CompanyData, risk: Set[str]) -> str:
    analysis = _short_analysis(company)
    proposal = [
        "📝 Коммерческое предложение:",
        "— Индивидуальная настройка РКО и платежной архитектуры под ваши обороты.",
        "— Согласование лимитов и назначений, чтобы не ловить стопы.",
        "— Сопровождение по комплаенсу и ответы на запросы банка.",
        "— Канал связи с менеджером и быстрые консультации по операциям.",
    ]
    note = legal_note(risk)
    return "\n\n".join(filter(None, [analysis, "\n".join(proposal), note]))


def render_mixed(company: CompanyData, risk: Set[str]) -> str:
    return "\n\n".join(
        filter(None, [
            _short_analysis(company),
            _mini_kp(),
            legal_note(risk),
        ])
    ).strip()


def render_request(company: CompanyData) -> str:
    return "\n".join(
        [
            "📋 ЗАЯВКА НА ОБСЛУЖИВАНИЕ:",
            f"Компания: {company.name or 'не указано'}",
            f"ИНН: {company.inn or 'не указано'}",
            f"ОГРН: {company.ogrn or 'не указано'}",
            f"Адрес: {company.address or company.region or 'не указано'}",
            f"Руководитель: {company.director or 'не указано'}",
            f"ОКВЭД: {company.okved_main or 'не указано'}",
            f"Дата регистрации: {company.reg_date or 'не указано'}",
            f"Штат: {company.employees_count or 'не указано'}",
            f"Статус: {company.status or 'не указано'}",
            "",
            "Сумма: _____",
            "Назначение: _____",
            "",
            "Готовы предложить условия? Укажите цену и сроки.",
        ]
    )


def render_proposal(company: CompanyData) -> str:
    return "\n".join(
        [
            "💼 ПРЕДЛОЖЕНИЕ ДЛЯ КОМПАНИИ:",
            f"Компания: {company.name or 'не указано'}",
            f"ИНН: {company.inn or 'не указано'}",
            f"ОГРН: {company.ogrn or 'не указано'}",
            f"Адрес: {company.address or company.region or 'не указано'}",
            f"Руководитель: {company.director or 'не указано'}",
            f"ОКВЭД: {company.okved_main or 'не указано'}",
            f"Дата регистрации: {company.reg_date or 'не указано'}",
            f"Штат: {company.employees_count or 'не указано'}",
            f"Выручка: {_fmt_money(company.revenue_last_year)}",
            f"Прибыль: {_fmt_money(company.profit_last_year)}",
            f"Статус: {company.status or 'не указано'}",
            "",
            "Цена: _____",
            "Выгрузка: _____",
        ]
    )


def _company_table(company: CompanyData) -> str:
    lines = [
        "Карточка компании:",
        f"• Название: {company.name or 'не указано'}",
        f"• ИНН: {company.inn or 'не указано'}",
        f"• ОГРН: {company.ogrn or 'не указано'}",
    ]
    if company.kpp:
        lines.append(f"• КПП: {company.kpp}")
    lines.extend([
        f"• Адрес: {company.address or company.region or 'не указано'}",
        f"• Руководитель: {company.director or 'не указано'}",
        f"• Статус: {company.status or 'не указано'}",
        f"• Дата регистрации: {company.reg_date or 'не указано'} ({company.age_years or '?'} лет)",
        f"• Основной ОКВЭД: {company.okved_main or 'не указано'}",
    ])
    if company.okved_name:
        lines.append(f"  ({company.okved_name})")
    lines.extend([
        f"• Штат: {company.employees_count or 'не указано'}",
        f"• Выручка: {_fmt_money(company.revenue_last_year)}",
        f"• Прибыль: {_fmt_money(company.profit_last_year)}",
    ])
    if company.capital:
        lines.append(f"• Уставный капитал: {_fmt_money(company.capital)}")
    if company.licenses:
        lines.append(f"• Лицензии: {', '.join(company.licenses)}")
    return "\n".join(lines)


def _recommendations(company: CompanyData) -> str:
    return "\n".join(
        [
            "Рекомендации:",
            "• Выстроить прозрачные договоры и назначения платежей под профиль ОКВЭД.",
            "• Согласовать лимиты и тайминг платежей, чтобы не ловить залипы.",
            "• Подготовить KYC-пакет и финансовую модель под оборот.",
            "• Настроить коммуникацию с банком: оперативные ответы на запросы.",
        ]
    )


def _short_analysis(company: CompanyData) -> str:
    lines = [
        "📊 Анализ компании:",
        f"1. Компания: {company.name or 'не указано'}",
        f"2. ОКВЭД: {company.okved_main or 'не указано'}",
    ]
    if company.okved_name:
        lines[-1] += f" ({company.okved_name})"
    lines.extend([
        f"3. Возраст: {company.age_years or '?'} лет; регион: {company.region or 'не указано'}",
        f"4. Штат: {company.employees_count or 'не указано'}",
        f"5. Выручка: {_fmt_money(company.revenue_last_year)}; прибыль: {_fmt_money(company.profit_last_year)}",
    ])
    if company.director:
        lines.append(f"6. Руководитель: {company.director}")
    if company.status and company.status != "Действующая":
        lines.append(f"⚠️ Статус: {company.status}")
    if company.licenses:
        lines.append(f"7. Лицензии: {', '.join(company.licenses)}")
    return "\n".join(lines)


def _mini_kp() -> str:
    return "\n".join(
        [
            "Мини-КП:",
            "— Сопровождаем по банкам и комплаенсу, чтобы платежи проходили без стопов.",
            "— Настраиваем структуру платежей и назначения под ваш оборот и ОКВЭД.",
            "— Помогаем избежать блокировок: лимиты, KYC, ответы на запросы.",
            "— Даем понятную схему движения денег между подрядчиками и поставщиками.",
            "Следующий шаг: пришлите текущую схему/оборот/банк — предложим адаптированное решение.",
        ]
    )


def _fmt_money(value: Optional[float]) -> str:
    """Форматирование денежных сумм."""
    if value is None:
        return "не указано"
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f} млрд ₽"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f} млн ₽"
    if value >= 1_000:
        return f"{value / 1_000:.0f} тыс ₽"
    return f"{value:.0f} ₽"
