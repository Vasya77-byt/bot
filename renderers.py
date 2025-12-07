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
        "Внутренний разбор: оцениваем риски по банку/115-ФЗ, структуру платежей и соответствие ОКВЭД.",
        _company_table(company),
        _recommendations(company),
    ]
    note = legal_note(risk)
    if note:
        parts.append(note)
    return "\n\n".join(parts)


def render_client_proposal(company: CompanyData, risk: Set[str]) -> str:
    analysis = _short_analysis(company)
    proposal = [
        "Коммерческое предложение:",
        "— Индивидуальная настройка РКО и платежной архитектуры под ваши обороты.",
        "— Согласование лимитов и назначений, чтобы не ловить стопы.",
        "— Сопровождение по комплаенсу и ответы на запросы банка.",
        "— Канал связи с менеджером и быстрые консультации по операциям.",
    ]
    note = legal_note(risk)
    return "\n\n".join(filter(None, [analysis, "\n".join(proposal), note]))


def render_mixed(company: CompanyData, risk: Set[str]) -> str:
    return "\n\n".join(
        [
            _short_analysis(company),
            _mini_kp(),
            legal_note(risk),
        ]
    ).strip()


def render_request(company: CompanyData) -> str:
    return "\n".join(
        [
            "[ДАННЫЕ_КОМПАНИИ]:",
            "- ЗАЯВКА",
            f"- inn: {company.inn or 'не указано'}",
            f"- name: {company.name or 'не указано'}",
            f"- region: {company.region or 'не указано'}",
            f"- reg_date: {company.reg_date or 'не указано'}",
            f"- okved_main: {company.okved_main or 'не указано'}",
            f"- employees_count: {company.employees_count or 'не указано'}",
            "- sum: ",
            "- designation: ",
            "есть что предложить? цена? срок?",
        ]
    )


def render_proposal(company: CompanyData) -> str:
    return "\n".join(
        [
            "[ДАННЫЕ_КОМПАНИИ]:",
            "- ПРЕДЛОЖЕНИЕ",
            f"- inn: {company.inn or 'не указано'}",
            f"- name: {company.name or 'не указано'}",
            f"- region: {company.region or 'не указано'}",
            f"- reg_date: {company.reg_date or 'не указано'}",
            f"- okved_main: {company.okved_main or 'не указано'}",
            f"- employees_count: {company.employees_count or 'не указано'}",
            "- sum: ",
            "- designation: ",
            "цена _____  выгрузка ______",
        ]
    )


def _company_table(company: CompanyData) -> str:
    return "\n".join(
        [
            "Таблица:",
            f"- Название: {company.name or 'не указано'}",
            f"- ИНН: {company.inn or 'не указано'}",
            f"- Регион: {company.region or 'не указано'}",
            f"- Возраст: {company.age_years or 'не указано'}",
            f"- Основной ОКВЭД: {company.okved_main or 'не указано'}",
            f"- Штат: {company.employees_count or 'не указано'}",
            f"- Выручка / прибыль: {company.revenue_last_year or 'не указано'} / {company.profit_last_year or 'не указано'}",
            f"- Лицензии: {', '.join(company.licenses) if company.licenses else 'не указано'}",
        ]
    )


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
        "Вот анализ компании в 3–5 пунктах:",
        f"1. Отрасль/ОКВЭД: {company.okved_main or 'не указано'}.",
        f"2. Возраст: {company.age_years or 'не указано'} лет; регион: {company.region or 'не указано'}.",
        f"3. Штат: {company.employees_count or 'не указано'}.",
        f"4. Выручка: {company.revenue_last_year or 'не указано'}; прибыль: {company.profit_last_year or 'не указано'}.",
    ]
    if company.licenses:
        lines.append(f"5. Лицензии: {', '.join(company.licenses)}.")
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

