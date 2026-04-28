from compliance import SLANG_TERMS, assess_risk, legal_note


class TestAssessRisk:
    def test_returns_empty_set_for_clean_text(self):
        assert assess_risk("обычный запрос на проверку контрагента") == set()

    def test_detects_single_term(self):
        assert assess_risk("нужен обнал срочно") == {"обнал"}

    def test_detects_multiple_terms(self):
        result = assess_risk("обнал через ИП с прокладка")
        assert "обнал" in result
        assert "через ип" in result
        assert "прокладка" in result

    def test_inflection_with_changed_suffix_not_detected(self):
        # Текущая реализация — substring match, не лемматизация.
        # "прокладкой" не содержит подстроки "прокладка" (последняя буква отличается).
        # Если зафиксим морфологию — этот тест должен упасть.
        assert assess_risk("работа с прокладкой") == set()

    def test_inflection_matches_only_when_lemma_is_prefix(self):
        # "обналичку" содержит "обнал" как подстроку, поэтому матчится.
        # Это побочный эффект substring-подхода, а не настоящая лемматизация.
        assert assess_risk("делал обналичку") == {"обнал"}

    def test_case_insensitive(self):
        assert assess_risk("ОБНАЛ") == {"обнал"}

    def test_empty_string_returns_empty(self):
        assert assess_risk("") == set()

    def test_all_slang_terms_detected_when_present(self):
        full_text = " ".join(SLANG_TERMS)
        assert assess_risk(full_text) == set(SLANG_TERMS)


class TestLegalNote:
    def test_empty_set_returns_empty_string(self):
        assert legal_note(set()) == ""

    def test_single_term_in_message(self):
        note = legal_note({"обнал"})
        assert "обнал" in note
        assert "легальном" in note

    def test_multiple_terms_sorted(self):
        note = legal_note({"прокладка", "обнал", "техничка"})
        # joined через ", " после sorted()
        assert "обнал, прокладка, техничка" in note

    def test_mentions_kyc_and_legal_context(self):
        note = legal_note({"обнал"})
        assert "KYC" in note
        assert "договоры" in note
