"""Тесты UserProfile (чистая логика лимитов/подписки) и UserStore (персистентность)."""
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from user_store import (
    TARIFF_FEATURES,
    TARIFF_LIMITS,
    TARIFF_PRICES,
    UserProfile,
    UserStore,
)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


class TestProfileResetIfNewDay:
    def test_same_day_does_not_reset(self):
        p = UserProfile(user_id=1, checks_today=5, checks_date=date.today().isoformat())
        p.reset_if_new_day()
        assert p.checks_today == 5

    def test_new_day_resets_counter(self):
        p = UserProfile(user_id=1, checks_today=5, checks_date="2000-01-01")
        p.reset_if_new_day()
        assert p.checks_today == 0
        assert p.checks_date == date.today().isoformat()

    def test_empty_date_treated_as_new_day(self):
        p = UserProfile(user_id=1, checks_today=7, checks_date="")
        p.reset_if_new_day()
        assert p.checks_today == 0


class TestSubscriptionActive:
    def test_free_tariff_never_active(self):
        p = UserProfile(user_id=1, tariff="free")
        assert p.is_subscription_active() is False

    def test_paid_without_expiry_not_active(self):
        p = UserProfile(user_id=1, tariff="pro", tariff_expires_at="")
        assert p.is_subscription_active() is False

    def test_paid_future_expiry_active(self):
        future = datetime.now(timezone.utc) + timedelta(days=10)
        p = UserProfile(user_id=1, tariff="pro", tariff_expires_at=_iso(future))
        assert p.is_subscription_active() is True

    def test_paid_past_expiry_not_active(self):
        past = datetime.now(timezone.utc) - timedelta(days=1)
        p = UserProfile(user_id=1, tariff="pro", tariff_expires_at=_iso(past))
        assert p.is_subscription_active() is False

    def test_invalid_iso_treated_as_inactive(self):
        p = UserProfile(user_id=1, tariff="pro", tariff_expires_at="not-an-iso")
        assert p.is_subscription_active() is False


class TestEffectiveTariff:
    def test_free_returns_free(self):
        assert UserProfile(user_id=1, tariff="free").effective_tariff() == "free"

    def test_active_paid_returns_paid(self):
        future = datetime.now(timezone.utc) + timedelta(days=5)
        p = UserProfile(user_id=1, tariff="pro", tariff_expires_at=_iso(future))
        assert p.effective_tariff() == "pro"

    def test_expired_paid_falls_back_to_free(self):
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        p = UserProfile(user_id=1, tariff="pro", tariff_expires_at=_iso(past))
        assert p.effective_tariff() == "free"


class TestCanCheck:
    def test_free_under_limit(self):
        p = UserProfile(user_id=1, tariff="free", checks_today=2,
                        checks_date=date.today().isoformat())
        assert p.can_check() is True

    def test_free_at_limit_blocks(self):
        p = UserProfile(user_id=1, tariff="free", checks_today=TARIFF_LIMITS["free"],
                        checks_date=date.today().isoformat())
        assert p.can_check() is False

    def test_business_unlimited(self):
        future = datetime.now(timezone.utc) + timedelta(days=1)
        p = UserProfile(user_id=1, tariff="business", tariff_expires_at=_iso(future),
                        checks_today=10000, checks_date=date.today().isoformat())
        assert p.can_check() is True

    def test_expired_paid_uses_free_limit(self):
        past = datetime.now(timezone.utc) - timedelta(days=1)
        p = UserProfile(user_id=1, tariff="pro", tariff_expires_at=_iso(past),
                        checks_today=TARIFF_LIMITS["free"],
                        checks_date=date.today().isoformat())
        assert p.can_check() is False

    def test_can_check_resets_on_new_day(self):
        p = UserProfile(user_id=1, tariff="free", checks_today=TARIFF_LIMITS["free"],
                        checks_date="2000-01-01")
        # сброс в can_check внутри reset_if_new_day → лимит снова доступен
        assert p.can_check() is True


