# -*- coding: utf-8 -*-
"""
Телеграм-бот для учёта доходов и расходов за день.

Версия 3: адаптирована под деплой как "web"-процесс (Procfile: `web: python
expense_bot.py`) на платформах вроде Render/Railway/Heroku.

Отличия от предыдущей версии:
- Токен и настройки берутся из переменных окружения, а не из кода
  (см. .gitignore — .env не коммитится, значит секреты должны жить в env).
- Если задана переменная WEBHOOK_URL — бот поднимает aiohttp-сервер и
  работает через вебхук, слушая порт из переменной PORT. Это нужно, потому
  что Procfile объявляет процесс типа "web": платформа ожидает, что
  приложение откроет порт и будет отвечать на HTTP, иначе посчитает деплой
  неудачным (fails health check).
- Если WEBHOOK_URL не задан (например, при локальном запуске) — бот
  работает как раньше, через polling. Это удобно для разработки на своём
  компьютере, где нет публичного HTTPS-адреса для вебхука.

Переменные окружения:
    BOT_TOKEN     — обязательна. Токен бота от @BotFather.
    WEBHOOK_URL   — опционально. Публичный базовый URL вашего сервиса,
                    например: https://your-app.onrender.com
                    Если задана — бот работает через webhook.
    PORT          — опционально. Порт для веб-сервера (платформа обычно
                    выставляет его сама). По умолчанию 8443.
    DB_PATH       — опционально. Путь к файлу базы SQLite.
                    По умолчанию "expenses.db".

ВАЖНО про базу данных на PaaS:
    Большинство бесплатных/стандартных инстансов на Render/Railway/Heroku
    имеют эфемерную файловую систему — при каждом новом деплое или
    перезапуске контейнера файл expenses.db будет создан заново с нуля,
    и все прошлые записи потеряются. Для постоянного хранения данных
    нужно подключить постоянный диск (persistent volume) или внешнюю БД
    (например, Postgres) — сообщите, если нужно на неё переехать.

Локальный запуск (polling, без вебхука):
    set BOT_TOKEN=ваш_токен      (Windows PowerShell: $env:BOT_TOKEN="...")
    python expense_bot.py

Запуск в контейнере (webhook), переменные окружения задаются на платформе:
    BOT_TOKEN=...
    WEBHOOK_URL=https://ваш-домен-на-платформе
"""

import os
import logging
import sqlite3
from datetime import datetime, date

from aiohttp import web

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==================== НАСТРОЙКИ (из переменных окружения) ====================

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError(
        "Не задана переменная окружения BOT_TOKEN. "
        "Локально: set BOT_TOKEN=ваш_токен (или $env:BOT_TOKEN=... в PowerShell). "
        "На платформе деплоя: добавьте BOT_TOKEN в настройках окружения сервиса."
    )

WEBHOOK_URL = os.environ.get("WEBHOOK_URL")  # например https://your-app.onrender.com
PORT = int(os.environ.get("PORT", "8443"))
DB_PATH = os.environ.get("DB_PATH", "expenses.db")

# Путь вебхука делаем на основе токена — так его сложнее подобрать посторонним
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"

EXPENSE_CATEGORIES = ["Еда", "Транспорт", "Жильё", "Развлечения", "Здоровье", "Другое"]
INCOME_CATEGORIES = ["Зарплата", "Подработка", "Подарок", "Прочее"]

# ==================== БАЗА ДАННЫХ ====================

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            category TEXT NOT NULL,
            amount REAL NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def add_record(user_id: int, rtype: str, category: str, amount: float):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO records (user_id, type, category, amount, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, rtype, category, amount, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def get_today_records(user_id: int):
    today_str = date.today().isoformat()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT type, category, amount, created_at FROM records "
        "WHERE user_id = ? AND date(created_at) = ? ORDER BY created_at",
        (user_id, today_str),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


# ==================== КЛАВИАТУРЫ ====================

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["💸 Расход", "💰 Доход"],
        ["📊 Итог за сегодня", "🧾 История за сегодня"],
    ],
    resize_keyboard=True,
)


def categories_keyboard(categories, prefix):
    buttons = [
        InlineKeyboardButton(cat, callback_data=f"{prefix}:{cat}") for cat in categories
    ]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(rows)


