"""Форматирование отчётов — обычная проверка, предложение, запрос счёта."""

from typing import Any
from risk_scoring import calculate_risk_score


def _first_num(*values):
    """Возвращает первое не-None значение (в отличие от `or`, считает 0 валидным)."""
    for v in values:
        if v is not None:
            return v
    return None


def _format_age(years: float) -> str:
    """Форматирует возраст: 1 год, 2 года, 5 лет, 1.5 → 1 год 6 мес."""
    y = int(years)
    months = round((years - y) * 12)
    parts = []
    if y > 0:
        if y % 10 == 1 and y % 100 != 11:
            parts.append(f"{y} год")
        elif y % 10 in (2, 3, 4) and y % 100 not in (12, 13, 14):
            parts.append(f"{y} года")
        else:
            parts.append(f"{y} лет")
    if months > 0:
        parts.append(f"{months} мес.")
    return " ".join(parts) if parts else "менее 1 мес."


def format_report_free(
    fields: dict[str, Any],
    zchb_data: dict[str, Any] | None = None,
    zsk_data: dict[str, Any] | None = None,
    rp_data: dict[str, Any] | None = None,
    fns_data: dict[str, Any] | None = None,
    sanctions_data: dict[str, Any] | None = None,
    cbrf_data: dict[str, Any] | None = None,
) -> str:
    """Краткий отчёт для бесплатных пользователей. Вердикт-ориентированный."""
    zchb = zchb_data or {}
    zsk = zsk_data or {}
    rp = rp_data or {}
    fns = fns_data or {}
    sanctions = sanctions_data or {}
    cbrf = cbrf_data or {}
    lines: list[str] = []

    entity_type = fields.get("entity_type", "ul")
    is_ip = entity_type == "ip"

    # ═══ ПО ДАННЫМ ИСТОЧНИКОВ (СВЕРХУ) ═══
    lines.append("─── <b>По данным источников</b> ───")
    _append_source_lights(zsk, rp, fns, lines)
    lines.append("")

    # ═══ КАРТОЧКА КОМПАНИИ ═══
    name = fields.get("name") or "Неизвестно"
    icon = "👤" if is_ip else "🏢"
    lines.append(f"{icon} <b>{_esc(name)}</b>")
    lines.append(f"ИНН <code>{fields.get('inn', '—')}</code> | {_status_label(fields.get('status'))}")

    reg = fields.get("registration_date")
    age = fields.get("company_age_years")
    if reg:
        age_s = f" ({_format_age(age)})" if age is not None else ""
        lines.append(f"📅 {reg}{age_s}")

    if not is_ip:
        mgr = fields.get("management_name")
        if mgr:
            lines.append(f"👤 {_esc(mgr)}")

    full_addr = fields.get("full_address") or zchb.get("address") or fields.get("city")
    if full_addr:
        lines.append(f"📍 {_esc(str(full_addr)[:80])}")

    # Финансы — 1 строка
    rev = _first_num(zchb.get("revenue"), fields.get("income"))
    if rev is not None:
        lines.append(f"📊 Выручка: {_money(rev)}")

    # Стоп-листы (важно для всех пользователей!)
    _format_stop_lists(zchb, sanctions, cbrf, fns, lines)

    return "\n".join(lines)


