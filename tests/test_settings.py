"""Тесты settings — конфигурация из env."""
import pytest

from settings import Settings


class TestPaymentsEnabled:
    def test_both_set_returns_true(self):
        s = Settings(api_id=1, api_hash="h", bot_token="t",
                     tochka_jwt="jwt", tochka_customer_code="cc")
        assert s.payments_enabled is True

    def test_jwt_missing_returns_false(self):
        s = Settings(api_id=1, api_hash="h", bot_token="t",
                     tochka_jwt="", tochka_customer_code="cc")
        assert s.payments_enabled is False

    def test_customer_code_missing_returns_false(self):
        s = Settings(api_id=1, api_hash="h", bot_token="t",
                     tochka_jwt="jwt", tochka_customer_code="")
        assert s.payments_enabled is False

    def test_both_missing_returns_false(self):
        s = Settings(api_id=1, api_hash="h", bot_token="t")
        assert s.payments_enabled is False


class TestFromEnvRequired:
    def _clear(self, monkeypatch):
        for var in ("TG_API_ID", "TG_API_HASH", "TG_BOT_TOKEN",
                    "TOCHKA_JWT", "TOCHKA_CUSTOMER_CODE", "TOCHKA_MERCHANT_ID",
                    "TOCHKA_BASE_URL", "TOCHKA_WEBHOOK_SECRET",
                    "PAYMENT_REDIRECT_URL", "PAYMENT_FAIL_REDIRECT_URL",
                    "WEBHOOK_HOST", "WEBHOOK_PORT"):
            monkeypatch.delenv(var, raising=False)

    def test_no_env_raises(self, monkeypatch):
        self._clear(monkeypatch)
        with pytest.raises(RuntimeError):
            Settings.from_env()

    def test_missing_api_id_raises(self, monkeypatch):
        self._clear(monkeypatch)
        monkeypatch.setenv("TG_API_HASH", "h")
        monkeypatch.setenv("TG_BOT_TOKEN", "t")
        with pytest.raises(RuntimeError):
            Settings.from_env()

    def test_missing_api_hash_raises(self, monkeypatch):
        self._clear(monkeypatch)
        monkeypatch.setenv("TG_API_ID", "1")
        monkeypatch.setenv("TG_BOT_TOKEN", "t")
        with pytest.raises(RuntimeError):
            Settings.from_env()

    def test_missing_bot_token_raises(self, monkeypatch):
        self._clear(monkeypatch)
        monkeypatch.setenv("TG_API_ID", "1")
        monkeypatch.setenv("TG_API_HASH", "h")
        with pytest.raises(RuntimeError):
            Settings.from_env()

    def test_zero_api_id_raises(self, monkeypatch):
        # api_id=0 проваливает условие and api_id, поэтому raise
        self._clear(monkeypatch)
        monkeypatch.setenv("TG_API_ID", "0")
        monkeypatch.setenv("TG_API_HASH", "h")
        monkeypatch.setenv("TG_BOT_TOKEN", "t")
        with pytest.raises(RuntimeError):
            Settings.from_env()

    def test_invalid_api_id_raises_value_error(self, monkeypatch):
        # Не число — int() бросает ValueError
        self._clear(monkeypatch)
        monkeypatch.setenv("TG_API_ID", "not-a-number")
        monkeypatch.setenv("TG_API_HASH", "h")
        monkeypatch.setenv("TG_BOT_TOKEN", "t")
        with pytest.raises(ValueError):
            Settings.from_env()


class TestFromEnvDefaults:
    def _telegram(self, monkeypatch):
        monkeypatch.setenv("TG_API_ID", "12345")
        monkeypatch.setenv("TG_API_HASH", "hash-x")
        monkeypatch.setenv("TG_BOT_TOKEN", "token-y")

    def test_all_telegram_set_no_optionals(self, monkeypatch):
        self._telegram(monkeypatch)
        for var in ("TOCHKA_JWT", "TOCHKA_CUSTOMER_CODE", "TOCHKA_MERCHANT_ID",
                    "TOCHKA_BASE_URL", "TOCHKA_WEBHOOK_SECRET",
                    "PAYMENT_REDIRECT_URL", "PAYMENT_FAIL_REDIRECT_URL",
                    "WEBHOOK_HOST", "WEBHOOK_PORT"):
            monkeypatch.delenv(var, raising=False)

        s = Settings.from_env()
        assert s.api_id == 12345
        assert s.api_hash == "hash-x"
        assert s.bot_token == "token-y"
        assert s.tochka_jwt == ""
        assert s.tochka_customer_code == ""
        assert s.tochka_merchant_id == ""
        assert s.tochka_base_url == "https://enter.tochka.com/uapi"
        assert s.tochka_webhook_secret == ""
        assert s.payment_redirect_url == "https://t.me/"
        assert s.payment_fail_redirect_url == "https://t.me/"
        assert s.webhook_host == "0.0.0.0"
        assert s.webhook_port == 8080
        assert s.payments_enabled is False

    def test_all_optionals_set(self, monkeypatch):
        self._telegram(monkeypatch)
        monkeypatch.setenv("TOCHKA_JWT", "jwt-1")
        monkeypatch.setenv("TOCHKA_CUSTOMER_CODE", "cc-1")
        monkeypatch.setenv("TOCHKA_MERCHANT_ID", "mid-1")
        monkeypatch.setenv("TOCHKA_BASE_URL", "https://sandbox.tochka.com/uapi")
        monkeypatch.setenv("TOCHKA_WEBHOOK_SECRET", "secret-1")
        monkeypatch.setenv("PAYMENT_REDIRECT_URL", "https://t.me/bot?ok=1")
        monkeypatch.setenv("PAYMENT_FAIL_REDIRECT_URL", "https://t.me/bot?fail=1")
        monkeypatch.setenv("WEBHOOK_HOST", "127.0.0.1")
        monkeypatch.setenv("WEBHOOK_PORT", "9090")

        s = Settings.from_env()
        assert s.tochka_jwt == "jwt-1"
        assert s.tochka_customer_code == "cc-1"
        assert s.tochka_merchant_id == "mid-1"
        assert s.tochka_base_url == "https://sandbox.tochka.com/uapi"
        assert s.tochka_webhook_secret == "secret-1"
        assert s.payment_redirect_url == "https://t.me/bot?ok=1"
        assert s.payment_fail_redirect_url == "https://t.me/bot?fail=1"
        assert s.webhook_host == "127.0.0.1"
        assert s.webhook_port == 9090
        assert s.payments_enabled is True

    def test_invalid_webhook_port_raises(self, monkeypatch):
        self._telegram(monkeypatch)
        monkeypatch.setenv("WEBHOOK_PORT", "not-a-number")
        with pytest.raises(ValueError):
            Settings.from_env()

    def test_payments_enabled_requires_both_jwt_and_cc(self, monkeypatch):
        self._telegram(monkeypatch)
        monkeypatch.setenv("TOCHKA_JWT", "jwt-only")
        monkeypatch.delenv("TOCHKA_CUSTOMER_CODE", raising=False)
        s = Settings.from_env()
        assert s.payments_enabled is False