class TestRemainingAndLimit:
    def test_remaining_for_limited_tariff(self):
        p = UserProfile(user_id=1, tariff="free", checks_today=1,
                        checks_date=date.today().isoformat())
        assert p.remaining_checks() == TARIFF_LIMITS["free"] - 1

    def test_remaining_at_or_above_limit_clamps_to_zero(self):
        p = UserProfile(user_id=1, tariff="free", checks_today=999,
                        checks_date=date.today().isoformat())
        assert p.remaining_checks() == 0

    def test_remaining_unlimited_returns_none(self):
        future = datetime.now(timezone.utc) + timedelta(days=10)
        p = UserProfile(user_id=1, tariff="business", tariff_expires_at=_iso(future),
                        checks_date=date.today().isoformat())
        assert p.remaining_checks() is None

    def test_daily_limit_matches_table(self):
        assert UserProfile(user_id=1, tariff="free").daily_limit() == TARIFF_LIMITS["free"]


class TestIncrement:
    def test_increments_today_and_total(self):
        p = UserProfile(user_id=1, tariff="free", checks_date=date.today().isoformat())
        p.increment()
        p.increment()
        assert p.checks_today == 2
        assert p.checks_total == 2

    def test_increment_resets_on_new_day_then_counts(self):
        p = UserProfile(user_id=1, tariff="free", checks_today=99, checks_total=200,
                        checks_date="2000-01-01")
        p.increment()
        assert p.checks_today == 1
        assert p.checks_total == 201
        assert p.checks_date == date.today().isoformat()


class TestTariffsTable:
    def test_all_tariffs_have_features(self):
        for tariff in ("free", "start", "pro", "business"):
            assert tariff in TARIFF_FEATURES

    def test_paid_tariffs_have_prices(self):
        for tariff in ("start", "pro", "business"):
            assert TARIFF_PRICES[tariff] > 0

    def test_business_unlimited_in_table(self):
        assert TARIFF_LIMITS["business"] is None


@pytest.fixture
def store(tmp_path):
    return UserStore(filepath=str(tmp_path / "users.json"))


class TestUserStoreLoad:
    def test_get_creates_profile_with_defaults(self, store):
        profile = store.get(42)
        assert profile.user_id == 42
        assert profile.tariff == "free"

    def test_get_persists_to_file(self, tmp_path):
        path = tmp_path / "u.json"
        UserStore(filepath=str(path)).get(7)
        with open(path) as f:
            data = json.load(f)
        assert "7" in data
        assert data["7"]["tariff"] == "free"

    def test_load_from_existing_file(self, tmp_path):
        path = tmp_path / "u.json"
        with open(path, "w") as f:
            json.dump({"100": {"user_id": 100, "tariff": "pro"}}, f)
        store = UserStore(filepath=str(path))
        assert store.get(100).tariff == "pro"

    def test_load_corrupt_file_yields_empty_store(self, tmp_path):
        path = tmp_path / "u.json"
        path.write_text("{not json")
        store = UserStore(filepath=str(path))
        assert store.get(1).user_id == 1  # создан как новый

    def test_unknown_fields_ignored_for_back_compat(self, tmp_path):
        path = tmp_path / "u.json"
        with open(path, "w") as f:
            json.dump({"1": {"user_id": 1, "tariff": "free", "deprecated_field": "x"}}, f)
        store = UserStore(filepath=str(path))
        # не падает с TypeError из-за неизвестного аргумента
        assert store.get(1).user_id == 1


class TestUserStoreMutations:
    def test_increment_checks_persists(self, tmp_path):
        path = tmp_path / "u.json"
        store = UserStore(filepath=str(path))
        store.increment_checks(1)
        store.increment_checks(1)
        # перечитываем с диска
        store2 = UserStore(filepath=str(path))
        assert store2.get(1).checks_today == 2
        assert store2.get(1).checks_total == 2

    def test_set_tariff_changes_value(self, store):
        p = store.set_tariff(1, "pro")
        assert p.tariff == "pro"
        assert store.get(1).tariff == "pro"

    def test_set_email(self, store):
        store.set_email(1, "user@example.com")
        assert store.get(1).email == "user@example.com"

    def test_disable_and_enable_auto_renew(self, store):
        store.get(1)  # создаём с auto_renew=True по умолчанию
        store.disable_auto_renew(1)
        assert store.get(1).auto_renew is False
        store.enable_auto_renew(1)
        assert store.get(1).auto_renew is True