def format_report(
    fields: dict[str, Any],
    links: dict[str, str] | None = None,
    zchb_data: dict[str, Any] | None = None,
    zsk_data: dict[str, Any] | None = None,
    rp_data: dict[str, Any] | None = None,
    fin_history: dict[str, Any] | None = None,
    fns_data: dict[str, Any] | None = None,
    sanctions_data: dict[str, Any] | None = None,
    cbrf_data: dict[str, Any] | None = None,
) -> str:
    zchb = zchb_data or {}
    zsk = zsk_data or {}
    rp = rp_data or {}
    fin = fin_history or {}
    fns = fns_data or {}
    sanctions = sanctions_data or {}
    lines: list[str] = []

    entity_type = fields.get("entity_type", "ul")
    is_ip = entity_type == "ip"

    # ═══ ПО ДАННЫМ ИСТОЧНИКОВ + СТОП-ЛИСТЫ (СВЕРХУ) ═══
    lines.append("─── <b>По данным источников</b> ───")
    _append_source_lights(zsk, rp, fns, lines)
    # Стоп-листы сразу после светофора
    _format_stop_lists(zchb, sanctions, cbrf_data or {}, fns, lines)
    lines.append("")

    # ═══ КАРТОЧКА КОМПАНИИ ═══
    name = fields.get("name") or "Неизвестно"
    icon = "👤" if is_ip else "🏢"
    lines.append(f"{icon} <b>{_esc(name)}</b>")
    lines.append(f"ИНН <code>{fields.get('inn', '—')}</code> | {_status_label(fields.get('status'))}")
    if not is_ip and fields.get("kpp"):
        lines.append(f"КПП: {fields['kpp']}")
    if fields.get("ogrn"):
        ogrn_label = "ОГРНИП" if is_ip else "ОГРН"
        lines.append(f"{ogrn_label}: {fields['ogrn']}")

    # Регистрация
    reg = fields.get("registration_date")
    age = fields.get("company_age_years")
    if reg:
        age_s = f" ({_format_age(age)})" if age is not None else ""
        lines.append(f"📅 Регистрация: {reg}{age_s}")

    # Руководитель (только для ЮЛ)
    if not is_ip:
        mgr = fields.get("management_name")
        if mgr:
            post = fields.get("management_post") or ""
            if post:
                lines.append(f"👤 {_esc(post)}: {_esc(mgr)}")
            else:
                lines.append(f"👤 Руководитель: {_esc(mgr)}")

    # Адрес
    # Адрес (полный из ЗЧБ API, иначе город из DaData)
    full_addr = fields.get("full_address") or zchb.get("address")
    city = fields.get("city")
    if full_addr:
        lines.append(f"📍 {_esc(full_addr)}")
    elif city:
        lines.append(f"📍 {_esc(city)}")

    # ОКВЭД
    okved = fields.get("okved_code")
    okved_text = fields.get("okved_text")
    if okved:
        s = f"🏭 ОКВЭД: {okved}"
        if okved_text:
            short = okved_text[:50] + "..." if len(okved_text) > 50 else okved_text
            s += f" — {_esc(short)}"
        lines.append(s)

    # Доп. ОКВЭДы (из ЗЧБ API)
    add_okveds = zchb.get("additional_okveds") or []
    if add_okveds:
        ok_strs = [f'{o["code"]}' for o in add_okveds[:5]]
        lines.append(f"   + {', '.join(ok_strs)}" + (f" и ещё {len(add_okveds)-5}" if len(add_okveds) > 5 else ""))

    # Уставный капитал (только для ЮЛ)
    if not is_ip:
        cap = fields.get("capital_value")
        if cap is not None:
            lines.append(f"💰 Уст. капитал: {_money(cap)}")

    # МСП (малое/среднее предприятие)
    msp = fields.get("msp_category") or zchb.get("msp_category")
    if msp:
        if isinstance(msp, dict):
            # ЗЧБ API возвращает {'1': 'Малое', '2': 'до 800 млн', '3': '16-100 чел'}
            cat_name = msp.get("1", "")
            revenue_cat = msp.get("2", "")
            staff_cat = msp.get("3", "")
            # Убираем HTML entities
            staff_cat = staff_cat.replace("&mdash;", "–").replace("&ndash;", "–")
            parts = [p for p in [cat_name, revenue_cat, staff_cat] if p]
            msp_text = " | ".join(parts) if parts else str(msp)
        else:
            msp_text = str(msp)
        lines.append(f"🏷 МСП: {_esc(msp_text)}")

    # ── Финансы ──
    lines.append("")
    lines.append("─── <b>Финансы</b> ───")

    # Приоритет: ЗЧБ API → FNS bo → DaData (проверенные API).
    fns_bo = fns.get("bo") or {}
    rev = _first_num(
        zchb.get("revenue"), fns_bo.get("revenue"),
        fields.get("income"),
    )
    profit = _first_num(zchb.get("net_profit"), fns_bo.get("net_profit"))
    if rev is not None or profit is not None:
        parts = []
        if rev is not None:
            parts.append(f"выручка {_money(rev)}")
        if profit is not None:
            parts.append(f"прибыль {_money(profit)}")
        # Указываем год и источник
        src = ""
        if zchb.get("revenue_year"):
            src = f" ({zchb['revenue_year']}, ЗЧБ)"
        elif fns_bo.get("latest_year"):
            src = f" ({fns_bo['latest_year']}, ФНС)"
        elif fields.get("finance_year"):
            src = f" ({fields['finance_year']}, DaData)"
        lines.append(f"📊 {', '.join(parts)}{src}")
    else:
        # Fallback: scraping
        rev_fallback = _first_num(zsk.get("revenue"), rp.get("revenue"))
        if rev_fallback is not None:
            lines.append(f"📊 Выручка: {_money(rev_fallback)} <i>(оценка)</i>")
        else:
            lines.append("📊 Финансы: данные запрошены, ожидайте в след. проверке")

    # Динамика по годам (приоритет ЗЧБ API → ФНС BO)
    zchb_years = zchb.get("finances") or []
    bo_years = fns_bo.get("years", [])
    trend_years = zchb_years if len(zchb_years) >= 2 else bo_years
    if trend_years and len(trend_years) >= 2:
        year_parts = []
        for yd in trend_years[:3]:
            y = yd.get("year")
            r = yd.get("revenue")
            if r is not None:
                year_parts.append(f"{y}: {_money_short(r)}")
        if year_parts:
            lines.append(f"📈 {' → '.join(reversed(year_parts))}")

    # Финансовая динамика ITSoft — ТОЛЬКО если нет данных ФНС по годам
    if not bo_years or len(bo_years) < 2:
        fin_years = fin.get("years", [])
        if fin_years:
            trend = fin.get("trend")
            trend_icon = {"up": "📈", "down": "📉", "stable": "➡️"}.get(trend, "📊")
            year_parts = []
            for yd in fin_years[:3]:
                y = yd["year"]
                inc = yd.get("income")
                if inc is not None and inc > 0:
                    year_parts.append(f"{y}: {_money_short(inc)}")
            if year_parts:
                lines.append(f"{trend_icon} {' → '.join(reversed(year_parts))} <i>(itsoft)</i>")

    # Расходы
    expense = _first_num(fns_bo.get("cost"), fields.get("expense"))
    if expense is not None:
        lines.append(f"💸 Расходы: {_money(expense)}")

    # Рентабельность
    if rev is not None and rev > 0 and profit is not None:
        margin = profit / rev * 100
        margin_icon = "📈" if margin > 10 else "📉" if margin < 0 else "➡️"
        lines.append(f"{margin_icon} Рентабельность: {margin:.1f}%")

    # Задолженность / штрафы (DaData)
    debt = fields.get("debt")
    penalty = fields.get("penalty")
    if debt is not None and debt > 0:
        lines.append(f"⚠️ Задолженность: {_money(debt)}")
    if penalty is not None and penalty > 0:
        lines.append(f"⚠️ Пени/штрафы: {_money(penalty)}")

    # ── Штат ──
    emp = _first_num(zchb.get("employee_count"), zsk.get("employee_count"), fields.get("employee_count"))
    if emp is not None:
        lines.append(f"👥 Штат: {emp} чел.")

    # ── Налогообложение ──
    tax = fields.get("tax_system")
    if tax and str(tax).lower() != "none":
        lines.append(f"💼 Налогообложение: {_esc(str(tax))}")

    # ── Учредители (макс 5) ──
    if not is_ip:
        founders = fields.get("founders") or []
        cap = fields.get("capital_value")
        if founders:
            lines.append("")
            lines.append("─── <b>Учредители</b> ───")
            for f in founders[:5]:
                fname = _esc(f.get("name", "—"))
                share = f.get("share")
                share_type = f.get("share_type")
                share_s = ""
                if share is not None:
                    try:
                        sv = float(share)
                        if share_type == "PERCENT":
                            # Уже в процентах
                            share_s = f" ({sv:.1f}%)"
                        elif share_type == "DECIMAL":
                            # Дробь: 0.5 = 50%
                            share_s = f" ({sv * 100:.1f}%)"
                        elif share_type == "NOMINAL" and cap and cap > 0:
                            # Номинал в рублях → пересчитываем в %
                            pct = sv / cap * 100
                            share_s = f" ({pct:.1f}%)"
                        elif cap and cap > 0:
                            # Тип неизвестен — пробуем по значению
                            if sv <= 100:
                                share_s = f" ({sv:.1f}%)"
                            else:
                                pct = sv / cap * 100
                                share_s = f" ({pct:.1f}%)"
                    except (ValueError, TypeError):
                        pass
                lines.append(f"  • {fname}{share_s}")
            if len(founders) > 5:
                lines.append(f"  <i>... ещё {len(founders) - 5}</i>")

    # ── Лицензии ──
    licenses = fields.get("licenses") or []
    if licenses:
        lines.append("")
        lines.append("─── <b>Лицензии</b> ───")
        for lic in licenses[:5]:
            short = lic[:80] + "..." if len(lic) > 80 else lic
            lines.append(f"  📜 {_esc(short)}")
        if len(licenses) > 5:
            lines.append(f"  <i>... ещё {len(licenses) - 5}</i>")

    # ── Сводка: Суды / ФССП / ФНС / ЦБ (короткая, детали по кнопкам) ──
    lines.append("")
    lines.append("─── <b>Проверки</b> ───")

    # Суды — 1 строка (приоритет: ЗЧБ API → scraping)
    courts_total = _first_num(zchb.get("courts_total"), zsk.get("courts_total"))
    if courts_total is not None:
        if courts_total == 0:
            lines.append("⚖️ Суды: нет ✅")
        else:
            defendant = _first_num(zchb.get("courts_defendant"), zsk.get("courts_defendant")) or 0
            plaintiff = _first_num(zchb.get("courts_plaintiff"), zsk.get("courts_plaintiff")) or 0
            lines.append(f"⚖️ Суды: {_fmt_number(courts_total)} (истец {_fmt_number(plaintiff)}, отв. {_fmt_number(defendant)}) ⤵️")
    else:
        lines.append("⚖️ Суды: проверяется...")

    # ФССП — 1 строка (приоритет: ЗЧБ API → scraping)
    fssp = _first_num(zchb.get("fssp_count"), zsk.get("fssp_total"))
    if fssp is not None:
        if fssp > 0:
            lines.append(f"👮 ФССП: {fssp} произв. ⤵️")
        else:
            lines.append("👮 ФССП: нет ✅")
    else:
        lines.append("👮 ФССП: проверяется...")

    # ФНС проверка — 1 строка
    check = fns.get("check") or {}
    nalogbi = fns.get("nalogbi") or {}
    fns_problems = []
    if check.get("mass_director"):
        fns_problems.append("масс.рук")
    if check.get("mass_address"):
        fns_problems.append("масс.адрес")
    if check.get("unreliable_address") or check.get("unreliable_director"):
        fns_problems.append("недост.")
    if check.get("disqualified"):
        fns_problems.append("дисквал")
    if check.get("tax_debt"):
        fns_problems.append("долг")
    if check.get("no_reports"):
        fns_problems.append("нет отч.")
    if check.get("capital_decrease"):
        fns_problems.append("уменьш.кап.")
    if check.get("liquidation_decision"):
        fns_problems.append("ликвид.")
    if nalogbi.get("has_blocked_accounts"):
        cnt = nalogbi.get("blocked_accounts_count", 0)
        fns_problems.append(f"блок.счетов:{cnt}")

    if fns_problems:
        lines.append(f"🏦 ФНС: ⚠️ {', '.join(fns_problems)} ⤵️")
    elif check.get("source"):
        lines.append("🏦 ФНС: чисто ✅")
    else:
        lines.append("🏦 ФНС: нет данных")

    # Госзакупки (ЗЧБ API)
    purch_count = zchb.get("purchases_supplier_count")
    if purch_count is not None and purch_count > 0:
        purch_sum = zchb.get("purchases_supplier_sum", 0)
        lines.append(f"📋 Госзакупки: {_fmt_number(purch_count)} контр. на {_money(purch_sum)}")

    # Доп. инфо (ЗЧБ API)
    branches = zchb.get("branches_count")
    trademarks = zchb.get("trademarks_count")
    inspections = zchb.get("inspections_count")
    extras = []
    if branches:
        extras.append(f"филиалы: {branches}")
    if trademarks:
        extras.append(f"тов.знаки: {trademarks}")
    if inspections:
        extras.append(f"проверки: {inspections}")
    if extras:
        lines.append(f"ℹ️ {', '.join(extras)}")

    # Стоп-листы уже показаны СВЕРХУ — не дублируем

    # ── Наш анализ (Risk Engine) ──
    risk = calculate_risk_score(fields, zchb_data=zchb, fns_data=fns, sanctions_data=sanctions, cbrf_data=cbrf_data or {})
    risk_factors = risk.get("factors", [])
    # Показываем только реальные проблемы (score > 0)
    problems = [f for f in risk_factors if f.get("score", 0) > 0]
    if problems:
        lines.append("")
        lines.append("─── <b>📋 Причины</b> ───")
        for f in problems[:8]:
            score_val = f.get("score", 0)
            if score_val >= 20:
                icon = "🔴"
            elif score_val >= 10:
                icon = "🟡"
            else:
                icon = "⚪"
            lines.append(f"  {icon} {f['name']}: {f.get('comment', '')}")

    # ── Ссылки на источники ──
    _links = links or {}
    if _links:
        lines.append("")
        link_parts = []
        if _links.get("rusprofile"):
            link_parts.append(f'<a href="{_links["rusprofile"]}">Русрпофайл</a>')
        if _links.get("zachestnyibiznes"):
            link_parts.append(f'<a href="{_links["zachestnyibiznes"]}">ЗЧБ</a>')
        if link_parts:
            lines.append(f"🔗 {' | '.join(link_parts)}")

    return "\n".join(lines)


