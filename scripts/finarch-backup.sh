#!/bin/bash
# Бэкап критичных данных бота в Yandex Object Storage.
#
# Что бэкапится:
#   - users.json, payments.json, monitoring.json, report_tokens.json
#   - metadata/ (директория)
#   - .env (отдельно, симметричное GPG-шифрование)
#
# Куда: S3-совместимый бакет (Yandex Object Storage / Selectel S3 / любой).
#
# Структура в бакете (GFS retention):
#   daily/YYYY-MM-DD/data.tar.gz
#   daily/YYYY-MM-DD/secrets.gpg
#   weekly/YYYY-Www/...   (только по воскресеньям)
#   monthly/YYYY-MM/...   (только 1 числа)
#
# Старые бэкапы удаляются автоматически:
#   daily   старше  7 дней
#   weekly  старше  4 недель
#   monthly старше  6 месяцев
#
# Конфиг: /etc/finarch-backup.env (см. scripts/BACKUP.md)
#   BOT_DIR=/opt/bot
#   S3_ENDPOINT=https://storage.yandexcloud.net
#   S3_BUCKET=mondaycompany-backups
#   S3_ACCESS_KEY=...
#   S3_SECRET_KEY=...
#   BACKUP_GPG_PASSPHRASE=...   # для шифрования .env
#   ALERT_BOT_TOKEN=...          # для уведомлений в Telegram
#   ALERT_CHAT_ID=...
#
# Зависимости: awscli (apt install awscli), gnupg, tar, curl
#
# Запуск вручную:
#   /usr/local/bin/finarch-backup.sh
#
# Запуск из systemd: см. scripts/systemd/finarch-backup.{service,timer}

set -euo pipefail

