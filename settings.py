import os
from dataclasses import dataclass


@dataclass
class Settings:
    # Telegram
    api_id: int
    api_hash: str
    bot_token: str
    # Активный платёжный провайдер: "tochka" или "yookassa".
    # Сам бот импортирует обоих клиентов, но обрабатывает платежи через
    # выбранный. Webhook-роуты обоих провайдеров всегда активны
    # (для миграции и обработки старых платежей).
    payment_provider: str = "tochka"
    # Tochka Bank acquiring (через JWT-API банка)
    tochka_jwt: str = ""
    tochka_customer_code: str = ""
    tochka_client_id: str = ""           # для регистрации webhook'ов
    tochka_merchant_id: str = ""         # обязателен если ≥2 торговых точек
    tochka_base_url: str = "https://enter.tochka.com/uapi"
    tochka_tax_system_code: str = ""     # система налогообложения для чека
    # ЮKassa acquiring (магазин в личном кабинете)
    yookassa_shop_id: str = ""
    yookassa_secret_key: str = ""
    yookassa_base_url: str = "https://api.yookassa.ru/v3"
    # Опциональный HMAC-секрет для проверки заголовка Y-Signature
    # (если HMAC включён в личном кабинете ЮKassa).
    yookassa_webhook_secret: str = ""
    # Чек 54-ФЗ: коды для ИП на УСН 6% без НДС (значения по умолчанию).
    yookassa_tax_system_code: int = 2    # 2=УСН доход, 3=УСН доход-расход, 6=ПСН
    # Ставка НДС в чеке: 1=без НДС, 2=0%, 3=10%, 4=20%, 5=10/110, 6=20/120
    yookassa_vat_code: int = 1
    # Сохранять карту для автоплатежей? У некоторых магазинов услуга
    # "save_payment_method" не подключена по умолчанию — ЮKassa тогда
    # отвечает 403 "This store can't make recurring payments".
    # В этом случае установите false и обратитесь в поддержку ЮKassa
    # с запросом активировать рекуррентные платежи.
    yookassa_save_payment_method: bool = True
    # URLs
    payment_redirect_url: str = "https://t.me/"           # успех
    payment_fail_redirect_url: str = "https://t.me/"      # неудача
    # Webhook server
    webhook_host: str = "0.0.0.0"
    webhook_port: int = 8080
    webhook_public_url: str = ""        # https://домен/tochka/webhook —
                                        # для авто-регистрации в Точке
    # Обязательная подписка на канал. Пусто = проверка отключена.
    # Бот должен быть админом канала, иначе get_chat_member вернёт ошибку.
    required_channel: str = ""          # @username или -100... id
    # Telegram user_id админов через запятую — для команды /admin
    admin_user_ids: str = ""
    # База для ссылок Telegram WebApp веб-отчёта.
    # Публичный HTTPS-URL aiohttp-сервера (вебхук-сервера), к которому
    # будет прибавляться /report/{token}. Пусто = кнопка веб-отчёта
    # не показывается.
    report_base_url: str = ""

    @property
    def tochka_enabled(self) -> bool:
        return bool(self.tochka_jwt and self.tochka_customer_code)

    @property
    def yookassa_enabled(self) -> bool:
        return bool(self.yookassa_shop_id and self.yookassa_secret_key)

    @property
    def payments_enabled(self) -> bool:
        if self.payment_provider == "yookassa":
            return self.yookassa_enabled
        return self.tochka_enabled

    @property
    def web_report_enabled(self) -> bool:
        return bool(self.report_base_url)

    @staticmethod
    def from_env() -> "Settings":
        api_id = int(os.getenv("TG_API_ID", "0"))
        api_hash = os.getenv("TG_API_HASH", "")
        bot_token = os.getenv("TG_BOT_TOKEN", "")
        if not (api_id and api_hash and bot_token):
            raise RuntimeError("Environment variables TG_API_ID, TG_API_HASH, TG_BOT_TOKEN are required")
        return Settings(
            api_id=api_id,
            api_hash=api_hash,
            bot_token=bot_token,
            payment_provider=os.getenv("PAYMENT_PROVIDER", "tochka").lower(),
            tochka_jwt=os.getenv("TOCHKA_JWT", ""),
            tochka_customer_code=os.getenv("TOCHKA_CUSTOMER_CODE", ""),
            tochka_client_id=os.getenv("TOCHKA_CLIENT_ID", ""),
            tochka_merchant_id=os.getenv("TOCHKA_MERCHANT_ID", ""),
            tochka_base_url=os.getenv(
                "TOCHKA_BASE_URL", "https://enter.tochka.com/uapi"
            ),
            tochka_tax_system_code=os.getenv("TOCHKA_TAX_SYSTEM_CODE", ""),
            yookassa_shop_id=os.getenv("YOOKASSA_SHOP_ID", ""),
            yookassa_secret_key=os.getenv("YOOKASSA_SECRET_KEY", ""),
            yookassa_base_url=os.getenv(
                "YOOKASSA_BASE_URL", "https://api.yookassa.ru/v3"
            ),
            yookassa_webhook_secret=os.getenv("YOOKASSA_WEBHOOK_SECRET", ""),
            yookassa_tax_system_code=int(
                os.getenv("YOOKASSA_TAX_SYSTEM_CODE", "2")
            ),
            yookassa_vat_code=int(os.getenv("YOOKASSA_VAT_CODE", "1")),
            yookassa_save_payment_method=os.getenv(
                "YOOKASSA_SAVE_PAYMENT_METHOD", "true",
            ).lower() in ("true", "1", "yes"),
            payment_redirect_url=os.getenv(
                "PAYMENT_REDIRECT_URL", "https://t.me/"
            ),
            payment_fail_redirect_url=os.getenv(
                "PAYMENT_FAIL_REDIRECT_URL", "https://t.me/"
            ),
            webhook_host=os.getenv("WEBHOOK_HOST", "0.0.0.0"),
            webhook_port=int(os.getenv("WEBHOOK_PORT", "8080")),
            webhook_public_url=os.getenv("WEBHOOK_PUBLIC_URL", ""),
            required_channel=os.getenv("REQUIRED_CHANNEL", ""),
            admin_user_ids=os.getenv("ADMIN_USER_IDS", ""),
            report_base_url=os.getenv("REPORT_BASE_URL", "").rstrip("/"),
        )
