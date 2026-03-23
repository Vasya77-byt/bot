#!/bin/bash
# =============================================================
# Скрипт развертывания INN Checker Bot на Ubuntu VPS
# Запускать от root: bash deploy.sh
# =============================================================

set -e

BOT_USER="botuser"
BOT_DIR="/home/$BOT_USER/inn_checker_bot"
SERVICE_NAME="inn-checker-bot"

echo "========================================="
echo "  Развертывание INN Checker Bot"
echo "========================================="

# 1. Обновление системы
echo ""
echo "[1/7] Обновление системы..."
apt update -qq && apt upgrade -y -qq

# 2. Установка Python и зависимостей
echo "[2/7] Установка Python 3.12..."
apt install -y -qq python3 python3-venv python3-pip curl

# 3. Создание пользователя для бота
echo "[3/7] Создание пользователя $BOT_USER..."
if id "$BOT_USER" &>/dev/null; then
    echo "  Пользователь $BOT_USER уже существует"
else
    useradd -m -s /bin/bash "$BOT_USER"
    echo "  Пользователь $BOT_USER создан"
fi

# 4. Копирование файлов бота
echo "[4/7] Копирование файлов бота..."
mkdir -p "$BOT_DIR"
cp config.py main.py dadata_client.py open_sources.py report_formatter.py \
   risk_scoring.py validators.py requirements.txt "$BOT_DIR/"

# Копируем .env если он есть, иначе создаем из примера
if [ -f .env ]; then
    cp .env "$BOT_DIR/.env"
    echo "  .env скопирован"
else
    cp .env.example "$BOT_DIR/.env"
    echo "  ВНИМАНИЕ: создан .env из шаблона — заполните ключи!"
    echo "  nano $BOT_DIR/.env"
fi

chown -R "$BOT_USER:$BOT_USER" "$BOT_DIR"

# 5. Создание виртуального окружения и установка зависимостей
echo "[5/7] Установка Python-зависимостей..."
sudo -u "$BOT_USER" bash -c "
    cd $BOT_DIR
    python3 -m venv venv
    source venv/bin/activate
    pip install --upgrade pip -q
    pip install -r requirements.txt -q
"
echo "  Зависимости установлены"

# 6. Установка systemd-сервиса
echo "[6/7] Настройка systemd-сервиса..."
cat > /etc/systemd/system/${SERVICE_NAME}.service << 'EOF'
[Unit]
Description=INN Checker Telegram Bot
After=network.target

[Service]
Type=simple
User=botuser
WorkingDirectory=/home/botuser/inn_checker_bot
ExecStart=/home/botuser/inn_checker_bot/venv/bin/python main.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
StandardOutput=journal
StandardError=journal
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

# 7. Проверка статуса
echo "[7/7] Проверка..."
sleep 3
if systemctl is-active --quiet "$SERVICE_NAME"; then
    echo ""
    echo "========================================="
    echo "  ГОТОВО! Бот запущен и работает"
    echo "========================================="
    echo ""
    echo "Полезные команды:"
    echo "  Статус:       systemctl status $SERVICE_NAME"
    echo "  Логи:         journalctl -u $SERVICE_NAME -f"
    echo "  Перезапуск:   systemctl restart $SERVICE_NAME"
    echo "  Остановка:    systemctl stop $SERVICE_NAME"
    echo "  Редактировать ключи: nano $BOT_DIR/.env"
    echo ""
else
    echo ""
    echo "ОШИБКА: бот не запустился!"
    echo "Смотри логи: journalctl -u $SERVICE_NAME -n 30"
    exit 1
fi
