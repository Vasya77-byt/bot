import os
from dataclasses import dataclass


@dataclass
class Settings:
    api_id: int
    api_hash: str
    bot_token: str

    @staticmethod
    def from_env() -> "Settings":
        api_id = int(os.getenv("TG_API_ID", "0"))
        api_hash = os.getenv("TG_API_HASH", "")
        bot_token = os.getenv("TG_BOT_TOKEN", "")
        if not (api_id and api_hash and bot_token):
            raise RuntimeError("Environment variables TG_API_ID, TG_API_HASH, TG_BOT_TOKEN are required")
        return Settings(api_id=api_id, api_hash=api_hash, bot_token=bot_token)

