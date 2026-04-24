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
    lines = []

    # ── Стоп-листы ──
    lines.append("—— Стоп-листы / 115-ФЗ / 550-П ——")
    if security:
        fssp_ok = not security.has_enforcement
        lines.append(f"{'✅' if fssp_ok else '⚠️'} ФССП: {'чисто' if fssp_ok else f'{security.enforcement_count} производств'}")
    lines.append("🔄 Росфинмониторинг: в разработке")
    lines.append("🔄 РНП (недобросовестные поставщики): в разработке")
    lines.append("🔄 Санкционные списки: в разработке")
    lines.append("🔄 Реестр предупреждений ЦБ: в разработке")
    lines.append("")

    # ── Карточка компании ──
    status = company.status or "неизвестно"
    status_emoji = "✅" if "действ" in status.lower() else "⚠️"
    lines.append(f"🏢 {company.name or 'Название не указано'}")
    lines.append(f"ИНН {company.inn or '—'} | {status_emoji} {status}")
    if company.kpp:
        lines.append(f"КПП: {company.kpp}")
    if company.ogrn:
        lines.append(f"ОГРН: {company.ogrn}")
    if company.reg_date:
        age = f" ({company.age_years} лет)" if company.age_years else ""
        lines.append(f"📅 Регистрация: {company.reg_date}{age}")
    if company.director:
        lines.append(f"👤 {company.director}")
    if company.address or company.region:
        lines.append(f"📍 {company.address or company.region}")
    if company.okved_main:
        okved_str = company.okved_main
        if company.okved_name:
            okved_str += f" — {company.okved_name}"
        lines.append(f"🏢 ОКВЭД: {okved_str}")
    if company.capital:
        lines.append(f"💰 Уст. капитал: {_fmt_money(company.capital)}")
    if company.employees_count:
        lines.append(f"👥 Штат: {company.employees_count} чел.")
    lines.append("")

    # ── Финансы ──
    if company.revenue_last_year or company.profit_last_year:
        lines.append("—— Финансы ——")
        rev = _fmt_money(company.revenue_last_year)
        prof = _fmt_money(company.profit_last_year)
        lines.append(f"💹 Выручка: {rev}, прибыль: {prof}")
        if company.source:
            lines.append(f"📡 Источник: {company.source}")
        lines.append("")

    # ── Проверки (ФССП) ──
    if security:
        lines.append("—— Проверки ——")
        if security.has_enforcement:
            lines.append(f"⚖️ ФССП: ⚠️ {security.enforcement_count} производств")
            if security.enforcement_total_sum > 0:
                lines.append(f"   💰 Сумма: {_fmt_money(security.enforcement_total_sum)}")
        else:
            lines.append("⚖️ ФССП: нет ✅")
        lines.append("")

    # ── Арбитраж / госконтракты (Rusprofile) ──
    has_rp = any([
        company.courts_total is not None,
        company.gov_contracts_count is not None,
        company.founders,
    ])
    if has_rp:
        lines.append("—— Дополнительно (Rusprofile) ——")
        if company.courts_total is not None:
            if company.courts_total == 0:
                lines.append("⚖️ Арбитраж: нет дел ✅")
            else:
                parts = []
                if company.courts_plaintiff is not None:
                    parts.append(f"истец: {company.courts_plaintiff}")
                if company.courts_defendant is not None:
                    parts.append(f"ответчик: {company.courts_defendant}")
                detail = f" ({', '.join(parts)})" if parts else ""
                marker = "🟡" if company.courts_total > 5 else "ℹ️"
                lines.append(f"⚖️ Арбитраж: {marker} {company.courts_total} дел{detail}")
        if company.gov_contracts_count is not None:
            if company.gov_contracts_count == 0:
                lines.append("🏛 Госконтракты: нет")
            else:
                amount_str = f" на {_fmt_money(company.gov_contracts_amount)}" if company.gov_contracts_amount else ""
                lines.append(f"🏛 Госконтракты: {company.gov_contracts_count}{amount_str}")
        if company.founders:
            founders_str = ", ".join(company.founders[:3])
            if len(company.founders) > 3:
                founders_str += f" и ещё {len(company.founders) - 3}"
            lines.append(f"👥 Учредители: {founders_str}")
        lines.append("")

    # ── Причины рисков ──
    reasons = _risk_reasons(company, security)
    if reasons:
        lines.append("—— 📋 Причины ——")
        lines.extend(reasons)

    return "\n".join(lines)


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


