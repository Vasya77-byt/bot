"""Тесты многоуровневой структуры реферальных наград."""
from __future__ import annotations

import pytest

from referral_tiers import TIERS, current_tier, next_tier, progress_to_next


def test_budgets_sum_correct_thresholds():
    """Контракт API: пороги монотонно растут."""
    thresholds = [t.threshold for t in TIERS]
    assert thresholds == sorted(thresholds)
    # Конкретные значения, ожидаемые UI
    assert thresholds == [0, 3, 10, 30, 100]


class TestCurrentTier:
    def test_zero_invitees_returns_none_tier(self):
        assert current_tier(0).key == "none"

    def test_below_bronze_still_none(self):
        assert current_tier(2).key == "none"

    def test_exact_bronze(self):
        assert current_tier(3).key == "bronze"

    def test_between_bronze_and_silver(self):
        assert current_tier(5).key == "bronze"
        assert current_tier(9).key == "bronze"

    def test_exact_silver(self):
        assert current_tier(10).key == "silver"

    def test_gold(self):
        assert current_tier(30).key == "gold"
        assert current_tier(50).key == "gold"
        assert current_tier(99).key == "gold"

    def test_diamond(self):
        assert current_tier(100).key == "diamond"
        assert current_tier(500).key == "diamond"


class TestNextTier:
    def test_zero_next_is_bronze(self):
        assert next_tier(0).key == "bronze"

    def test_just_below_silver_next_is_silver(self):
        assert next_tier(9).key == "silver"

    def test_at_diamond_returns_none(self):
        assert next_tier(100) is None
        assert next_tier(1000) is None


class TestProgressToNext:
    def test_zero_invitees(self):
        from_base, target, ratio = progress_to_next(0)
        assert from_base == 0
        assert target == 3
        assert ratio == 0.0

    def test_halfway_to_bronze(self):
        # 2 из 3 нужных = 2/3
        from_base, target, ratio = progress_to_next(2)
        assert from_base == 2
        assert target == 3
        assert 0.6 < ratio < 0.7

    def test_between_bronze_and_silver(self):
        # 5 invitees: bronze=3 → нужно 7 до silver (10), сейчас от базы 2
        from_base, target, ratio = progress_to_next(5)
        assert from_base == 2
        assert target == 7
        assert abs(ratio - 2 / 7) < 0.01

    def test_diamond_max(self):
        from_base, target, ratio = progress_to_next(100)
        assert ratio == 1.0

    def test_ratio_clamped_to_one_at_threshold(self):
        # Ровно на пороге → ratio == 0.0 от следующего
        from_base, target, ratio = progress_to_next(3)
        # current=bronze, next=silver. base=3, target=10-3=7, from_base=0
        assert from_base == 0
        assert target == 7
        assert ratio == 0.0