# ─────────────────────────────────────────────────
# Детальные форматтеры (для callback-кнопок)
# ─────────────────────────────────────────────────

def _append_source_lights(zsk: dict, rp: dict, fns: dict, lines: list[str]) -> None:
    """Светофор по данным источников: ЗЧБ, Русрпофайл, ЗСК ЦБ."""
    has_any = False

    # ЗЧБ
    zsk_color = zsk.get("reliability_color")
    zsk_label = zsk.get("reliability_label")
    zsk_score = zsk.get("reliability_score")
    if zsk_color:
        icon = {"green": "🟢", "red": "🔴", "yellow": "🟡"}.get(zsk_color, "⚪")
        score_s = f" (балл: {zsk_score})" if zsk_score else ""
        lines.append(f"{icon} ЗЧБ: <b>{zsk_label or zsk_color}</b>{score_s}")
        has_any = True

    # Русрпофайл
    rp_color = rp.get("reliability_color")
    rp_label = rp.get("reliability_label")
    if rp_color:
        icon = {"green": "🟢", "red": "🔴", "yellow": "🟡"}.get(rp_color, "⚪")
        lines.append(f"{icon} Русрпофайл: <b>{rp_label or rp_color}</b>")
        has_any = True

    # ЗСК ЦБ (с расшифровкой кодов критериев)
    fns_zsk = fns.get("zsk") or {}
    fns_zsk_color = fns_zsk.get("zsk_color")
    fns_zsk_level = fns_zsk.get("zsk_level")
    if fns_zsk_color:
        icon = {"green": "🟢", "red": "🔴", "yellow": "🟡"}.get(fns_zsk_color, "⚪")
        lines.append(f"{icon} ЗСК ЦБ: <b>{fns_zsk_level or fns_zsk_color}</b>")
        # Коды критериев риска
        risk_reasons = fns_zsk.get("risk_reasons", [])
        risk_codes = fns_zsk.get("risk_codes", [])
        for i, reason in enumerate(risk_reasons[:3]):
            code = risk_codes[i] if i < len(risk_codes) else ""
            code_s = f" [{code}]" if code else ""
            lines.append(f"   ⚠️ {_esc(reason)}{code_s}")
        has_any = True

    if not has_any:
        lines.append("⚪ Нет данных от источников")


