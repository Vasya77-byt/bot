"""Тесты monitoring — snapshot/diff/format для уведомлений."""

from monitoring import (
    EventCategory,
    FieldChange,
    TRACKED_FIELDS,
    categorize_change,
    diff_snapshots,
    format_change_message,
    make_snapshot,
)
from schemas import CompanyData
from security_check import SecurityResult


class TestMakeSnapshot:
    def test_with_company_and_security(self):
        c = CompanyData(
            inn="1", name="ООО Х", status="Действующая",
            director="Иван И.", address="Москва", ogrn="OGRN-1",
            kpp="KPP-1", okved_main="62.01", capital=100_000.0,
            employees_count=20,
        )
        s = SecurityResult(
            enforcement_count=3, enforcement_total_sum=500_000.0,
            risk_level="medium",
        )
        snap = make_snapshot(c, s)
        assert snap["name"] == "ООО Х"
        assert snap["status"] == "Действующая"
        assert snap["director"] == "Иван И."
        assert snap["address"] == "Москва"
        assert snap["ogrn"] == "OGRN-1"
        assert snap["fssp_count"] == 3
        assert snap["fssp_total_sum"] == 500_000.0
        assert snap["risk_level"] == "medium"

    def test_company_none_fills_with_nones(self):
        snap = make_snapshot(None, None)
        # Все TRACKED-поля присутствуют как None — это контракт diff'а
        for key, _ in TRACKED_FIELDS:
            assert key in snap
            assert snap[key] is None

    def test_security_none_keeps_company_data(self):
        c = CompanyData(inn="1", name="X", status="Действующая")
        snap = make_snapshot(c, None)
        assert snap["name"] == "X"
        assert snap["fssp_count"] is None
        assert snap["risk_level"] is None


class TestDiffSnapshots:
    def test_empty_old_returns_no_changes(self):
        # Первичный замер не считается изменением
        new = {"name": "X"}
        assert diff_snapshots({}, new) == []
        assert diff_snapshots(None, new) == []

    def test_no_change_returns_empty(self):
        snap = {"name": "X", "status": "Действующая"}
        assert diff_snapshots(snap, snap) == []

    def test_status_change_detected(self):
        old = make_snapshot(CompanyData(name="X", status="Действующая"))
        new = make_snapshot(CompanyData(name="X", status="Ликвидируется"))
        changes = diff_snapshots(old, new)
        assert len(changes) == 1
        assert changes[0].field == "status"
        assert changes[0].old == "Действующая"
        assert changes[0].new == "Ликвидируется"

    def test_director_change_detected(self):
        old = make_snapshot(CompanyData(name="X", director="Иван И."))
        new = make_snapshot(CompanyData(name="X", director="Пётр П."))
        changes = diff_snapshots(old, new)
        assert any(c.field == "director" for c in changes)

    def test_fssp_count_change_detected(self):
        old = make_snapshot(None, SecurityResult(enforcement_count=2))
        new = make_snapshot(None, SecurityResult(enforcement_count=5))
        changes = diff_snapshots(old, new)
        assert any(c.field == "fssp_count" and c.old == 2 and c.new == 5
                   for c in changes)

    def test_none_to_none_ignored(self):
        old = {"director": None}
        new = {"director": None}
        assert diff_snapshots(old, new) == []

    def test_none_to_value_is_change(self):
        # «Появилось» — это изменение
        old = {"name": None, "status": None, "director": None, "address": None,
               "ogrn": None, "kpp": None, "okved_main": None, "capital": None,
               "employees_count": None, "fssp_count": None,
               "fssp_total_sum": None, "risk_level": None}
        new = dict(old, director="Иван И.")
        changes = diff_snapshots(old, new)
        assert len(changes) == 1
        assert changes[0].field == "director"
        assert changes[0].old is None
        assert changes[0].new == "Иван И."

    def test_value_to_none_is_change(self):
        # «Пропали данные» — тоже изменение, надо предупредить пользователя
        old = make_snapshot(CompanyData(name="X", director="Иван И."))
        new = make_snapshot(CompanyData(name="X", director=None))
        changes = diff_snapshots(old, new)
        assert any(c.field == "director" and c.new is None for c in changes)

    def test_missing_key_in_new_skipped(self):
        # Если поля нет в новом снимке (например, FSSP не запросили) —
        # это не считается «удалили»
        old = {"name": "X", "fssp_count": 2}
        new = {"name": "X"}  # fssp_count отсутствует
        assert diff_snapshots(old, new) == []

    def test_multiple_changes(self):
        old = make_snapshot(
            CompanyData(name="A", status="Действующая", director="Иван"),
            SecurityResult(enforcement_count=1, risk_level="low"),
        )
        new = make_snapshot(
            CompanyData(name="A", status="Ликвидируется", director="Пётр"),
            SecurityResult(enforcement_count=12, risk_level="high"),
        )
        changes = diff_snapshots(old, new)
        fields = {c.field for c in changes}
        assert "status" in fields
        assert "director" in fields
        assert "fssp_count" in fields
        assert "risk_level" in fields

    def test_changes_in_tracked_field_order(self):
        old = make_snapshot(CompanyData(name="A", status="Действующая",
                                        director="X", address="Москва"))
        new = make_snapshot(CompanyData(name="A", status="Ликвидируется",
                                        director="Y", address="СПб"))
        changes = diff_snapshots(old, new)
        ordered_fields = [c.field for c in changes]
        # Порядок в diff соответствует TRACKED_FIELDS
        tracked_keys = [k for k, _ in TRACKED_FIELDS]
        assert ordered_fields == [k for k in tracked_keys if k in ordered_fields]


