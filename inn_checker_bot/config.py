import os
from dotenv import load_dotenv

load_dotenv()

# ── Telegram ──
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

# ── DaData ──
DADATA_API_KEY = os.getenv("DADATA_API_KEY", "")
DADATA_SECRET_KEY = os.getenv("DADATA_SECRET_KEY", "")
DADATA_FIND_BY_ID_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/findById/party"
DADATA_TIMEOUT = 10.0

# ── GigaChat (Сбер) ──
GIGACHAT_CREDENTIALS = os.getenv("GIGACHAT_CREDENTIALS", "")
# Модели: GigaChat (Lite, дешёвая), GigaChat-Pro, GigaChat-Max
GIGACHAT_MODEL = os.getenv("GIGACHAT_MODEL", "GigaChat")

# ── API-FNS ──
APIFNS_KEY = os.getenv("APIFNS_KEY", "")

# ── Пороги скоринга ──
RISK_GREEN_MAX = 15    # 0..15 — зелёный
RISK_YELLOW_MAX = 35   # 16..35 — жёлтый
# 36+ — красный

# ── Скрытая админ-авторизация ──
ADMIN_COMMAND = os.getenv("ADMIN_COMMAND", "/f_access")
ADMIN_LOGIN = os.getenv("ADMIN_LOGIN", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "")
# Telegram user IDs админов (через запятую)
ADMIN_IDS_STR = os.getenv("ADMIN_TELEGRAM_IDS", "")
ADMIN_IDS: set[int] = set()
if ADMIN_IDS_STR:
    for uid in ADMIN_IDS_STR.split(","):
        uid = uid.strip()
        if uid.isdigit():
            ADMIN_IDS.add(int(uid))

# ── Поддержка ──
# Chat ID куда пересылать обращения (ваш личный Telegram ID)
SUPPORT_CHAT_ID = os.getenv("SUPPORT_CHAT_ID", "")

# ── Лимиты планов ──
# Free=3/день, Start=50/день, Pro=300/день, Business=безлимит
PLAN_LIMITS = {
    "free":     {"daily_checks": 3,    "full_report": False, "ai_analysis": False, "egrul": False},
    "promo":    {"daily_checks": 999,  "full_report": True,  "ai_analysis": True,  "egrul": True},
    "start":    {"daily_checks": 50,   "full_report": True,  "ai_analysis": False, "egrul": True},
    "pro":      {"daily_checks": 300,  "full_report": True,  "ai_analysis": True,  "egrul": True},
    "business": {"daily_checks": 9999, "full_report": True,  "ai_analysis": True,  "egrul": True},
    "admin":    {"daily_checks": 9999, "full_report": True,  "ai_analysis": True,  "egrul": True},
}

# ── Тарифы (на 15% ниже рынка: Контур ~1000-1500₽, Руспрофайл ~499₽) ──
TARIFF_INFO = {
    "start": {
        "name": "⭐ Start",
        "price": "490 ₽/мес",
        "checks": "50 проверок/день",
        "features": "Полный отчёт • ЕГРЮЛ • Суды/ФССП • Стоп-листы",
    },
    "pro": {
        "name": "💎 Pro",
        "price": "1 290 ₽/мес",
        "checks": "300 проверок/день",
        "features": "Всё из Start • ИИ-анализ • Связи • История • Мониторинг",
    },
    "business": {
        "name": "🏆 Business",
        "price": "2 490 ₽/мес",
        "checks": "Безлимитные проверки",
        "features": "Всё из Pro • API доступ • Массовые проверки • PDF/1С экспорт",
    },
}

# ── ЗаЧестныйБизнес API ──
ZCHB_API_KEY = os.getenv("ZCHB_API_KEY", "")

# ── OpenSanctions ──
OPENSANCTIONS_API_KEY = os.getenv("OPENSANCTIONS_API_KEY", "")

# ── Антифлуд ──
RATE_LIMIT_MESSAGES = int(os.getenv("RATE_LIMIT_MESSAGES", "30"))  # сообщений
RATE_LIMIT_PERIOD = int(os.getenv("RATE_LIMIT_PERIOD", "60"))       # за секунд
MAX_AUTH_ATTEMPTS = int(os.getenv("MAX_AUTH_ATTEMPTS", "5"))         # макс. попыток авторизации
AUTH_BAN_MINUTES = int(os.getenv("AUTH_BAN_MINUTES", "30"))          # бан на минут
