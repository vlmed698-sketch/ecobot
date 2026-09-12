# -*- coding: utf-8 -*-
"""
Телеграм-бот для учёта доходов и расходов за день.
Поддерживает И inline-кнопки, И Reply-клавиатуру (нижнюю).

Деплой на Railway:
- TELEGRAM_BOT_TOKEN — токен бота
- RAILWAY_PUBLIC_DOMAIN — домен Railway (подставляется автоматически)
- PORT — порт (подставляется автоматически)
- DB_PATH — путь к SQLite (по умолчанию /data/expenses.db)
- TZ_OFFSET — часовой пояс (по умолчанию 3, Москва)
"""

import os
import re
import sqlite3
import logging
from datetime import datetime, date, timezone, timedelta

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ==================== НАСТРОЙКИ ====================

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()

RAILWAY_DOMAIN = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").strip()

WEBHOOK_PATH = "/webhook"
PORT = int(os.environ.get("PORT", 8080))

DB_PATH = os.environ.get("DB_PATH", "/data/expenses.db")
if not os.path.isdir(os.path.dirname(DB_PATH)):
    DB_PATH = "expenses.db"

TZ_OFFSET = int(os.environ.get("TZ_OFFSET", "3"))
LOCAL_TZ = timezone(timedelta(hours=TZ_OFFSET))

EXPENSE_CATEGORIES = ["Еда", "Транспорт", "Жильё", "Развлечения", "Здоровье", "Другое"]
INCOME_CATEGORIES = ["Зарплата", "Подработка", "Подарок", "Прочее"]

# Тексты Reply-кнопок
BTN_EXPENSE = "💸 Расход"
BTN_INCOME = "💰 Доход"
BTN_SUMMARY = "📊 Итог за сегодня"
BTN_HISTORY = "🧾 История за сегодня"

REPLY_BUTTONS = {BTN_EXPENSE, BTN_INCOME, BTN_SUMMARY, BTN_HISTORY}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ==================== ВРЕМЯ ====================

def now_local() -> datetime:
    return datetime.now(LOCAL_TZ)


def today_local() -> date:
    return now_local().date()


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
    logger.info("DB initialized at %s", DB_PATH)


def add_record(user_id: int, rtype: str, category: str, amount: float):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO records (user_id, type, category, amount, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, rtype, category, amount, now_local().isoformat()),
    )
    conn.commit()
    conn.close()
    logger.info(
        "DB INSERT user_id=%s type=%s category=%s amount=%s",
        user_id, rtype, category, amount,
    )


def get_today_records(user_id: int):
    today_str = today_local().isoformat()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT type, category, amount, created_at FROM records "
        "WHERE user_id = ? AND date(created_at) = ? ORDER BY created_at",
        (user_id, today_str),
    )
    rows = cur.fetchall()
    conn.close()
    logger.info(
        "DB SELECT user_id=%s date=%s rows=%s",
        user_id, today_str, len(rows),
    )
    return rows


# ==================== КЛАВИАТУРЫ ====================

def reply_keyboard():
    """Нижняя Reply-клавиатура."""
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(BTN_EXPENSE), KeyboardButton(BTN_INCOME)],
            [KeyboardButton(BTN_SUMMARY), KeyboardButton(BTN_HISTORY)],
        ],
        resize_keyboard=True,
    )


def main_menu_keyboard():
    """Inline-клавиатура под сообщением."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("💸 Расход", callback_data="start_expense"),
                InlineKeyboardButton("💰 Доход", callback_data="start_income"),
            ],
            [
                InlineKeyboardButton("📊 Итог за сегодня", callback_data="summary"),
                InlineKeyboardButton("🧾 История за сегодня", callback_data="history"),
            ],
        ]
    )


def categories_keyboard(categories, prefix):
    buttons = [
        InlineKeyboardButton(cat, callback_data=f"{prefix}:{cat}") for cat in categories
    ]
    rows = [buttons[i: i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


# ==================== СОСТОЯНИЕ ====================

def get_state(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.user_data


def clear_state(context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()


async def safe_edit(query, text, reply_markup=None, parse_mode=None):
    try:
        await query.edit_message_text(
            text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
    except Exception as e:
        logger.info("edit_message_text skipped: %s", e)


# ==================== ЛОГИКА ДЕЙСТВИЙ (общая для inline и reply) ====================

async def action_start_expense(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начать ввод расхода. Работает и из inline, и из reply."""
    clear_state(context)
    context.user_data["record_type"] = "expense"
    context.user_data["stage"] = "choosing_category"

    if update.callback_query:
        await update.callback_query.answer()
        await safe_edit(
            update.callback_query,
            "На что потрачено? Выбери категорию:",
            reply_markup=categories_keyboard(EXPENSE_CATEGORIES, "cat_exp"),
        )
    else:
        await update.message.reply_text(
            "На что потрачено? Выбери категорию:",
            reply_markup=categories_keyboard(EXPENSE_CATEGORIES, "cat_exp"),
        )


