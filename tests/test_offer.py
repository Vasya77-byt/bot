"""Тесты публичной оферты — содержит обязательные юр-разделы.

Цель — поймать регрессии в тексте оферты: если правится прайс или
тарифы, но забыли обновить оферту, тесты сразу укажут.
"""
import re

import pytest

from offer import OFFER_TEXT
from user_store import TARIFF_PRICES


class TestOfferStructure:
    def test_has_title(self):
        assert "Публичная оферта" in OFFER_TEXT

    def test_has_subject_section(self):
        assert "Предмет договора" in OFFER_TEXT

    def test_has_payment_section(self):
        assert "Стоимость и порядок оплаты" in OFFER_TEXT

    def test_has_refund_section(self):
        assert "Возврат средств" in OFFER_TEXT

    def test_has_responsibility_section(self):
        assert "Ответственность" in OFFER_TEXT

    def test_has_requisites_section(self):
        assert "Реквизиты" in OFFER_TEXT


class TestOfferLegalContent:
    def test_mentions_acceptance_criterion(self):
        # «Акцептом оферты считается факт оплаты» — без этого договор
        # не считается заключённым
        assert "Акцептом" in OFFER_TEXT or "акцепт" in OFFER_TEXT.lower()

    def test_mentions_egrul_egrip(self):
        assert "ЕГРЮЛ" in OFFER_TEXT
        assert "ЕГРИП" in OFFER_TEXT

    def test_mentions_telegram_bot_as_delivery_channel(self):
        assert "Telegram" in OFFER_TEXT

    def test_mentions_tochka_acquiring(self):
        # АО «Точка» — обязательно для договора с эквайрером
        assert "Точка" in OFFER_TEXT

    def test_mentions_card_save_consent(self):
        # 152-ФЗ + правила Точки требуют явного согласия на сохранение карты
        assert "сохранение карты" in OFFER_TEXT or "сохранении карты" in OFFER_TEXT

    def test_mentions_auto_renewal(self):
        assert "автоматического продления" in OFFER_TEXT or "автопродление" in OFFER_TEXT.lower()

    def test_mentions_30_day_period(self):
        # Период списания — должен совпадать с кодом (subscription.py: days=30)
        assert "30" in OFFER_TEXT

    def test_mentions_cancel_subscription_command(self):
        # Право клиента отменить автопродление — обязательно для оферты
        assert "/cancel_subscription" in OFFER_TEXT

    def test_disclaims_data_accuracy(self):
        # Дисклеймер про «не гарантирует 100%» — критичен для бота
        # с публичными данными от третьих сторон
        assert "не гарантирует" in OFFER_TEXT.lower() or "100%" in OFFER_TEXT

    def test_refund_for_unused_period_promised(self):
        assert "пропорционально" in OFFER_TEXT.lower()

    def test_refund_for_consumed_checks_excluded(self):
        # «Не подлежат возврату средства за проведённые проверки»
        assert "не подлежат возврату" in OFFER_TEXT.lower() or "не возврат" in OFFER_TEXT.lower()


class TestOfferPricingMatchesCode:
    """Цены в оферте должны быть синхронны с TARIFF_PRICES."""

    @pytest.mark.parametrize("tariff", ["start", "pro", "business"])
    def test_paid_tariff_listed_in_offer(self, tariff):
        # Названия тарифов с большой буквы в оферте
        assert tariff.capitalize() in OFFER_TEXT

    @pytest.mark.parametrize("tariff,price", list(TARIFF_PRICES.items()))
    def test_each_tariff_price_appears(self, tariff, price):
        # Цена в оферте записана через неразрывный пробел («1 290 ₽»),
        # поэтому ищем «1 290» или «1290»
        with_space = f"{price:,}".replace(",", " ")
        assert (str(price) in OFFER_TEXT) or (with_space in OFFER_TEXT), (
            f"Tariff {tariff} price {price} not found in offer"
        )


class TestOfferTariffNamesMatchCode:
    def test_all_paid_tariffs_listed(self):
        # Free / Start / Pro / Business — должны все упоминаться в п. 1.2
        for tariff in ("Free", "Start", "Pro", "Business"):
            assert tariff in OFFER_TEXT, f"Missing tariff: {tariff}"
