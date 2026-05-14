# Интеграция ЮKassa

Альтернативный платёжный провайдер. Активируется через
`PAYMENT_PROVIDER=yookassa` в `.env`. Tochka Bank остаётся в коде —
webhook-роуты обоих провайдеров всегда активны.

## Архитектурно

```
[Telegram-бот] → SubscriptionService.create_initial_payment()
                                     │
                                     │  provider=="yookassa"
                                     ▼
                          YooKassaClient.create_payment()
                          POST /v3/payments
                          {amount, save_payment_method:true,
                           receipt, metadata}
                                     │
                                     │  возвращает confirmation_url
                                     ▼
                          Бот шлёт пользователю кнопку «Оплатить»
                                     │
                                     ▼
                          Пользователь → форма ЮKassa → 3DS
                                     │
                                     │  webhook POST /yookassa/webhook
                                     ▼
                          webhook_server.yookassa_webhook()
                          ┌─ IP-whitelist (185.71.76.0/27, ...)
                          ├─ опц. HMAC Y-Signature
                          ├─ parse_webhook → WebhookEvent
                          └─ subscription.handle_yookassa_webhook_paid()
                                     │
                                     ▼
                          UserStore.activate_subscription()
                          + сохранение payment_method.id для автосписаний
```

Автоплатежи (продления):

```
[renewal_scheduler] → subscription.try_renew(profile)
                       provider=="yookassa"
                       │
                       ▼
                       yookassa.charge_recurring(
                         payment_method_id=profile.yookassa_payment_method_id
                       )
                       POST /v3/payments
                       — без confirmation, без участия клиента
```

## Подключение

### Шаг 1. Получить shopId и Секретный ключ

1. Войти в личный кабинет: https://yookassa.ru/my
2. **Настройки → API**
3. **Создать новый ключ** → **«Секретный ключ для боевых платежей»**
4. Сохранить пару:
   - `shopId` — публичный идентификатор магазина
   - `secret_key` — длинная строка (показывается один раз, как у S3)

> Боевой и тестовый магазины — это разные `shopId`. Тестовый создаётся
> бесплатно в том же кабинете и работает по тем же эндпоинтам
> (`api.yookassa.ru/v3`), но без реальных списаний.

### Шаг 2. Настроить онлайн-кассу (один раз)

Если касса в ЮKassa ещё не подключена:

1. **Настройки → Касса для чеков**
2. Выбрать оператора ФД: Атол, Эвотор, Бизнес.Ру и т.п.
3. Указать ИНН ИП и систему налогообложения (УСН 6% / 15% / Патент)

Без подключённой кассы ЮKassa отклонит создание платежа с
обязательным полем `receipt`.

### Шаг 3. Webhook

В кабинете: **Настройки → HTTP-уведомления → Добавить**.

| Поле | Значение |
|------|----------|
| URL | `https://fridaycompanybot.ru/yookassa/webhook` |
| События | `payment.succeeded`, `payment.canceled`, `refund.succeeded` |
| HMAC-секрет | (опционально, см. ниже) |

⚠️ Только HTTPS — на HTTP уведомления не уходят.

#### HMAC-подпись (опционально)

В разделе HTTP-уведомлений можно включить **подпись Y-Signature**.
Если включили — сохраните секрет в `.env` как `YOOKASSA_WEBHOOK_SECRET`.

Если не включили — webhook валидируется только IP-whitelist'ом
официальных адресов ЮKassa (`185.71.76.0/27` и др.).

### Шаг 4. Прописать переменные в `.env`

```bash
PAYMENT_PROVIDER=yookassa

YOOKASSA_SHOP_ID=12345678
YOOKASSA_SECRET_KEY=test_xxxxxxxxxxxxxxxxxxxxxxxx  # или live_...
YOOKASSA_WEBHOOK_SECRET=                            # пусто если без HMAC
YOOKASSA_TAX_SYSTEM_CODE=2                          # 2=УСН доход
YOOKASSA_VAT_CODE=1                                 # 1=без НДС

# Эти — общие для всех провайдеров
PAYMENT_REDIRECT_URL=https://t.me/your_bot_name
WEBHOOK_HOST=0.0.0.0
WEBHOOK_PORT=8080
```

### Шаг 5. Перезапустить бота

```bash
sudo systemctl restart finarch-bot
sudo journalctl -u finarch-bot -n 30 -f
```

В логах должна появиться строка:
```
Payments enabled: provider=yookassa tochka=False yookassa=True
```

## Тестовый платёж

1. Активируйте тестовый магазин: `YOOKASSA_SHOP_ID` и `YOOKASSA_SECRET_KEY`
   = тестовые значения из ЛК ЮKassa.
