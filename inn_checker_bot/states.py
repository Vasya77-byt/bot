"""FSM-состояния для сценариев бота."""
from aiogram.fsm.state import State, StatesGroup


class ProposalFlow(StatesGroup):
    """Сценарий: предложение"""
    waiting_inn     = State()   # ждём ИНН (если не передан в команде)
    waiting_purpose = State()   # вопрос 1: назначение платежа
    waiting_price   = State()   # вопрос 2: цена дисконта
    waiting_term    = State()   # вопрос 3: срок отгрузки
    waiting_client  = State()   # вопрос 4: кому предлагаем


class InvoiceFlow(StatesGroup):
    """Сценарий: запрос счета"""
    waiting_inn       = State()   # ИНН (на кого выставляем)
    waiting_purpose   = State()   # вопрос 1: назначение платежа
    waiting_target    = State()   # вопрос 2: на кого выставляем (ИНН/название)
    waiting_from_whom = State()   # вопрос 3: у кого запрашиваем
    waiting_amount    = State()   # вопрос 4: сумма
    waiting_issuer    = State()   # вопрос 5: от кого выставляем


class CompareFlow(StatesGroup):
    """Сценарий: сравнение двух компаний"""
    waiting_inn1 = State()  # ИНН первой компании
    waiting_inn2 = State()  # ИНН второй компании


class AdminAuthFlow(StatesGroup):
    """Скрытая авторизация администратора."""
    waiting_login  = State()   # ввод логина
    waiting_pass   = State()   # ввод пароля
    waiting_secret = State()   # ввод секретного слова


class PromoFlow(StatesGroup):
    """Ввод промокода."""
    waiting_code = State()


class SupportFlow(StatesGroup):
    """Обращение в поддержку."""
    waiting_message = State()
