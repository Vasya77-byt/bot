"""Клиент GigaChat (Sber) для ИИ-анализа компаний."""

import asyncio
import logging
import os
import re
import uuid
from typing import Optional

import requests
import urllib3

# GigaChat использует сертификат Сбера, не входящий в стандартные CA
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger("financial-architect")

AUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
API_URL = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"


# Маппинг текстового вердикта → эмодзи. Используется при рендере итога.
_VERDICT_EMOJI = [
    ("критич", "🔴"),
    ("высок",  "🟠"),
    ("осторож", "🟡"),
    ("безопас", "🟢"),
    ("надёж",   "🟢"),
    ("надеж",   "🟢"),
]


def _verdict_emoji(verdict: str) -> str:
    v = (verdict or "").lower()
    for needle, emoji in _VERDICT_EMOJI:
        if needle in v:
            return emoji
    return "⚪"


class GigaChatClient:
    def __init__(self) -> None:
        self.credentials = os.getenv("GIGACHAT_CREDENTIALS", "")
        self.timeout = float(os.getenv("GIGACHAT_TIMEOUT", "30"))
        self._token: Optional[str] = None

    def _get_token(self) -> Optional[str]:
        if not self.credentials:
            return None
        try:
            resp = requests.post(
                AUTH_URL,
                headers={
                    "Authorization": f"Basic {self.credentials}",
                    "RqUID": str(uuid.uuid4()),
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={"scope": "GIGACHAT_API_PERS"},
                timeout=self.timeout,
                verify=False,
            )
            resp.raise_for_status()
            self._token = resp.json().get("access_token")
            return self._token
        except Exception as exc:
            logger.error("GigaChat auth failed: %s", exc)
            return None

    def _chat(self, prompt: str) -> Optional[str]:
        token = self._get_token()
        if not token:
            return None
        try:
            resp = requests.post(
                API_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "GigaChat",
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.7,
                    "max_tokens": 1000,
                },
                timeout=self.timeout,
                verify=False,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        except Exception as exc:
            logger.error("GigaChat request failed: %s", exc)
            return None

    async def help_user(self, user_text: str) -> Optional[str]:
        """Помощник для непонятных запросов. Получает текст пользователя
        (например, неизвестную команду или вопрос вроде «как проверить
        компанию?») и возвращает короткий помогающий ответ
        (3-5 строк), либо None если не настроен GigaChat.
        """
        if not self.credentials or not user_text or not user_text.strip():
            return None

        prompt = (
            "Ты помощник в Telegram-боте MondayCompany — это сервис "
            "проверки контрагентов и физлиц по российским реестрам.\n\n"
            "Что умеет бот:\n"
            "• Проверка компании по ИНН или названию (отчёт со скорингом)\n"
            "• Под отчётом кнопки: ⚖️ Суды, 📊 Финансы, 🤖 ИИ-анализ, "
            "🏛 ЕГРЮЛ, 📜 История, 🔗 Связи, 👁 Отслеживать, 📄 Скачать PDF\n"
            "• /menu — главное меню\n"
            "• /referral — реферальная программа\n"
            "• /documents — правовые документы (оферта, политика)\n"
            "• «👤 Профиль» (внизу) — статус подписки + «Мои компании»\n"
            "• «💎 Тарифы» (внизу) — оплата подписки\n"
            "• Отмена автопродления — кнопка в карточке профиля\n\n"
            f"Пользователь написал: «{user_text}»\n\n"
            "Ответь дружелюбно, кратко (3-5 строк, без markdown). "
            "Если запрос про проверку компании или функцию бота — "
            "подскажи конкретный шаг или команду. Если запрос не "
            "по теме сервиса — вежливо скажи, что бот помогает только "
            "с проверкой компаний и предложи начать с ввода ИНН или "
            "названия. Не выдумывай функции которых нет в списке выше."
        )

        return await asyncio.to_thread(self._chat, prompt)

    async def analyze_company(
        self,
        name: str,
        inn: str,
        okved: Optional[str] = None,
        okved_name: Optional[str] = None,
        age_years: Optional[int] = None,
        revenue: Optional[float] = None,
        profit: Optional[float] = None,
        employees: Optional[int] = None,
        region: Optional[str] = None,
        status: Optional[str] = None,
    ) -> Optional[str]:
        if not self.credentials:
            return None

        def fmt_money(v: Optional[float]) -> str:
            if not v:
                return "нет данных"
            if v >= 1_000_000_000:
                return f"{v / 1_000_000_000:.1f} млрд ₽"
            if v >= 1_000_000:
                return f"{v / 1_000_000:.1f} млн ₽"
            if v >= 1_000:
                return f"{v / 1_000:.0f} тыс ₽"
            return f"{v:.0f} ₽"

        parts = [
            f"Название: {name}",
            f"ИНН: {inn}",
            f"Статус: {status or 'нет данных'}",
        ]
        if okved:
            parts.append(f"ОКВЭД: {okved}{f' — {okved_name}' if okved_name else ''}")
        parts += [
            f"Возраст компании: {f'{age_years} лет' if age_years else 'нет данных'}",
            f"Регион: {region or 'нет данных'}",
            f"Численность персонала: {f'{employees} чел.' if employees else 'нет данных'}",
            f"Выручка (последний год): {fmt_money(revenue)}",
            f"Чистая прибыль (последний год): {fmt_money(profit)}",
        ]
        if revenue and profit:
            margin = (profit / revenue * 100) if revenue > 0 else 0
            parts.append(f"Рентабельность: {margin:.1f}%")

        company_info = "\n".join(parts)

        prompt = (
            "Ты эксперт по оценке надёжности российских контрагентов. "
            "По данным компании дай краткий вердикт.\n\n"
            f"Данные из реестров ФНС/DaData:\n{company_info}\n\n"
            "Если реестровые данные не соответствуют реальному масштабу "
            "компании (например, у крупного банка в выписке указано одно "
            "юрлицо группы) — учти это в анализе.\n\n"
            "Ответь СТРОГО в этом формате — одна строка на каждое поле, "
            "без markdown, без нумерации, без пустых строк:\n\n"
            "ВЫВОД: <одно из: безопасно | осторожно | высокий риск | критический>\n"
            "ПЛЮСЫ: <2-4 факта через запятую>\n"
            "РИСКИ: <2-4 факта через запятую>\n"
            "РЕКОМЕНДАЦИЯ: <1-2 конкретных действия через запятую>"
        )

        raw = await asyncio.to_thread(self._chat, prompt)
        if raw is None:
            return None
        return _format_analysis(raw)


def _parse_analysis(text: str) -> dict:
    """Разбирает ответ модели на 4 секции. Возвращает dict с ключами
    verdict, pluses, risks, recommendation. Отсутствующие — пустые строки."""
    out = {"verdict": "", "pluses": "", "risks": "", "recommendation": ""}
    labels = [
        ("verdict",        "ВЫВОД"),
        ("pluses",         "ПЛЮСЫ"),
        ("risks",          "РИСКИ"),
        ("recommendation", "РЕКОМЕНДАЦИЯ"),
    ]
    # Один проход: для каждой метки находим её значение до следующей метки.
    for i, (key, label) in enumerate(labels):
        # До следующей метки или конца строки
        next_labels = [lbl for _, lbl in labels[i + 1:]]
        stop = "|".join(next_labels) or r"\Z"
        pattern = rf"{label}\s*:\s*(.+?)(?=\n\s*(?:{stop})\s*:|\Z)"
        m = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
        if m:
            out[key] = m.group(1).strip().rstrip(".")
    return out


def _format_analysis(raw: str) -> str:
    """Парсит и собирает блок в желаемом формате с эмодзи.
    Если структура не распарсилась — возвращает сырой текст."""
    parsed = _parse_analysis(raw)
    if not (parsed["verdict"] or parsed["risks"] or parsed["pluses"]):
        # Парсинг не удался — возвращаем как есть
        return raw.strip()

    emoji = _verdict_emoji(parsed["verdict"])
    lines = [f"{emoji} ВЫВОД: {parsed['verdict'] or '—'}", ""]
    if parsed["pluses"]:
        lines.append(f"✅ Плюсы: {parsed['pluses']}")
        lines.append("")
    if parsed["risks"]:
        lines.append(f"⚠️ Риски: {parsed['risks']}")
        lines.append("")
    if parsed["recommendation"]:
        lines.append(f"💡 РЕКОМЕНДАЦИЯ: {parsed['recommendation']}")
    return "\n".join(lines).rstrip()
