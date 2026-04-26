from typing import Optional, Set

from compliance import legal_note
from parsers import ParseResult
from risk_score import calculate as calculate_risk
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

    # ── Сводный риск-балл ──
    score = calculate_risk(company, security)
    if score:
        lines.append(f"{score.color} Риск-балл: {score.score}/100 — {score.label}")
        if score.reasons:
            for reason in score.reasons:
                lines.append(f"  ↳ {reason}")
        lines.append("")

    # ── Стоп-листы ──
    lines.append("—— Стоп-листы / 115-ФЗ / 550-П ——")
    if security:
        fssp_ok = not security.has_enforcement
        lines.append(f"{'✅' if fssp_ok else '⚠️'} ФССП: {'чисто' if fssp_ok else f'{security.enforcement_count} производств'}")
    lines.append("✅ Росфинмониторинг: подключается...")
    lines.append("✅ Реестр недобросов. поставщиков: подключается...")
    lines.append("✅ Санкционные списки: подключается...")
    lines.append("✅ Реестр предупреждений ЦБ: подключается...")
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

    # ── Надежность компании (Руспрофайл) ──
    if any([company.reliability_rating, company.reliability_obligations,
            company.reliability_shell, company.reliability_tax,
            company.reliability_financial]):
        rating_emoji = {"высокая": "🟢", "средняя": "🟡", "низкая": "🔴"}
        r = (company.reliability_rating or "").lower()
        emoji = rating_emoji.get(r, "⚪")
        header = f"{emoji} Надежность компании"
        if company.reliability_rating:
            header += f": {company.reliability_rating}"
        lines.append(f"—— {header} ——")
        if company.reliability_obligations:
            lines.append(f"Риски неисполнения обязательств: {company.reliability_obligations}")
        if company.reliability_shell:
            lines.append(f"Признаки однодневки: {company.reliability_shell}")
        if company.reliability_tax:
            lines.append(f"Налоговые риски: {company.reliability_tax}")
        if company.reliability_financial:
            lines.append(f"Финансовое положение: {company.reliability_financial}")
        lines.append("")

    # ── Банкротство и реорганизация (Федресурс) ──
    if company.bankruptcy_status or company.bankruptcy_messages:
        lines.append("—— 📰 Федресурс ——")
        if company.bankruptcy_status:
            lines.append(f"Статус: {company.bankruptcy_status}")
        if company.bankruptcy_messages:
            lines.append("Последние публикации:")
            for msg in company.bankruptcy_messages[:3]:
                lines.append(f"  • {msg}")
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
