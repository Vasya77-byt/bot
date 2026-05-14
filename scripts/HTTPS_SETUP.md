# Развёртывание HTTPS на сервере

Подключаем `fridaycompanybot.ru` к webhook-серверу бота через nginx +
Let's Encrypt. Это нужно для приёма webhook'ов от ЮKassa и Точки —
оба провайдера слать на HTTP отказываются.

## Архитектура

```
[ЮKassa / Точка / Пользователь]
            │
            │  HTTPS :443
            ▼
   ┌────────────────────┐
   │      nginx         │  TLS-терминирование, доступ к /yookassa/...
   │  fridaycompanybot.ru│  и /tochka/webhook, /report/{token}, /health
   └─────────┬──────────┘
             │ HTTP 127.0.0.1:8080
             ▼
   ┌────────────────────┐
   │  finarch-bot       │  aiohttp webhook-сервер внутри Python-процесса
   │  (Python aiohttp)  │  Слушает ТОЛЬКО на 127.0.0.1, недоступен извне
   └────────────────────┘
```

Все 9 шагов ниже выполняются на сервере по SSH **под root**.

---

## Шаг 1. DNS на reg.ru — направить домен на IP сервера

> ⚠️ Выполняется в браузере на сайте reg.ru, **не на сервере**.

1. Зайти в личный кабинет: https://www.reg.ru/user/account/
2. Найти домен `fridaycompanybot.ru` → **Управление доменом** → **DNS-серверы и DNS**
3. **Добавить запись:**
   - Тип: **A**
   - Имя: `@` (или оставить пусто — это «корень домена»)
   - Значение: `<IP_СЕРВЕРА>` (например `89.124.93.171`)
   - TTL: `3600` (или по умолчанию)
4. Дополнительно: чтобы `www.fridaycompanybot.ru` тоже работал, добавьте:
   - Тип: **A**
   - Имя: `www`
   - Значение: тот же IP
5. **Сохранить**

### Проверка DNS

Подождите 5-30 минут, потом из любой точки:

```bash
# На своём компьютере или сервере
dig +short fridaycompanybot.ru
```

Должно вернуть IP сервера. Если пусто — DNS ещё распространяется,
подождите ещё. Можно также проверить через https://dnschecker.org/

**Не переходите к Шагу 2 пока DNS не отдаёт правильный IP.**

---

## Шаг 2. SSH-подключение к серверу

```bash
ssh root@<IP_СЕРВЕРА>
```

Все следующие команды — в этой SSH-сессии.

---

## Шаг 3. Установить nginx

```bash
apt update
apt install -y nginx
systemctl enable --now nginx
systemctl status nginx | head -5
```

Должно быть `active (running)`.

Проверка снаружи (в браузере): откройте `http://<IP_СЕРВЕРА>` — должна
показаться приветственная страница nginx («Welcome to nginx!»).

Если страница не открывается — проблема с firewall, см. Шаг 4.

---

## Шаг 4. Открыть порты 80 и 443 в firewall

```bash
ufw status
```

Если firewall активен (status: active) — открываем:

```bash
ufw allow 80/tcp
ufw allow 443/tcp
ufw status
```

Должна быть строка `22/tcp ALLOW` (SSH) и `80/tcp`, `443/tcp` тоже ALLOW.

> Если ufw неактивен (status: inactive) — это нормально для VDSina,
> там обычно firewall выключен и доступ управляется на уровне облака.
> Тогда ничего делать не нужно.

---

## Шаг 5. Положить nginx-конфиг сайта

```bash
cd /opt/bot
git pull origin claude/ready-to-work-N5I2b
```

Скопируйте шаблон:

```bash
cp /opt/bot/scripts/nginx/fridaycompanybot.ru.conf \
   /etc/nginx/sites-available/fridaycompanybot.ru

ln -sf /etc/nginx/sites-available/fridaycompanybot.ru \
       /etc/nginx/sites-enabled/fridaycompanybot.ru
```

Удалите дефолтный конфиг (он перехватывает все запросы):

```bash
rm -f /etc/nginx/sites-enabled/default
```

Проверьте конфиг и перезагрузите:

```bash
nginx -t                  # должно вывести "syntax is ok" + "test is successful"
systemctl reload nginx
```

### Проверка

В браузере откройте `http://fridaycompanybot.ru/health` — пока вернёт
ошибку 502 Bad Gateway, потому что бот ещё не слушает на 127.0.0.1:8080.
Это нормально, исправим в Шаге 8.

---

## Шаг 6. Установить certbot

```bash
apt install -y certbot python3-certbot-nginx
```

---

## Шаг 7. Получить SSL-сертификат от Let's Encrypt

```bash
certbot --nginx \
        -d fridaycompanybot.ru \
        -d www.fridaycompanybot.ru \
        --email <ваш_email> \
        --agree-tos \
        --no-eff-email \
        --redirect
```

Замените `<ваш_email>` на реальный (туда придут уведомления об истечении
сертификата за 30 дней).

Что произойдёт:
- Certbot временно изменит nginx-конфиг для ACME-проверки
- Получит сертификат через Let's Encrypt (валидность 90 дней)
- Положит его в `/etc/letsencrypt/live/fridaycompanybot.ru/`
- Допишет SSL-блок в `/etc/nginx/sites-available/fridaycompanybot.ru`
- Настроит редирект `HTTP → HTTPS` (флаг `--redirect`)
- Перезагрузит nginx

В конце увидите примерно:
```
Successfully deployed certificate for fridaycompanybot.ru
https://fridaycompanybot.ru
```

### Проверка авто-обновления