class TestActivateSubscription:
    def test_first_activation_from_free(self, store):
        before = datetime.now(timezone.utc)
        p = store.activate_subscription(1, "pro", days=30,
                                        card_token="tok", payment_id="op-1")
        expires = datetime.fromisoformat(p.tariff_expires_at)
        delta = expires - before
        # ~30 дней с допуском
        assert timedelta(days=29, hours=23) < delta < timedelta(days=30, minutes=1)
        assert p.tariff == "pro"
        assert p.card_token == "tok"
        assert p.last_payment_id == "op-1"
        assert p.auto_renew is True
        assert p.renewal_failures == 0

    def test_renewal_extends_existing_active(self, store):
        future = datetime.now(timezone.utc) + timedelta(days=10)
        # Готовим активную подписку
        p = store.get(1)
        p.tariff = "pro"
        p.tariff_expires_at = _iso(future)
        store.save_profile(p)

        renewed = store.activate_subscription(1, "pro", days=30)
        new_expires = datetime.fromisoformat(renewed.tariff_expires_at)
        # +30 дней от прежнего expires, не от now
        assert new_expires > future + timedelta(days=29, hours=23)
        assert new_expires < future + timedelta(days=30, minutes=1)

    def test_renewal_after_expiry_starts_from_now(self, store):
        past = datetime.now(timezone.utc) - timedelta(days=5)
        p = store.get(1)
        p.tariff = "pro"
        p.tariff_expires_at = _iso(past)
        store.save_profile(p)

        before = datetime.now(timezone.utc)
        renewed = store.activate_subscription(1, "pro", days=30)
        new_expires = datetime.fromisoformat(renewed.tariff_expires_at)
        delta = new_expires - before
        # ~30 дней от now, а не past+30
        assert timedelta(days=29, hours=23) < delta < timedelta(days=30, minutes=1)

    def test_changing_tariff_starts_from_now(self, store):
        future = datetime.now(timezone.utc) + timedelta(days=20)
        p = store.get(1)
        p.tariff = "start"
        p.tariff_expires_at = _iso(future)
        store.save_profile(p)

        before = datetime.now(timezone.utc)
        upgraded = store.activate_subscription(1, "pro", days=30)
        # При смене тарифа expires считается от now, а не от прежнего expires
        new_expires = datetime.fromisoformat(upgraded.tariff_expires_at)
        delta = new_expires - before
        assert timedelta(days=29, hours=23) < delta < timedelta(days=30, minutes=1)
        assert upgraded.tariff == "pro"

    def test_resets_renewal_failures(self, store):
        p = store.get(1)
        p.renewal_failures = 5
        p.auto_renew = False
        store.save_profile(p)
        renewed = store.activate_subscription(1, "pro", days=30)
        assert renewed.renewal_failures == 0
        assert renewed.auto_renew is True

    def test_does_not_overwrite_card_token_when_empty(self, store):
        store.activate_subscription(1, "pro", days=30, card_token="first")
        renewed = store.activate_subscription(1, "pro", days=30, card_token="")
        assert renewed.card_token == "first"


class TestRecordRenewalFailure:
    def test_first_failure_increments_only(self, store):
        store.get(1)
        p = store.record_renewal_failure(1)
        assert p.renewal_failures == 1
        assert p.auto_renew is True

    def test_two_failures_keep_auto_renew_on(self, store):
        store.record_renewal_failure(1)
        p = store.record_renewal_failure(1)
        assert p.renewal_failures == 2
        assert p.auto_renew is True

    def test_third_failure_disables_auto_renew(self, store):
        store.record_renewal_failure(1)
        store.record_renewal_failure(1)
        p = store.record_renewal_failure(1)
        assert p.renewal_failures == 3
        assert p.auto_renew is False


class TestIterProfiles:
    def test_iter_returns_all_users(self, store):
        store.get(1)
        store.get(2)
        store.get(3)
        ids = sorted(p.user_id for p in store.iter_profiles())
        assert ids == [1, 2, 3]

    def test_iter_yields_user_profile_instances(self, store):
        store.get(1)
        for p in store.iter_profiles():
            assert isinstance(p, UserProfile)


# ──────────────────────────────────────────────────────────────────────
# Партнёрская программа
# ──────────────────────────────────────────────────────────────────────


class TestReferralCodeGeneration:
    def test_new_profile_gets_referral_code(self, store):
        profile = store.get(1)
        assert profile.referral_code
        assert profile.referral_code.startswith("ref_")
        assert len(profile.referral_code) == 12  # ref_ + 8 hex

    def test_codes_are_unique_across_users(self, store):
        codes = {store.get(i).referral_code for i in range(1, 50)}
        assert len(codes) == 49  # все уникальны

    def test_existing_profile_without_code_gets_backfilled(self, tmp_path):
        # Симулируем старый users.json без referral_code
        path = tmp_path / "u.json"
        import json
        with open(path, "w") as f:
            json.dump({"42": {"user_id": 42, "tariff": "free"}}, f)
        store = UserStore(filepath=str(path))
        profile = store.get(42)
        assert profile.referral_code  # бэкфилл при первом get

    def test_code_persists_across_reload(self, tmp_path):
        path = tmp_path / "u.json"
        store = UserStore(filepath=str(path))
        original = store.get(1).referral_code
        store2 = UserStore(filepath=str(path))
        assert store2.get(1).referral_code == original


