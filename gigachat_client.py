"""Клиент GigaChat (Sber) для ИИ-анализа компаний."""

import asyncio
import logging
import os
import uuid
from typing import Optional

import requests

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

        parts = [f"Компания: {name}", f"ИНН: {inn}"]
        if status:
            parts.append(f"Статус: {status}")
        if okved:
            okved_str = okved
            if okved_name:
                okved_str += f" ({okved_name})"
            parts.append(f"ОКВЭД: {okved_str}")
        if age_years:
            parts.append(f"Возраст: {age_years} лет")
        if region:
            parts.append(f"Регион: {region}")
        if employees:
            parts.append(f"Сотрудников: {employees}")
        if revenue:
            parts.append(f"Выручка: {revenue:,.0f} ₽")
        if profit:
            parts.append(f"Прибыль: {profit:,.0f} ₽")

        company_info = "\n".join(parts)

        prompt = (
            f"Проведи краткий деловой анализ российской компании по следующим данным:\n\n"
            f"{company_info}\n\n"
            f"Структура ответа:\n"
            f"1. Общая оценка компании (2-3 предложения)\n"
            f"2. Основные риски (2-3 пункта)\n"
            f"3. Сильные стороны (2-3 пункта)\n"
            f"4. Рекомендации по работе (1-2 пункта)\n\n"
            f"Отвечай кратко и по делу, без лишних вводных слов."
        )

        return await asyncio.to_thread(self._chat, prompt)
