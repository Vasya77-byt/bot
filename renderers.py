from typing import List, Optional, Set

from compliance import legal_note
from parsers import ParseResult
from schemas import CarryoverSummary, CompanyData, Contract, Debt, empty_company


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


# ── Рендеринг учётных данных (контракты, долги, сводка) ──


def render_contracts(contracts: List[Contract]) -> str:
    if not contracts:
        return "Контракты: нет активных контрактов."
    lines = ["Активные контракты:"]
    for i, c in enumerate(contracts, 1):
        status_label = {"active": "действует", "completed": "завершён", "cancelled": "отменён"}.get(
            c.status or "", c.status or "—"
        )
        remaining = f"{c.remaining_amount:,.0f}" if c.remaining_amount else "—"
        lines.append(
            f"{i}. {c.counterparty or '—'} (ИНН {c.inn or '—'}) — {c.subject or '—'}"
        )
        lines.append(
            f"   Сумма: {c.total_amount or '—'} | Оплачено: {c.paid_amount or '—'} "
            f"| Остаток: {remaining} | Статус: {status_label}"
        )
        if c.start_date or c.end_date:
            lines.append(f"   Срок: {c.start_date or '—'} — {c.end_date or '—'}")
    return "\n".join(lines)


def render_debts(debts: List[Debt]) -> str:
    if not debts:
        return "Задолженности: нет непогашенных задолженностей."

    receivables = [d for d in debts if d.direction == "receivable"]
    payables = [d for d in debts if d.direction == "payable"]

    lines = ["Задолженности:"]

    if receivables:
        total_r = sum(d.amount or 0 for d in receivables)
        lines.append(f"\nДебиторская (нам должны) — итого: {total_r:,.0f}:")
        for d in receivables:
            status_label = {"outstanding": "не погашена", "overdue": "просрочена",
                            "settled": "погашена"}.get(d.status or "", d.status or "—")
            lines.append(
                f"  • {d.counterparty or '—'} (ИНН {d.inn or '—'}): "
                f"{d.amount or '—'} — {status_label}"
            )

    if payables:
        total_p = sum(d.amount or 0 for d in payables)
        lines.append(f"\nКредиторская (мы должны) — итого: {total_p:,.0f}:")
        for d in payables:
            status_label = {"outstanding": "не погашена", "overdue": "просрочена",
                            "settled": "погашена"}.get(d.status or "", d.status or "—")
            lines.append(
                f"  • {d.counterparty or '—'} (ИНН {d.inn or '—'}): "
                f"{d.amount or '—'} — {status_label}"
            )

    return "\n".join(lines)


def render_carryover_summary(summary: CarryoverSummary) -> str:
    lines = [
        f"Сводка переноса {summary.year_from} → {summary.year_to}:",
        f"• Активных контрактов перенесено: {len(summary.active_contracts)}",
        f"• Непогашенных задолженностей перенесено: {len(summary.outstanding_debts)}",
        f"• Дебиторская задолженность (нам должны): {summary.total_receivables:,.0f}",
        f"• Кредиторская задолженность (мы должны): {summary.total_payables:,.0f}",
    ]
    remaining = sum(c.remaining_amount or 0 for c in summary.active_contracts)
    if remaining:
        lines.append(f"• Остаток по контрактам: {remaining:,.0f}")
    if summary.notes:
        lines.append(f"• Примечание: {summary.notes}")
    return "\n".join(lines)


def render_ledger_status(
    contracts: List[Contract],
    debts: List[Debt],
    year: int,
) -> str:
    parts = [
        f"Учётный период: {year} год",
        render_contracts(contracts),
        render_debts(debts),
    ]
    return "\n\n".join(parts)