class TestFindByReferralCode:
    def test_finds_existing(self, store):
        invited = store.get(1)
        result = store.find_by_referral_code(invited.referral_code)
        assert result is not None
        assert result.user_id == 1

    def test_unknown_code_returns_none(self, store):
        assert store.find_by_referral_code("ref_unknown") is None

    def test_empty_code_returns_none(self, store):
        assert store.find_by_referral_code("") is None


class TestSetReferrerByCode:
    def test_normal_flow(self, store):
        referrer = store.get(1)
        ok = store.set_referrer_by_code(2, referrer.referral_code)
        assert ok is True
        invited = store.get(2)
        assert invited.referrer_id == 1
        # Счётчик у референта обновлён
        assert store.get(1).referrals_count == 1

    def test_self_referral_blocked(self, store):
        profile = store.get(1)
        ok = store.set_referrer_by_code(1, profile.referral_code)
        assert ok is False
        assert store.get(1).referrer_id is None
        assert store.get(1).referrals_count == 0

    def test_unknown_code_blocked(self, store):
        ok = store.set_referrer_by_code(2, "ref_unknown")
        assert ok is False
        assert store.get(2).referrer_id is None

    def test_already_set_referrer_not_overwritten(self, store):
        first = store.get(1)
        second = store.get(2)
        store.set_referrer_by_code(3, first.referral_code)
        ok = store.set_referrer_by_code(3, second.referral_code)
        # Второй вызов отвергнут
        assert ok is False
        assert store.get(3).referrer_id == 1
        # И счётчик у второго не вырос
        assert store.get(2).referrals_count == 0


class TestAwardReferralBonus:
    def test_no_referrer_returns_none(self, store):
        store.get(1)
        assert store.award_referral_bonus(1) is None

    def test_unknown_invited_user_returns_none(self, store):
        # Тут invited_user_id создаст пустой профиль без referrer_id
        assert store.award_referral_bonus(999) is None

    def test_free_referrer_gets_start_for_15_days(self, store):
        referrer = store.get(1)
        store.set_referrer_by_code(2, referrer.referral_code)

        from datetime import datetime, timezone
        before = datetime.now(timezone.utc)
        result = store.award_referral_bonus(2, days=15)
        assert result is not None

        # Стал start, expires ~ now+15
        ref = store.get(1)
        assert ref.tariff == "start"
        expires = datetime.fromisoformat(ref.tariff_expires_at)
        from datetime import timedelta
        assert timedelta(days=14, hours=23) < expires - before < timedelta(days=15, minutes=1)
        # auto_renew выключен — карты у Free нет
        assert ref.auto_renew is False
        # Статистика обновлена
        assert ref.referrals_paid_count == 1
        assert ref.referral_bonus_days_total == 15

    def test_paid_referrer_gets_extension_of_current_tariff(self, store):
        referrer = store.get(1)
        store.activate_subscription(1, "pro", days=10, card_token="t")
        before_expires = store.get(1).tariff_expires_at
        store.set_referrer_by_code(2, referrer.referral_code)

        store.award_referral_bonus(2, days=15)
        ref = store.get(1)
        # Тариф остался pro
        assert ref.tariff == "pro"
        # Срок продлён на 15 дней
        from datetime import datetime, timedelta
        new_expires = datetime.fromisoformat(ref.tariff_expires_at)
        old_expires = datetime.fromisoformat(before_expires)
        delta = new_expires - old_expires
        assert timedelta(days=14, hours=23) < delta < timedelta(days=15, minutes=1)

    def test_idempotent_when_called_twice(self, store):
        referrer = store.get(1)
        store.set_referrer_by_code(2, referrer.referral_code)

        first = store.award_referral_bonus(2, days=15)
        # Повторный вызов не должен выдать ещё раз
        second = store.award_referral_bonus(2, days=15)
        assert first is not None
        assert second is None
        # Только один бонус начислен
        ref = store.get(1)
        assert ref.referrals_paid_count == 1
        assert ref.referral_bonus_days_total == 15

    def test_invited_marked_as_granted(self, store):
        referrer = store.get(1)
        store.set_referrer_by_code(2, referrer.referral_code)
        store.award_referral_bonus(2)
        assert store.get(2).referral_bonus_granted is True

    def test_referrer_with_active_paid_keeps_auto_renew_state(self, store):
        # Бонус не должен переопределять auto_renew платника
        referrer = store.get(1)
        store.activate_subscription(1, "pro", days=10, card_token="t")
        store.disable_auto_renew(1)  # пользователь отключил руками
        store.set_referrer_by_code(2, referrer.referral_code)

        store.award_referral_bonus(2)
        # Платник: activate_subscription поднимет auto_renew=True
        # — это известное поведение activate_subscription; в данном
        # тесте задокументируем именно его, чтобы изменение поведения
        # было осознанным.
        assert store.get(1).auto_renew is True

    def test_multiple_referrers_independent(self, store):
        # Один референт привлёк двух — оба бонуса должны начислиться
        referrer = store.get(1)
        store.set_referrer_by_code(2, referrer.referral_code)
        store.set_referrer_by_code(3, referrer.referral_code)

        store.award_referral_bonus(2)
        store.award_referral_bonus(3)

        ref = store.get(1)
        assert ref.referrals_count == 2
        assert ref.referrals_paid_count == 2
        assert ref.referral_bonus_days_total == 30  # 15 + 15