def _format_stop_lists(zchb: dict, sanctions: dict, cbrf: dict, fns: dict, lines: list[str]) -> None:
    """Стоп-листы: 115-ФЗ, 550-П, террористы, санкции, блокировки."""
    check = fns.get("check") or {}
    nalogbi = fns.get("nalogbi") or {}
    lines.append("")
    lines.append("─── <b>Стоп-листы / 115-ФЗ / 550-П</b> ───")

    # 1. Террорист/экстремист (ЗЧБ API → Росфинмониторинг)
    if zchb.get("terrorist"):
        lines.append("🚨 <b>ТЕРРОРИСТ/ЭКСТРЕМИСТ — В РЕЕСТРЕ!</b>")
    else:
        lines.append("✅ Росфинмониторинг: чисто")

    # 2. Недобросовестный поставщик (ЗЧБ API → ЕИС)
    if zchb.get("bad_supplier"):
        lines.append("⚠️ <b>Недобросовестный поставщик (ЕИС)!</b>")
    else:
        lines.append("✅ Реестр недобросов. поставщиков: чисто")

    # 3. Санкции (OpenSanctions)
    if sanctions.get("found"):
        lines.append("🛑 Санкции: ⚠️ <b>найден в списках!</b>")
    else:
        lines.append("✅ Санкционные списки: чисто")

    # 4. Реестр предупреждений ЦБ (нелегальная деятельность)
    if cbrf.get("found"):
        cnt = cbrf.get("count", 0)
        lines.append(f"🚫 Реестр ЦБ (нелегальная деят.): ⚠️ <b>{cnt}</b>")
    else:
        lines.append("✅ Реестр предупреждений ЦБ: чисто")

    # 5. Блокировка счетов ФНС
    if nalogbi.get("has_blocked_accounts"):
        cnt = nalogbi.get("blocked_accounts_count", 0)
        lines.append(f"🔒 Блокировка счетов ФНС: ⚠️ <b>{cnt} решений</b>")
    elif nalogbi.get("source"):
        lines.append("✅ Блокировка счетов ФНС: нет")

    # 6. Недостоверные сведения в ЕГРЮЛ (ФНС)
    unreliable = []
    if check.get("unreliable_address"):
        unreliable.append("адрес")
    if check.get("unreliable_director"):
        unreliable.append("руководитель")
    if check.get("unreliable_founder"):
        unreliable.append("учредитель")
    if unreliable:
        lines.append(f"⚠️ Недостоверные сведения ЕГРЮЛ: <b>{', '.join(unreliable)}</b>")

    # 7. Дисквалификация
    if check.get("disqualified"):
        lines.append("🚨 <b>Руководитель дисквалифицирован!</b>")


def _format_risk_traffic_light(zsk: dict, rp: dict, fns: dict, lines: list[str],
                                risk_result: dict | None = None) -> None:
    """Свой светофор + по данным ЗЧБ/Русрпофайл."""
    lines.append("")
    lines.append("─── <b>ОЦЕНКА РИСКА</b> ───")

    # Наш собственный Risk Engine
    if risk_result:
        emoji = risk_result.get("emoji", "⚪")
        label = risk_result.get("label", "Нет данных")
        score = risk_result.get("total_score", 0)
        lines.append(f"{emoji} <b>{label}</b> (балл: {score})")

        # Причины — только негативные (score > 0)
        neg_factors = [f for f in risk_result.get("factors", []) if f["score"] > 0]
        if neg_factors:
            lines.append("")
            lines.append("<b>📋 Причины:</b>")
            for f in sorted(neg_factors, key=lambda x: -x["score"])[:8]:
                icon = "🔴" if f["score"] >= 20 else "🟡" if f["score"] >= 10 else "⚪"
                lines.append(f"  {icon} {f['name']}: {f['comment']}")

        # Рекомендация
        lines.append("")
        if score >= 50:
            lines.append("⛔ <b>НЕ РЕКОМЕНДУЕТСЯ работать</b>")
        elif score >= 25:
            lines.append("⚠️ <b>Работать с осторожностью</b> (предоплата)")
        else:
            lines.append("✅ <b>МОЖНО работать</b>")

    # Подтверждение от внешних источников
    lines.append("")
    lines.append("<b>По данным источников:</b>")

    # ЗЧБ
    zsk_color = zsk.get("reliability_color")
    zsk_label = zsk.get("reliability_label")
    if zsk_color:
        icon = {"green": "🟢", "red": "🔴", "yellow": "🟡"}.get(zsk_color, "⚪")
        lines.append(f"  {icon} ЗЧБ: {zsk_label or zsk_color}")

    # Русрпофайл
    rp_color = rp.get("reliability_color")
    rp_label = rp.get("reliability_label")
    if rp_color:
        icon = {"green": "🟢", "red": "🔴", "yellow": "🟡"}.get(rp_color, "⚪")
        lines.append(f"  {icon} Русрпофайл: {rp_label or rp_color}")

    # ЗСК ЦБ (с расшифровкой кодов критериев)
    fns_zsk = fns.get("zsk") or {}
    fns_zsk_color = fns_zsk.get("zsk_color")
    if fns_zsk_color:
        icon = {"green": "🟢", "red": "🔴", "yellow": "🟡"}.get(fns_zsk_color, "⚪")
        lines.append(f"  {icon} ЗСК ЦБ: {fns_zsk.get('zsk_level', fns_zsk_color)}")
        # Расшифровка кодов критериев риска
        risk_reasons = fns_zsk.get("risk_reasons") or []
        risk_codes = fns_zsk.get("risk_codes") or []
        if risk_codes:
            for code, reason in zip(risk_codes, risk_reasons):
                lines.append(f"     ⤷ <i>{code}: {reason}</i>")


def format_courts_detail(zsk_data: dict[str, Any] | None, zchb_data: dict[str, Any] | None = None, court_cases: dict[str, Any] | None = None) -> str:
    """Детальная информация по судам для кнопки ⚖️."""
    zsk = zsk_data or {}
    zchb = zchb_data or {}
    cases = court_cases or {}
    lines: list[str] = []
    lines.append("⚖️ <b>Судебные дела (детально)</b>")
    lines.append("")

    # Статистика из ЗЧБ API (точные данные)
    plaintiff = _first_num(zchb.get("courts_plaintiff"), zsk.get("courts_plaintiff"))
    defendant = _first_num(zchb.get("courts_defendant"), zsk.get("courts_defendant"))
    total = _first_num(zchb.get("courts_total"), zsk.get("courts_total"))

    if total is not None and total > 0:
        lines.append(f"Всего дел: <b>{_fmt_number(total)}</b>")
        if plaintiff is not None:
            psum = zchb.get("courts_plaintiff_sum")
            s = f"  Истец: {_fmt_number(plaintiff)} дел"
            if psum:
                s += f" на {_money(psum)}"
            lines.append(s)
        if defendant is not None:
            dsum = zchb.get("courts_defendant_sum")
            s = f"  Ответчик: {_fmt_number(defendant)} дел"
            if dsum:
                s += f" на {_money(dsum)}"
            lines.append(s)
    elif total == 0:
        lines.append("✅ Судебных дел не найдено")
    else:
        _format_courts(zsk, lines)

    # Детальные дела из ЗЧБ API
    case_list = cases.get("cases") or []
    if case_list:
        lines.append("")
        lines.append(f"─── <b>Последние дела</b> ({cases.get('total', len(case_list))} всего) ───")
        for c in case_list[:7]:
            num = c.get("number", "—")
            status = c.get("status", "")
            amount = c.get("amount")
            date = c.get("date", "")
            status_icon = "🔵" if "рассмат" in status.lower() else "✅" if "заверш" in status.lower() else "⚪"
            s = f"{status_icon} <b>{num}</b> ({date})"
            if amount:
                s += f" — {_money(amount)}"
            lines.append(s)
            cat = c.get("category", "")
            if cat:
                short_cat = cat[:120] + "..." if len(cat) > 120 else cat
                lines.append(f"   <i>{_esc(short_cat)}</i>")

    fssp = _first_num(zchb.get("fssp_count"), zsk.get("fssp_total"))
    if fssp is not None:
        lines.append("")
        if fssp > 0:
            fssp_sum = _first_num(zchb.get("fssp_sum"), zsk.get("fssp_sum"))
            s = f"👮 ФССП: {fssp} производств"
            if fssp_sum:
                s += f" на {_money(fssp_sum)}"
            lines.append(s)
        else:
            lines.append("👮 ФССП: нет производств ✅")

    if total is None and fssp is None and not case_list:
        lines.append("ℹ️ Данные по судам и ФССП не найдены")

    return "\n".join(lines)