def reset_pending(context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("awaiting_amount", None)
    context.user_data.pop("record_type", None)
    context.user_data.pop("category", None)


# ==================== ОБРАБОТЧИКИ КНОПОК МЕНЮ ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reset_pending(context)
    await update.message.reply_text(
        "Привет! Я помогу считать расходы и доходы за день.\n\n"
        "Выбери действие на клавиатуре ниже 👇",
        reply_markup=MAIN_KEYBOARD,
    )


async def expense_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reset_pending(context)
    context.user_data["record_type"] = "expense"
    await update.message.reply_text(
        "На что потрачено? Выбери категорию:",
        reply_markup=categories_keyboard(EXPENSE_CATEGORIES, "cat_exp"),
    )


async def income_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reset_pending(context)
    context.user_data["record_type"] = "income"
    await update.message.reply_text(
        "Откуда доход? Выбери категорию:",
        reply_markup=categories_keyboard(INCOME_CATEGORIES, "cat_inc"),
    )


async def today_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reset_pending(context)
    user_id = update.effective_user.id
    rows = get_today_records(user_id)

    if not rows:
        await update.message.reply_text(
            "Сегодня записей ещё нет.", reply_markup=MAIN_KEYBOARD
        )
        return

    total_expense = 0.0
    total_income = 0.0
    expense_by_cat = {}
    income_by_cat = {}

    for rtype, category, amount, _ in rows:
        if rtype == "expense":
            total_expense += amount
            expense_by_cat[category] = expense_by_cat.get(category, 0) + amount
        else:
            total_income += amount
            income_by_cat[category] = income_by_cat.get(category, 0) + amount

    balance = total_income - total_expense

    lines = [f"📊 Итог за {date.today().strftime('%d.%m.%Y')}\n"]
    lines.append(f"💰 Доходы: {total_income:.2f}")
    for cat, amt in sorted(income_by_cat.items(), key=lambda x: -x[1]):
        lines.append(f"   • {cat}: {amt:.2f}")

    lines.append(f"\n💸 Расходы: {total_expense:.2f}")
    for cat, amt in sorted(expense_by_cat.items(), key=lambda x: -x[1]):
        lines.append(f"   • {cat}: {amt:.2f}")

    lines.append(f"\n⚖️ Баланс за день: {balance:+.2f}")

    await update.message.reply_text("\n".join(lines), reply_markup=MAIN_KEYBOARD)


async def today_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reset_pending(context)
    user_id = update.effective_user.id
    rows = get_today_records(user_id)

    if not rows:
        await update.message.reply_text(
            "Сегодня записей ещё нет.", reply_markup=MAIN_KEYBOARD
        )
        return

    lines = ["🧾 История за сегодня:\n"]
    for rtype, category, amount, created_at in rows[-10:]:
        t = datetime.fromisoformat(created_at).strftime("%H:%M")
        emoji = "💸" if rtype == "expense" else "💰"
        sign = "-" if rtype == "expense" else "+"
        lines.append(f"{t}  {emoji} {sign}{amount:.2f}  ({category})")

    await update.message.reply_text("\n".join(lines), reply_markup=MAIN_KEYBOARD)


# ==================== ВЫБОР КАТЕГОРИИ (инлайн-кнопки) ====================

async def category_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    _, category = query.data.split(":", 1)
    context.user_data["category"] = category
    context.user_data["awaiting_amount"] = True

    rtype = context.user_data.get("record_type", "expense")
    label = "расход" if rtype == "expense" else "доход"

    await query.edit_message_text(
        f"Категория: {category}\nТеперь введи сумму {label}а (просто число, например 350):"
    )


# ==================== ЕДИНЫЙ ОБРАБОТЧИК ТЕКСТА ====================

async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if text == "💸 Расход":
        await expense_start(update, context)
        return
    if text == "💰 Доход":
        await income_start(update, context)
        return
    if text == "📊 Итог за сегодня":
        await today_summary(update, context)
        return
    if text == "🧾 История за сегодня":
        await today_history(update, context)
        return

    if context.user_data.get("awaiting_amount"):
        cleaned = text.replace(",", ".").replace(" ", "")
        try:
            amount = float(cleaned)
            if amount <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "Похоже, это не похоже на число. Введи сумму ещё раз, например: 350\n"
                "Или нажми любую кнопку меню, чтобы отменить ввод."
            )
            return

        rtype = context.user_data.get("record_type", "expense")
        category = context.user_data.get("category", "Другое")
        user_id = update.effective_user.id

        add_record(user_id, rtype, category, amount)

        label = "Расход" if rtype == "expense" else "Доход"
        emoji = "💸" if rtype == "expense" else "💰"

        reset_pending(context)

        await update.message.reply_text(
            f"{emoji} {label} записан: {amount:.2f} ({category})",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    await update.message.reply_text(
        "Не понял. Используй кнопки на клавиатуре.", reply_markup=MAIN_KEYBOARD
    )


# ==================== СБОРКА ПРИЛОЖЕНИЯ TELEGRAM ====================

def build_application() -> Application:
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(category_chosen, pattern=r"^(cat_exp|cat_inc):"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    return app


# ==================== ЗАПУСК: WEBHOOK (для деплоя как web-процесс) ====================

async def run_webhook_mode():
    """
    Поднимает собственный aiohttp-сервер:
    - POST {WEBHOOK_PATH}  — сюда Telegram присылает обновления.
    - GET  /                — health-check для платформы (чтобы деплой не падал).
    Использует aiohttp напрямую (без extra 'webhooks' от python-telegram-bot),
    поэтому в requirements.txt достаточно пакета aiohttp.
    """
    application = build_application()

    async def telegram_webhook(request: web.Request) -> web.Response:
        data = await request.json()
        update = Update.de_json(data, application.bot)
        await application.process_update(update)
        return web.Response(text="OK")

    async def health_check(request: web.Request) -> web.Response:
        return web.Response(text="Bot is running")

    aio_app = web.Application()
    aio_app.router.add_post(WEBHOOK_PATH, telegram_webhook)
    aio_app.router.add_get("/", health_check)

    await application.initialize()
    await application.bot.set_webhook(url=f"{WEBHOOK_URL}{WEBHOOK_PATH}")
    await application.start()

    runner = web.AppRunner(aio_app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()

    logger.info("Webhook-сервер запущен на порту %s, путь %s", PORT, WEBHOOK_PATH)

    # Держим процесс живым бесконечно
    import asyncio

    try:
        await asyncio.Event().wait()
    finally:
        await application.stop()
        await application.shutdown()


# ==================== ЗАПУСК: POLLING (для локальной разработки) ====================

def run_polling_mode():
    application = build_application()
    logger.info("Бот запущен в режиме polling. Нажмите Ctrl+C для остановки.")
    application.run_polling()


# ==================== ТОЧКА ВХОДА ====================

def main():
    if WEBHOOK_URL:
        import asyncio

        asyncio.run(run_webhook_mode())
    else:
        run_polling_mode()


if __name__ == "__main__":
    main()
