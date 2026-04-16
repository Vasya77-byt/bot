from typing import Optional, Set

from compliance import legal_note
from parsers import ParseResult
from schemas import CompanyData, empty_company
from security_check import SecurityResult
from user_store import TARIFF_FEATURES, TARIFF_LABELS, UserProfile


def render_response(parsed: ParseResult, company: Optional[CompanyData], risk: Set[str], security: Optional[SecurityResult] = None) -> str:
    company = company or empty_company(parsed.inn)

    if parsed.is_request:
        return render_request(company)
    if parsed.is_proposal:
        return render_proposal(company)

    mode = (parsed.mode or "").lower()
    if mode == "internal_analysis":
        return render_internal_analysis(company, risk, security)
    if mode == "client_proposal":
        return render_client_proposal(company, risk)

    return render_mixed(company, risk)


def render_comparison(
    company1: Optional[CompanyData], inn1: str,
    company2: Optional[CompanyData], inn2: str,
) -> str:
    """Сравнение двух компаний."""
    c1 = company1 or empty_company(inn1)
    c2 = company2 or empty_company(inn2)

    def row(label: str, v1, v2) -> str:
        v1 = str(v1) if v1 else "—"
        v2 = str(v2) if v2 else "—"
        mark = "✅" if v1 == v2 else "↔️"
        return f"{mark} {label}:\n   {v1}\n   {v2}"

    lines = [
        "🔀 Сравнение компаний",
        "",
        f"1️⃣ {c1.name or inn1}",
        f"2️⃣ {c2.name or inn2}",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        row("ИНН", c1.inn, c2.inn),
        row("ОГРН", c1.ogrn, c2.ogrn),
        row("Статус", c1.status, c2.status),
        row("Регион", c1.region, c2.region),
        row("ОКВЭД", c1.okved_main, c2.okved_main),
        row("Возраст (лет)", c1.age_years, c2.age_years),
        row("Штат", c1.employees_count, c2.employees_count),
        row("Выручка", _fmt_money(c1.revenue_last_year), _fmt_money(c2.revenue_last_year)),
        row("Прибыль", _fmt_money(c1.profit_last_year), _fmt_money(c2.profit_last_year)),
        row("Руководитель", c1.director, c2.director),
    ]

    return "\n".join(lines)


def render_internal_analysis(company: CompanyData, risk: Set[str], security: Optional[SecurityResult] = None) -> str:
    parts = [
        "📊 Внутренний разбор: оцениваем риски по банку/115-ФЗ, структуру платежей и соответствие ОКВЭД.",
        _company_table(company),
    ]

    # Блок безопасности
    if security:
        parts.append("━━━━━━━━━━━━━━━━━━━━")
        parts.append(_security_block(security, company.name))

    parts.append("━━━━━━━━━━━━━━━━━━━━")
    parts.append(_recommendations(company))

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


def _security_block(result: SecurityResult, company_name: Optional[str] = None) -> str:
    """Блок безопасности для встраивания в общий отчёт."""
    risk_emoji = {"low": "🟢", "medium": "🟡", "high": "🟠", "critical": "🔴"}
    risk_label = {"low": "Низкий", "medium": "Средний", "high": "Высокий", "critical": "Критический"}

    emoji = risk_emoji.get(result.risk_level, "⚪")
    label = risk_label.get(result.risk_level, "Неизвестен")

    lines = [
        "🔒 Проверка безопасности",
        f"{emoji} Уровень риска: {label}",
    ]

    # ФССП
    lines.append("")
    lines.append("⚖️ ФССП (исполнительные производства):")
    if result.has_enforcement:
        lines.append(f"   ⚠️ Найдено производств: {result.enforcement_count}")
        if result.enforcement_total_sum > 0:
            lines.append(f"   💰 Общая сумма: {_fmt_money(result.enforcement_total_sum)}")
        for detail in result.enforcement_details[:5]:
            lines.append(f"   • {detail}")
        if len(result.enforcement_details) > 5:
            lines.append(f"   ... и ещё {len(result.enforcement_details) - 5}")
    else:
        lines.append("   ✅ Исполнительных производств не найдено")

    # ЗЧБ (когда подключим)
    if result.zchb_details:
        lines.append("")
        lines.append("📋 ЗаЧестныйБизнес:")
        lines.append(f"   {result.zchb_details}")

    # Контур.Фокус (когда подключим)
    if result.focus_details:
        lines.append("")
        lines.append("🔍 Контур.Фокус:")
        lines.append(f"   {result.focus_details}")

    return "\n".join(lines)


def render_profile(profile: UserProfile) -> str:
    """Рендер профиля пользователя."""
    tariff_label = TARIFF_LABELS.get(profile.tariff, profile.tariff)
    limit = profile.daily_limit()
    remaining = profile.remaining_checks()
    profile.reset_if_new_day()

    limit_str = str(limit) if limit is not None else "∞"
    remaining_str = str(remaining) if remaining is not None else "∞"

    lines = [
        "👤 Ваш профиль",
        "",
        f"Тариф: {tariff_label}",
        f"Проверок сегодня: {profile.checks_today}/{limit_str}",
        f"Осталось: {remaining_str}",
        f"Всего проверок: {profile.checks_total}",
        "",
        "─── Возможности ───",
    ]

    features = TARIFF_FEATURES.get(profile.tariff, {})
    for feature, enabled in features.items():
        mark = "✅" if enabled else "❌"
        lines.append(f"{mark} {feature}")

    return "\n".join(lines)


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