def format_fns_detail(fns_data: dict[str, Any] | None, cbrf_data: dict[str, Any] | None = None) -> str:
    """Детальная информация ФНС + блокировки + ЗСК + бухотчётность + отказы ЦБ."""
    fns = fns_data or {}
    cbrf = cbrf_data or {}
    lines: list[str] = []
    lines.append("🏦 <b>Проверка ФНС (детально)</b>")
    _format_fns_section(fns, lines)

    # ── ЗСК ЦБ (Знай Своего Клиента) ──
    fns_zsk = fns.get("zsk") or {}
    if fns_zsk.get("zsk_level"):
        lines.append("")
        lines.append("─── <b>ЗСК ЦБ (Знай Своего Клиента)</b> ───")
        color = fns_zsk.get("zsk_color", "")
        level = fns_zsk.get("zsk_level", "")
        icon = {"green": "🟢", "red": "🔴", "yellow": "🟡"}.get(color, "⚪")
        lines.append(f"{icon} <b>{level}</b>")
        zsk_text = fns_zsk.get("zsk_text")
        if zsk_text:
            lines.append(f"  <i>{_esc(zsk_text[:200])}</i>")
        # Коды критериев с расшифровкой
        risk_reasons = fns_zsk.get("risk_reasons", [])
        risk_codes = fns_zsk.get("risk_codes", [])
        if risk_reasons:
            lines.append("")
            lines.append("<b>Критерии риска:</b>")
            for i, reason in enumerate(risk_reasons):
                code = risk_codes[i] if i < len(risk_codes) else ""
                lines.append(f"  ⚠️ <b>{code}</b> — {_esc(reason)}")

    # ── Бухгалтерская отчётность (BO) ──
    fns_bo = fns.get("bo") or {}
    bo_years = fns_bo.get("years", [])
    if bo_years:
        lines.append("")
        lines.append("─── <b>Бухотчётность ФНС</b> ───")
        for yd in bo_years[:5]:
            y = yd.get("year", "?")
            parts = []
            r = yd.get("revenue")
            if r is not None:
                parts.append(f"выр. {_money_short(r)}")
            np = yd.get("net_profit")
            if np is not None:
                parts.append(f"приб. {_money_short(np)}")
            ta = yd.get("total_assets")
            if ta is not None:
                parts.append(f"акт. {_money_short(ta)}")
            gp = yd.get("gross_profit")
            if gp is not None:
                parts.append(f"вал.приб. {_money_short(gp)}")
            if parts:
                lines.append(f"  <b>{y}</b>: {', '.join(parts)}")

    # ── Реестр предупреждений ЦБ ──
    if cbrf.get("source"):
        lines.append("")
        lines.append("─── <b>Реестр предупреждений ЦБ</b> ───")
        if cbrf.get("found"):
            cnt = cbrf.get("count", 0)
            lines.append(f"⚠️ <b>Найдено в реестре ЦБ: {cnt}</b>")
            for d in cbrf.get("details", [])[:5]:
                name = _esc(d.get("name", "—"))
                dtype = d.get("type", "")
                date = d.get("date", "")
                lines.append(f"  • {name}")
                if dtype or date:
                    lines.append(f"    {_esc(dtype)} ({date})")
        else:
            lines.append("✅ В реестре предупреждений ЦБ не найдено")

    if not fns.get("check") and not fns.get("nalogbi") and not cbrf.get("source"):
        lines.append("")
        lines.append("ℹ️ Данные ФНС недоступны")

    return "\n".join(lines)


def _format_fns_section(fns: dict, lines: list[str]) -> None:
    """Форматирует блок данных ФНС (проверка контрагента, блокировки)."""
    check = fns.get("check") or {}
    nalogbi = fns.get("nalogbi") or {}

    has_fns_data = False

    # Проверка контрагента
    risks = []
    if check.get("mass_director"):
        detail = check.get("mass_director_detail", "")
        risks.append(f"массовый руководитель" + (f" ({_esc(detail)})" if detail else ""))
    if check.get("mass_founder"):
        risks.append("массовый учредитель")
    if check.get("mass_address"):
        detail = check.get("mass_address_detail", "")
        risks.append(f"массовый адрес" + (f" ({_esc(detail)})" if detail else ""))
    if check.get("unreliable_address"):
        risks.append("недост. адрес")
    if check.get("unreliable_director"):
        risks.append("недост. руководитель")
    if check.get("unreliable_founder"):
        risks.append("недост. учредитель")
    if check.get("disqualified"):
        risks.append("дисквалификация руководителя")
    if check.get("liquidation_decision"):
        risks.append("решение о ликвидации")
    if check.get("exclusion_decision"):
        risks.append("решение об исключении")
    if check.get("reorganization_decision"):
        risks.append("решение о реорганизации")
    if check.get("tax_debt"):
        risks.append("задолженность по налогам")
    if check.get("no_reports"):
        risks.append("не сдаёт отчётность")
    if check.get("capital_decrease"):
        risks.append("уменьшение уставного капитала")

    positives = []
    if check.get("has_licenses"):
        positives.append("есть лицензии")
    if check.get("has_branches"):
        positives.append("есть филиалы")
    if check.get("capital_above_50k"):
        positives.append("уст. капитал > 50 тыс.")

    if risks or positives or (check and check.get("source")):
        has_fns_data = True
        lines.append("")
        lines.append("─── <b>Проверка ФНС</b> ───")
        if risks:
            for risk in risks:
                lines.append(f"⚠️ {risk}")
        if positives:
            for p in positives:
                lines.append(f"✅ {p}")
        if not risks and not positives and check.get("source"):
            if check.get("clean"):
                lines.append("✅ Негативных признаков не выявлено")
            else:
                lines.append("ℹ️ Данные получены")

    # Блокировка счетов
    if nalogbi.get("source"):
        if not has_fns_data:
            lines.append("")
            lines.append("─── <b>Проверка ФНС</b> ───")
            has_fns_data = True

        if nalogbi.get("has_blocked_accounts"):
            cnt = nalogbi.get("blocked_accounts_count", 0)
            lines.append(f"🔒 Блокировка счетов: <b>{cnt} решений</b>")
            details = nalogbi.get("blocking_details", [])
            for d in details[:3]:
                bank = d.get("bank") or "?"
                date = d.get("date") or ""
                lines.append(f"   • {_esc(bank)} ({date})")
        else:
            lines.append("🔓 Блокировка счетов: нет")