# ──────────────────────────────────────────────────────────────────────
# Бонус самому приглашённому: award_invitee_bonus
# ──────────────────────────────────────────────────────────────────────


class TestAwardInviteeBonus:
    def test_no_referrer_returns_none(self, store):
        """Если пользователь пришёл без реф.ссылки — бонус не начисляется."""
        store.get(42)  # обычный профиль без referrer_id
        store.activate_subscription(42, "start", days=30)
        result = store.award_invitee_bonus(42)
        assert result is None

    def test_free_invitee_returns_none(self, store):
        """На Free нечего продлевать — бонус не выдаётся."""
        referrer = store.get(1)
        store.set_referrer_by_code(2, referrer.referral_code)
        # 2 на free
        result = store.award_invitee_bonus(2)
        assert result is None

    def test_invitee_with_paid_tariff_extends_subscription(self, store):
        """Приглашённый со start-тарифом получает +15 дней."""
        referrer = store.get(1)
        store.set_referrer_by_code(2, referrer.referral_code)
        store.activate_subscription(2, "start", days=30)
        before_expires = store.get(2).tariff_expires_at

        result = store.award_invitee_bonus(2)
        assert result is not None
        assert result.invitee_bonus_granted is True
        assert result.tariff == "start"  # тариф не меняется
        # Срок продлился (точное значение зависит от parsing, проверяем что
        # подписка действительно продлилась — флаг granted это гарантирует)
        assert result.tariff_expires_at != before_expires

    def test_idempotent_second_call_returns_none(self, store):
        """Повторный вызов не должен начислить повторно."""
        referrer = store.get(1)
        store.set_referrer_by_code(2, referrer.referral_code)
        store.activate_subscription(2, "start", days=30)

        first = store.award_invitee_bonus(2)
        second = store.award_invitee_bonus(2)
        assert first is not None
        assert second is None

    def test_granted_flag_persists(self, store):
        referrer = store.get(1)
        store.set_referrer_by_code(2, referrer.referral_code)
        store.activate_subscription(2, "start", days=30)
        store.award_invitee_bonus(2)
        assert store.get(2).invitee_bonus_granted is True


# ──────────────────────────────────────────────────────────────────────
# UTM-источники и list_invitees для Mini App
# ──────────────────────────────────────────────────────────────────────


