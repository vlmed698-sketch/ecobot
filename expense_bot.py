# -*- coding: utf-8 -*-
"""
Телеграм-бот для учёта доходов и расходов за день.
Работает и в личке, и в группах.

Деплой на Railway:
- Токен берётся из переменной окружения TELEGRAM_BOT_TOKEN.
- Публичный домен — из RAILWAY_PUBLIC_DOMAIN.
- Порт — из PORT.
- База — по пути DB_PATH (по умолчанию /data/expenses.db, если смонтирован volume;
  иначе ./expenses.db).
- Часовой пояс — TZ_OFFSET (по умолчанию 3, Москва).
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

# Часовой пояс для отображения и фильтрации по дате.
# По умолчанию UTC+3 (Москва). Задаётся переменной окружения TZ_OFFSET.
TZ_OFFSET = int(os.environ.get("TZ_OFFSET", "3"))
LOCAL_TZ = timezone(timedelta(hours=TZ_OFFSET))

EXPENSE_CATEGORIES = ["Еда", "Транспорт", "Жильё", "Развлечения", "Здоровье", "Другое"]
INCOME_CATEGORIES = ["Зарплата", "Подработка", "Подарок", "Прочее"]

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ==================== ВРЕМЯ ====================

def now_local() -> datetime:
    """Текущее время в локальном часовом поясе."""
    return datetime.now(LOCAL_TZ)


def today_local() -> date:
    """Текущая дата в локальном часовом поясе."""
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
        "DB INSERT user_id=%s type=%s category=%s amount=%s at %s",
        user_id, rtype, category, amount, now_local().isoformat(),
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

def main_menu_keyboard():
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


# ==================== СОСТОЯНИЕ ПОЛЬЗОВАТЕЛЯ ====================

def get_state(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.user_data


def clear_state(context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()


# ==================== ВСПОМОГАТЕЛЬНОЕ ====================

async def safe_edit(query, text, reply_markup=None, parse_mode=None):
    """Безопасно редактирует сообщение. Не падает, если текст не изменился."""
    try:
        await query.edit_message_text(
            text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
    except Exception as e:
        logger.info("edit_message_text skipped: %s", e)


# ==================== ОБРАБОТЧИКИ ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я помогу считать расходы и доходы за день.\n\n"
        "Выбери действие:",
        reply_markup=main_menu_keyboard(),
    )


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Главное меню:",
        reply_markup=main_menu_keyboard(),
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data
    state = get_state(context)

    logger.info("CALLBACK user_id=%s data=%s state=%s", user_id, data, dict(state))

    # --- Отмена ---
    if data == "cancel":
        clear_state(context)
        await query.answer("Отменено")
        await safe_edit(query, "Отменено. Нажми /menu, чтобы начать заново.")
        return

    # --- Меню ---
    if data == "menu":
        clear_state(context)
        await query.answer()
        await safe_edit(query, "Главное меню:", reply_markup=main_menu_keyboard())
        return

    # --- Начать расход ---
    if data == "start_expense":
        clear_state(context)
        state["record_type"] = "expense"
        state["stage"] = "choosing_category"
        await query.answer()
        await safe_edit(
            query,
            "На что потрачено? Выбери категорию:",
            reply_markup=categories_keyboard(EXPENSE_CATEGORIES, "cat_exp"),
        )
        return

    # --- Начать доход ---
    if data == "start_income":
        clear_state(context)
        state["record_type"] = "income"
        state["stage"] = "choosing_category"
        await query.answer()
        await safe_edit(
            query,
            "Откуда доход? Выбери категорию:",
            reply_markup=categories_keyboard(INCOME_CATEGORIES, "cat_inc"),
        )
        return

    # --- Выбор категории ---
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
        logger.info("PROMPT sent to user_id=%s, msg_id=%s", user_id, query.message.message_id)
        return

    # --- Итог ---
    # ВАЖНО: НЕ редактируем старое сообщение, а отвечаем новым.
    # Иначе при повторном нажатии на уже отредактированное сообщение
    # Telegram вернёт BadRequest, и пользователь не увидит ответа.
    if data == "summary":
        await query.answer()
        await send_summary(query.message, user_id)
        return

    # --- История ---
    if data == "history":
        await query.answer()
        await send_history(query.message, user_id)
        return

    # --- Неизвестная кнопка (устаревшая от прошлой версии бота) ---
    logger.warning("UNKNOWN callback data=%s user_id=%s", data, user_id)
    await query.answer(
        "Кнопка устарела. Открой /menu заново.",
        show_alert=True,
    )


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
    text = update.message.text
    state = get_state(context)

    logger.info(
        "TEXT user_id=%s chat_type=%s text=%r state=%s",
        user_id, chat_type, text, dict(state),
    )

    amount = try_parse_amount(text)

    # --- Случай 1: активный шаг ввода суммы ---
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
            reply_markup=main_menu_keyboard(),
        )
        return

    # --- Случай 2: reply на сообщение-запрос бота ---
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
                    reply_markup=main_menu_keyboard(),
                )
                return
            else:
                # Ответ на сообщение бота, но это не запрос суммы.
                await update.message.reply_text(
                    "Чтобы начать, нажми /menu.",
                    reply_markup=main_menu_keyboard(),
                )
                return

    # --- Случай 3: обычное сообщение ---
    if chat_type == "private":
        await update.message.reply_text(
            "Чтобы начать, нажми /menu.",
            reply_markup=main_menu_keyboard(),
        )
    # в группе молчим, чтобы не спамить


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

    # run_webhook — синхронный, сам управляет event loop.
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=WEBHOOK_PATH,
        webhook_url=webhook_url,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