def _format_courts(zsk: dict, lines: list[str]) -> None:
    """Форматирует блок судов."""
    courts_total = zsk.get("courts_total")
    defendant = zsk.get("courts_defendant")
    plaintiff = zsk.get("courts_plaintiff")
    courts_sum = zsk.get("courts_sum")
    active_sum = zsk.get("courts_active_sum")

    if courts_total is not None:
        if courts_total == 0:
            lines.append("⚖️ Суды: не найдены ✅")
        else:
            s = f"⚖️ Суды: {_fmt_number(courts_total)} дел"
            details = []
            if defendant:
                details.append(f"ответчик {_fmt_number(defendant)}")
            if plaintiff:
                details.append(f"истец {_fmt_number(plaintiff)}")
            if details:
                s += f" ({', '.join(details)})"
            lines.append(s)

            if courts_sum:
                lines.append(f"   💰 общая сумма: {_money(courts_sum)}")
            if active_sum:
                lines.append(f"   🔄 на рассмотрении: {_money(active_sum)}")
    else:
        lines.append("⚖️ Суды: проверяется...")


def _format_sanctions_section(sanctions: dict, lines: list[str]) -> None:
    """Форматирует блок санкционной проверки."""
    if not sanctions:
        return

    lines.append("")
    lines.append("─── <b>Санкции</b> ───")

    if sanctions.get("found"):
        lines.append("⚠️ <b>Найден в санкционных списках!</b>")
        for m in sanctions.get("matches", []):
            name = _esc(m.get("name", "?"))
            score = m.get("score", 0)
            datasets = m.get("datasets", [])
            ds_text = ", ".join(str(d) for d in datasets[:3]) if datasets else "—"
            lines.append(f"  • {name} (совпадение {score}%)")
            lines.append(f"    Списки: {_esc(ds_text)}")
    else:
        lines.append("✅ Не найден в санкционных списках")

    source = sanctions.get("source", "")
    if source:
        lines.append(f"<i>Источник: {_esc(source)}</i>")


def _status_label(status: str | None) -> str:
    return {
        "ACTIVE": "✅ Действующая",
        "LIQUIDATING": "⚠️ Ликвидируется",
        "LIQUIDATED": "❌ Ликвидирована",
        "BANKRUPT": "❌ Банкрот",
        "REORGANIZING": "🔄 Реорганизация",
    }.get(status or "", f"❓ {status or 'н/д'}")


def _money(v: float | int | None) -> str:
    if v is None:
        return "н/д"
    v = float(v)
    if abs(v) >= 1_000_000_000_000:
        return f"{v / 1_000_000_000_000:.1f} трлн ₽"
    elif abs(v) >= 1_000_000_000:
        return f"{v / 1_000_000_000:.1f} млрд ₽"
    elif abs(v) >= 1_000_000:
        return f"{v / 1_000_000:.1f} млн ₽"
    elif abs(v) >= 1_000:
        return f"{v / 1_000:.0f} тыс ₽"
    else:
        return f"{v:,.0f} ₽".replace(",", " ")


def _money_short(v: float | int | None) -> str:
    """Короткий формат без знака ₽."""
    if v is None:
        return "н/д"
    v = float(v)
    if abs(v) >= 1_000_000_000_000:
        return f"{v / 1_000_000_000_000:.1f} трлн"
    elif abs(v) >= 1_000_000_000:
        return f"{v / 1_000_000_000:.1f} млрд"
    elif abs(v) >= 1_000_000:
        return f"{v / 1_000_000:.1f} млн"
    elif abs(v) >= 1_000:
        return f"{v / 1_000:.0f} тыс"
    else:
        return f"{v:.0f}"


def _fmt_number(n: int) -> str:
    """Форматирует число с пробелами: 23101 → 23 101."""
    return f"{n:,}".replace(",", " ")


def format_proposal(
    number: int,
    fields: dict[str, Any],
    purpose: str,
    price: str,
    term: str,
    client: str,
    zchb_data: dict[str, Any] | None = None,
    zsk_data: dict[str, Any] | None = None,
    rp_data: dict[str, Any] | None = None,
    links: dict[str, str] | None = None,
) -> str:
    """Формат отчёта для сценария 'Предложение'."""
    zchb = zchb_data or {}
    zsk = zsk_data or {}
    rp = rp_data or {}
    lines: list[str] = []

    lines.append(f"📝 <b>Предложение {number}</b>")
    lines.append("─" * 20)

    name = fields.get("name") or "Неизвестно"
    lines.append(f"🏢 <b>{_esc(name)}</b>")
    lines.append(f"ИНН: <code>{fields.get('inn', '—')}</code>")

    reg = fields.get("registration_date")
    if reg:
        age = fields.get("company_age_years")
        age_s = f" ({_format_age(age)})" if age is not None else ""
        lines.append(f"📅 {reg}{age_s}")

    city = fields.get("city")
    if city:
        lines.append(f"📍 {_esc(city)}")

    okved = fields.get("okved_code")
    okved_text = fields.get("okved_text")
    if okved:
        s = f"🏭 {okved}"
        if okved_text:
            short = okved_text[:50] + "..." if len(okved_text) > 50 else okved_text
            s += f" — {_esc(short)}"
        lines.append(s)

    rev = _first_num(zchb.get("revenue"), fields.get("income"))
    profit = _first_num(zchb.get("net_profit"), zsk.get("net_profit"))
    if rev is not None or profit is not None:
        parts = []
        if rev is not None:
            parts.append(f"выручка {_money(rev)}")
        if profit is not None:
            parts.append(f"прибыль {_money(profit)}")
        lines.append(f"📊 {', '.join(parts)}")

    lines.append(f"Статус: {_status_label(fields.get('status'))}")

    # Суды кратко (ЗЧБ API → scraping)
    courts_total = _first_num(zchb.get("courts_total"), zsk.get("courts_total"))
    if courts_total is not None and courts_total > 0:
        defendant = _first_num(zchb.get("courts_defendant"), zsk.get("courts_defendant"))
        s = f"⚖️ Суды: {_fmt_number(courts_total)}"
        if defendant:
            s += f" (ответчик {_fmt_number(defendant)})"
        lines.append(s)

    # Светофор (scraping — только для надёжности, ЗЧБ API не возвращает цвет)
    color = zsk.get("reliability_color") or rp.get("reliability_color")
    label = zsk.get("reliability_label") or rp.get("reliability_label")
    if color == "green":
        lines.append(f"🟢 {label or 'Низкий риск'}")
    elif color == "red":
        lines.append(f"🔴 {label or 'Высокий риск'}")
    elif color == "yellow":
        lines.append(f"🟡 {label or 'Средний риск'}")
    else:
        lines.append("⚪ Нет данных")

    lines.append("─" * 20)
    lines.append(f"📌 Назначение: {_esc(purpose)}")
    lines.append(f"💵 Цена: {_esc(price)}")
    lines.append(f"📦 Срок: {_esc(term)}")
    lines.append(f"👤 Кому: {_esc(client)}")

    return "\n".join(lines)


