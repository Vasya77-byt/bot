"""Многоуровневая структура реферальных наград.

Tier определяется по числу ОПЛАТИВШИХ приглашённых (referrals_paid_count).
Сами награды (Bronze +30д, Silver +90д, Gold Pro lifetime, Diamond Business
lifetime) выдаются отдельным механизмом (Фаза 2). На Фазе 1 модуль
используется только для визуализации в Mini App'е.

Пороги — 3/10/30/100, сбалансированный профиль:
* Bronze   достижим почти каждым активным юзером
* Silver   требует реальной работы по привлечению
* Gold     для тех кто делает это как side-activity
* Diamond  легендарный уровень, ~единицы пользователей
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Tier:
    key: str                # "none" / "bronze" / "silver" / "gold" / "diamond"
    label: str              # человеческое имя для UI
    threshold: int          # минимум оплативших приглашённых
    emoji: str              # значок для отображения
    reward_text: str        # описание награды (для UI)


# Порядок: от низкого порога к высокому. Включаем "no tier" для unified API.
TIERS: tuple[Tier, ...] = (
    Tier("none",    "Начало пути",  0,   "🌱", "Пригласите первого друга"),
    Tier("bronze",  "Bronze",       3,   "🥉", "+30 дней Pro"),
    Tier("silver",  "Silver",       10,  "🥈", "+90 дней Pro"),
    Tier("gold",    "Gold",         30,  "🥇", "Pro навсегда (lifetime)"),
    Tier("diamond", "Diamond",      100, "💎", "Business навсегда + 10% revshare"),
)


def current_tier(paid_count: int) -> Tier:
    """Возвращает самый высокий tier, чей порог достигнут."""
    achieved = TIERS[0]
    for t in TIERS:
        if paid_count >= t.threshold:
            achieved = t
    return achieved


def next_tier(paid_count: int) -> Optional[Tier]:
    """Возвращает следующий tier, до которого ещё нужно дорасти, или None
    если пользователь уже на максимуме (Diamond)."""
    for t in TIERS:
        if paid_count < t.threshold:
            return t
    return None


def progress_to_next(paid_count: int) -> tuple[int, int, float]:
    """Возвращает (текущее_кол-во_от_базы, нужно_от_базы, доля_0..1).

    Например, при paid_count=5 (между Bronze=3 и Silver=10):
        from_base = 5 - 3 = 2
        target = 10 - 3 = 7
        ratio = 2 / 7 ≈ 0.286

    Если уже Diamond — возвращаем (0, 0, 1.0).
    """
    cur = current_tier(paid_count)
    nxt = next_tier(paid_count)
    if nxt is None:
        return (0, 0, 1.0)
    base = cur.threshold
    target = nxt.threshold - base
    from_base = paid_count - base
    ratio = (from_base / target) if target > 0 else 1.0
    ratio = max(0.0, min(1.0, ratio))
    return (from_base, target, ratio)
