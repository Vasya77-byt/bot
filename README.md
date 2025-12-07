# Финансовый архитектор — Telegram-бот

Бот помогает с анализом компаний (ИНН/JSON), подготовкой КП (текст, PDF, PNG) и мок-данными СБИС.

## Быстрый старт
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export TG_API_ID=... TG_API_HASH=... TG_BOT_TOKEN=...
python main.py
```

### Docker Compose
```bash
docker-compose up
```
Использует `.env` и поднимает мок СБИС на `8081`.

## Команды бота
- `/start`, `/help` — приветствие + меню кнопок.
- `/menu` — показать меню.
- `/kp <pdf|png> <ИНН?>` — сгенерировать КП и отправить файл.

### Текстовые триггеры
- `дай заявку <ИНН>` — блок заявки.
- `дай предложение <ИНН>` — блок предложения.
- `mode=internal_analysis` — внутренний анализ.
- `mode=client_proposal` — КП.
- `кп pdf` / `кп png` — мгновенная генерация файлов.

## Кнопки меню
- Внутренний анализ, Коммерческое предложение.
- Дай заявку / Дай предложение.
- Пример JSON.
- Сгенерировать КП (PDF/PNG).

## Переменные окружения (основные)
```
TG_API_ID, TG_API_HASH, TG_BOT_TOKEN

SBIS_LOGIN, SBIS_PASSWORD, SBIS_API_KEY, SBIS_CLIENT_ID
SBIS_BASE_URL (по умолчанию боевой метод СБИС)
SBIS_TIMEOUT=10
SBIS_RETRIES=2
SBIS_RETRY_DELAY=1.0
SBIS_CACHE_TTL=300
SBIS_MOCK=false
SBIS_MOCK_PORT=8081

LOG_LEVEL=INFO
LOG_FORMAT=json|plain

SENTRY_DSN (опц.), SENTRY_ENV, SENTRY_RELEASE,
SENTRY_TRACES_SAMPLE_RATE, SENTRY_SEND_DEFAULT_PII,
SENTRY_MAX_BREADCRUMBS, SENTRY_ATTACH_STACKTRACE

# Хранилище файлов КП
STORAGE_DIR=storage
S3_BUCKET (опц.), S3_PREFIX, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY,
AWS_DEFAULT_REGION, AWS_ENDPOINT_URL

# Кеш/метаданные
CACHE_DIR=.cache
METADATA_DIR=metadata
```

### Хранилище и S3
- По умолчанию файлы КП сохраняются локально в `STORAGE_DIR`.
- Если задать `S3_BUCKET` (+ креды `AWS_*`, `S3_PREFIX` опционально, `AWS_ENDPOINT_URL` для кастомного совместимого S3), файлы будут загружаться в бакет; иначе просто пишутся локально.
- Кеш SBIS хранится в памяти и в файле `CACHE_DIR/sbis_cache.json`.
- Метаданные по сгенерированным КП пишутся в jsonl `METADATA_DIR/kp_metadata.jsonl` (время, имя файла, формат, ИНН, название).

## Логи и мониторинг
- Логи: stdout JSON или plain (`LOG_FORMAT`).
- Sentry: включается при наличии `SENTRY_DSN`, PII-скраббер включён.

## Тесты и линты
```bash
pip install -r requirements-dev.txt
make lint-python   # ruff + mypy
make test          # pytest
make lint-dockerfile  # hadolint
```

## Мок СБИС
- Запуск вручную: `python sbis_mock.py` (порт 8081).
- В compose поднимается как сервис `sbis-mock`.

## Генерация файлов
- PDF/PNG создаются в памяти; при отправке сохраняются в `STORAGE_DIR` и опционально загружаются в S3.
- Метаданные КП пишутся в `METADATA_DIR/kp_metadata.jsonl`.

