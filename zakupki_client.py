"""Клиент к данным госзакупок (FZ-44 + FZ-223) — прямое обогащение.

Получает то, чего нет в ZCHB-card: список последних контрактов с
деталями (заказчик, сумма, дата, реестровый номер) и независимую
проверку реестра недобросовестных поставщиков (РНП).

Не заменяет ZCHB (там нужны другие поля), а **дополняет** Full-отчёт
новой фичей «📦 Топ-N контрактов» + второй источник для РНП.

Источник данных конфигурируется через env:
- ZAKUPKI_API_URL    — базовый URL API
- ZAKUPKI_API_KEY    — ключ авторизации (опц., зависит от провайдера)
- ZAKUPKI_TIMEOUT    — таймаут HTTP, секунды (default 15)

В качестве источника подходит:
- damia.ru (REST API для FZ-44/223)
- Открытое API ЕИС (требует регистрации ФКС-ключа)
- Любой агрегатор, отдающий JSON с контрактами по ИНН

Если ENV не настроены — клиент молча возвращает пустые результаты,
graceful degradation: UI показывает «данные недоступны» без падения.

Cross-user persistent cache: 24ч (популярные ИНН тянутся 1 раз/день).
api_quota tracking: запросы учитываются в счётчике 'zakupki'.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests

from api_quota import ApiQuotaExhausted, get_quota
from cache import FileTTLCache

logger = logging.getLogger("financial-architect")


@dataclass
class Contract:
    """Один контракт, нормализованный из ответа провайдера."""
    reg_number: str = ""          # реестровый номер контракта
    customer_name: str = ""       # заказчик
    customer_inn: str = ""
    sum_rub: float = 0.0
    signed_at: str = ""           # ISO-дата подписания
    status: str = ""              # «исполнен», «расторгнут», «действует»
    law: str = ""                 # «44-ФЗ» / «223-ФЗ»
    subject: str = ""             # предмет контракта (опц., короткое описание)


@dataclass
class ZakupkiStats:
    """Агрегированные данные по контрактам — для UI и risk score."""
    contracts: List[Contract] = field(default_factory=list)
    total_count: int = 0          # всего контрактов как поставщик
    total_sum: float = 0.0        # сумма всех контрактов
    is_in_rnp: Optional[bool] = None  # None = не проверяли (API недоступна)
    rnp_reason: str = ""          # если есть — причина включения в РНП


class ZakupkiClient:
    """Клиент к API госзакупок.

    Использование: один экземпляр на процесс. При незаданном API URL
    методы тихо возвращают пустоту — graceful degradation для развёртки
    без подключённого источника.
    """

    def __init__(self) -> None:
        self.api_url = os.getenv("ZAKUPKI_API_URL", "").rstrip("/")
        self.api_key = os.getenv("ZAKUPKI_API_KEY", "")
        self.timeout = float(os.getenv("ZAKUPKI_TIMEOUT", "15"))
        ttl = float(os.getenv("ZAKUPKI_CACHE_TTL", str(24 * 3600)))
        self._cache = FileTTLCache("zakupki", ttl=ttl)

    @property
    def enabled(self) -> bool:
        """True если можно реально дёргать API. False — fallback на пусто."""
        return bool(self.api_url)

    async def get_stats(self, inn: str, top_n: int = 5) -> ZakupkiStats:
        """Возвращает агрегированную сводку контрактов и флаг РНП.

        Кэш cross-user 24ч. Если API не настроен или вернул ошибку —
        возвращаем пустой ZakupkiStats (UI обработает как «нет данных»).
        """
        if not self.enabled:
            return ZakupkiStats()

        cache_key = f"stats:{inn}:{top_n}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return _stats_from_dict(cached)

        try:
            get_quota().check("zakupki")
        except ApiQuotaExhausted as exc:
            logger.warning("Zakupki skipped: %s", exc)
            return ZakupkiStats()

        raw = await asyncio.to_thread(self._fetch_raw, inn, top_n)
        if raw is None:
            return ZakupkiStats()

        get_quota().record("zakupki")
        stats = self._parse_stats(raw, top_n=top_n)
        self._cache.set(cache_key, _stats_to_dict(stats))
        return stats

    def _fetch_raw(self, inn: str, top_n: int) -> Optional[Dict[str, Any]]:
        """Сетевой вызов. Реализация зависит от провайдера — см. enum
        ZAKUPKI_PROVIDER (planned для будущего). Сейчас — generic GET
        с параметрами inn и limit; формат ответа должен соответствовать
        контракту _parse_stats (см. ниже).

        Возвращает dict с ответом или None при ошибке (сетевой error / 5xx).
        """
        url = f"{self.api_url}/contracts"
        params = {"inn": inn, "limit": top_n}
        if self.api_key:
            params["api_key"] = self.api_key
        try:
            resp = requests.get(url, params=params, timeout=self.timeout)
            if resp.status_code == 200:
                return resp.json()
            logger.warning(
                "Zakupki HTTP %s: %s", resp.status_code, resp.text[:200],
            )
        except requests.RequestException as exc:
            logger.warning("Zakupki request failed: %s", exc)
        except ValueError as exc:
            logger.warning("Zakupki invalid JSON: %s", exc)
        return None

    def _parse_stats(self, raw: Dict[str, Any], top_n: int) -> ZakupkiStats:
        """Парсинг ответа API в ZakupkiStats.

        Ожидаемый формат ответа (контракт между нашим кодом и
        провайдером — параметры именования провайдера должны быть
        смаплены сюда):

        {
            "total_count": int,           # всего контрактов
            "total_sum": float,           # сумма всех (₽)
            "is_in_rnp": bool,            # в реестре недобросовестных
            "rnp_reason": str (опц.),     # причина включения
            "contracts": [                # топ-N последних
                {
                    "reg_number": str,
                    "customer_name": str,
                    "customer_inn": str,
                    "sum_rub": float,
                    "signed_at": str (ISO),
                    "status": str,
                    "law": "44" | "223",
                    "subject": str (опц.),
                },
                ...
            ]
        }

        При несоответствии формата — best-effort: возвращаем то, что
        смогли распарсить, остальное оставляем пустым.
        """
        contracts: List[Contract] = []
        for item in (raw.get("contracts") or [])[:top_n]:
            if not isinstance(item, dict):
                continue
            law = str(item.get("law") or "")
            # Нормализация: «44» / «223» → «44-ФЗ» / «223-ФЗ»
            if law in ("44", "223"):
                law = f"{law}-ФЗ"
            try:
                sum_rub = float(item.get("sum_rub") or 0)
            except (TypeError, ValueError):
                sum_rub = 0.0
            contracts.append(Contract(
                reg_number=str(item.get("reg_number") or ""),
                customer_name=str(item.get("customer_name") or ""),
                customer_inn=str(item.get("customer_inn") or ""),
                sum_rub=sum_rub,
                signed_at=str(item.get("signed_at") or ""),
                status=str(item.get("status") or ""),
                law=law,
                subject=str(item.get("subject") or ""),
            ))

        try:
            total_count = int(raw.get("total_count") or 0)
        except (TypeError, ValueError):
            total_count = 0
        try:
            total_sum = float(raw.get("total_sum") or 0)
        except (TypeError, ValueError):
            total_sum = 0.0

        rnp_raw = raw.get("is_in_rnp")
        # Принимаем bool/int/None. Любое не-True/1 → False (явно), кроме None.
        if rnp_raw is None:
            is_in_rnp: Optional[bool] = None
        else:
            is_in_rnp = bool(rnp_raw)

        return ZakupkiStats(
            contracts=contracts,
            total_count=total_count,
            total_sum=total_sum,
            is_in_rnp=is_in_rnp,
            rnp_reason=str(raw.get("rnp_reason") or ""),
        )


def _stats_to_dict(stats: ZakupkiStats) -> Dict[str, Any]:
    """Сериализация для кэша."""
    return {
        "total_count": stats.total_count,
        "total_sum": stats.total_sum,
        "is_in_rnp": stats.is_in_rnp,
        "rnp_reason": stats.rnp_reason,
        "contracts": [
            {
                "reg_number": c.reg_number,
                "customer_name": c.customer_name,
                "customer_inn": c.customer_inn,
                "sum_rub": c.sum_rub,
                "signed_at": c.signed_at,
                "status": c.status,
                "law": c.law,
                "subject": c.subject,
            }
            for c in stats.contracts
        ],
    }


def _stats_from_dict(data: Dict[str, Any]) -> ZakupkiStats:
    """Десериализация из кэша."""
    contracts = [
        Contract(**c) for c in (data.get("contracts") or []) if isinstance(c, dict)
    ]
    return ZakupkiStats(
        contracts=contracts,
        total_count=int(data.get("total_count") or 0),
        total_sum=float(data.get("total_sum") or 0),
        is_in_rnp=data.get("is_in_rnp"),
        rnp_reason=str(data.get("rnp_reason") or ""),
    )


def format_stats_message(stats: ZakupkiStats, inn: str) -> str:
    """Человеко-читаемое сообщение для Telegram (новая кнопка «📦 Контракты»).

    Если API не настроен и stats пустой — возвращает информативный
    fallback. Если данные есть — формирует богатый блок с топ-N.
    """
    if not stats.contracts and stats.total_count == 0 and stats.is_in_rnp is None:
        return (
            "📦 Контракты по госзакупкам\n\n"
            "Данные временно недоступны или контрактов не найдено."
        )

    lines = [
        f"📦 Госзакупки (ИНН {inn})",
        "",
    ]
    if stats.total_count > 0:
        sum_str = (
            f"{stats.total_sum:,.0f} ₽".replace(",", " ")
            if stats.total_sum > 0 else "—"
        )
        lines.append(
            f"Всего контрактов как поставщик: {stats.total_count}",
        )
        lines.append(f"Общая сумма: {sum_str}")
        lines.append("")

    if stats.is_in_rnp is True:
        marker = "🔴 В реестре недобросовестных поставщиков"
        if stats.rnp_reason:
            marker += f": {stats.rnp_reason}"
        lines.append(marker)
        lines.append("")
    elif stats.is_in_rnp is False:
        lines.append("✅ В РНП не значится")
        lines.append("")

    if stats.contracts:
        lines.append("Последние контракты:")
        for i, c in enumerate(stats.contracts[:5], start=1):
            sum_str = (
                f"{c.sum_rub:,.0f} ₽".replace(",", " ")
                if c.sum_rub > 0 else "—"
            )
            date_str = c.signed_at[:10] if c.signed_at else "—"
            law_str = c.law or "—"
            customer = c.customer_name or "заказчик не указан"
            lines.append(
                f"{i}. {date_str} · {law_str} · {sum_str}\n   {customer}",
            )
            if c.status:
                lines[-1] += f"\n   Статус: {c.status}"

    return "\n".join(lines)
