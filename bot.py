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
from cart_export import CartExporter, PERIODS, make_xlsx, period_dates

BUILD_VERSION = "2026.11.05.2"


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
DEFAULT_ORDER_SUM_FIELD = "finishedPrice"
ORDER_SUM_FIELDS = ("finishedPrice", "priceWithDisc", "totalPrice")


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
CART_EXPORTERS = [
    CartExporter(
        os.getenv(f"WB_ANALYTICS_TOKEN_{i}", "").strip() or cabinet.token,
        os.getenv(f"WB_CONTENT_TOKEN_{i}", "").strip() or cabinet.token,
    )
    for i, cabinet in enumerate(CABINETS, 1)
]


def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Выгрузить добавили в корзину", callback_data="cart:open")],
        [InlineKeyboardButton(f"Версия сборки: {BUILD_VERSION}", callback_data="cart:version")],
    ])


def cart_period_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data=f"cart:period:{days}")]
        for days, label in PERIODS.items()
    ])


async def cart_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await deny_access(update)
        return
    await update.effective_message.reply_text(
        "Выберите период: месяц — 30 полных дней. Все периоды по вчера включительно, по Москве.",
        reply_markup=cart_period_menu(),
    )


async def deliver_cart_export(application, chat_id, user_id, cabinet_index, days, start, end):
    stage = "получение данных WB"
    try:
        rows = await CART_EXPORTERS[cabinet_index].rows(start, end)
        if not rows:
            await application.bot.send_message(chat_id=chat_id, text="За выбранный период WB не вернул товары.")
            return
        stage = "формирование Excel"
        document = await asyncio.to_thread(make_xlsx, rows, CABINETS[cabinet_index].name, start, end)
        try:
            stage = "отправка Excel в Telegram"
            await application.bot.send_document(
                chat_id=chat_id, document=document,
                filename=f"cart_cabinet_{cabinet_index+1}_{start}_{end}.xlsx",
                caption=f"{CABINETS[cabinet_index].name}\n{start:%d.%m.%Y} — {end:%d.%m.%Y}\n"
                        "Одна строка на товар, все ШК в одной ячейке. Показатели из воронки продаж WB.",
                read_timeout=120, write_timeout=120, connect_timeout=30, pool_timeout=30,
            )
        finally:
            document.close()
    except WildberriesAPIError as exc:
        await application.bot.send_message(chat_id=chat_id, text=str(exc))
    except Exception as exc:
        log.exception("Cart export failed for cabinet %s", cabinet_index + 1)
        from telegram.error import TimedOut, NetworkError, BadRequest
        if isinstance(exc, TimedOut):
            reason = "истекло время ожидания Telegram. Проверьте, не пришёл ли файл, и повторите запрос позже"
        elif isinstance(exc, BadRequest):
            reason = str(exc)[:300]
        elif isinstance(exc, NetworkError):
            reason = "ошибка соединения с Telegram"
        elif isinstance(exc, ValueError) and stage == "формирование Excel":
            reason = str(exc)[:300]
        else:
            reason = f"неожиданная ошибка {type(exc).__name__}; подробности в журнале бота"
        await application.bot.send_message(
            chat_id=chat_id,
            text=f"Кабинет: {CABINETS[cabinet_index].name}\nЭтап: {stage}\nПричина: {reason}\nСборка: {BUILD_VERSION}",
        )
    finally:
        application.bot_data.setdefault("cart_busy", set()).discard(user_id)


async def cart_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await deny_access(update)
        return
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if data == "cart:version":
        await query.message.reply_text(f"Версия сборки: {BUILD_VERSION}")
        return
    if data == "cart:open":
        await query.edit_message_text("Выберите период (полные дни по вчера включительно, Москва):", reply_markup=cart_period_menu())
        return
    parts = data.split(":")
    try:
        if len(parts) == 3 and parts[1] == "period":
            days = int(parts[2])
            if days not in PERIODS:
                return
            await query.edit_message_text(
                f"Период: {PERIODS[days]}. Выберите кабинет:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(cabinet.name, callback_data=f"cart:cabinet:{days}:{i}")]
                    for i, cabinet in enumerate(CABINETS)
                ] + [[InlineKeyboardButton("Назад к периодам", callback_data="cart:open")]]),
            )
        elif len(parts) == 4 and parts[1] == "cabinet":
            days, index = int(parts[2]), int(parts[3])
            if days not in PERIODS or not 0 <= index < len(CABINETS):
                return
            busy = context.application.bot_data.setdefault("cart_busy", set())
            user_id = update.effective_user.id
            if user_id in busy or CART_EXPORTERS[index].lock.locked():
                await query.message.reply_text("Выгрузка уже формируется. Дождитесь завершения и повторите запрос.")
                return
            start, end = period_dates(days)
            busy.add(user_id)
            try:
                await query.message.reply_text(f"Формирую Excel: {CABINETS[index].name}, {start:%d.%m.%Y} — {end:%d.%m.%Y}.")
                context.application.create_task(deliver_cart_export(
                    context.application, update.effective_chat.id, user_id, index, days, start, end
                ), update=update)
            except Exception:
                busy.discard(user_id)
                raise
    except ValueError:
        return


def load_settings() -> dict[str, int | str]:
    defaults: dict[str, int | str] = {
        "start": DEFAULT_ACTIVITY_START,
        "end": DEFAULT_ACTIVITY_END,
        "order_sum_field": DEFAULT_ORDER_SUM_FIELD,
    }
    if not SETTINGS_FILE.exists():
        return defaults.copy()
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        start = int(data.get("start", DEFAULT_ACTIVITY_START))
        end = int(data.get("end", DEFAULT_ACTIVITY_END))
        order_sum_field = str(data.get("order_sum_field", DEFAULT_ORDER_SUM_FIELD))
        if not (0 <= start <= 23 and 0 <= end <= 23):
            raise ValueError("invalid activity window")
        if order_sum_field not in ORDER_SUM_FIELDS:
            order_sum_field = DEFAULT_ORDER_SUM_FIELD
        return {
            "start": start,
            "end": end,
            "order_sum_field": order_sum_field,
        }
    except Exception:
        log.exception("Failed to read %s; using default settings", SETTINGS_FILE)
        return defaults.copy()


