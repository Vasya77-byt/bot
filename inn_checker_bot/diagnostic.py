"""Full diagnostic test suite for the bot."""
import asyncio
import re


async def test_all():
    from dadata_client import fetch_company_data, extract_company_fields, DaDataError
    from open_sources import fetch_zsk_data, fetch_rusprofile_data, generate_links
    from itsoft_client import fetch_finance_history
    from report_formatter import format_report, format_proposal, format_invoice, _esc
    from validators import validate_inn
    from proposal_counter import get_stats
    from invoice_counter import get_invoice_stats
    import cache

    passed = 0
    failed = 0
    fails = []

    def check(name, condition):
        nonlocal passed, failed
        if condition:
            passed += 1
            print(f"  [OK] {name}")
        else:
            failed += 1
            fails.append(name)
            print(f"  [FAIL] {name}")

    # ====== TEST 1: Standard report - Yandex ======
    print("=" * 50)
    print("TEST 1: Standard report - Yandex")
    print("=" * 50)
    inn = "7736207543"
    raw = await fetch_company_data(inn)
    fields = extract_company_fields(raw)
    ogrn = fields.get("ogrn")
    zsk, rp, fin = await asyncio.gather(
        fetch_zsk_data(inn, ogrn),
        fetch_rusprofile_data(inn),
        fetch_finance_history(inn),
    )
    links = generate_links(inn, ogrn)
    report = format_report(fields, links=links, zsk_data=zsk, rp_data=rp, fin_history=fin)
    clean = re.sub(r"<[^>]+>", "", report)
    print(clean)
    print()
    check("Company name", "ЯНДЕКС" in report)
    check("INN present", "7736207543" in report)
    check("KPP present", "КПП:" in report)
    check("OGRN present", "ОГРН:" in report)
    check("Status active", "Действующая" in report)
    check("Registration", "Регистрация:" in report)
    check("OKVED", "ОКВЭД:" in report)
    check("Finance data", "Финансы:" in report)
    check("Dynamics 3yr", "Динамика:" in report)
    check("Courts defendant", "ответчик" in report)
    check("Courts plaintiff", "истец" in report)
    check("FSSP", "ФССП:" in report)
    check("Rating", "Оценка:" in report)
    check("Has separator", "\u2014" in report)  # em dash

    # ====== TEST 2: Small company ======
    print()
    print("=" * 50)
    print("TEST 2: Small company (7728168971)")
    print("=" * 50)
    inn2 = "7728168971"
    raw2 = await fetch_company_data(inn2)
    fields2 = extract_company_fields(raw2)
    ogrn2 = fields2.get("ogrn")
    zsk2, rp2, fin2 = await asyncio.gather(
        fetch_zsk_data(inn2, ogrn2),
        fetch_rusprofile_data(inn2),
        fetch_finance_history(inn2),
    )
    report2 = format_report(fields2, zsk_data=zsk2, rp_data=rp2, fin_history=fin2)
    clean2 = re.sub(r"<[^>]+>", "", report2)
    print(clean2)
    check("Small co has name", "Компания:" in report2)
    check("Small co has status", "Статус:" in report2)
    print(f"  INFO: ZSK emp={zsk2.get('employee_count')}, DaData emp={fields2.get('employee_count')}")
    print(f"  INFO: Courts={zsk2.get('courts_total')}, defendant={zsk2.get('courts_defendant')}")

    # ====== TEST 3: INN Validation ======
    print()
    print("=" * 50)
    print("TEST 3: INN Validation (10 + 12 digit)")
    print("=" * 50)
    check("10-digit RZD", validate_inn("7707083893")[0] == True)
    check("10-digit Yandex", validate_inn("7736207543")[0] == True)
    check("12-digit IP", validate_inn("500100732259")[0] == True)
    check("12-digit bad checksum", validate_inn("123456789012")[0] == False)
    check("5-digit rejected", validate_inn("12345")[0] == False)
    check("Letters rejected", validate_inn("abcdefghij")[0] == False)
    check("Empty rejected", validate_inn("")[0] == False)

    # ====== TEST 4: Proposal format ======
    print()
    print("=" * 50)
    print("TEST 4: Proposal format")
    print("=" * 50)
    proposal = format_proposal(
        number=42, fields=fields,
        purpose="Postavka", price="150000",
        term="14 days", client="Romashka",
        zsk_data=zsk, rp_data=rp,
    )
    clean_p = re.sub(r"<[^>]+>", "", proposal)
    print(clean_p)
    check("Proposal has number 42", "42" in proposal)
    check("Proposal has purpose", "Postavka" in proposal)
    check("Proposal has price", "150000" in proposal)
    check("Proposal has client", "Romashka" in proposal)
    check("Proposal has separator", "\u2500" in proposal)

    # ====== TEST 5: Invoice format ======
    print()
    print("=" * 50)
    print("TEST 5: Invoice format")
    print("=" * 50)
    invoice = format_invoice(
        number=7, from_whom="Supplier LLC",
        purpose="Payment", target_inn="7707083893",
        amount="500000", issuer="Our Company",
        target_name="OAO RZD",
    )
    clean_i = re.sub(r"<[^>]+>", "", invoice)
    print(clean_i)
    check("Invoice number 7", "7" in invoice)
    check("Invoice from whom", "Supplier LLC" in invoice)
    check("Invoice INN", "7707083893" in invoice)
    check("Invoice target name", "RZD" in invoice)
    check("Invoice amount", "500000" in invoice)
    check("Invoice issuer", "Our Company" in invoice)

    # ====== TEST 6: Stats ======
    print()
    print("=" * 50)
    print("TEST 6: Stats commands")
    print("=" * 50)
    s = get_stats()
    inv_s = get_invoice_stats()
    print(f"  Proposals: total={s['total']}, today={s['today']}, date={s['today_date']}")
    print(f"  Invoices:  total={inv_s['total']}, today={inv_s['today']}, date={inv_s['today_date']}")
    check("Stats works", "total" in s and "today" in s)
    check("Invoice stats works", "total" in inv_s and "today" in inv_s)

    # ====== TEST 7: Cache ======
    print()
    print("=" * 50)
    print("TEST 7: Cache system")
    print("=" * 50)
    cache.put("diag_test", {"value": 42}, ttl=60)
    check("Cache put+get", cache.get("diag_test") is not None)
    check("Cache value correct", cache.get("diag_test")["value"] == 42)
    check("Cache miss returns None", cache.get("nonexistent_key_xyz") is None)
    check("Cache size >= 1", cache.size() >= 1)

    # ====== TEST 8: Edge cases ======
    print()
    print("=" * 50)
    print("TEST 8: Edge cases and error handling")
    print("=" * 50)
    try:
        await fetch_company_data("0000000000")
        check("Not found raises DaDataError", False)
    except DaDataError:
        check("Not found raises DaDataError", True)
    except Exception:
        check("Not found raises DaDataError", False)

    zsk_no_ogrn = await fetch_zsk_data("7736207543", None)
    check("ZSK without OGRN graceful", "source" in zsk_no_ogrn)

    fin_bad = await fetch_finance_history("0000000000")
    check("Finance bad INN graceful", len(fin_bad.get("years", [])) == 0)

    # Report without any external data
    report_bare = format_report(fields, zsk_data=None, rp_data=None, fin_history=None)
    check("Report without ZSK still works", "ЯНДЕКС" in report_bare)
    check("Shows no data labels", "нет данных" in report_bare)

    # ====== TEST 9: HTML safety ======
    print()
    print("=" * 50)
    print("TEST 9: HTML/XSS safety")
    print("=" * 50)
    escaped = _esc('<script>alert("xss")</script>')
    check("XSS escaped", "<script>" not in escaped and "&lt;" in escaped)

    # ====== TEST 10: Financial dynamics detail ======
    print()
    print("=" * 50)
    print("TEST 10: Financial dynamics (egrul.itsoft.ru)")
    print("=" * 50)
    print(f"  Trend: {fin.get('trend')}")
    print(f"  Years available: {len(fin.get('years', []))}")
    for yd in fin.get("years", [])[:5]:
        print(f"    {yd['year']}: income={yd.get('income')}, outcome={yd.get('outcome')}")
    check("Has trend", fin.get("trend") is not None)
    check("Has years data", len(fin.get("years", [])) > 0)
    check("Trend is up/down/stable", fin.get("trend") in ("up", "down", "stable"))

    # ====== TEST 11: ZSK courts detail ======
    print()
    print("=" * 50)
    print("TEST 11: ZSK courts detail")
    print("=" * 50)
    print(f"  courts_total: {zsk.get('courts_total')}")
    print(f"  courts_defendant: {zsk.get('courts_defendant')}")
    print(f"  courts_plaintiff: {zsk.get('courts_plaintiff')}")
    print(f"  courts_sum: {zsk.get('courts_sum')}")
    print(f"  courts_active_sum: {zsk.get('courts_active_sum')}")
    check("Has courts_total", zsk.get("courts_total") is not None)
    check("Has courts_defendant", zsk.get("courts_defendant") is not None)
    check("Has courts_plaintiff", zsk.get("courts_plaintiff") is not None)

    # ====== TEST 12: Systemd service check ======
    print()
    print("=" * 50)
    print("TEST 12: Service health")
    print("=" * 50)
    import subprocess
    result = subprocess.run(
        ["systemctl", "is-active", "inn-checker-bot"],
        capture_output=True, text=True
    )
    status = result.stdout.strip()
    print(f"  Service status: {status}")
    check("Service is active", status == "active")

    # ====== SUMMARY ======
    print()
    print("=" * 50)
    total = passed + failed
    print(f"SUMMARY: {passed}/{total} passed, {failed} failed")
    print("=" * 50)
    if fails:
        print("  FAILED TESTS:")
        for f in fails:
            print(f"    - {f}")
    else:
        print("  ALL TESTS PASSED!")


if __name__ == "__main__":
    asyncio.run(test_all())
