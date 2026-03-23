#!/bin/bash
# ═══════════════════════════════════════════════
# СКРИПТ БЕЗОПАСНОСТИ СЕРВЕРА
# Для @Fridaycompany_bot
# ═══════════════════════════════════════════════
#
# Что делает:
# 1. Настраивает UFW файрвол (разрешает только SSH)
# 2. Устанавливает fail2ban (блокирует брутфорс SSH)
# 3. Создаёт swap файл (1 ГБ)
# 4. Устанавливает автообновления безопасности
# 5. Закрывает Zabbix порт
# 6. Настраивает права на файлы бота
# 7. Настраивает SSH безопасность (меняет порт на 2222)
#
# Запуск: sudo bash secure_server.sh
#

set -e

echo "═══════════════════════════════════════"
echo "  НАСТРОЙКА БЕЗОПАСНОСТИ СЕРВЕРА"
echo "═══════════════════════════════════════"

# ─── 1. UFW ФАЙРВОЛ ───
echo ""
echo "[1/7] Настраиваю файрвол (UFW)..."
apt-get update -qq
apt-get install -y ufw

# Правила: разрешаем только SSH (стандартный + новый порт)
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp comment 'SSH standard'
ufw allow 2222/tcp comment 'SSH new port'

# Включаем UFW
echo "y" | ufw enable
ufw status verbose
echo "✅ Файрвол настроен"

# ─── 2. FAIL2BAN ───
echo ""
echo "[2/7] Устанавливаю fail2ban..."
apt-get install -y fail2ban

# Конфигурация
cat > /etc/fail2ban/jail.local << 'JAILEOF'
[DEFAULT]
bantime = 3600
findtime = 600
maxretry = 5

[sshd]
enabled = true
port = 22,2222
filter = sshd
logpath = /var/log/auth.log
maxretry = 3
bantime = 7200
JAILEOF

systemctl enable fail2ban
systemctl restart fail2ban
echo "✅ fail2ban установлен и настроен"

# ─── 3. SWAP (1 ГБ) ───
echo ""
echo "[3/7] Настраиваю swap..."
if [ ! -f /swapfile ]; then
    fallocate -l 1G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
    # Оптимизация swap
    sysctl vm.swappiness=10
    echo 'vm.swappiness=10' >> /etc/sysctl.conf
    echo "✅ Swap 1 ГБ создан"
else
    echo "⏩ Swap уже существует"
fi
free -h

# ─── 4. АВТООБНОВЛЕНИЯ БЕЗОПАСНОСТИ ───
echo ""
echo "[4/7] Настраиваю автообновления..."
apt-get install -y unattended-upgrades
dpkg-reconfigure -plow unattended-upgrades 2>/dev/null || true

cat > /etc/apt/apt.conf.d/20auto-upgrades << 'AUTOEOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
AUTOEOF

echo "✅ Автообновления настроены"

# ─── 5. ЗАКРЫВАЕМ ZABBIX ───
echo ""
echo "[5/7] Закрываю Zabbix порт..."
systemctl stop zabbix-agent 2>/dev/null || true
systemctl disable zabbix-agent 2>/dev/null || true
echo "✅ Zabbix agent остановлен"

# ─── 6. ПРАВА НА ФАЙЛЫ БОТА ───
echo ""
echo "[6/7] Настраиваю права на файлы..."
BOT_DIR="/root/inn_checker_bot"
if [ -d "$BOT_DIR" ]; then
    chmod 600 "$BOT_DIR/.env"
    chmod 700 "$BOT_DIR/data" 2>/dev/null || true
    echo "✅ Права настроены (.env: 600, data/: 700)"
fi

# ─── 7. SSH БЕЗОПАСНОСТЬ ───
echo ""
echo "[7/7] Усиливаю SSH..."
# Добавляем порт 2222
if ! grep -q "^Port 2222" /etc/ssh/sshd_config; then
    echo "Port 2222" >> /etc/ssh/sshd_config
fi

# Ограничиваем попытки входа
if ! grep -q "^MaxAuthTries" /etc/ssh/sshd_config; then
    echo "MaxAuthTries 3" >> /etc/ssh/sshd_config
fi

# Отключаем пустые пароли
sed -i 's/#PermitEmptyPasswords.*/PermitEmptyPasswords no/' /etc/ssh/sshd_config

# Логирование
sed -i 's/#LogLevel.*/LogLevel VERBOSE/' /etc/ssh/sshd_config

systemctl restart sshd
echo "✅ SSH усилен (доп. порт 2222, макс. 3 попытки)"

# ─── ИТОГИ ───
echo ""
echo "═══════════════════════════════════════"
echo "  ✅ БЕЗОПАСНОСТЬ НАСТРОЕНА"
echo "═══════════════════════════════════════"
echo ""
echo "Сводка:"
echo "  ✅ UFW файрвол включён (SSH only)"
echo "  ✅ fail2ban: бан после 3 попыток на 2 часа"
echo "  ✅ Swap: 1 ГБ"
echo "  ✅ Автообновления безопасности"
echo "  ✅ Zabbix закрыт"
echo "  ✅ .env файл защищён (chmod 600)"
echo "  ✅ SSH: доп. порт 2222, макс. 3 попытки"
echo ""
echo "⚠️  ВАЖНО: SSH теперь доступен на портах 22 И 2222"
echo "   После проверки порта 2222 можно закрыть 22:"
echo "   ufw delete allow 22/tcp"
echo ""
