# Project context

Telegram-бот «Финансовый архитектор» — анализ компаний по ИНН, выдача отчётов, продажа подписок через Точка Банк.

## Server

- **Hosting**: Timeweb Cloud (VPS)
- **IP**: 72.56.235.166
- **Hostname**: 7514295-js737642
- **OS**: Ubuntu 24.04
- **Bot path**: /opt/bot
- **SSH**: `ssh root@72.56.235.166`

## Telegram bot

- **Username**: @Fridaycompany_bot
- **Bot ID**: 8609947135
- **Display name**: Monday_company
- **Support contact**: @YRS75 (`SUPPORT_USERNAME` в .env)

## Repo

- **GitHub**: vasya77-byt/bot
- **Working branch**: `claude/clarify-task-YrQBO`
- **Default**: `main`

## Stack

- **MTProto client**: pyrofork 2.3.69 (форк pyrogram, фикс `KeyError: 0` в DH-обмене)
- **Web (webhook)**: aiohttp + FastAPI
- **PDF/PNG**: fpdf2 + Pillow (модуль exports.py — сейчас не используется)
- **Logging**: JSON через logging_config.JsonFormatter (timestamp, level, logger, module, line, extras)
- **Sentry**: интеграция в telemetry.py (DSN опционален)

## External APIs (см. .env)

| API | Что даёт | Статус |
|-----|----------|--------|
| DaData (`DADATA_API_KEY`) | базовые данные о компании | работает |
| API-FNS (`FNS_API_KEY`) | данные ЕГРЮЛ + ФНС-карточка | работает |
| ФССП (`FSSP_API_KEY`) | исполнительные производства | ключ пустой, не подключено |
| СБИС (`SBIS_*`) | финансовые данные | mock-сервер sbis_mock.py:8081 |
| Точка Банк (`TOCHKA_*`) | эквайринг, автопродление подписок | нужны JWT и customer_code |

## Tariffs

| Тариф | Цена/мес | Лимит проверок/день |
|-------|----------|---------------------|
| Free | 0 | 3 |
| Start | 490 ₽ | 50 |
| Pro | 1 290 ₽ | 300 |
| Business | 2 490 ₽ | безлимит |

## Storage

- `storage/` — файлы (если включён S3, грузятся туда; см. `STORAGE_DIR`/`S3_*`)
- `metadata/` — метаданные сгенерённых файлов (`METADATA_DIR`)
- `users.json` — профили пользователей (тариф, лимиты, история, email)
- `payments.json` — журнал платежей и идемпотентность webhook
- `.cache/` — кэш для DaData/SBIS

## Deployment

`deploy/tg-bot.service` — systemd unit:
- `Type=notify` + `WatchdogSec=120` — бот шлёт heartbeat каждые 30s через `sd_notify.py`, systemd убивает зависший процесс
- `Restart=always`, `RestartSec=10` — автоперезапуск
- `ExecStart=/opt/bot/.venv/bin/python /opt/bot/main.py` — изолированный virtualenv
- Логи: `/var/log/tg-bot.log` + `journalctl -u tg-bot`

Команды:
```bash
systemctl start tg-bot           # запустить
systemctl restart tg-bot         # перезапуск
systemctl status tg-bot          # статус
journalctl -u tg-bot -f          # live-логи
```

## Known issues

- **pyrogram 2.0.106 баг `KeyError: 0`** при DH-обмене → бот ретраит в цикле, Telegram временно блокирует IP за флуд auth-попыток. Лечится переходом на pyrofork.
- **Rate-limit Telegram по IP** длится 2–24 часа после флуда. Проверка:
  ```bash
  python3 -c "import socket; s=socket.socket(); s.settimeout(5); print(s.connect_ex(('149.154.167.51', 443)))"
  ```
  Код 0 = OK, 11/110/111 = в блоке.

## Что было удалено и почему (актуально для контекста)

- Кнопки главного меню «Коммерческое предложение», «Дай заявку», «Дай предложение», «Сгенерировать КП (PDF/PNG)»
- Кнопки карточки компании «📝 Предложение», «🧾 Запрос счёта», «📄 PDF»
- Команда `/kp` и текстовые триггеры «кп pdf»/«кп png»
- Связанные хелперы (`_kp_template`, `_send_kp_file`, etc.)
- Импорты `BytesIO`, `build_kp_pdf/png`, `save_file_bytes`, `MetadataStore`

Файлы `exports.py`, `metadata_store.py`, `storage.py`, функции `render_request`/`render_proposal` в `renderers.py` оставлены на случай возврата фичи.

## Что в работе / TODO

- ⚖️ Суды — заглушка в `handle_callback` (callback `ca_courts`); план — через api-fns.ru endpoint `arb`
- 🤖 ИИ-анализ — заглушка (`ca_ai`)
- 🔗 Связи компании — заглушка (`ca_links`)
- ФССП проверка (нужно заполнить `FSSP_API_KEY`)

## Coding conventions

- Все логи — через `logging.getLogger("financial-architect")` (или подмодули)
- Структурные поля — через `extra={"key": value}` в logger-вызовах
- Состояние пользователя — `_user_state: dict[int, Any]` в main.py
- Никаких секретов в коде — только `.env`