async def action_start_income(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_state(context)
    context.user_data["record_type"] = "income"
    context.user_data["stage"] = "choosing_category"

    if update.callback_query:
        await update.callback_query.answer()
        await safe_edit(
            update.callback_query,
            "Откуда доход? Выбери категорию:",
            reply_markup=categories_keyboard(INCOME_CATEGORIES, "cat_inc"),
        )
    else:
        await update.message.reply_text(
            "Откуда доход? Выбери категорию:",
            reply_markup=categories_keyboard(INCOME_CATEGORIES, "cat_inc"),
        )


async def action_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать итог. Работает и из inline, и из reply."""
    user_id = update.effective_user.id
    if update.callback_query:
        await update.callback_query.answer()
        target = update.callback_query.message
    else:
        target = update.message
    await send_summary(target, user_id)


async def action_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if update.callback_query:
        await update.callback_query.answer()
        target = update.callback_query.message
    else:
        target = update.message
    await send_history(target, user_id)


# ==================== ОБРАБОТЧИКИ ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я помогу считать расходы и доходы за день.\n\n"
        "Используй кнопки внизу или inline-кнопки в сообщении:",
        reply_markup=reply_keyboard(),
    )
    await update.message.reply_text(
        "Или нажми кнопку здесь:",
        reply_markup=main_menu_keyboard(),
    )


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Главное меню:",
        reply_markup=reply_keyboard(),
    )
    await update.message.reply_text(
        "Или inline:",
        reply_markup=main_menu_keyboard(),
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data
    state = get_state(context)

    logger.info("CALLBACK user_id=%s data=%s state=%s", user_id, data, dict(state))

    if data == "cancel":
        clear_state(context)
        await query.answer("Отменено")
        await safe_edit(query, "Отменено. Нажми /menu, чтобы начать заново.")
        return

    if data == "menu":
        clear_state(context)
        await query.answer()
        await safe_edit(query, "Главное меню:", reply_markup=main_menu_keyboard())
        return

    if data == "start_expense":
        await action_start_expense(update, context)
        return

    if data == "start_income":
        await action_start_income(update, context)
        return

    if data.startswith("cat_exp:") or data.startswith("cat_inc:"):
        if state.get("stage") != "choosing_category":
            await query.answer("Начни заново: /menu", show_alert=True)
            return
        prefix, category = data.split(":", 1)
        state["category"] = category
        state["stage"] = "entering_amount"
        rtype = state.get("record_type", "expense")
        label = "расход" if rtype == "expense" else "доход"
        await query.answer()
        await safe_edit(
            query,
            f"Категория: <b>{category}</b>\n"
            f"Теперь отправь сообщением сумму {label}а — просто число, например <code>350</code>.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Отмена", callback_data="cancel")]]
            ),
        )
        logger.info("PROMPT sent to user_id=%s", user_id)
        return

    if data == "summary":
        await action_summary(update, context)
        return

    if data == "history":
        await action_history(update, context)
        return

    logger.warning("UNKNOWN callback data=%s user_id=%s", data, user_id)
    await query.answer("Кнопка устарела. Открой /menu заново.", show_alert=True)


def try_parse_amount(text: str):
    try:
        amount = float(str(text).strip().replace(",", "."))
        if amount <= 0:
            return None
        return amount
    except (ValueError, AttributeError):
        return None


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    user_id = update.effective_user.id
    chat_type = update.effective_chat.type
    text = update.message.text.strip()
    state = get_state(context)

    logger.info(
        "TEXT user_id=%s chat_type=%s text=%r state=%s",
        user_id, chat_type, text, dict(state),
    )

    # --- 0. Reply-кнопки главного меню ---
    if text == BTN_EXPENSE:
        await action_start_expense(update, context)
        return
    if text == BTN_INCOME:
        await action_start_income(update, context)
        return
    if text == BTN_SUMMARY:
        await action_summary(update, context)
        return
    if text == BTN_HISTORY:
        await action_history(update, context)
        return

    amount = try_parse_amount(text)

    # --- 1. Активный шаг ввода суммы ---
    if state.get("stage") == "entering_amount":
        if amount is None:
            await update.message.reply_text(
                "Это не похоже на число. Отправь сумму цифрами, например: 350"
            )
            return

        rtype = state.get("record_type", "expense")
        category = state.get("category", "Другое")
        add_record(user_id, rtype, category, amount)
        clear_state(context)

        label = "Расход" if rtype == "expense" else "Доход"
        emoji = "💸" if rtype == "expense" else "💰"
        await update.message.reply_text(
            f"{emoji} {label} записан: <b>{amount:.2f}</b> ({category})",
            parse_mode="HTML",
            reply_markup=reply_keyboard(),
        )
        return

    # --- 2. reply на сообщение-запрос бота ---
    if amount is not None and update.message.reply_to_message:
        replied = update.message.reply_to_message
        me = await context.bot.get_me()
        if replied.from_user and replied.from_user.id == me.id:
            prompt_text = replied.text or ""
            category = None
            rtype = None
            if "Категория:" in prompt_text:
                m = re.search(r"Категория:\s*(?:<b>)?([^<\n]+)", prompt_text)
                if m:
                    category = m.group(1).strip()
            if "сумму расхода" in prompt_text:
                rtype = "expense"
            elif "сумму дохода" in prompt_text:
                rtype = "income"

            if category and rtype:
                add_record(user_id, rtype, category, amount)
                label = "Расход" if rtype == "expense" else "Доход"
                emoji = "💸" if rtype == "expense" else "💰"
                await update.message.reply_text(
                    f"{emoji} {label} записан: <b>{amount:.2f}</b> ({category})",
                    parse_mode="HTML",
                    reply_markup=reply_keyboard(),
                )
                return
            else:
                await update.message.reply_text(
                    "Чтобы начать, нажми /menu.",
                    reply_markup=reply_keyboard(),
                )
                return

    # --- 3. Обычное сообщение ---
    if chat_type == "private":
        await update.message.reply_text(
            "Чтобы начать, нажми /menu.",
            reply_markup=reply_keyboard(),
        )
    # в группе молчим


async def send_summary(message, user_id: int):
    rows = get_today_records(user_id)
    if not rows:
        await message.reply_text(
            f"Сегодня ({today_local().strftime('%d.%m.%Y')}) записей ещё нет.",
            reply_markup=main_menu_keyboard(),
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
    lines = [f"📊 <b>Итог за {today_local().strftime('%d.%m.%Y')}</b>\n"]
    lines.append(f"💰 Доходы: <b>{total_income:.2f}</b>")
    for cat, amt in sorted(income_by_cat.items(), key=lambda x: -x[1]):
        lines.append(f"   • {cat}: {amt:.2f}")
    lines.append(f"\n💸 Расходы: <b>{total_expense:.2f}</b>")
    for cat, amt in sorted(expense_by_cat.items(), key=lambda x: -x[1]):
        lines.append(f"   • {cat}: {amt:.2f}")
    lines.append(f"\n⚖️ Баланс за день: <b>{balance:+.2f}</b>")

    await message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(),
    )


async def send_history(message, user_id: int):
    rows = get_today_records(user_id)
    if not rows:
        await message.reply_text(
            f"Сегодня ({today_local().strftime('%d.%m.%Y')}) записей ещё нет.",
            reply_markup=main_menu_keyboard(),
        )
        return

    lines = ["🧾 <b>История за сегодня:</b>\n"]
    for rtype, category, amount, created_at in rows[-10:]:
        t = datetime.fromisoformat(created_at).strftime("%H:%M")
        emoji = "💸" if rtype == "expense" else "💰"
        sign = "-" if rtype == "expense" else "+"
        lines.append(f"{t}  {emoji} {sign}{amount:.2f}  ({category})")

    await message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(),
    )


async def summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_summary(update.message, update.effective_user.id)


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_history(update.message, update.effective_user.id)


# ==================== ЗАПУСК ====================

def build_application() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("summary", summary_command))
    app.add_handler(CommandHandler("history", history_command))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    return app


def main():
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан")

    init_db()

    app = build_application()

    if WEBHOOK_URL:
        webhook_url = WEBHOOK_URL
    elif RAILWAY_DOMAIN:
        webhook_url = f"https://{RAILWAY_DOMAIN}{WEBHOOK_PATH}"
    else:
        raise RuntimeError(
            "Не задан ни RAILWAY_PUBLIC_DOMAIN, ни WEBHOOK_URL — "
            "некуда регистрировать webhook."
        )

    logger.info("Starting webhook at %s", webhook_url)
    logger.info("Timezone offset: UTC+%s", TZ_OFFSET)

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=WEBHOOK_PATH,
        webhook_url=webhook_url,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
