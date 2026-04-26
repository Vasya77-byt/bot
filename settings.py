import os
from dataclasses import dataclass, field


@dataclass
class Settings:
    # Telegram
    api_id: int
    api_hash: str
    bot_token: str
    # Tochka Bank acquiring
    tochka_jwt: str = ""
    tochka_customer_code: str = ""
    tochka_merchant_id: str = ""
    tochka_base_url: str = "https://enter.tochka.com/uapi"
    tochka_webhook_secret: str = ""
    # URLs
    payment_redirect_url: str = "https://t.me/"           # куда перекинуть после успеха
    payment_fail_redirect_url: str = "https://t.me/"      # после неудачи
    # Webhook server
    webhook_host: str = "0.0.0.0"
    webhook_port: int = 8080
    # AI analysis (GigaChat)
    gigachat_credentials: str = ""
    # Администраторы (доступ к /report)
    admin_ids: list[int] = field(default_factory=list)

    @property
    def payments_enabled(self) -> bool:
        return bool(self.tochka_jwt and self.tochka_customer_code)

    @staticmethod
    def from_env() -> "Settings":
        api_id = int(os.getenv("TG_API_ID", "0"))
        api_hash = os.getenv("TG_API_HASH", "")
        bot_token = os.getenv("TG_BOT_TOKEN", "")
        if not (api_id and api_hash and bot_token):
            raise RuntimeError("Environment variables TG_API_ID, TG_API_HASH, TG_BOT_TOKEN are required")

        admin_ids_raw = os.getenv("ADMIN_IDS", "")
        admin_ids: list[int] = []
        for piece in admin_ids_raw.split(","):
            piece = piece.strip()
            if piece.isdigit():
                admin_ids.append(int(piece))

        return Settings(
            api_id=api_id,
            api_hash=api_hash,
            bot_token=bot_token,
            tochka_jwt=os.getenv("TOCHKA_JWT", ""),
            tochka_customer_code=os.getenv("TOCHKA_CUSTOMER_CODE", ""),
            tochka_merchant_id=os.getenv("TOCHKA_MERCHANT_ID", ""),
            tochka_base_url=os.getenv("TOCHKA_BASE_URL", "https://enter.tochka.com/uapi"),
            tochka_webhook_secret=os.getenv("TOCHKA_WEBHOOK_SECRET", ""),
            payment_redirect_url=os.getenv("PAYMENT_REDIRECT_URL", "https://t.me/"),
            payment_fail_redirect_url=os.getenv("PAYMENT_FAIL_REDIRECT_URL", "https://t.me/"),
            webhook_host=os.getenv("WEBHOOK_HOST", "0.0.0.0"),
            webhook_port=int(os.getenv("WEBHOOK_PORT", "8080")),
            gigachat_credentials=os.getenv("GIGACHAT_CREDENTIALS", ""),
            admin_ids=admin_ids,
        )
