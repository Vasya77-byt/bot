"""Тесты политики конфиденциальности — фиксируем обязательные юр-разделы."""
from privacy import PRIVACY_TEXT


class TestPrivacyText:
    def test_has_title(self):
        assert "Политика конфиденциальности" in PRIVACY_TEXT

    def test_has_required_sections(self):
        # Секции, без которых документ не считается валидной политикой
        assert "Общие положения" in PRIVACY_TEXT
        assert "Какие данные" in PRIVACY_TEXT
        assert "Как мы используем" in PRIVACY_TEXT
        assert "Хранение данных" in PRIVACY_TEXT
        assert "Права пользователя" in PRIVACY_TEXT
        assert "Контакты" in PRIVACY_TEXT

    def test_mentions_152_fz_subjects(self):
        # 152-ФЗ требует чтобы был указан перечень обрабатываемых данных
        assert "user_id" in PRIVACY_TEXT
        assert "ИНН компаний" in PRIVACY_TEXT
        assert "тариф" in PRIVACY_TEXT.lower()

    def test_mentions_third_parties(self):
        # Передача данных третьим лицам должна быть явно описана
        assert "Точка" in PRIVACY_TEXT

    def test_mentions_user_rights(self):
        # Право запросить информацию + право удаления — обязательно
        assert "Запросить информацию" in PRIVACY_TEXT
        assert "удалени" in PRIVACY_TEXT.lower()