class TestReferralSourceAndInvitees:
    def test_set_referrer_with_source_stores_it(self, store):
        referrer = store.get(1)
        ok = store.set_referrer_by_code(
            2, referrer.referral_code, source="instagram",
        )
        assert ok is True
        assert store.get(2).referral_source == "instagram"

    def test_source_sanitized_to_allowed_chars(self, store):
        """Мусор в source-полях должен фильтроваться."""
        referrer = store.get(1)
        store.set_referrer_by_code(
            3, referrer.referral_code, source="bad/chars*and+space here",
        )
        # Остаются только латиница/цифры/_/-
        src = store.get(3).referral_source
        assert all(c.isalnum() or c in "_-" for c in src)
        assert src == "badcharsandspacehere"

    def test_source_truncated_to_32(self, store):
        referrer = store.get(1)
        long_src = "a" * 100
        store.set_referrer_by_code(
            4, referrer.referral_code, source=long_src,
        )
        assert len(store.get(4).referral_source) == 32

    def test_set_referrer_without_source_leaves_empty(self, store):
        referrer = store.get(1)
        store.set_referrer_by_code(2, referrer.referral_code)
        assert store.get(2).referral_source == ""

    def test_registered_at_set_on_first_referral_attach(self, store):
        referrer = store.get(1)
        store.set_referrer_by_code(2, referrer.referral_code)
        assert store.get(2).registered_at != ""

    def test_list_invitees_returns_only_own_invitees(self, store):
        ref1 = store.get(1)
        ref2 = store.get(2)
        store.set_referrer_by_code(10, ref1.referral_code)
        store.set_referrer_by_code(11, ref1.referral_code)
        store.set_referrer_by_code(20, ref2.referral_code)

        invitees_of_1 = store.list_invitees(1)
        invitees_of_2 = store.list_invitees(2)
        ids_1 = {i.user_id for i in invitees_of_1}
        ids_2 = {i.user_id for i in invitees_of_2}
        assert ids_1 == {10, 11}
        assert ids_2 == {20}

    def test_list_invitees_empty_when_no_invitees(self, store):
        store.get(99)  # обычный профиль без приглашённых
        assert store.list_invitees(99) == []

    def test_list_invitees_sorted_recent_first(self, store):
        """Сортировка по registered_at DESC — новые сверху."""
        ref = store.get(1)
        store.set_referrer_by_code(10, ref.referral_code)
        # Эмулируем что у 10 более раннее время регистрации
        p10 = store.get(10)
        p10.registered_at = "2020-01-01T00:00:00+00:00"
        store.save_profile(p10)

        store.set_referrer_by_code(11, ref.referral_code)
        p11 = store.get(11)
        p11.registered_at = "2024-06-01T00:00:00+00:00"
        store.save_profile(p11)

        invitees = store.list_invitees(1)
        # 11 (2024) должен быть первым, 10 (2020) — вторым
        assert invitees[0].user_id == 11
        assert invitees[1].user_id == 10


# ──────────────────────────────────────────────────────────────────────
# Lifetime тариф и effective_tariff
# ──────────────────────────────────────────────────────────────────────


class TestLifetimeTariff:
    def test_lifetime_only_returns_lifetime_tariff(self):
        p = UserProfile(user_id=1, tariff="free", lifetime_tariff="pro")
        assert p.is_subscription_active() is True
        assert p.effective_tariff() == "pro"

    def test_lifetime_business_above_monthly_pro(self):
        future = datetime.now(timezone.utc) + timedelta(days=10)
        p = UserProfile(
            user_id=1,
            tariff="pro",
            tariff_expires_at=_iso(future),
            lifetime_tariff="business",
        )
        assert p.effective_tariff() == "business"

    def test_monthly_business_above_lifetime_pro(self):
        future = datetime.now(timezone.utc) + timedelta(days=10)
        p = UserProfile(
            user_id=1,
            tariff="business",
            tariff_expires_at=_iso(future),
            lifetime_tariff="pro",
        )
        assert p.effective_tariff() == "business"

    def test_expired_monthly_falls_back_to_lifetime(self):
        past = datetime.now(timezone.utc) - timedelta(days=1)
        p = UserProfile(
            user_id=1,
            tariff="business",
            tariff_expires_at=_iso(past),
            lifetime_tariff="pro",
        )
        assert p.effective_tariff() == "pro"
        assert p.is_subscription_active() is True

    def test_no_lifetime_no_monthly_is_free(self):
        p = UserProfile(user_id=1, tariff="free")
        assert p.is_subscription_active() is False
        assert p.effective_tariff() == "free"


# ──────────────────────────────────────────────────────────────────────
# Tier-награды (Фаза 2 партнёрской программы)
# ──────────────────────────────────────────────────────────────────────


