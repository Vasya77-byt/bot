"""Клиент GigaChat (Sber) для ИИ-анализа компаний."""

import asyncio
import logging
import os
import uuid
from typing import Optional

import requests
import urllib3

# GigaChat использует сертификат Сбера, не входящий в стандартные CA
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger("financial-architect")

AUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
API_URL = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"


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
            f"Ты — эксперт по оценке благонадёжности российских компаний. "
            f"Твоя задача — помочь предпринимателю решить, безопасно ли начинать сотрудничество с этой компанией.\n\n"
            f"Данные компании:\n{company_info}\n\n"
            f"Составь структурированный анализ строго по следующему шаблону. "
            f"Каждый пункт — конкретно по данной компании, без общих фраз.\n\n"
            f"ИТОГОВАЯ ОЦЕНКА: [Надёжный партнёр / Требует осторожности / Высокий риск]\n\n"
            f"📊 ФИНАНСОВОЕ СОСТОЯНИЕ\n"
            f"Оцени конкретно: выручку, прибыль, рентабельность. "
            f"Укажи, что именно говорят цифры о финансовой устойчивости.\n\n"
            f"⚠️ ВЫЯВЛЕННЫЕ РИСКИ\n"
            f"Перечисли конкретные риски на основе данных (возраст, финансы, ОКВЭД, регион). "
            f"Каждый риск — отдельная строка с пояснением.\n\n"
            f"✅ ПОЛОЖИТЕЛЬНЫЕ ФАКТОРЫ\n"
            f"Перечисли конкретные сильные стороны из данных. "
            f"Каждый фактор — отдельная строка с пояснением.\n\n"
            f"📋 РЕКОМЕНДАЦИИ К РАБОТЕ\n"
            f"Дай 2-3 конкретных совета: какие документы запросить, "
            f"на что обратить внимание при заключении договора, "
            f"какие условия прописать для снижения рисков.\n\n"
            f"Пиши чётко, без воды. Используй только факты из предоставленных данных."
        )

        return await asyncio.to_thread(self._chat, prompt)
