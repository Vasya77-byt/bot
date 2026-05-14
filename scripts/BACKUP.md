# Бэкапы бота в Yandex Object Storage

Автоматический ежедневный бэкап критичных данных бота в облако.
GFS-схема: 7 daily + 4 weekly + 6 monthly.

## Что бэкапится

| Файл | Содержимое |
|------|------------|
| `users.json` | Профили, тарифы, подписки, реферальные коды |
| `payments.json` | История платежей |
| `monitoring.json` | Подписки на отслеживание компаний |
| `report_tokens.json` | Токены веб-отчётов |
| `metadata/` | Метаданные отслеживаемых компаний |
| `.env` | API-ключи и секреты (шифруются GPG) |

Файлы складываются в два архива:
- `data.tar.gz` — все JSON и metadata, не шифрованы
- `secrets.env.gpg` — только `.env`, симметричное AES-256 GPG

## Архитектура в бакете

```
s3://<bucket>/
├── daily/
│   ├── 2026-04-30/
│   │   ├── data.tar.gz
│   │   └── secrets.env.gpg
│   └── 2026-05-01/
│       └── ...
├── weekly/
│   └── 2026-W17/
│       └── ...
└── monthly/
    └── 2026-04/
        └── ...
```

## Деплой на сервере (пошагово)

### Шаг 1: Создать бакет в Yandex Cloud

1. Зайдите на https://cloud.yandex.ru → войдите через Yandex ID
2. Если нет облака — создайте (бесплатно). Привяжите карту для оплаты (~50 ₽/мес за бэкапы такого объёма).
3. Перейдите: **Object Storage → Создать бакет**
   - Имя: `mondaycompany-backups` (или своё уникальное)
   - Класс хранилища: **Стандартное**
   - Доступ: **Закрытый** (важно!)
   - Регион: **ru-central1**
4. Сохраните имя бакета.

### Шаг 2: Создать сервисный аккаунт со статическим ключом

1. **Каталог → Сервисные аккаунты → Создать**
   - Имя: `bot-backup`
   - Роль: **storage.editor** (read+write только этого бакета)
2. У созданного аккаунта: **Создать ключ → Статический ключ доступа**
3. **Сохраните оба значения**:
   - `Access Key ID` (это `S3_ACCESS_KEY`)
   - `Secret Key` (это `S3_SECRET_KEY`) — показывается **один раз**, потом не вернётся!

### Шаг 3: Установить зависимости на сервере

```bash
ssh root@89.124.93.171
apt update
apt install -y awscli gnupg curl
```

Проверьте версию `aws --version` — должна быть 1.18+ или 2.x.

### Шаг 4: Сгенерировать пароль для шифрования .env

```bash
# На любой машине — пароль для шифрования .env-бэкапов
openssl rand -base64 32
```

**Этот пароль СОХРАНИТЕ В МЕНЕДЖЕРЕ ПАРОЛЕЙ** (Bitwarden, KeePass, любой).
Без него восстановить `.env` из бэкапа будет невозможно.

### Шаг 5: Создать конфиг `/etc/finarch-backup.env`

```bash
sudo tee /etc/finarch-backup.env > /dev/null <<'EOF'
# Где лежит бот
BOT_DIR=/opt/bot

# Yandex Object Storage
S3_ENDPOINT=https://storage.yandexcloud.net
S3_REGION=ru-central1
S3_BUCKET=mondaycompany-backups
S3_ACCESS_KEY=<Access Key ID из шага 2>
S3_SECRET_KEY=<Secret Key из шага 2>

# Пароль для шифрования .env из шага 4
BACKUP_GPG_PASSPHRASE=<длинный пароль>

# Telegram-уведомления (можно использовать тот же бот, что и сам сервис)
ALERT_BOT_TOKEN=<токен бота>
ALERT_CHAT_ID=<ваш Telegram user_id>
EOF

sudo chmod 600 /etc/finarch-backup.env
sudo chown root:root /etc/finarch-backup.env
```

> ⚠️ Файл содержит секреты — `chmod 600` обязателен. Без этого любой
> пользователь сервера сможет прочитать ключи к бэкапам.

### Шаг 6: Скопировать скрипты на сервер

Скрипты лежат в репозитории в `scripts/`. После `git pull` на сервере:

```bash
cd /opt/bot
git pull origin claude/ready-to-work-N5I2b

# Копируем скрипты в /usr/local/bin (PATH)
sudo cp scripts/finarch-backup.sh /usr/local/bin/finarch-backup.sh
sudo cp scripts/finarch-restore.sh /usr/local/bin/finarch-restore.sh
sudo chmod +x /usr/local/bin/finarch-backup.sh /usr/local/bin/finarch-restore.sh

# Копируем systemd unit + timer
sudo cp scripts/systemd/finarch-backup.service /etc/systemd/system/
sudo cp scripts/systemd/finarch-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
```