class TestFormatChangeMessage:
    def test_empty_changes_returns_empty_string(self):
        assert format_change_message("123", "X", []) == ""

    def test_single_change(self):
        ch = FieldChange(field="status", label="Статус",
                         old="Действующая", new="Ликвидируется")
        text = format_change_message("123", "ООО Тест", [ch])
        assert "ООО Тест" in text
        assert "ИНН: 123" in text
        assert "Статус: Действующая → Ликвидируется" in text

    def test_uses_inn_when_no_name(self):
        ch = FieldChange(field="status", label="Статус", old="A", new="B")
        text = format_change_message("123", "", [ch])
        # Без имени — заголовок с ИНН
        assert "123" in text

    def test_none_value_rendered_as_no_data(self):
        ch = FieldChange(field="director", label="Руководитель",
                         old=None, new="Иван И.")
        text = format_change_message("123", "X", [ch])
        assert "не было данных → Иван И." in text

    def test_float_formatting(self):
        ch = FieldChange(field="fssp_total_sum", label="Сумма по ФССП",
                         old=100000.0, new=2_500_000.5)
        text = format_change_message("1", "X", [ch])
        # %g — без хвостовых нулей
        assert "100000" in text
        assert "2.5e+06" in text or "2500000.5" in text

    def test_multiple_changes_all_listed(self):
        changes = [
            FieldChange("status", "Статус", "A", "B"),
            FieldChange("director", "Руководитель", "Иван", "Пётр"),
            FieldChange("fssp_count", "Производств ФССП", 1, 5),
        ]
        text = format_change_message("123", "X", changes)
        assert "Статус: A → B" in text
        assert "Руководитель: Иван → Пётр" in text
        assert "Производств ФССП: 1 → 5" in text