def save_settings(settings: dict[str, int | str]) -> None:
    SETTINGS_FILE.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8"
    )


SETTINGS = load_settings()
ACTIVITY = SETTINGS  # Backward-compatible alias used by activity-window functions.


def format_money(value: Decimal) -> str:
    value = value.quantize(Decimal("0.01"))
    if value == value.to_integral():
        return f"{int(value):,}".replace(",", " ") + " ₽"
    rubles = f"{value:,.2f}".replace(",", " ").replace(".", ",")
    return rubles + " ₽"


def order_trend_arrow(today_qty: int, yesterday_qty: int) -> str:
    """Treat a change of up to and including 15% of yesterday as flat."""
    difference = today_qty - yesterday_qty
    if abs(difference) * 100 <= yesterday_qty * 15:
        return "🟡➡️"
    return "🟢⬆️" if difference > 0 else "🔴⬇️"


def format_report(cabinet_name: str, report: DailyReport) -> str:
    now = report.as_of or datetime.now(MOSCOW_TZ)
    lines = [
        f"<b>{'Итоговый отчёт за' if report.is_final else 'Заказы за'} {now:%d.%m.%Y}</b>",
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
            yesterday_qty = report.yesterday_orders_by_article.get(article, 0)
            arrow = order_trend_arrow(qty, yesterday_qty)
            lines.append(f"{html.escape(article)} — <b>{qty}</b> шт. {arrow}")
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


def sumprice_text() -> str:
    return (
        "<b>Способ расчёта суммы заказов</b>\n\n"
        f"Сейчас: <b>{html.escape(str(SETTINGS['order_sum_field']))}</b>\n\n"
        "Выберите поле WB, по которому бот будет складывать стоимость всех заказов за текущие сутки."
    )


def sumprice_menu() -> InlineKeyboardMarkup:
    current = str(SETTINGS["order_sum_field"])
    rows = []
    for field in ORDER_SUM_FIELDS:
        prefix = "✅ " if field == current else ""
        rows.append(
            [InlineKeyboardButton(f"{prefix}{field}", callback_data=f"sumprice:set:{field}")]
        )
    return InlineKeyboardMarkup(rows)


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
    yesterday: bool = False,
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
    report_time = datetime.now(MOSCOW_TZ)

    for cabinet in CABINETS:
        try:
            if yesterday:
                report = await cabinet.build_daily_report(
                    str(SETTINGS["order_sum_field"]), yesterday=True, now=report_time
                )
            else:
                report = await cabinet.build_daily_report(str(SETTINGS["order_sum_field"]))
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
        "/yesterday — итоговый отчёт за вчера.\n"
        "/activity — настроить время автоматической отправки по Москве.\n"
        "/sumprice — выбрать способ расчёта суммы заказов.\n"
        "/cart — выгрузить добавили в корзину.\n"
        f"Версия сборки: {BUILD_VERSION}",
        reply_markup=main_menu(),
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


async def yesterday_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await deny_access(update)
        return
    await update.effective_message.reply_text("Формирую итоговый отчёт за вчера по Москве…")
    await send_all_reports(
        context.application,
        force=True,
        recipient_ids=[update.effective_chat.id],
        yesterday=True,
    )


async def activity_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await deny_access(update)
        return
    await update.effective_message.reply_text(
        activity_text(), parse_mode=ParseMode.HTML, reply_markup=activity_menu()
    )


async def sumprice_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await deny_access(update)
        return
    await update.effective_message.reply_text(
        sumprice_text(), parse_mode=ParseMode.HTML, reply_markup=sumprice_menu()
    )


async def sumprice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await deny_access(update)
        return

    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if not data.startswith("sumprice:set:"):
        return

    field = data.split(":", 2)[-1]
    if field not in ORDER_SUM_FIELDS:
        await query.answer("Неизвестный вариант", show_alert=True)
        return

    SETTINGS["order_sum_field"] = field
    save_settings(SETTINGS)
    log.info(
        "Order sum field changed by Telegram user %s: %s",
        update.effective_user.id,
        field,
    )
    await query.edit_message_text(
        sumprice_text(), parse_mode=ParseMode.HTML, reply_markup=sumprice_menu()
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
        save_settings(SETTINGS)
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
            BotCommand("yesterday", "Итоговый отчет за вчера"),
            BotCommand("cart", "Выгрузить добавили в корзину"),
            BotCommand("menu", f"Главное меню · {BUILD_VERSION}"),
            BotCommand("activity", "Время активности отчётов"),
            BotCommand("sumprice", "Расчёт суммы заказов"),
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
    application.add_handler(CommandHandler("menu", start_command))
    application.add_handler(CommandHandler("cart", cart_command))
    application.add_handler(CallbackQueryHandler(cart_callback, pattern=r"^cart:"))
    application.add_handler(CommandHandler("report", report_command))
    application.add_handler(CommandHandler("yesterday", yesterday_command))
    application.add_handler(CommandHandler("activity", activity_command))
    application.add_handler(CommandHandler("sumprice", sumprice_command))
    application.add_handler(CallbackQueryHandler(activity_callback, pattern=r"^activity:"))
    application.add_handler(CallbackQueryHandler(sumprice_callback, pattern=r"^sumprice:"))
    application.add_error_handler(error_handler)
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
