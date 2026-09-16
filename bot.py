from __future__ import annotations

import asyncio
import html
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from wb_client import DailyReport, WildberriesAPIError, WildberriesClient


load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("wb_sales_bot")

MOSCOW_TZ = ZoneInfo("Europe/Moscow")


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Не заполнена обязательная переменная окружения: {name}")
    return value


TELEGRAM_BOT_TOKEN = required_env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = int(required_env("TELEGRAM_CHAT_ID"))


def parse_allowed_user_ids(raw: str) -> set[int]:
    ids: set[int] = set()
    for item in raw.replace(";", ",").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            ids.add(int(item))
        except ValueError as exc:
            raise RuntimeError(
                "TELEGRAM_ALLOWED_USER_IDS должен содержать Telegram ID через запятую"
            ) from exc
    if not ids:
        raise RuntimeError("Не заполнена обязательная переменная TELEGRAM_ALLOWED_USER_IDS")
    return ids


TELEGRAM_ALLOWED_USER_IDS = parse_allowed_user_ids(
    required_env("TELEGRAM_ALLOWED_USER_IDS")
)

CABINETS = [
    WildberriesClient(required_env("WB_TOKEN_1"), required_env("WB_NAME_1")),
    WildberriesClient(required_env("WB_TOKEN_2"), required_env("WB_NAME_2")),
]


def format_money(value) -> str:
    # 12 345,67 ₽; whole rubles are shown without kopecks.
    value = value.quantize(__import__("decimal").Decimal("0.01"))
    if value == value.to_integral():
        return f"{int(value):,}".replace(",", " ") + " ₽"
    rubles = f"{value:,.2f}".replace(",", " ").replace(".", ",")
    return rubles + " ₽"


def format_report(cabinet_name: str, report: DailyReport) -> str:
    now = datetime.now(MOSCOW_TZ)
    lines = [
        f"<b>Заказы за {now:%d.%m.%Y}</b>",
        f"Кабинет: <b>{html.escape(cabinet_name)}</b>",
        "",
        f"Заказано итого: <b>{report.ordered_total} шт.</b>",
        f"Сумма заказов: <b>{format_money(report.ordered_sum)}</b>",
        f"Выкуплено итого: <b>{report.bought_total} шт.</b>",
        f"Сумма выкупов: <b>{format_money(report.bought_sum)}</b>",
    ]

    if report.orders_by_article:
        lines.append("")
        # Sort by order count descending, then alphabetically for stable output.
        for article, qty in sorted(
            report.orders_by_article.items(), key=lambda item: (-item[1], item[0].lower())
        ):
            lines.append(f"{html.escape(article)} — <b>{qty}</b> шт.")
    else:
        lines.extend(["", "Заказов по артикулам нет."])

    return "\n".join(lines)


async def send_all_reports(application: Application) -> None:
    """Send one independent Telegram message for each WB cabinet."""
    for cabinet in CABINETS:
        try:
            report = await cabinet.build_daily_report()
            await application.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=format_report(cabinet.name, report),
                parse_mode=ParseMode.HTML,
            )
        except WildberriesAPIError as exc:
            log.exception("WB API error for cabinet %s", cabinet.name)
            await application.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=(
                    f"⚠️ <b>Не удалось получить отчёт</b>\n"
                    f"Кабинет: <b>{html.escape(cabinet.name)}</b>\n"
                    f"{html.escape(str(exc))}"
                ),
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:  # one cabinet must never block the other one
            log.exception("Unexpected error for cabinet %s", cabinet.name)
            await application.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=(
                    f"⚠️ <b>Ошибка отчёта</b>\n"
                    f"Кабинет: <b>{html.escape(cabinet.name)}</b>\n"
                    f"{html.escape(type(exc).__name__)}: {html.escape(str(exc))}"
                ),
                parse_mode=ParseMode.HTML,
            )


def is_authorized(update: Update) -> bool:
    """Allow bot commands only for Telegram users from the whitelist."""
    return bool(
        update.effective_user
        and update.effective_user.id in TELEGRAM_ALLOWED_USER_IDS
    )


async def deny_access(update: Update) -> None:
    user_id = update.effective_user.id if update.effective_user else "unknown"
    log.warning("Unauthorized Telegram access attempt. user_id=%s", user_id)
    if update.effective_message:
        await update.effective_message.reply_text("⛔ Доступ к боту запрещён.")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await deny_access(update)
        return
    await update.effective_message.reply_text(
        "Бот активен.\n\n"
        "Каждый час он отправляет накопительный отчёт за текущие сутки по двум кабинетам.\n"
        "/report — получить отчёт вручную."
    )


async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await deny_access(update)
        return
    await update.effective_message.reply_text("Формирую отчёт за текущие сутки…")
    await send_all_reports(context.application)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Telegram handler error", exc_info=context.error)


async def post_init(application: Application) -> None:
    scheduler = AsyncIOScheduler(timezone=MOSCOW_TZ)
    scheduler.add_job(
        send_all_reports,
        trigger="cron",
        minute=0,
        kwargs={"application": application},
        id="hourly_wb_report",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    scheduler.start()
    application.bot_data["scheduler"] = scheduler
    log.info("Scheduler started. Hourly reports will be sent at minute 00 (Europe/Moscow).")


async def post_shutdown(application: Application) -> None:
    scheduler = application.bot_data.get("scheduler")
    if scheduler:
        scheduler.shutdown(wait=False)
    await asyncio.gather(*(cabinet.close() for cabinet in CABINETS), return_exceptions=True)


def main() -> None:
    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("report", report_command))
    application.add_error_handler(error_handler)
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