```bash
certbot renew --dry-run
```

Должно вернуть `Congratulations, all simulated renewals succeeded`.

Certbot автоматически создаёт systemd timer `certbot.timer` — он
обновляет сертификаты дважды в день за 30 дней до истечения.
Проверить: `systemctl list-timers certbot.timer`.

---

## Шаг 8. Бот слушает только локально

По умолчанию `WEBHOOK_HOST=0.0.0.0` — это значит порт 8080 открыт во
внешний мир. С появлением nginx это не нужно и небезопасно. Переключаем:

```bash
# Открываем .env бота — путь зависит от вашей установки.
# Обычно /opt/bot/.env или /etc/finarch-bot.env
sudo nano /opt/bot/.env
```

Найдите строку `WEBHOOK_HOST=0.0.0.0` и поменяйте на:

```
WEBHOOK_HOST=127.0.0.1
WEBHOOK_PORT=8080
WEBHOOK_PUBLIC_URL=https://fridaycompanybot.ru/tochka/webhook
```

Если строки `WEBHOOK_HOST` нет — просто добавьте.

Сохраните (Ctrl+O Enter, Ctrl+X) и перезапустите бота:

```bash
sudo systemctl restart finarch-bot
sleep 2
sudo journalctl -u finarch-bot -n 20 --no-pager
```

В логах должно появиться:
```
Webhook server listening on 127.0.0.1:8080
```

Проверим что порт слушается только локально:

```bash
ss -tlnp | grep 8080
```

Должно вывести строку с `127.0.0.1:8080`, **не** `0.0.0.0:8080`.

---

## Шаг 9. Тест HTTPS из интернета

С любой машины (вашего компьютера):

```bash
# Health-check — самый простой тест
curl https://fridaycompanybot.ru/health
# Ожидаем: {"status":"ok"}

# Webhook ЮKassa с чужим IP — должен вернуть 403 forbidden
curl -X POST https://fridaycompanybot.ru/yookassa/webhook \
     -H "Content-Type: application/json" \
     -d '{"event":"payment.succeeded","object":{}}'
# Ожидаем: {"error":"forbidden"}

# Webhook Точки без подписи — должен вернуть 403
curl -X POST https://fridaycompanybot.ru/tochka/webhook -d "not-a-jwt"
# Ожидаем: {"error":"bad signature"}
```

Все три проверки прошли — **HTTPS готов**.

В браузере: https://fridaycompanybot.ru/health — должен показать JSON
с зелёным замком в адресной строке.

---

## Что дальше

Теперь у вас есть рабочий HTTPS на `https://fridaycompanybot.ru`.

### Регистрация webhook ЮKassa

Возвращайтесь к [YOOKASSA.md → Шаг 3](../YOOKASSA.md), пункт «Webhook»:
1. В личном кабинете ЮKassa → Настройки → HTTP-уведомления
2. Добавить URL: `https://fridaycompanybot.ru/yookassa/webhook`
3. События: `payment.succeeded`, `payment.canceled`, `refund.succeeded`
4. (Опционально) включить Y-Signature и сохранить секрет в `.env`

### Регистрация webhook Точки

Если в дальнейшем понадобится — можно автоматически из API:

```bash
python3 -c "
import asyncio
from settings import Settings
from tochka_client import TochkaClient

async def main():
    s = Settings.from_env()
    c = TochkaClient(s.tochka_jwt, s.tochka_customer_code,
                     client_id=s.tochka_client_id)
    ok = await c.register_webhook(
        url='https://fridaycompanybot.ru/tochka/webhook',
        events=['acquiringInternetPayment'],
    )
    print('OK' if ok else 'FAILED')

asyncio.run(main())
"
```

---

## Troubleshooting

### `Failed authorization procedure ... unauthorized`

Certbot не смог дотянуться до сервера по 80 порту:
- Проверьте DNS: `dig +short fridaycompanybot.ru` должен показывать IP
- Откройте 80 порт: `ufw allow 80/tcp`
- Проверьте что nginx запущен: `systemctl status nginx`
- На уровне VDSina — нет ли защиты в панели хостера

### `502 Bad Gateway` при заходе на https://fridaycompanybot.ru/health

nginx работает, но не достучался до бота на 127.0.0.1:8080:
- `systemctl status finarch-bot` — бот запущен?
- `ss -tlnp | grep 8080` — порт слушается?
- В `.env` проверьте `WEBHOOK_HOST=127.0.0.1` (не `localhost` — иногда IPv6-резолв ломается)
- Логи nginx: `tail -50 /var/log/nginx/fridaycompanybot.ru.error.log`

### Webhook ЮKassa возвращает 403 forbidden даже на боевые webhook'и

nginx не пробрасывает `X-Forwarded-For`:
- Проверьте конфиг: `grep X-Forwarded-For /etc/nginx/sites-available/fridaycompanybot.ru`
- Должна быть строка `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;`
- Если потерялась после `certbot --nginx` — восстановите из `/opt/bot/scripts/nginx/fridaycompanybot.ru.conf`

### `curl: (60) SSL certificate problem`

Это значит у клиента нет корневых сертификатов Let's Encrypt
(старая система). Игнорировать `-k` нельзя — это укажет на реальную
проблему. Обновите ca-certificates: `apt install --reinstall ca-certificates`.

### Сертификат истёк через 90 дней

Этого не должно случиться — `certbot.timer` обновляет автоматически.
Если всё-таки случилось:
- `systemctl status certbot.timer` — таймер активен?
- Ручное обновление: `certbot renew`
- Логи: `journalctl -u certbot.service`