def _risk_reasons(company: CompanyData, security: Optional[SecurityResult] = None) -> list:
    """Список причин риска по компании."""
    reasons = []
    if company.capital and company.capital <= 10_000:
        reasons.append("🟡 Уставный капитал: Минимальный")
    if company.status and "ликвид" in company.status.lower():
        reasons.append("🔴 Статус: Ликвидируется")
    if company.status and "реорган" in company.status.lower():
        reasons.append("🟡 Статус: В реорганизации")
    if security and security.has_enforcement:
        marker = "🔴" if security.enforcement_count > 10 else "🟡"
        reasons.append(f"{marker} ФССП: {security.enforcement_count} исполнительных производств")
    if company.courts_total and company.courts_total > 10:
        reasons.append(f"🟡 Арбитраж: {company.courts_total} судебных дел")
    return reasons


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


def render_egryl(company: CompanyData) -> str:
    """Полная карточка ЕГРЮЛ."""
    status = company.status or "—"
    status_emoji = "✅" if "действ" in status.lower() else "⚠️"

    lines = [
        "🏛 Выписка ЕГРЮЛ",
        "",
        f"{status_emoji} {status}",
        "",
        "─── Реквизиты ───",
        f"📋 {company.name or '—'}",
        f"ИНН:  {company.inn or '—'}",
    ]
    if company.ogrn:
        lines.append(f"ОГРН: {company.ogrn}")
    if company.kpp:
        lines.append(f"КПП:  {company.kpp}")
    if company.reg_date:
        age = f" ({company.age_years} лет)" if company.age_years else ""
        lines.append(f"📅 Дата регистрации: {company.reg_date}{age}")
    lines.append("")

    lines.append("─── Адрес ───")
    lines.append(f"📍 {company.address or company.region or '—'}")
    lines.append("")

    lines.append("─── Руководство ───")
    lines.append(f"👤 {company.director or '—'}")
    lines.append("")

    lines.append("─── Деятельность ───")
    okved_str = company.okved_main or "—"
    if company.okved_name:
        okved_str += f"\n   {company.okved_name}"
    lines.append(f"ОКВЭД: {okved_str}")
    if company.employees_count:
        lines.append(f"👥 Штат: {company.employees_count} чел.")
    if company.capital:
        lines.append(f"💰 Уставный капитал: {_fmt_money(company.capital)}")
    if company.licenses:
        lines.append(f"📜 Лицензии: {', '.join(company.licenses)}")
    lines.append("")

    if company.source:
        lines.append(f"📡 Источник данных: {company.source.upper()}")

    return "\n".join(lines)


def render_fns_card(company: CompanyData) -> str:
    """Карточка с акцентом на налоговые и регистрационные данные."""
    status = company.status or "—"
    status_emoji = "✅" if "действ" in status.lower() else "⚠️"

    lines = [
        "🏦 Данные ФНС России",
        "",
        f"📋 {company.name or '—'}",
        f"ИНН: {company.inn or '—'} | ОГРН: {company.ogrn or '—'}",
        "",
        "─── Регистрация ───",
        f"{status_emoji} Статус: {status}",
    ]
    if company.reg_date:
        age = f" ({company.age_years} лет)" if company.age_years else ""
        lines.append(f"📅 Дата регистрации: {company.reg_date}{age}")
    lines.append("")

    lines.append("─── ОКВЭД ───")
    if company.okved_main:
        okved_str = company.okved_main
        if company.okved_name:
            okved_str += f" — {company.okved_name}"
        lines.append(f"• Основной: {okved_str}")
    else:
        lines.append("• Нет данных")
    lines.append("")

    has_fin = company.capital or company.revenue_last_year or company.profit_last_year
    if has_fin:
        lines.append("─── Финансы ───")
        if company.capital:
            lines.append(f"💰 Уставный капитал: {_fmt_money(company.capital)}")
        if company.revenue_last_year:
            lines.append(f"💹 Выручка: {_fmt_money(company.revenue_last_year)}")
        if company.profit_last_year:
            lines.append(f"📈 Прибыль: {_fmt_money(company.profit_last_year)}")
        lines.append("")

    lines.append("─── Контакты ───")
    lines.append(f"📍 {company.address or company.region or '—'}")
    lines.append(f"👤 Руководитель: {company.director or '—'}")

    return "\n".join(lines)


def render_checks_history(history: list) -> str:
    """История последних проверок пользователя."""
    if not history:
        return "📜 История проверок пуста.\n\nОтправьте ИНН компании, чтобы начать."

    lines = [f"📜 История проверок (последние {len(history)}):"]
    lines.append("")
    for i, entry in enumerate(reversed(history), 1):
        name = entry.get("name") or "—"
        inn = entry.get("inn") or "—"
        date = (entry.get("date") or "")[:10] or "—"
        lines.append(f"{i}. {name}")
        lines.append(f"   ИНН: {inn} | {date}")
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
