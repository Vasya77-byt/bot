"""Выдать пользователю тариф Business на 100 лет (для админа/тестов).

Запуск: python3 scripts/grant_business.py <telegram_user_id>
"""
import sys
from datetime import datetime, timedelta, timezone

# Запускать из корня проекта /opt/bot
sys.path.insert(0, ".")

from user_store import UserStore


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 scripts/grant_business.py <telegram_user_id>")
        sys.exit(1)

    user_id = int(sys.argv[1])
    us = UserStore()
    p = us.get(user_id)
    p.tariff = "business"
    p.tariff_expires_at = (
        datetime.now(timezone.utc) + timedelta(days=36500)
    ).isoformat()
    p.auto_renew = False
    us.save_profile(p)

    print(f"user_id  = {p.user_id}")
    print(f"tariff   = {p.tariff}")
    print(f"expires  = {p.tariff_expires_at}")
    print(f"active   = {p.is_subscription_active()}")
    print(f"effective= {p.effective_tariff()}")


if __name__ == "__main__":
    main()
