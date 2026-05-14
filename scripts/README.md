# scripts/

Инфраструктурные скрипты и systemd-юниты, которые ставятся вручную на сервере.

## finarch-alert — Telegram-алерт при падении сервиса

`finarch-alert.sh` шлёт сообщение в Telegram-чат, когда systemd-сервис
переходит в состояние `failed`. Используется через `OnFailure=` в unit'ах.

### Установка на сервере

```bash
# 1. Скрипт
install -m 755 scripts/finarch-alert.sh /usr/local/bin/finarch-alert.sh

# 2. Конфиг с токеном (mode 600 — токен это секрет)
cat > /etc/finarch-alert.env <<EOF
ALERT_BOT_TOKEN=<token из BotFather>
ALERT_CHAT_ID=<твой Telegram user id>
EOF
chmod 600 /etc/finarch-alert.env
chown root:root /etc/finarch-alert.env

# 3. Шаблон сервиса алертов
install -m 644 scripts/systemd/finarch-alert@.service /etc/systemd/system/

# 4. Drop-in для основного сервиса — подключает OnFailure
install -d /etc/systemd/system/finarch-bot.service.d
install -m 644 scripts/systemd/finarch-bot.service.d/onfailure.conf \
        /etc/systemd/system/finarch-bot.service.d/

systemctl daemon-reload
```

### Тест

Перед интеграцией с systemd проверь сам скрипт:

```bash
/usr/local/bin/finarch-alert.sh finarch-bot
```

В Telegram должно прилететь сообщение «🚨 Сервис упал: finarch-bot…».
Если придёт `Forbidden: bot can't initiate conversation with a user` —
значит ты не нажал `/start` алерт-боту. Открой чат с ним, нажми /start,
повтори тест.

### Тест полного пайплайна

Чтобы убедиться что `OnFailure` срабатывает, надо ввести сервис
в состояние `failed`. Простой `kill -9` не подойдёт — `Restart=on-failure`
сразу его перезапустит. Используй так:

```bash
# Ставим лимит перезапусков на 1 и сломанный ExecStart на минуту:
systemctl edit --runtime finarch-bot
# в редакторе:
# [Service]
# ExecStart=
# ExecStart=/bin/false

systemctl restart finarch-bot
# Через ~10 секунд сервис уйдёт в failed → должен прилететь алерт

# Откатить:
systemctl revert --runtime finarch-bot
systemctl restart finarch-bot
```
