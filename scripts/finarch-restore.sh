#!/bin/bash
# Восстановление бота из бэкапа в Yandex Object Storage.
#
# Использование:
#   finarch-restore.sh                      # последний daily
#   finarch-restore.sh daily/2026-04-30     # конкретный бэкап
#   finarch-restore.sh --list               # показать что есть в бакете
#
# По умолчанию восстанавливает в /tmp/finarch-restore-* (НЕ перетирает
# текущие данные). Чтобы применить — скопируйте файлы вручную:
#   sudo systemctl stop finarch-bot
#   cp /tmp/finarch-restore-XXXX/users.json /opt/bot/
#   ...
#   sudo systemctl start finarch-bot
#
# Конфиг: тот же /etc/finarch-backup.env

set -euo pipefail

ENV_FILE="${FINARCH_BACKUP_ENV:-/etc/finarch-backup.env}"
if [[ -r "$ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
fi

: "${S3_ENDPOINT:?S3_ENDPOINT required}"
: "${S3_BUCKET:?S3_BUCKET required}"
: "${S3_ACCESS_KEY:?S3_ACCESS_KEY required}"
: "${S3_SECRET_KEY:?S3_SECRET_KEY required}"

export AWS_ACCESS_KEY_ID="$S3_ACCESS_KEY"
export AWS_SECRET_ACCESS_KEY="$S3_SECRET_KEY"
export AWS_DEFAULT_REGION="${S3_REGION:-ru-central1}"

AWS_S3="aws --endpoint-url=$S3_ENDPOINT s3"

# ── Режим --list ──
if [[ "${1:-}" == "--list" ]]; then
    echo "Доступные бэкапы в s3://$S3_BUCKET:"
    for tier in daily weekly monthly; do
        echo ""
        echo "── $tier ──"
        $AWS_S3 ls "s3://$S3_BUCKET/$tier/" 2>/dev/null \
            | awk '{print $2}' | sort -r | head -20
    done
    exit 0
fi

# ── Определяем какой бэкап восстанавливать ──
TARGET="${1:-}"
if [[ -z "$TARGET" ]]; then
    # Берём самый свежий daily
    TARGET="daily/$($AWS_S3 ls "s3://$S3_BUCKET/daily/" 2>/dev/null \
        | awk '{print $2}' | tr -d '/' | sort -r | head -1)"
    if [[ "$TARGET" == "daily/" ]]; then
        echo "Не найден ни один бэкап в daily/" >&2
        exit 1
    fi
fi

TARGET="${TARGET%/}"
echo "Восстанавливаю из: s3://$S3_BUCKET/$TARGET/"

RESTORE_DIR="$(mktemp -d -t finarch-restore-XXXXXX)"
echo "Целевая папка: $RESTORE_DIR"

# ── Скачиваем data ──
$AWS_S3 cp "s3://$S3_BUCKET/$TARGET/data.tar.gz" "$RESTORE_DIR/data.tar.gz" \
    || { echo "Не удалось скачать data.tar.gz" >&2; exit 1; }

tar -xzf "$RESTORE_DIR/data.tar.gz" -C "$RESTORE_DIR" \
    || { echo "Распаковка не удалась" >&2; exit 1; }
rm "$RESTORE_DIR/data.tar.gz"

# ── Скачиваем и расшифровываем secrets (если есть) ──
if $AWS_S3 ls "s3://$S3_BUCKET/$TARGET/secrets.env.gpg" >/dev/null 2>&1; then
    if [[ -z "${BACKUP_GPG_PASSPHRASE:-}" ]]; then
        echo ""
        echo "⚠️  Найден secrets.env.gpg, но BACKUP_GPG_PASSPHRASE не задан."
        echo "    Установите переменную в $ENV_FILE и запустите снова."
    else
        $AWS_S3 cp "s3://$S3_BUCKET/$TARGET/secrets.env.gpg" \
            "$RESTORE_DIR/secrets.env.gpg" \
            || { echo "Не удалось скачать secrets" >&2; exit 1; }
        echo "$BACKUP_GPG_PASSPHRASE" | gpg \
            --batch --yes --quiet \
            --passphrase-fd 0 \
            --decrypt \
            --output "$RESTORE_DIR/.env" \
            "$RESTORE_DIR/secrets.env.gpg" \
            || { echo "GPG расшифровка упала (неверный пароль?)" >&2; exit 1; }
        rm "$RESTORE_DIR/secrets.env.gpg"
        chmod 600 "$RESTORE_DIR/.env"
        echo "✅ .env расшифрован"
    fi
fi

echo ""
echo "✅ Готово. Файлы лежат в: $RESTORE_DIR"
echo ""
echo "Содержимое:"
ls -la "$RESTORE_DIR"
echo ""
echo "Применить (внимательно!):"
echo "  sudo systemctl stop finarch-bot"
echo "  sudo cp $RESTORE_DIR/*.json ${BOT_DIR:-/opt/bot}/"
echo "  sudo cp -r $RESTORE_DIR/metadata ${BOT_DIR:-/opt/bot}/  # если есть"
echo "  sudo cp $RESTORE_DIR/.env ${BOT_DIR:-/opt/bot}/        # если расшифрован"
echo "  sudo chown -R \$(stat -c '%U:%G' ${BOT_DIR:-/opt/bot}) ${BOT_DIR:-/opt/bot}/"
echo "  sudo systemctl start finarch-bot"
