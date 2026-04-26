"""Реферальная программа.

Каждому пользователю при первом обращении доступна реф.ссылка вида
`t.me/<bot>?start=ref_<user_id>`. Когда новый пользователь приходит по такой
ссылке, мы фиксируем связь «приглашённый → реферер» и при первой оплате
приглашённого:
  - даём ему скидку 15% на первый платёж
  - начисляем рефереру 15 бонусных дней подписки.
"""

import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger("financial-architect")

STORAGE_FILE = os.getenv("REFERRALS_FILE", "referrals.json")

REFERRAL_DISCOUNT_PCT = 15  # скидка для нового пользователя на первый платёж
REFERRAL_BONUS_DAYS = 15    # дней начисляется рефереру при оплате приглашённого


@dataclass
class ReferralRecord:
    referred_user_id: int
    referrer_user_id: int
    joined_at: str
    converted_at: str = ""
    bonus_awarded: bool = False


def make_referral_code(user_id: int) -> str:
    return f"ref_{user_id}"


def parse_referral_code(code: str) -> Optional[int]:
    if not code or not code.startswith("ref_"):
        return None
    try:
        return int(code[4:])
    except ValueError:
        return None


class ReferralStore:
    def __init__(self, filepath: str = STORAGE_FILE) -> None:
        self.filepath = filepath
        self._data: Dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
            except Exception as exc:
                logger.warning("ReferralStore: failed to load %s: %s", self.filepath, exc)
                self._data = {}

    def _save(self) -> None:
        try:
            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.error("ReferralStore: failed to save: %s", exc)

    def link(self, referred_user_id: int, referrer_user_id: int) -> bool:
        """Связывает приглашённого с реферером. Возвращает True, если связка создана.
        Запрещает: ссылаться на себя; повторное связывание уже зарегистрированного юзера."""
        if referred_user_id == referrer_user_id:
            return False
        key = str(referred_user_id)
        if key in self._data:
            return False
        record = ReferralRecord(
            referred_user_id=referred_user_id,
            referrer_user_id=referrer_user_id,
            joined_at=datetime.now(timezone.utc).isoformat(),
        )
        self._data[key] = asdict(record)
        self._save()
        return True

    def get_referrer(self, referred_user_id: int) -> Optional[int]:
        rec = self._data.get(str(referred_user_id))
        return rec.get("referrer_user_id") if rec else None

    def has_active_referrer(self, referred_user_id: int) -> bool:
        """True, если у пользователя есть реферер и бонус ещё не был выдан."""
        rec = self._data.get(str(referred_user_id))
        if not rec:
            return False
        return not rec.get("bonus_awarded", False)

    def mark_converted(self, referred_user_id: int) -> Optional[int]:
        """Отмечает первую оплату приглашённого. Возвращает referrer_user_id,
        если бонус ещё не начислялся, иначе None."""
        key = str(referred_user_id)
        if key not in self._data:
            return None
        rec = self._data[key]
        if rec.get("bonus_awarded"):
            return None
        rec["converted_at"] = datetime.now(timezone.utc).isoformat()
        rec["bonus_awarded"] = True
        self._data[key] = rec
        self._save()
        return rec["referrer_user_id"]

    def list_referrals(self, referrer_user_id: int) -> List[ReferralRecord]:
        result = []
        for raw in self._data.values():
            if raw.get("referrer_user_id") == referrer_user_id:
                result.append(ReferralRecord(**raw))
        return result

    def stats(self, referrer_user_id: int) -> dict:
        records = self.list_referrals(referrer_user_id)
        converted = sum(1 for r in records if r.bonus_awarded)
        return {
            "total": len(records),
            "converted": converted,
            "pending": len(records) - converted,
            "bonus_days": converted * REFERRAL_BONUS_DAYS,
        }