def format_invoice(
    number: int,
    from_whom: str,
    purpose: str,
    target_inn: str,
    amount: str,
    issuer: str,
    target_name: str | None = None,
) -> str:
    """Формат отчёта для сценария 'Запрос счета'."""
    lines: list[str] = []

    lines.append(f"🧾 <b>Запрос Счета {number}</b>")
    lines.append("─" * 20)
    lines.append(f"1 — {_esc(from_whom)}")
    lines.append(f"2 — назначение: {_esc(purpose)}")

    inn_line = f"3 — ИНН: <code>{_esc(target_inn)}</code>"
    if target_name:
        inn_line += f" ({_esc(target_name)})"
    lines.append(inn_line)

    lines.append(f"4 — сумма: {_esc(amount)}")
    lines.append(f"Выставить от: {_esc(issuer)}")

    return "\n".join(lines)


def format_comparison(
    data1: dict[str, Any],
    data2: dict[str, Any],
) -> str:
    """Формат сравнения двух компаний бок о бок."""
    f1 = data1["fields"]
    f2 = data2["fields"]
    zchb1 = data1.get("zchb_data") or {}
    zchb2 = data2.get("zchb_data") or {}
    z1 = data1.get("zsk_data") or {}
    z2 = data2.get("zsk_data") or {}
    r1 = data1.get("rp_data") or {}
    r2 = data2.get("rp_data") or {}
    fin1 = data1.get("fin_history") or {}
    fin2 = data2.get("fin_history") or {}
    fns1 = data1.get("fns_data") or {}
    fns2 = data2.get("fns_data") or {}

    lines: list[str] = []
    lines.append("⚖️ <b>Сравнение компаний</b>")
    lines.append("─" * 25)

    # Названия
    n1 = f1.get("name", "—")
    n2 = f2.get("name", "—")
    lines.append(f"🅰️ <b>{_esc(n1)}</b>")
    lines.append(f"🅱️ <b>{_esc(n2)}</b>")
    lines.append("")

    # Таблица сравнения
    rows = [
        ("ИНН", f1.get("inn", "—"), f2.get("inn", "—")),
        ("Статус", _status_short(f1.get("status")), _status_short(f2.get("status"))),
        ("Возраст", _age_str(f1.get("company_age_years")), _age_str(f2.get("company_age_years"))),
        ("Город", f1.get("city", "—"), f2.get("city", "—")),
        (
            "Выручка",
            _money(_first_num(
                (fns1.get("bo") or {}).get("revenue"),
                z1.get("revenue"), r1.get("revenue"), f1.get("income"),
            )),
            _money(_first_num(
                (fns2.get("bo") or {}).get("revenue"),
                z2.get("revenue"), r2.get("revenue"), f2.get("income"),
            )),
        ),
        (
            "Прибыль",
            _money(_first_num(
                (fns1.get("bo") or {}).get("net_profit"), z1.get("net_profit"),
            )),
            _money(_first_num(
                (fns2.get("bo") or {}).get("net_profit"), z2.get("net_profit"),
            )),
        ),
        (
            "Тренд",
            _trend_icon(fin1.get("trend")),
            _trend_icon(fin2.get("trend")),
        ),
        (
            "Штат",
            _emp_str(_first_num(zchb1.get("employee_count"), z1.get("employee_count"), f1.get("employee_count"))),
            _emp_str(_first_num(zchb2.get("employee_count"), z2.get("employee_count"), f2.get("employee_count"))),
        ),
        (
            "Суды",
            _courts_short(zchb1) if zchb1.get("courts_total") is not None else _courts_short(z1),
            _courts_short(zchb2) if zchb2.get("courts_total") is not None else _courts_short(z2),
        ),
        (
            "ФССП",
            _fssp_short(zchb1) if zchb1.get("fssp_count") is not None else _fssp_short(z1),
            _fssp_short(zchb2) if zchb2.get("fssp_count") is not None else _fssp_short(z2),
        ),
        (
            "Надёжность",
            _reliability_short(z1, r1),
            _reliability_short(z2, r2),
        ),
        (
            "Проверка ФНС",
            _fns_check_short(fns1),
            _fns_check_short(fns2),
        ),
        (
            "Блокировки",
            _fns_blocking_short(fns1),
            _fns_blocking_short(fns2),
        ),
    ]

    for label, v1, v2 in rows:
        lines.append(f"<b>{label}:</b>")
        lines.append(f"  🅰️ {v1}")
        lines.append(f"  🅱️ {v2}")

    # Вердикт
    lines.append("")
    lines.append("─" * 25)
    score1 = _quick_score(f1, z1, r1, fin1)
    score2 = _quick_score(f2, z2, r2, fin2)
    if score1 > score2:
        lines.append(f"✅ <b>Рекомендация: 🅰️ {_esc(n1)}</b> ({score1} vs {score2})")
    elif score2 > score1:
        lines.append(f"✅ <b>Рекомендация: 🅱️ {_esc(n2)}</b> ({score2} vs {score1})")
    else:
        lines.append(f"🤝 <b>Компании примерно равны</b> ({score1} = {score2})")

    return "\n".join(lines)


def _status_short(status: str | None) -> str:
    return {"ACTIVE": "✅", "LIQUIDATING": "⚠️", "LIQUIDATED": "❌",
            "BANKRUPT": "❌", "REORGANIZING": "🔄"}.get(status or "", "❓")


def _age_str(age) -> str:
    if age is None:
        return "—"
    return f"{age} лет"


def _trend_icon(trend: str | None) -> str:
    return {"up": "📈 рост", "down": "📉 падение", "stable": "➡️ стабильно"}.get(trend or "", "—")


def _emp_str(emp) -> str:
    if emp is None:
        return "—"
    return f"{emp} чел."


def _courts_short(zsk: dict) -> str:
    total = zsk.get("courts_total")
    if total is None:
        return "—"
    if total == 0:
        return "нет ✅"
    defendant = zsk.get("courts_defendant", 0)
    return f"{_fmt_number(total)} (отв. {_fmt_number(defendant)})"


def _fssp_short(zsk: dict) -> str:
    total = zsk.get("fssp_total")
    if total is None:
        return "—"
    if total == 0:
        return "нет ✅"
    return f"{total} производств"


