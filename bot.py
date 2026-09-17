from __future__ import annotations

import asyncio
import html
import json
import logging
import os
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from wb_client import DailyReport, WildberriesAPIError, WildberriesClient


load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("wb_sales_bot")

MOSCOW_TZ = ZoneInfo("Europe/Moscow")
SETTINGS_FILE = Path(os.getenv("SETTINGS_FILE", "activity_settings.json"))
DEFAULT_ACTIVITY_START = 0
DEFAULT_ACTIVITY_END = 23


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Не заполнена обязательная переменная окружения: {name}")
    return value


TELEGRAM_BOT_TOKEN = required_env("TELEGRAM_BOT_TOKEN")


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


def load_activity_settings() -> dict[str, int]:
    if not SETTINGS_FILE.exists():
        return {"start": DEFAULT_ACTIVITY_START, "end": DEFAULT_ACTIVITY_END}
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        start = int(data.get("start", DEFAULT_ACTIVITY_START))
        end = int(data.get("end", DEFAULT_ACTIVITY_END))
        if 0 <= start <= 23 and 0 <= end <= 23:
            return {"start": start, "end": end}
    except Exception:
        log.exception("Failed to read %s; using default activity window", SETTINGS_FILE)
    return {"start": DEFAULT_ACTIVITY_START, "end": DEFAULT_ACTIVITY_END}


def save_activity_settings(settings: dict[str, int]) -> None:
    SETTINGS_FILE.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8"
    )


ACTIVITY = load_activity_settings()


def format_money(value: Decimal) -> str:
    value = value.quantize(Decimal("0.01"))
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
        "",
        f"Заказов FBO — <b>{report.ordered_fbo}</b> / FBS — <b>{report.ordered_fbs}</b>",
        "",
        f"Заказано итого: <b>{report.ordered_total} шт.</b>",
        f"Сумма заказов: <b>{format_money(report.ordered_sum)}</b>",
        f"Выкуплено итого: <b>{report.bought_total} шт.</b>",
        f"Сумма выкупов: <b>{format_money(report.bought_sum)}</b>",
    ]

    if report.orders_by_article:
        lines.append("")
        for article, qty in sorted(
            report.orders_by_article.items(), key=lambda item: (-item[1], item[0].lower())
        ):
            lines.append(f"{html.escape(article)} — <b>{qty}</b> шт.")
    else:
        lines.extend(["", "Заказов по артикулам нет."])

    return "\n".join(lines)


def activity_text() -> str:
    return (
        "<b>Время активности автоматических отчётов</b>\n\n"
        f"С: <b>{ACTIVITY['start']:02d}:00</b>\n"
        f"По: <b>{ACTIVITY['end']:02d}:00</b>\n\n"
        "Время московское. Границы включены.\n"
        "Команда /report работает в любое время."
    )


def activity_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"С: {ACTIVITY['start']:02d}:00", callback_data="activity:set:start"
                ),
                InlineKeyboardButton(
                    f"По: {ACTIVITY['end']:02d}:00", callback_data="activity:set:end"
                ),
            ]
        ]
    )


def hour_keyboard(field: str) -> InlineKeyboardMarkup:
    rows = []
    for start in range(0, 24, 4):
        rows.append(
            [
                InlineKeyboardButton(
                    f"{hour:02d}:00", callback_data=f"activity:hour:{field}:{hour}"
                )
                for hour in range(start, start + 4)
            ]
        )
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="activity:back")])
    return InlineKeyboardMarkup(rows)


def is_within_activity_window(now: datetime | None = None) -> bool:
    now = now or datetime.now(MOSCOW_TZ)
    hour = now.astimezone(MOSCOW_TZ).hour
    start = ACTIVITY["start"]
    end = ACTIVITY["end"]
    if start <= end:
        return start <= hour <= end
    return hour >= start or hour <= end