ENV_FILE="${FINARCH_BACKUP_ENV:-/etc/finarch-backup.env}"
if [[ -r "$ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
fi

: "${BOT_DIR:?BOT_DIR required (например /opt/bot)}"
: "${S3_ENDPOINT:?S3_ENDPOINT required}"
: "${S3_BUCKET:?S3_BUCKET required}"
: "${S3_ACCESS_KEY:?S3_ACCESS_KEY required}"
: "${S3_SECRET_KEY:?S3_SECRET_KEY required}"
: "${BACKUP_GPG_PASSPHRASE:?BACKUP_GPG_PASSPHRASE required}"

# Параметры aws cli для S3-совместимого хранилища
export AWS_ACCESS_KEY_ID="$S3_ACCESS_KEY"
export AWS_SECRET_ACCESS_KEY="$S3_SECRET_KEY"
export AWS_DEFAULT_REGION="${S3_REGION:-ru-central1}"

AWS_S3="aws --endpoint-url=$S3_ENDPOINT s3"
AWS_S3API="aws --endpoint-url=$S3_ENDPOINT s3api"

TIMESTAMP="$(date '+%Y-%m-%d_%H%M%S')"
DATE_DAILY="$(date '+%Y-%m-%d')"
DATE_WEEKLY="$(date '+%Y-W%V')"
DATE_MONTHLY="$(date '+%Y-%m')"
DAY_OF_WEEK="$(date '+%u')"  # 1=пн, 7=вс
DAY_OF_MONTH="$(date '+%d')"

HOST="$(hostname)"
LOG_PREFIX="[$(date '+%Y-%m-%d %H:%M:%S')]"

notify() {
    # Шлёт сообщение в Telegram. Молча падает если токен не настроен.
    local text="$1"
    [[ -z "${ALERT_BOT_TOKEN:-}" || -z "${ALERT_CHAT_ID:-}" ]] && return 0
    curl -fsS --max-time 15 \
        -X POST "https://api.telegram.org/bot${ALERT_BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${ALERT_CHAT_ID}" \
        --data-urlencode "text=${text}" \
        -o /dev/null 2>/dev/null || true
}

fail() {
    local err="$1"
    echo "$LOG_PREFIX ERROR: $err" >&2
    notify "🚨 Бэкап провалился ($HOST)
$err"
    exit 1
}

echo "$LOG_PREFIX Starting backup for $BOT_DIR"

# ──────────────────────────────────────────────────────────────────────
# 1. Создаём временную директорию и собираем архив с данными
# ──────────────────────────────────────────────────────────────────────

WORK_DIR="$(mktemp -d -t finarch-backup-XXXXXX)"
trap 'rm -rf "$WORK_DIR"' EXIT

DATA_FILES=()
for f in users.json payments.json monitoring.json report_tokens.json; do
    [[ -f "$BOT_DIR/$f" ]] && DATA_FILES+=("$f")
done

if [[ -d "$BOT_DIR/metadata" ]]; then
    DATA_FILES+=("metadata")
fi

if [[ ${#DATA_FILES[@]} -eq 0 ]]; then
    fail "В $BOT_DIR нет файлов для бэкапа (users.json и т.п.)"
fi

DATA_ARCHIVE="$WORK_DIR/data-$TIMESTAMP.tar.gz"
tar -czf "$DATA_ARCHIVE" -C "$BOT_DIR" "${DATA_FILES[@]}" \
    || fail "Не удалось создать tar-архив"

DATA_SIZE="$(du -h "$DATA_ARCHIVE" | cut -f1)"
echo "$LOG_PREFIX Data archive: $DATA_SIZE ($DATA_ARCHIVE)"

# ──────────────────────────────────────────────────────────────────────
# 2. Шифруем .env (если есть) симметричным GPG
# ──────────────────────────────────────────────────────────────────────

SECRETS_ARCHIVE=""
if [[ -f "$BOT_DIR/.env" ]]; then
    SECRETS_ARCHIVE="$WORK_DIR/secrets-$TIMESTAMP.env.gpg"
    echo "$BACKUP_GPG_PASSPHRASE" | gpg \
        --batch --yes --quiet \
        --passphrase-fd 0 \
        --symmetric --cipher-algo AES256 \
        --output "$SECRETS_ARCHIVE" \
        "$BOT_DIR/.env" \
        || fail "GPG шифрование .env упало"
    echo "$LOG_PREFIX Secrets encrypted: $SECRETS_ARCHIVE"
else
    echo "$LOG_PREFIX .env не найден в $BOT_DIR, пропускаю секреты"
fi

# ──────────────────────────────────────────────────────────────────────
# 3. Загружаем в S3 по схеме daily / weekly / monthly
# ──────────────────────────────────────────────────────────────────────

upload_to() {
    # Загружает оба архива (data + secrets) в указанный префикс бакета.
    local prefix="$1"
    local dest_data="s3://$S3_BUCKET/$prefix/data.tar.gz"
    local dest_secrets="s3://$S3_BUCKET/$prefix/secrets.env.gpg"

    $AWS_S3 cp "$DATA_ARCHIVE" "$dest_data" --only-show-errors \
        || fail "Загрузка $dest_data провалилась"
    if [[ -n "$SECRETS_ARCHIVE" ]]; then
        $AWS_S3 cp "$SECRETS_ARCHIVE" "$dest_secrets" --only-show-errors \
            || fail "Загрузка $dest_secrets провалилась"
    fi
    echo "$LOG_PREFIX Uploaded -> s3://$S3_BUCKET/$prefix/"
}

UPLOADED_TIERS=("daily/$DATE_DAILY")
upload_to "daily/$DATE_DAILY"

# По воскресеньям — копия в weekly/
if [[ "$DAY_OF_WEEK" == "7" ]]; then
    upload_to "weekly/$DATE_WEEKLY"
    UPLOADED_TIERS+=("weekly/$DATE_WEEKLY")
fi

# Первого числа — копия в monthly/
if [[ "$DAY_OF_MONTH" == "01" ]]; then
    upload_to "monthly/$DATE_MONTHLY"
    UPLOADED_TIERS+=("monthly/$DATE_MONTHLY")
fi

# ──────────────────────────────────────────────────────────────────────
# 4. Чистим старые бэкапы (GFS retention)
# ──────────────────────────────────────────────────────────────────────

cleanup_prefix() {
    # Удаляет из бакета все объекты под $prefix/, у которых дата
    # в имени старше $cutoff_iso (YYYY-MM-DD или YYYY-MM).
    local prefix="$1"
    local cutoff="$2"
    local removed=0

    # Листим все папки первого уровня под prefix
    local subprefixes
    subprefixes="$($AWS_S3API list-objects-v2 \
        --bucket "$S3_BUCKET" \
        --prefix "$prefix/" \
        --delimiter "/" \
        --query 'CommonPrefixes[].Prefix' \
        --output text 2>/dev/null || true)"

    [[ -z "$subprefixes" || "$subprefixes" == "None" ]] && return 0

    for sp in $subprefixes; do
        # из "daily/2026-04-15/" вытаскиваем "2026-04-15"
        local id
        id="$(basename "$sp")"
        if [[ "$id" < "$cutoff" ]]; then
            $AWS_S3 rm "s3://$S3_BUCKET/$sp" --recursive --only-show-errors \
                >/dev/null 2>&1 || true
            echo "$LOG_PREFIX Removed old: $sp"
            removed=$((removed + 1))
        fi
    done
    echo "$LOG_PREFIX Cleanup $prefix/: removed $removed"
}

CUTOFF_DAILY="$(date -d '7 days ago' '+%Y-%m-%d')"
CUTOFF_WEEKLY="$(date -d '28 days ago' '+%Y-W%V')"
CUTOFF_MONTHLY="$(date -d '6 months ago' '+%Y-%m')"

cleanup_prefix "daily" "$CUTOFF_DAILY"
cleanup_prefix "weekly" "$CUTOFF_WEEKLY"
cleanup_prefix "monthly" "$CUTOFF_MONTHLY"

# ──────────────────────────────────────────────────────────────────────
# 5. Успех — уведомляем в Telegram
# ──────────────────────────────────────────────────────────────────────

TIERS_TEXT="$(printf ' • %s\n' "${UPLOADED_TIERS[@]}")"

echo "$LOG_PREFIX Backup completed successfully"
notify "✅ Бэкап готов ($HOST)
Размер data: $DATA_SIZE
Загружено в:
$TIERS_TEXT"