2. В боте: **Тарифы → Start**
3. Бот спросит email (если не введён ранее) — введите любой валидный
4. Получите кнопку «Оплатить» → откроется форма ЮKassa
5. Тестовая карта: `5555 5555 5555 4444`, любой срок, любой CVV
6. Webhook должен прийти на `/yookassa/webhook` — в логах:
   ```
   YooKassa webhook: payment.succeeded status=succeeded payment_id=...
   YooKassa subscription activated: user=N tariff=start ...
   ```
7. В Telegram бот сам пришлёт уведомление об успешной оплате
8. Проверьте `payments.json` — там должна быть запись с
   `"provider": "yookassa"` и `"status": "paid"`

## Переключение на боевой магазин

1. В `.env` поменяйте `YOOKASSA_SHOP_ID` / `YOOKASSA_SECRET_KEY` на
   боевые значения
2. В кабинете ЮKassa добавьте боевой webhook на тот же URL
   (тестовый можно отключить)
3. `systemctl restart finarch-bot`

## Что важно понимать

### Чеки 54-ФЗ

ЮKassa передаёт чек на онлайн-кассу автоматически — данные берутся
из поля `receipt` в запросе создания платежа. Бот формирует это
поле в `YooKassaClient._build_receipt()`:

```json
{
  "customer": {"email": "<email пользователя>"},
  "tax_system_code": 2,
  "items": [{
    "description": "Подписка на тариф Pro",
    "quantity": "1.00",
    "amount": {"value": "1290.00", "currency": "RUB"},
    "vat_code": 1,
    "payment_subject": "service",
    "payment_mode": "full_prepayment"
  }]
}
```

Чек придёт на указанный email после успешной оплаты.

### Автоплатежи

ЮKassa сохраняет карту через `save_payment_method: true`. После
успешного первого платежа webhook payment.succeeded приносит
`object.payment_method.id` — это токен бессрочной привязки. Бот
сохраняет его в `users.json` как `yookassa_payment_method_id`.

При продлении (`subscription.try_renew`) платёж создаётся через
`POST /v3/payments` с указанным `payment_method_id` без открытия
страницы оплаты. Деньги списываются автоматически.

### Отмена автопродления

Команда `/cancel_subscription` для ЮKassa только локально отключает
`auto_renew=False`. У ЮKassa **нет API для отзыва сохранённой
карты** — её отвязка делается клиентом в своём банке или
автоматически когда карта истекает.

Текущая активная подписка не теряется — она доходит до
`tariff_expires_at`. Просто следующих автосписаний не будет.

### Идемпотентность

Каждый webhook от ЮKassa может прийти несколько раз (повторы при
сетевых ошибках). `handle_yookassa_webhook_paid()` идемпотентен —
проверяет `payments.find_by_operation(payment_id).status` и не
активирует подписку повторно.

Каждый POST к ЮKassa передаёт `Idempotence-Key` — это защищает от
двойного списания при ретраях нашей стороной.

## Файлы

| Файл | Назначение |
|------|-----------|
| `yookassa_client.py` | REST-клиент API, парсинг webhook, чек 54-ФЗ |
| `subscription.py` | `_create_yookassa_initial`, `_try_renew_yookassa`, `handle_yookassa_webhook_paid` |
| `webhook_server.py` | Роут `POST /yookassa/webhook` с IP-whitelist + HMAC |
| `settings.py` | Переменные `YOOKASSA_*` и флаг `PAYMENT_PROVIDER` |
| `tests/test_yookassa_client.py` | 41 unit-тест клиента |
| `tests/test_subscription_yookassa.py` | 17 тестов оркестрации |
| `tests/test_webhook_server.py::TestYooKassaWebhook` | 8 тестов webhook |

## Troubleshooting

### `email_required` при попытке оплаты

Бот сначала спрашивает email через FSM (см. `main.py:1840`). Если
этот шаг был пропущен — выполните `/cancel` и снова выберите тариф.

### `403 forbidden` в webhook

Скорее всего IP запроса не входит в whitelist ЮKassa. Это бывает
если:
- Запрос пришёл не от ЮKassa (атака/сканер) — нормальное поведение
- nginx-прокси не передаёт `X-Forwarded-For` — добавьте в nginx:
  ```nginx
  location /yookassa/webhook {
      proxy_pass http://127.0.0.1:8080;
      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
      proxy_set_header X-Real-IP $remote_addr;
  }
  ```

### `bad signature` в webhook (с HMAC)

`YOOKASSA_WEBHOOK_SECRET` в `.env` не совпадает с тем, что в кабинете
ЮKassa. Скопируйте секрет ещё раз из ЛК и перезапустите бота.

### Платёж succeeded в кабинете, но webhook не дошёл

Поллер `payment_poller.py` каждые 5 минут опрашивает ЮKassa по
pending-платежам через `get_payment` и активирует, если статус
`succeeded`. Это safety-net на случай потерянного webhook'а.
Если ждать не хотите — можно вызвать поллер вручную или нажать
«Обновить статус» в админ-панели (TODO).