async def send_all_reports(
    application: Application,
    *,
    force: bool = False,
    recipient_ids: list[int] | None = None,
) -> None:
    """Send one independent message per WB cabinet to each recipient."""
    if not force and not is_within_activity_window():
        now = datetime.now(MOSCOW_TZ)
        log.info(
            "Hourly report skipped at %s Moscow time: outside activity window %02d:00-%02d:00",
            now.strftime("%H:%M"),
            ACTIVITY["start"],
            ACTIVITY["end"],
        )
        return

    # Automatic reports go to every user in the Telegram whitelist.
    targets = recipient_ids if recipient_ids is not None else sorted(TELEGRAM_ALLOWED_USER_IDS)

    for cabinet in CABINETS:
        try:
            report = await cabinet.build_daily_report()
            text = format_report(cabinet.name, report)
        except WildberriesAPIError as exc:
            log.exception("WB API error for cabinet %s", cabinet.name)
            text = (
                f"⚠️ <b>Не удалось получить отчёт</b>\n"
                f"Кабинет: <b>{html.escape(cabinet.name)}</b>\n"
                f"{html.escape(str(exc))}"
            )
        except Exception as exc:
            log.exception("Unexpected error for cabinet %s", cabinet.name)
            text = (
                f"⚠️ <b>Ошибка отчёта</b>\n"
                f"Кабинет: <b>{html.escape(cabinet.name)}</b>\n"
                f"{html.escape(type(exc).__name__)}: {html.escape(str(exc))}"
            )

        for chat_id in targets:
            try:
                await application.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                # One unavailable/blocked user must not prevent reports to the others.
                log.exception(
                    "Failed to send cabinet %s report to Telegram ID %s",
                    cabinet.name,
                    chat_id,
                )


def is_authorized(update: Update) -> bool:
    return bool(
        update.effective_user
        and update.effective_user.id in TELEGRAM_ALLOWED_USER_IDS
    )


async def deny_access(update: Update) -> None:
    user_id = update.effective_user.id if update.effective_user else "unknown"
    log.warning("Unauthorized Telegram access attempt. user_id=%s", user_id)
    if update.callback_query:
        await update.callback_query.answer("Доступ запрещён", show_alert=True)
    elif update.effective_message:
        await update.effective_message.reply_text("⛔ Доступ к боту запрещён.")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await deny_access(update)
        return
    await update.effective_message.reply_text(
        "Бот активен.\n\n"
        "Каждый час он отправляет накопительный отчёт за текущие сутки по двум кабинетам.\n"
        "/report — получить отчёт вручную.\n"
        "/activity — настроить время автоматической отправки по Москве."
    )


async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await deny_access(update)
        return
    await update.effective_message.reply_text("Формирую отчёт за текущие сутки…")
    await send_all_reports(
        context.application,
        force=True,
        recipient_ids=[update.effective_chat.id],
    )


async def activity_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await deny_access(update)
        return
    await update.effective_message.reply_text(
        activity_text(), parse_mode=ParseMode.HTML, reply_markup=activity_menu()
    )


async def activity_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await deny_access(update)
        return

    query = update.callback_query
    await query.answer()
    data = query.data or ""

    if data == "activity:back":
        await query.edit_message_text(
            activity_text(), parse_mode=ParseMode.HTML, reply_markup=activity_menu()
        )
        return

    if data.startswith("activity:set:"):
        field = data.rsplit(":", 1)[-1]
        label = "С" if field == "start" else "По"
        await query.edit_message_text(
            f"Выберите время <b>«{label}»</b> по Москве:",
            parse_mode=ParseMode.HTML,
            reply_markup=hour_keyboard(field),
        )
        return

    if data.startswith("activity:hour:"):
        _, _, field, raw_hour = data.split(":", 3)
        if field not in {"start", "end"}:
            return
        try:
            hour = int(raw_hour)
        except ValueError:
            return
        if not 0 <= hour <= 23:
            return

        ACTIVITY[field] = hour
        save_activity_settings(ACTIVITY)
        log.info(
            "Activity window changed by Telegram user %s: %02d:00-%02d:00",
            update.effective_user.id,
            ACTIVITY["start"],
            ACTIVITY["end"],
        )
        await query.edit_message_text(
            activity_text(), parse_mode=ParseMode.HTML, reply_markup=activity_menu()
        )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Telegram handler error", exc_info=context.error)


async def post_init(application: Application) -> None:
    await application.bot.set_my_commands(
        [
            BotCommand("report", "Отчёт за текущие сутки"),
            BotCommand("activity", "Время активности отчётов"),
            BotCommand("start", "Справка"),
        ]
    )

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
    log.info(
        "Scheduler started. Hourly checks at minute 00 (Europe/Moscow); active %02d:00-%02d:00.",
        ACTIVITY["start"],
        ACTIVITY["end"],
    )


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
    application.add_handler(CommandHandler("activity", activity_command))
    application.add_handler(CallbackQueryHandler(activity_callback, pattern=r"^activity:"))
    application.add_error_handler(error_handler)
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
