from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx


log = logging.getLogger(__name__)

STATISTICS_API = "https://statistics-api.wildberries.ru"
MOSCOW_TZ = ZoneInfo("Europe/Moscow")


class WildberriesAPIError(RuntimeError):
    pass


@dataclass(slots=True)
class DailyReport:
    ordered_total: int
    bought_total: int
    orders_by_article: dict[str, int]


class WildberriesClient:
    """Minimal async client for WB Statistics API."""

    def __init__(self, token: str, name: str, timeout: float = 30.0) -> None:
        self.token = token.strip()
        self.name = name.strip()
        self._client = httpx.AsyncClient(
            base_url=STATISTICS_API,
            timeout=httpx.Timeout(timeout),
            headers={"Authorization": self.token},
        )
        # WB limits these report methods to 1 request/minute per seller account.
        # A short cache prevents /report immediately after the scheduled run
        # from causing an unnecessary 429.
        self._cache: dict[str, tuple[datetime, list[dict[str, Any]]]] = {}
        self._locks = {
            "orders": asyncio.Lock(),
            "sales": asyncio.Lock(),
        }

    async def close(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _today_moscow() -> datetime:
        return datetime.now(MOSCOW_TZ)

    @staticmethod
    def _parse_wb_dt(value: str | None) -> datetime | None:
        if not value:
            return None
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if dt.tzinfo is None:
            # WB Statistics API documents date/time for these methods in Moscow time.
            dt = dt.replace(tzinfo=MOSCOW_TZ)
        return dt.astimezone(MOSCOW_TZ)

    async def _get_day_rows(self, kind: str, *, force: bool = False) -> list[dict[str, Any]]:
        if kind not in {"orders", "sales"}:
            raise ValueError(f"Unsupported kind: {kind}")

        async with self._locks[kind]:
            now = datetime.now(MOSCOW_TZ)
            cached = self._cache.get(kind)
            if not force and cached and now - cached[0] < timedelta(seconds=65):
                return cached[1]

            today = now.date().isoformat()
            endpoint = f"/api/v1/supplier/{kind}"
            params = {"dateFrom": today, "flag": 1}

            try:
                response = await self._client.get(endpoint, params=params)
            except httpx.HTTPError as exc:
                raise WildberriesAPIError(
                    f"{self.name}: ошибка соединения с WB API: {exc}"
                ) from exc

            if response.status_code == 429:
                # If WB rate-limits us and we have recent data, use it rather than
                # failing the entire Telegram report.
                if cached:
                    log.warning("%s: WB returned 429 for %s, using cached data", self.name, kind)
                    return cached[1]
                raise WildberriesAPIError(
                    f"{self.name}: WB API временно ограничил частоту запросов (429)."
                )

            if response.status_code != 200:
                body = response.text[:500]
                raise WildberriesAPIError(
                    f"{self.name}: WB API {kind} вернул HTTP {response.status_code}: {body}"
                )

            try:
                payload = response.json()
            except ValueError as exc:
                raise WildberriesAPIError(
                    f"{self.name}: WB API вернул некорректный JSON для {kind}."
                ) from exc

            if not isinstance(payload, list):
                raise WildberriesAPIError(
                    f"{self.name}: неожиданный формат ответа WB API для {kind}."
                )

            rows = [row for row in payload if isinstance(row, dict)]
            self._cache[kind] = (now, rows)
            return rows

    async def build_daily_report(self) -> DailyReport:
        today = self._today_moscow().date()

        orders_rows, sales_rows = await asyncio.gather(
            self._get_day_rows("orders"),
            self._get_day_rows("sales"),
        )

        # Orders: count every order created today, including orders later cancelled.
        # Deduplicate by srid where available, because WB documents it as order identifier.
        order_seen: set[str] = set()
        orders_by_article: Counter[str] = Counter()
        ordered_total = 0

        for index, row in enumerate(orders_rows):
            dt = self._parse_wb_dt(str(row.get("date") or ""))
            if dt is not None and dt.date() != today:
                continue

            srid = str(row.get("srid") or "").strip()
            unique_key = srid or f"row:{index}:{row.get('gNumber')}:{row.get('nmId')}:{row.get('date')}"
            if unique_key in order_seen:
                continue
            order_seen.add(unique_key)

            article = str(row.get("supplierArticle") or "Без артикула").strip() or "Без артикула"
            ordered_total += 1
            orders_by_article[article] += 1

        # Sales endpoint contains both sales and returns. Real sales use saleID starting with S.
        sale_seen: set[str] = set()
        bought_total = 0
        for index, row in enumerate(sales_rows):
            dt = self._parse_wb_dt(str(row.get("date") or ""))
            if dt is not None and dt.date() != today:
                continue

            sale_id = str(row.get("saleID") or "").strip()
            if not sale_id.startswith("S"):
                continue

            unique_key = sale_id or f"sale:{index}:{row.get('srid')}:{row.get('nmId')}:{row.get('date')}"
            if unique_key in sale_seen:
                continue
            sale_seen.add(unique_key)
            bought_total += 1

        return DailyReport(
            ordered_total=ordered_total,
            bought_total=bought_total,
            orders_by_article=dict(orders_by_article),
        )
