from compliance import SLANG_TERMS, assess_risk, legal_note


class TestAssessRiskBasic:
    def test_returns_empty_set_for_clean_text(self):
        assert assess_risk("обычный запрос на проверку контрагента") == set()

    def test_detects_single_term(self):
        assert assess_risk("нужен обнал срочно") == {"обнал"}

    def test_case_insensitive(self):
        assert assess_risk("ОБНАЛ") == {"обнал"}

    def test_empty_string_returns_empty(self):
        assert assess_risk("") == set()

    def test_all_slang_terms_detected_when_present(self):
        full_text = " ".join(SLANG_TERMS)
        assert assess_risk(full_text) == set(SLANG_TERMS)


class TestAssessRiskInflection:
    """Главное улучшение: ловим словоформы, а не только точную подстроку."""

    def test_prokladka_oblique_singular(self):
        # Косвенные падежи единственного числа
        assert assess_risk("работа с прокладкой") == {"прокладка"}
        assert assess_risk("на прокладке остановились") == {"прокладка"}
        assert assess_risk("без прокладки никуда") == {"прокладка"}

    def test_prokladka_plural(self):
        assert assess_risk("две прокладки") == {"прокладка"}
        assert assess_risk("через цепочку прокладок") == {"прокладка"}
        assert assess_risk("в прокладках") == {"прокладка"}

    def test_obnal_variants(self):
        assert assess_risk("делал обналичку") == {"обнал"}
        assert assess_risk("нужен обнал") == {"обнал"}
        assert assess_risk("обналом занимались") == {"обнал"}
        assert assess_risk("обналичили деньги") == {"обнал"}

    def test_prokrutit_variants(self):
        assert assess_risk("надо прокрутить через счёт") == {"прокрутить"}
        assert assess_risk("прокрутил миллион") == {"прокрутить"}
        assert assess_risk("прокрутят за день") == {"прокрутить"}

    def test_obelit_variants(self):
        assert assess_risk("хочу обелить бизнес") == {"обелить"}
        assert assess_risk("они обелили схему") == {"обелить"}
        assert assess_risk("обелим обороты") == {"обелить"}

    def test_technichka_variants(self):
        assert assess_risk("у нас есть техничка") == {"техничка"}
        assert assess_risk("на технички записать") == {"техничка"}
        assert assess_risk("работа с техничкой") == {"техничка"}


class TestAssessRiskFalsePositives:
    """Стемы выбраны так, чтобы не ловить нейтральные слова."""

    def test_technichka_does_not_match_technicheskij(self):
        # Стем «техничк» не матчит «технический» (после «технич» идёт «е», не «к»)
        assert assess_risk("технический отдел компании") == set()
        assert assess_risk("по технической причине") == set()

    def test_unrelated_words_not_matched(self):
        assert assess_risk("прокатилась волна жалоб") == set()
        assert assess_risk("обещание было выполнено") == set()
        assert assess_risk("обязательно проверьте") == set()


class TestAssessRiskMultiword:
    def test_through_ip_detected(self):
        assert assess_risk("работаю через ИП без НДС") == {"через ип"}

    def test_through_ip_substring_does_not_inflect(self):
        # «через ИП» — устойчивая фраза
        assert "через ип" in assess_risk("через ИП проводить")

    def test_agent_phrase_detected(self):
        assert assess_risk("деньги по агентской схеме") == {"по агентской"}

    def test_nal_beznal_detected(self):
        assert assess_risk("нал ↔ безнал в ТРЦ") == {"нал ↔ безнал"}


class TestAssessRiskCombined:
    def test_multiple_terms_in_single_text(self):
        result = assess_risk("обнал через ИП с прокладкой и техничкой")
        assert result == {"обнал", "через ип", "прокладка", "техничка"}

    def test_uppercase_inflected_form(self):
        # Регистронезависимо + словоформа
        assert assess_risk("ПРОКЛАДКОЙ воспользовались") == {"прокладка"}


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
