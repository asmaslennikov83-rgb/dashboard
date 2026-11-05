"""Sales-funnel export. Workbook generation uses only the Python standard library."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from io import BytesIO
from time import monotonic
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

import httpx

from wb_client import MOSCOW_TZ, WildberriesAPIError

PERIODS = {30: "месяц", 21: "3 недели", 14: "2 недели", 7: "1 неделя"}


def period_dates(days, now=None):
    if days not in PERIODS:
        raise ValueError("Неизвестный период")
    end = (now or datetime.now(MOSCOW_TZ)).astimezone(MOSCOW_TZ).date() - timedelta(days=1)
    return end - timedelta(days=days - 1), end


class CartExporter:
    def __init__(self, analytics_token, content_token):
        self.tokens = {"analytics": analytics_token, "content": content_token}
        self.lock = asyncio.Lock()
        self.last = {}

    async def _post(self, client, kind, url, body):
        interval = 21 if kind == "analytics" else 0.65
        if kind in self.last:
            await asyncio.sleep(max(0, interval - (monotonic() - self.last[kind])))
        self.last[kind] = monotonic()
        try:
            response = await client.post(url, json=body, headers={"Authorization": self.tokens[kind]})
        except httpx.HTTPError as exc:
            raise WildberriesAPIError("Не удалось соединиться с WB. Попробуйте позже.") from exc
        if response.status_code in (401, 403):
            category = "Аналитика" if kind == "analytics" else "Контент"
            raise WildberriesAPIError(f"Проверьте токен выбранного кабинета: нужен доступ к категории «{category}».")
        if response.status_code == 429:
            raise WildberriesAPIError("WB ограничил частоту запросов. Повторите позже; для базовых токенов интервал может составлять 30 минут.")
        if response.status_code != 200:
            raise WildberriesAPIError(f"WB вернул HTTP {response.status_code}. Выгрузка не сформирована.")
        try:
            payload = response.json()
        except ValueError as exc:
            raise WildberriesAPIError("WB вернул некорректный JSON.") from exc
        if not isinstance(payload, dict) or payload.get("error"):
            raise WildberriesAPIError("WB вернул ошибку или неожиданный формат данных.")
        return payload

    async def rows(self, start, end):
        async with self.lock, httpx.AsyncClient(timeout=60) as client:
            products = {}
            offset = 0
            while True:
                payload = await self._post(client, "analytics", "https://seller-analytics-api.wildberries.ru/api/analytics/v3/sales-funnel/products", {
                    "selectedPeriod": {"start": start.isoformat(), "end": end.isoformat()},
                    "nmIds": [], "brandNames": [], "subjectIds": [], "tagIds": [],
                    "skipDeletedNm": False, "orderBy": {"field": "orderCount", "mode": "desc"},
                    "limit": 1000, "offset": offset,
                })
                page = payload.get("data", {}).get("products")
                if not isinstance(page, list):
                    raise WildberriesAPIError("В ответе WB отсутствует список товаров.")
                added = 0
                for item in page:
                    try:
                        product = item["product"]
                        nm = int(product["nmId"])
                        stats = item["statistic"]["selected"]
                        carts, orders = stats["cartCount"], stats["orderCount"]
                        if any(type(v) is not int or v < 0 for v in (carts, orders)):
                            raise ValueError("Invalid counts")
                        if nm not in products:
                            added += 1
                        products[nm] = (str(product["vendorCode"]), carts, orders)
                    except (KeyError, TypeError, ValueError) as exc:
                        raise WildberriesAPIError("WB вернул неполные показатели товара; файл не сформирован.") from exc
                if len(page) < 1000:
                    break
                if not added:
                    raise WildberriesAPIError("Пагинация WB не продвигается.")
                offset += len(page)
            if not products:
                return []
            barcodes = {}
            cursor = {"limit": 100}
            seen = set()
            while True:
                payload = await self._post(client, "content", "https://content-api.wildberries.ru/content/v2/get/cards/list", {
                    "settings": {"cursor": cursor, "filter": {"withPhoto": -1}},
                })
                cards = payload.get("cards")
                if not isinstance(cards, list):
                    raise WildberriesAPIError("В ответе WB отсутствует список карточек.")
                for card in cards:
                    nm = int(card["nmID"])
                    codes = barcodes.setdefault(nm, set())
                    for size in card.get("sizes", []):
                        codes.update(str(code) for code in size.get("skus", []) if code)
                next_cursor = payload.get("cursor", {})
                if len(cards) < 100:
                    break
                key = (next_cursor.get("updatedAt"), next_cursor.get("nmID"))
                if not all(key) or key in seen:
                    raise WildberriesAPIError("Некорректный курсор карточек WB.")
                seen.add(key)
                cursor = {"limit": 100, "updatedAt": key[0], "nmID": key[1]}
            # Missing/removed cards retain their metrics. Missing barcodes are explicit.
            return [("; ".join(sorted(barcodes.get(nm, []))) or "ШК не найден", *values)
                    for nm, values in sorted(products.items(), key=lambda item: (item[1][0].casefold(), item[0]))]


def make_xlsx(rows, cabinet, start, end):
    """Create an XLSX with literal string cells (including barcodes and seller codes)."""
    def text_cell(ref, value, style=0):
        value = "".join(c for c in str(value) if ord(c) >= 32 or c in "\t\n\r")
        if len(value) > 32767:
            raise ValueError("Значение превышает размер ячейки Excel.")
        return f'<c r="{ref}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{escape(value)}</t></is></c>'
    content = []
    for index, values in enumerate([
        [f"Кабинет: {cabinet} | {start:%d.%m.%Y} — {end:%d.%m.%Y}"],
        ["Показатели по товару. Все ШК перечислены вместе; без разбивки по размерам."],
        ["ШК", "Артикул продавца", "Добавили в корзину", "Заказали"],
        *rows,
    ], 1):
        cells = []
        for col, value in enumerate(values):
            ref = f'{chr(65 + col)}{index}'
            if index > 3 and col >= 2:
                cells.append(f'<c r="{ref}"><v>{int(value)}</v></c>')
            else:
                cells.append(text_cell(ref, value, 1 if index == 3 else 0))
        content.append(f'<row r="{index}">{"".join(cells)}</row>')
    sheet = ('<?xml version="1.0" encoding="UTF-8"?>'
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
             '<sheetViews><sheetView workbookViewId="0"><pane ySplit="3" topLeftCell="A4" state="frozen"/></sheetView></sheetViews>'
             '<cols><col min="1" max="1" width="48" customWidth="1"/><col min="2" max="2" width="35" customWidth="1"/>'
             '<col min="3" max="4" width="25" customWidth="1"/></cols>'
             f'<sheetData>{"".join(content)}</sheetData><autoFilter ref="A3:D{max(3, len(rows)+3)}"/>'
             '<mergeCells count="2"><mergeCell ref="A1:D1"/><mergeCell ref="A2:D2"/></mergeCells></worksheet>')
    files = {
        '[Content_Types].xml': '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>',
        '_rels/.rels': '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        'xl/workbook.xml': '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Воронка продаж" sheetId="1" r:id="rId1"/></sheets></workbook>',
        'xl/_rels/workbook.xml.rels': '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>',
        'xl/styles.xml': '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts><fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf/></cellStyleXfs><cellXfs count="2"><xf fontId="0" fillId="0" borderId="0" numFmtId="0" xfId="0" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf><xf fontId="1" fillId="0" borderId="0" numFmtId="0" xfId="0"/></cellXfs></styleSheet>',
        'xl/worksheets/sheet1.xml': sheet,
    }
    stream = BytesIO()
    with ZipFile(stream, 'w', ZIP_DEFLATED) as archive:
        for name, value in files.items():
            archive.writestr(name, value.encode('utf-8'))
    stream.seek(0)
    return stream