def _reliability_short(zsk: dict, rp: dict) -> str:
    color = zsk.get("reliability_color") or rp.get("reliability_color")
    label = zsk.get("reliability_label") or rp.get("reliability_label")
    icons = {"green": "🟢", "yellow": "🟡", "red": "🔴"}
    return f"{icons.get(color, '⚪')} {label or 'н/д'}"


def _fns_check_short(fns: dict) -> str:
    check = fns.get("check") or {}
    if not check.get("source"):
        return "—"
    problems = []
    if check.get("mass_director"):
        problems.append("масс.рук")
    if check.get("mass_address"):
        problems.append("масс.адрес")
    if check.get("unreliable_address"):
        problems.append("недост.адрес")
    if check.get("unreliable_director"):
        problems.append("недост.рук")
    if check.get("disqualified"):
        problems.append("дисквал")
    if check.get("tax_debt"):
        problems.append("долг")
    if check.get("no_reports"):
        problems.append("нет отч.")
    if problems:
        return "⚠️ " + ", ".join(problems)
    return "✅ чисто"


def _fns_blocking_short(fns: dict) -> str:
    nalogbi = fns.get("nalogbi") or {}
    if not nalogbi.get("source"):
        return "—"
    if nalogbi.get("has_blocked_accounts"):
        cnt = nalogbi.get("blocked_accounts_count", 0)
        return f"🔒 {cnt}"
    return "🔓 нет"


def _quick_score(fields: dict, zsk: dict, rp: dict, fin: dict) -> int:
    """Быстрый балл для сравнения (0-100)."""
    score = 50

    if fields.get("status") == "ACTIVE":
        score += 10
    elif fields.get("status") in ("LIQUIDATED", "BANKRUPT"):
        score -= 30

    age = fields.get("company_age_years")
    if age is not None:
        if age >= 5:
            score += 10
        elif age < 1:
            score -= 10

    color = zsk.get("reliability_color") or rp.get("reliability_color")
    if color == "green":
        score += 15
    elif color == "red":
        score -= 20
    elif color == "yellow":
        score -= 5

    courts = zsk.get("courts_defendant", 0) or 0
    if courts == 0:
        score += 5
    elif courts > 50:
        score -= 10

    trend = fin.get("trend")
    if trend == "up":
        score += 5
    elif trend == "down":
        score -= 5

    fssp = zsk.get("fssp_total")
    if fssp is not None and fssp == 0:
        score += 5
    elif fssp and fssp > 5:
        score -= 10

    return max(0, min(100, score))


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ─────────────────────────────────────────────────
# Новые форматы: история, массовая, связи, госзакупки
# ─────────────────────────────────────────────────

def format_changes(
    changes: list[dict[str, Any]],
    company_name: str,
    limit: int = 15,
) -> str:
    """Форматирует историю изменений компании."""
    lines: list[str] = []
    lines.append(f"📜 <b>История изменений</b>")
    lines.append(f"Компания: <b>{_esc(company_name)}</b>")
    lines.append("")

    if not changes:
        lines.append("ℹ️ Записи не найдены")
        return "\n".join(lines)

    for i, ch in enumerate(changes[:limit]):
        date = ch.get("date") or "—"
        grn = ch.get("grn") or ""
        desc = ch.get("description") or ch.get("type") or "Изменение"
        # Ограничиваем длину описания
        if len(desc) > 120:
            desc = desc[:117] + "..."
        grn_s = f" (ГРН {grn})" if grn else ""
        lines.append(f"<b>{date}</b>{grn_s}")
        lines.append(f"  {_esc(desc)}")
        if i < len(changes[:limit]) - 1:
            lines.append("")

    if len(changes) > limit:
        lines.append("")
        lines.append(f"<i>... ещё {len(changes) - limit} записей</i>")

    return "\n".join(lines)


def format_bulk_report(results: list[dict[str, Any]]) -> str:
    """Форматирует сводку массовой проверки."""
    lines: list[str] = []
    lines.append(f"📋 <b>Массовая проверка ({len(results)} ИНН)</b>")
    lines.append("─" * 25)

    for r in results:
        inn = r.get("inn", "?")
        if r.get("ok"):
            f = r.get("fields", {})
            name = f.get("name", "—")
            status = _status_short(f.get("status"))
            entity = "ИП" if f.get("entity_type") == "ip" else "ЮЛ"
            lines.append(f"{status} <code>{inn}</code> [{entity}] <b>{_esc(name)}</b>")
        else:
            err = r.get("error", "ошибка")
            lines.append(f"❌ <code>{inn}</code> — {_esc(err[:60])}")

    lines.append("─" * 25)
    ok_count = sum(1 for r in results if r.get("ok"))
    lines.append(f"✅ Успешно: {ok_count}/{len(results)}")

    return "\n".join(lines)


def format_affiliated(companies: list[dict[str, Any]]) -> str:
    """Форматирует список связанных компаний."""
    lines: list[str] = []
    lines.append("🔗 <b>Связанные компании</b>")
    lines.append("")

    if not companies:
        lines.append("ℹ️ Связанные компании не найдены")
        return "\n".join(lines)

    for i, c in enumerate(companies[:20]):
        name = c.get("name") or "—"
        inn = c.get("inn") or ""
        role = c.get("role") or ""
        status = c.get("status") or ""
        status_icon = "✅" if status == "ACTIVE" else "❌" if status in ("LIQUIDATED", "BANKRUPT") else "⚠️"

        lines.append(f"{status_icon} <b>{_esc(name)}</b>")
        parts = []
        if inn:
            parts.append(f"ИНН: <code>{inn}</code>")
        if role:
            parts.append(f"Роль: {_esc(role)}")
        if parts:
            lines.append(f"  {' • '.join(parts)}")
        if i < len(companies[:20]) - 1:
            lines.append("")

    if len(companies) > 20:
        lines.append(f"\n<i>... ещё {len(companies) - 20} компаний</i>")

    lines.append("")
    lines.append(f"Всего связей: <b>{len(companies)}</b>")

    return "\n".join(lines)


def format_contracts(data: dict[str, Any]) -> str:
    """Форматирует данные о госзакупках."""
    lines: list[str] = []
    lines.append("📋 <b>Госзакупки</b>")
    lines.append("")

    total = data.get("total_count", 0)
    total_sum = data.get("total_sum", 0)

    lines.append(f"Всего контрактов: <b>{total}</b>")
    if total_sum:
        lines.append(f"Общая сумма: <b>{_money(total_sum)}</b>")

    contracts = data.get("contracts", [])
    if contracts:
        lines.append("")
        lines.append("─── <b>Последние контракты</b> ───")
        for c in contracts[:10]:
            date = c.get("date") or "—"
            amount = c.get("amount")
            subject = c.get("subject") or "—"
            if len(subject) > 80:
                subject = subject[:77] + "..."

            amount_s = f" — {_money(amount)}" if amount else ""
            lines.append(f"📅 {date}{amount_s}")
            lines.append(f"  {_esc(subject)}")
            lines.append("")

    return "\n".join(lines)