### Шаг 7: Запустить вручную и проверить

```bash
sudo /usr/local/bin/finarch-backup.sh
```

Должны:
- Увидеть в логах строки `Data archive: 2.5K`, `Uploaded -> s3://...`
- Получить уведомление в Telegram «✅ Бэкап готов»
- Проверить в Яндекс-консоли что в бакете появилась папка `daily/<сегодня>/`

Если ошибка про aws cli credentials — перепроверьте `/etc/finarch-backup.env`.

### Шаг 8: Включить автозапуск каждый день в 03:00

```bash
sudo systemctl enable --now finarch-backup.timer

# Проверить расписание
sudo systemctl list-timers finarch-backup.timer
```

Должна быть строка вида:
```
NEXT                        LEFT       LAST  PASSED  UNIT                    ACTIVATES
Fri 2026-05-01 03:00:00 ... 12h left   -     -       finarch-backup.timer    finarch-backup.service
```

## Тест восстановления (рекомендуется делать раз в месяц)

```bash
# Список доступных бэкапов
sudo /usr/local/bin/finarch-restore.sh --list

# Восстановить последний daily в /tmp/finarch-restore-XXXX/
sudo /usr/local/bin/finarch-restore.sh

# Или конкретный
sudo /usr/local/bin/finarch-restore.sh daily/2026-04-30
```

Скрипт **не перетирает** текущие файлы — он только распакует во временную
папку. Вы сами решаете, копировать ли поверх продакшна.

## Реальное восстановление (после полной потери сервера)

```bash
# 1. На новом сервере подготовить окружение
apt install -y awscli gnupg curl
# (плюс зависимости бота: python3, venv и т.д.)

# 2. Создать /etc/finarch-backup.env с теми же ключами S3
#    (пароль GPG — из менеджера паролей)

# 3. Скачать и расшифровать
finarch-restore.sh daily/<нужная-дата>

# 4. Применить
systemctl stop finarch-bot 2>/dev/null
cp /tmp/finarch-restore-*/users.json /opt/bot/
cp /tmp/finarch-restore-*/payments.json /opt/bot/
cp /tmp/finarch-restore-*/monitoring.json /opt/bot/
cp -r /tmp/finarch-restore-*/metadata /opt/bot/
cp /tmp/finarch-restore-*/.env /opt/bot/
chmod 600 /opt/bot/.env

# 5. Запустить бот
systemctl start finarch-bot
journalctl -u finarch-bot -f
```

## Стоимость

При текущем размере данных бота (~единицы МБ) GFS-схема даст в бакете
~150-200 МБ суммарно. Yandex Object Storage по тарифу «Стандартное»:

- Хранение: 1,89 ₽/ГБ/месяц
- Запросы PUT: 0,4 ₽/1000 запросов
- Трафик внутри облака: бесплатно

**Итог:** ~2-5 ₽/месяц при текущем объёме данных. При росте до 100 МБ
JSON-данных — ~20-30 ₽/месяц.

## Безопасность

- ✅ `.env` шифруется AES-256 ДО загрузки в облако
- ✅ `/etc/finarch-backup.env` доступен только root (`chmod 600`)
- ✅ Бакет приватный, ключ доступа ограничен ролью storage.editor
- ✅ Telegram-уведомления о падении бэкапа через `OnFailure=` в systemd

**Что НЕ делает скрипт (и почему):**
- Не делает encryption-at-rest для data.tar.gz — там нет секретов,
  только бизнес-данные. Если хотите — раскомментируйте секцию
  шифрования всего архива в `finarch-backup.sh`.
- Не валидирует целостность скачанных архивов через checksum —
  S3 уже даёт MD5/ETag, при загрузке/скачивании aws-cli проверяет.

## Troubleshooting

### `Could not connect to the endpoint URL`
- Проверьте, что сервер имеет доступ к `storage.yandexcloud.net:443`
- `curl -I https://storage.yandexcloud.net` должен вернуть 200/403

### `An error occurred (InvalidAccessKeyId)`
- Перепроверьте `S3_ACCESS_KEY` и `S3_SECRET_KEY` в `/etc/finarch-backup.env`
- В Яндекс-консоли убедитесь что ключ не удалён

### `GPG шифрование .env упало`
- Установите `gpg`: `apt install -y gnupg`
- Проверьте, что в `/etc/finarch-backup.env` задан `BACKUP_GPG_PASSPHRASE`

### Telegram-уведомления не приходят
- Проверьте `ALERT_BOT_TOKEN` и `ALERT_CHAT_ID` в конфиге
- Отправьте боту любое сообщение в личке — нужно, чтобы он мог писать вам
- Проверьте логи: `journalctl -u finarch-backup -n 50`