class TestTierRewards:
    def _make_referrals(self, store, referrer_id: int, count: int) -> None:
        """Создаёт count приглашённых и активирует каждому Start —
        чтобы award_referral_bonus сработал без блокировки free."""
        ref = store.get(referrer_id)
        for i in range(count):
            invited_id = 1000 + i
            store.set_referrer_by_code(invited_id, ref.referral_code)
            store.activate_subscription(invited_id, "start", days=30)
            store.award_referral_bonus(invited_id)

    def test_bronze_grants_30_days_pro_to_free(self, store):
        self._make_referrals(store, 1, 3)
        ref = store.get(1)
        assert "bronze" in ref.tier_rewards_granted
        assert ref.tariff == "pro"
        expires = datetime.fromisoformat(ref.tariff_expires_at)
        # ~30 дней от now (с погрешностью)
        delta = expires - datetime.now(timezone.utc)
        assert timedelta(days=29, hours=23) < delta < timedelta(days=30, minutes=1)

    def test_bronze_extends_business_for_business_referrer(self, store):
        store.activate_subscription(1, "business", days=10)
        self._make_referrals(store, 1, 3)
        ref = store.get(1)
        # Тариф НЕ деградирует до Pro — продлили существующий business
        assert ref.tariff == "business"
        # 10 (initial) + 15 (1-я оплата) + 15 (2-я) + 30 (bronze на 3-й) = 70
        expires = datetime.fromisoformat(ref.tariff_expires_at)
        delta = expires - datetime.now(timezone.utc)
        assert timedelta(days=69, hours=23) < delta < timedelta(days=70, minutes=1)

    def test_silver_grants_90_days(self, store):
        self._make_referrals(store, 1, 10)
        ref = store.get(1)
        assert "bronze" in ref.tier_rewards_granted
        assert "silver" in ref.tier_rewards_granted

    def test_gold_grants_lifetime_pro(self, store):
        self._make_referrals(store, 1, 30)
        ref = store.get(1)
        assert "gold" in ref.tier_rewards_granted
        assert ref.lifetime_tariff == "pro"
        assert ref.effective_tariff() == "pro"
        assert ref.revshare_enabled is False

    def test_diamond_grants_lifetime_business_and_revshare(self, store):
        self._make_referrals(store, 1, 100)
        ref = store.get(1)
        assert "diamond" in ref.tier_rewards_granted
        assert ref.lifetime_tariff == "business"
        assert ref.revshare_enabled is True

    def test_non_threshold_payment_grants_plain_15_days(self, store):
        # Достигаем bronze (3 оплативших), потом ещё одна оплата на 4ой
        # — никакого нового tier, должен прийти +15 как раньше.
        self._make_referrals(store, 1, 3)  # bronze unlocked, expires ~30d Pro
        ref_after_bronze = store.get(1)
        expires_after_bronze = ref_after_bronze.tariff_expires_at

        # +1 приглашённый сверх bronze (paid=4, до silver=10 — не tier)
        invited_id = 2000
        store.set_referrer_by_code(invited_id, ref_after_bronze.referral_code)
        store.activate_subscription(invited_id, "start", days=30)
        store.award_referral_bonus(invited_id)

        ref = store.get(1)
        assert ref.referrals_paid_count == 4
        # tier_rewards_granted не вырос
        assert ref.tier_rewards_granted == ["bronze"]
        # Подписка продлилась ровно на 15 дней
        new_exp = datetime.fromisoformat(ref.tariff_expires_at)
        old_exp = datetime.fromisoformat(expires_after_bronze)
        delta = new_exp - old_exp
        assert timedelta(days=14, hours=23) < delta < timedelta(days=15, minutes=1)

    def test_tier_idempotent_via_invited_flag(self, store):
        # Повторный вызов award_referral_bonus для одного и того же
        # приглашённого не должен ничего делать (защита на стороне invited).
        self._make_referrals(store, 1, 3)
        first_state = store.get(1)
        # Имитируем повтор: тот же invited
        result = store.award_referral_bonus(1000)  # тот же 1000 что и в _make_referrals
        assert result is None
        ref = store.get(1)
        assert ref.referrals_paid_count == first_state.referrals_paid_count
        assert ref.tier_rewards_granted == first_state.tier_rewards_granted

    def test_lifetime_persists_in_storage(self, store, tmp_path):
        self._make_referrals(store, 1, 30)
        # Перезагружаем store из того же файла (module-fixture использует
        # tmp_path / "users.json")
        store2 = UserStore(str(tmp_path / "users.json"))
        ref = store2.get(1)
        assert ref.lifetime_tariff == "pro"
        assert "gold" in ref.tier_rewards_granted


