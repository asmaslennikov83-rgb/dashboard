import asyncio
import os
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import httpx

# Test credentials never connect to Telegram or WB.
for key, value in {
    "TELEGRAM_BOT_TOKEN": "123456:test",
    "TELEGRAM_ALLOWED_USER_IDS": "123,456",
    "WB_TOKEN_1": "test-one", "WB_TOKEN_2": "test-two",
    "WB_NAME_1": "One", "WB_NAME_2": "Two",
}.items():
    os.environ[key] = value

import bot
from wb_client import MOSCOW_TZ, WildberriesClient, WildberriesAPIError


def order(srid, article, date, **extra):
    return dict(srid=srid, supplierArticle=article, date=date,
                finishedPrice=100, priceWithDisc=120, totalPrice=150,
                warehouseType="Склад WB", **extra)


class TrendTests(unittest.TestCase):
    def test_fifteen_percent_threshold_and_zero(self):
        for today, yesterday, expected in [
            (11, 12, "🟡➡️"), (10, 12, "🔴⬇️"), (14, 12, "🟢⬆️"),
            (17, 20, "🟡➡️"), (23, 20, "🟡➡️"), (16, 20, "🔴⬇️"),
            (24, 20, "🟢⬆️"), (1, 0, "🟢⬆️"), (0, 0, "🟡➡️"),
            (0, 1, "🔴⬇️"), (12, 12, "🟡➡️"),
        ]:
            with self.subTest(today=today, yesterday=yesterday):
                self.assertEqual(bot.order_trend_arrow(today, yesterday), expected)


class ReportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = WildberriesClient("test", "Cabinet")
        self.now = datetime(2026, 10, 15, 14, 0, tzinfo=MOSCOW_TZ)

    async def asyncTearDown(self):
        await self.client.close()

    async def report(self, today, yesterday, sales=(), price="finishedPrice"):
        async def rows(kind, *, as_of, yesterday=False):
            self.assertEqual(as_of, self.now)
            return list(sales) if kind == "sales" else list(previous if yesterday else current)
        current, previous = today, yesterday
        with patch.object(self.client, "_today_moscow", return_value=self.now), \
             patch.object(self.client, "_get_day_rows", side_effect=rows):
            return await self.client.build_daily_report(price)

    async def test_same_time_cutoff_duplicates_cancellations_and_totals(self):
        a = order("a", "A<&", "2026-10-15T14:00:00", isCancel=True)
        b = order("b", "B", "2026-10-15T10:00:00")
        b["warehouseType"] = "Склад продавца"
        today = [a, a.copy(), b, order("later", "A<&", "2026-10-15T14:00:01")]
        yesterday = [
            order("y1", "A<&", "2026-10-14T14:00:00"),
            order("y1", "A<&", "2026-10-14T14:00:00"),
            order("y2", "A<&", "2026-10-14T11:00:00"),
            order("late", "B", "2026-10-14T14:00:01"),
            order("old", "B", "2026-10-13T12:00:00"),
        ]
        sale = dict(saleID="S1", date="2026-10-15T10:00:00", priceWithDisc=80)
        sales = [sale, sale.copy(), dict(saleID="R1", date=sale["date"], finishedPrice=50)]
        for price, amount in [("finishedPrice", 200), ("priceWithDisc", 240), ("totalPrice", 300)]:
            report = await self.report(today, yesterday, sales, price)
            self.assertEqual(report.orders_by_article, {"A<&": 1, "B": 1})
            self.assertEqual(report.yesterday_orders_by_article, {"A<&": 2})
            self.assertEqual((report.ordered_total, report.ordered_fbo, report.ordered_fbs), (2, 1, 1))
            self.assertEqual(report.ordered_sum, Decimal(amount))
            self.assertEqual((report.bought_total, report.bought_sum), (1, Decimal(80)))
            text = bot.format_report("Cab<&", report)
            self.assertIn("A&lt;&amp; — <b>1</b> шт. 🔴⬇️", text)
            self.assertIn("B — <b>1</b> шт. 🟢⬆️", text)
            self.assertIn("15.10.2026", text)

    async def test_moscow_conversion_and_month_boundary(self):
        self.now = datetime(2026, 11, 1, 0, 0, tzinfo=MOSCOW_TZ)
        report = await self.report(
            [order("a", "A", "2026-10-31T21:00:00Z")],
            [order("b", "A", "2026-10-30T21:00:00Z"),
             order("c", "A", "2026-10-31T00:00:01")],
        )
        self.assertEqual(report.orders_by_article, {"A": 1})
        self.assertEqual(report.yesterday_orders_by_article, {"A": 1})
        self.assertIn("🟡➡️", bot.format_report("One", report))

    async def test_empty_today_and_no_price_fallback(self):
        report = await self.report([], [order("y", "Only yesterday", "2026-10-14T10:00:00")])
        self.assertEqual(report.ordered_total, 0)
        self.assertNotIn("Only yesterday", bot.format_report("One", report))
        row = order("a", "A", "2026-10-15T10:00:00")
        row["finishedPrice"] = None
        report = await self.report([row], [])
        self.assertEqual(report.ordered_sum, Decimal(0))

    async def test_day_cache_rate_limit_and_error_isolation(self):
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(200, json=[{"day": request.url.params["dateFrom"]}])
        await self.client._client.aclose()
        self.client._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        # Preserve absolute URLs because the real client normally has base_url.
        self.client._client.base_url = "https://statistics-api.wildberries.ru"
        with patch("wb_client.asyncio.sleep", new_callable=AsyncMock) as sleep:
            current = await self.client._get_day_rows("orders", as_of=self.now)
            previous = await self.client._get_day_rows("orders", as_of=self.now, yesterday=True)
            cached = await self.client._get_day_rows("orders", as_of=self.now)
            self.assertEqual(current, [{"day": "2026-10-15"}])
            self.assertEqual(previous, [{"day": "2026-10-14"}])
            self.assertEqual(cached, current)
            self.assertEqual(len(requests), 2)
            self.assertEqual(requests[0].url.params["flag"], "1")
            self.assertEqual(sleep.await_count, 1)
            self.assertGreater(sleep.await_args.args[0], 60)

            await self.client._client.aclose()
            self.client._client = httpx.AsyncClient(
                base_url="https://statistics-api.wildberries.ru",
                transport=httpx.MockTransport(lambda request: httpx.Response(429)),
            )
            with self.assertRaises(WildberriesAPIError):
                await self.client._get_day_rows("orders", as_of=self.now + timedelta(days=1))

    async def test_separate_cabinets(self):
        other = WildberriesClient("other", "Other")
        try:
            self.client._cache[("orders", "2026-10-15")] = (self.now, [{"supplierArticle": "A"}])
            self.assertEqual(other._cache, {})
        finally:
            await other.close()


if __name__ == "__main__":
    unittest.main()
