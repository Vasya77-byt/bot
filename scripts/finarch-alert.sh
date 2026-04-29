#!/bin/bash
# Шлёт алерт в Telegram о падении systemd-сервиса.
# Вызывается из OnFailure= в unit-файле.
#
# Конфиг: /etc/finarch-alert.env
#   ALERT_BOT_TOKEN=<bot token>
#   ALERT_CHAT_ID=<telegram user id>
#
# Использование: finarch-alert.sh <unit_name>

set -euo pipefail

ENV_FILE="/etc/finarch-alert.env"
if [[ -r "$ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
fi

: "${ALERT_BOT_TOKEN:?ALERT_BOT_TOKEN required in $ENV_FILE}"
: "${ALERT_CHAT_ID:?ALERT_CHAT_ID required in $ENV_FILE}"

UNIT="${1:-unknown}"
HOST="$(hostname)"
TIMESTAMP="$(date '+%Y-%m-%d %H:%M:%S %Z')"
LOG_TAIL="$(journalctl -u "$UNIT" -n 20 --no-pager 2>/dev/null | tail -c 2500 || echo 'logs unavailable')"

# Telegram Markdown ломается на спецсимволах в логах — шлём plain text.
MESSAGE=$(cat <<EOF
🚨 Сервис упал: $UNIT
🖥 Хост: $HOST
⏰ Время: $TIMESTAMP

Последние строки лога:
$LOG_TAIL
EOF
)

curl -fsS --max-time 15 \
    -X POST "https://api.telegram.org/bot${ALERT_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${ALERT_CHAT_ID}" \
    --data-urlencode "text=${MESSAGE}" \
    -o /dev/null || echo "Failed to send alert to Telegram" >&2