class TestCategorizeChange:
    """D3: классификация одного изменения в EventCategory."""

    def test_status_to_bankrupt(self):
        ch = FieldChange("status", "Статус", "Действующая", "Банкрот")
        assert categorize_change(ch) == EventCategory.BANKRUPTCY

    def test_status_to_liquidation(self):
        ch = FieldChange("status", "Статус", "Действующая", "Ликвидируется")
        assert categorize_change(ch) == EventCategory.LIQUIDATION

    def test_status_reorg(self):
        ch = FieldChange("status", "Статус", "Действующая", "Реорганизация")
        assert categorize_change(ch) == EventCategory.REORG

    def test_director_change(self):
        ch = FieldChange("director", "Руководитель", "Иван", "Пётр")
        assert categorize_change(ch) == EventCategory.DIRECTOR_CHANGE

    def test_fssp_count_increase(self):
        ch = FieldChange("fssp_count", "Производств ФССП", 2, 5)
        assert categorize_change(ch) == EventCategory.NEW_LAWSUITS

    def test_fssp_count_decrease(self):
        ch = FieldChange("fssp_count", "Производств ФССП", 5, 2)
        assert categorize_change(ch) == EventCategory.LAWSUITS_DECREASE

    def test_risk_increase_low_to_high(self):
        ch = FieldChange("risk_level", "Уровень риска", "low", "high")
        assert categorize_change(ch) == EventCategory.RISK_INCREASE

    def test_risk_increase_med_to_critical(self):
        ch = FieldChange("risk_level", "Уровень риска", "medium", "critical")
        assert categorize_change(ch) == EventCategory.RISK_INCREASE

    def test_risk_decrease(self):
        ch = FieldChange("risk_level", "Уровень риска", "high", "medium")
        assert categorize_change(ch) == EventCategory.RISK_DECREASE

    def test_capital_decrease(self):
        ch = FieldChange("capital", "Уст. капитал", 1_000_000.0, 500_000.0)
        assert categorize_change(ch) == EventCategory.CAPITAL_DECREASE

    def test_capital_increase(self):
        ch = FieldChange("capital", "Уст. капитал", 100_000.0, 500_000.0)
        assert categorize_change(ch) == EventCategory.CAPITAL_INCREASE

    def test_address_change(self):
        ch = FieldChange("address", "Адрес", "Москва", "СПб")
        assert categorize_change(ch) == EventCategory.ADDRESS_CHANGE

    def test_name_change(self):
        ch = FieldChange("name", "Название", "ООО А", "ООО Б")
        assert categorize_change(ch) == EventCategory.NAME_CHANGE

    def test_okved_change(self):
        ch = FieldChange("okved_main", "ОКВЭД", "62.01", "47.11")
        assert categorize_change(ch) == EventCategory.OKVED_CHANGE

    def test_unknown_field_is_other(self):
        ch = FieldChange("unknown_field", "Что-то", "a", "b")
        assert categorize_change(ch) == EventCategory.OTHER


class TestFormatChangeMessageCategorized:
    """D3: проверяем что новый формат группирует и помечает события."""

    def test_bankruptcy_message_has_critical_marker(self):
        ch = FieldChange("status", "Статус", "Действующая", "Банкрот")
        text = format_change_message("123", "ООО Тест", [ch])
        assert "🚨" in text  # critical эмодзи
        assert "Банкротство" in text  # категория-секция
        assert "Серьёзное изменение" in text
        # Рекомендация для critical событий
        assert "приостанов" in text.lower() or "проверить" in text.lower()

    def test_director_change_warning_marker(self):
        ch = FieldChange("director", "Руководитель", "Иван", "Пётр")
        text = format_change_message("123", "X", [ch])
        assert "⚠️" in text or "Внимание" in text
        assert "Смена руководителя" in text

    def test_fssp_increase_emoji(self):
        ch = FieldChange("fssp_count", "Производств ФССП", 1, 10)
        text = format_change_message("123", "X", [ch])
        assert "⚖️" in text
        assert "Новые производства ФССП" in text

    def test_positive_event_uses_positive_tone(self):
        ch = FieldChange("fssp_count", "Производств ФССП", 5, 1)
        text = format_change_message("123", "X", [ch])
        assert "✅" in text
        # Положительные события — отдельная подача
        assert "меньше" in text.lower() or "позитивн" in text.lower() or "снизи" in text.lower()

    def test_critical_overrides_info(self):
        """Если есть critical + info — общий заголовок critical."""
        changes = [
            FieldChange("status", "Статус", "Действующая", "Банкрот"),
            FieldChange("okved_main", "ОКВЭД", "62.01", "47.11"),
        ]
        text = format_change_message("123", "X", changes)
        assert "🚨" in text  # critical верх
        assert "Банкротство" in text
        assert "Смена основного ОКВЭД" in text

    def test_sections_separated(self):
        changes = [
            FieldChange("status", "Статус", "Действующая", "Банкрот"),
            FieldChange("director", "Руководитель", "Иван", "Пётр"),
        ]
        text = format_change_message("123", "X", changes)
        # Каждая категория — на отдельной строке с эмодзи
        assert "🚨 Банкротство" in text
        assert "👤 Смена руководителя" in text

    def test_info_only_simple_header(self):
        ch = FieldChange("name", "Название", "ООО А", "ООО Б")
        text = format_change_message("123", "X", [ch])
        assert "🔔" in text or "Изменения" in text