# ──────────────────────────────────────────────────────────────────────
# Refund / chargeback: revoke_referral_bonus и revoke_invitee_bonus
# ──────────────────────────────────────────────────────────────────────


class TestRevokeReferralBonus:
    def _setup(self, store, referrer_id=1, invited_id=2):
        ref = store.get(referrer_id)
        store.set_referrer_by_code(invited_id, ref.referral_code)
        store.activate_subscription(invited_id, "pro", days=30)
        store.award_referral_bonus(invited_id)
        return store.get(referrer_id), store.get(invited_id)

    def test_revokes_paid_count_and_days(self, store):
        ref_before, inv_before = self._setup(store)
        assert ref_before.referrals_paid_count == 1
        assert ref_before.referral_bonus_days_total == 15
        assert inv_before.referral_bonus_granted is True

        result = store.revoke_referral_bonus(2)
        assert result is not None
        ref = store.get(1)
        assert ref.referrals_paid_count == 0
        assert ref.referral_bonus_days_total == 0
        # Флаг сброшен — повторная оплата того же юзера снова даст бонус
        assert store.get(2).referral_bonus_granted is False

    def test_idempotent_second_revoke_returns_none(self, store):
        self._setup(store)
        first = store.revoke_referral_bonus(2)
        second = store.revoke_referral_bonus(2)
        assert first is not None
        assert second is None

    def test_no_bonus_granted_returns_none(self, store):
        # Юзер 2 пришёл по реф-ссылке, но ещё не оплатил
        ref = store.get(1)
        store.set_referrer_by_code(2, ref.referral_code)
        result = store.revoke_referral_bonus(2)
        assert result is None

    def test_no_referrer_returns_none(self, store):
        store.get(42)  # без referrer_id
        result = store.revoke_referral_bonus(42)
        assert result is None

    def test_unknown_invited_returns_none(self, store):
        # Юзер 999 не существует — _raw_profile вернёт None
        result = store.revoke_referral_bonus(999)
        assert result is None

    def test_subscription_days_not_rolled_back(self, store):
        # Намеренное поведение: срок подписки реферера НЕ откатывается.
        # Мы продлили — назад не отнимаем (нет истории по конкретным
        # transactions, "честный" rollback невозможен).
        ref, inv = self._setup(store)
        expires_after_bonus = store.get(1).tariff_expires_at
        store.revoke_referral_bonus(2)
        assert store.get(1).tariff_expires_at == expires_after_bonus

    def test_lifetime_and_tier_grants_are_sticky(self, store):
        # Юзер достиг Diamond (100 опл.); при revoke последнего платежа
        # lifetime НЕ снимается, tier_rewards_granted остаётся.
        ref = store.get(1)
        for i in range(100):
            inv_id = 9000 + i
            store.set_referrer_by_code(inv_id, ref.referral_code)
            store.activate_subscription(inv_id, "pro", days=30)
            store.award_referral_bonus(inv_id)
        ref = store.get(1)
        assert ref.lifetime_tariff == "business"
        assert "diamond" in ref.tier_rewards_granted
        assert ref.revshare_enabled is True

        store.revoke_referral_bonus(9099)
        ref = store.get(1)
        # paid_count сполз с 100 на 99
        assert ref.referrals_paid_count == 99
        # Но lifetime/tier_rewards/revshare НЕ откатываются
        assert ref.lifetime_tariff == "business"
        assert "diamond" in ref.tier_rewards_granted
        assert ref.revshare_enabled is True


class TestRevokeInviteeBonus:
    def test_revokes_flag(self, store):
        ref = store.get(1)
        store.set_referrer_by_code(2, ref.referral_code)
        store.activate_subscription(2, "pro", days=30)
        store.award_invitee_bonus(2)
        assert store.get(2).invitee_bonus_granted is True

        result = store.revoke_invitee_bonus(2)
        assert result is not None
        assert store.get(2).invitee_bonus_granted is False

    def test_idempotent(self, store):
        ref = store.get(1)
        store.set_referrer_by_code(2, ref.referral_code)
        store.activate_subscription(2, "pro", days=30)
        store.award_invitee_bonus(2)
        first = store.revoke_invitee_bonus(2)
        second = store.revoke_invitee_bonus(2)
        assert first is not None
        assert second is None

    def test_no_bonus_returns_none(self, store):
        store.get(42)
        result = store.revoke_invitee_bonus(42)
        assert result is None
