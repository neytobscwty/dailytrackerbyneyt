import logging
import sqlite3
from datetime import datetime, timedelta, date
import calendar
import os

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)


BOT_TOKEN = os.getenv("BOT_TOKEN")

DB_PATH = "tracker.db"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------- СОСТОЯНИЕ ----------

# current_activity: "contrib" / "soft" / "hands" / "coding"
user_state: dict[int, dict] = {}


def get_user_state(user_id: int) -> dict:
    if user_id not in user_state:
        user_state[user_id] = {
            "wake_time": None,
            "sleep_time": None,
            "current_activity": None,
            "current_start": None,
            "totals": {
                "contrib": timedelta(0),
                "soft": timedelta(0),
                "hands": timedelta(0),
                "coding": timedelta(0),
            },
        }
    return user_state[user_id]


# ---------- УТИЛЫ ----------

def format_timedelta(td: timedelta) -> str:
    total_seconds = max(0, int(td.total_seconds()))
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    return f"{hours}h {minutes}min"


def build_main_menu_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("🟢 Woke up", callback_data="woke_up"),
            InlineKeyboardButton("😴 Sleep time", callback_data="sleep_time"),
        ],
        [
            InlineKeyboardButton("🟦 Contribution (X/DC)", callback_data="start_contrib"),
        ],
        [
            InlineKeyboardButton("🟧 Abuse", callback_data="abuse_menu"),
        ],
        [
            InlineKeyboardButton("🟪 Coding", callback_data="start_coding"),
        ],
        [
            InlineKeyboardButton("📊 Daily Report", callback_data="daily_report"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


ACTIVITY_NAMES = {
    "contrib": "Contribution (X/DC)",
    "soft": "Abuse Soft (Retro/Free)",
    "hands": "Abuse Hands (Retro/Free)",
    "coding": "Coding",
}


def build_activity_keyboard(code: str) -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton(
                f"⏸ Pause {ACTIVITY_NAMES[code]}",
                callback_data=f"pause_{code}",
            ),
        ],
        [
            InlineKeyboardButton("📋 Menu", callback_data="show_menu"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def build_paused_keyboard(code: str) -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton(
                f"▶️ Continue {ACTIVITY_NAMES[code]}",
                callback_data=f"continue_{code}",
            ),
        ],
        [
            InlineKeyboardButton("📋 Menu", callback_data="show_menu"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def build_abuse_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("Soft (Retro/Free)", callback_data="start_soft"),
        ],
        [
            InlineKeyboardButton("Hands (Retro/Free)", callback_data="start_hands"),
        ],
        [
            InlineKeyboardButton("📋 Menu", callback_data="show_menu"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


# ---------- БД ----------

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_stats (
            user_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            alive_seconds INTEGER NOT NULL,
            contrib_seconds INTEGER NOT NULL,
            hands_seconds INTEGER NOT NULL,
            soft_seconds INTEGER NOT NULL,
            coding_seconds INTEGER NOT NULL,
            PRIMARY KEY (user_id, date)
        )
        """
    )
    conn.commit()
    conn.close()


def save_daily_stats_to_db(
    user_id: int,
    day_date: date,
    alive: timedelta,
    totals: dict,
):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    values = (
        user_id,
        day_date.isoformat(),
        int(alive.total_seconds()),
        int(totals["contrib"].total_seconds()),
        int(totals["hands"].total_seconds()),
        int(totals["soft"].total_seconds()),
        int(totals["coding"].total_seconds()),
    )

    cur.execute(
        """
        INSERT OR REPLACE INTO daily_stats (
            user_id, date, alive_seconds,
            contrib_seconds, hands_seconds, soft_seconds, coding_seconds
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        values,
    )
    conn.commit()
    conn.close()


def get_stats_range(user_id: int, start_date: date, end_date: date):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            COALESCE(SUM(alive_seconds), 0),
            COALESCE(SUM(contrib_seconds), 0),
            COALESCE(SUM(hands_seconds), 0),
            COALESCE(SUM(soft_seconds), 0),
            COALESCE(SUM(coding_seconds), 0),
            COUNT(*)
        FROM daily_stats
        WHERE user_id = ?
          AND date >= ?
          AND date <= ?
        """,
        (user_id, start_date.isoformat(), end_date.isoformat()),
    )
    row = cur.fetchone()
    conn.close()
    if row is None:
        return None

    alive_sec, contrib_sec, hands_sec, soft_sec, coding_sec, days_count = row

    return {
        "alive": timedelta(seconds=alive_sec),
        "contrib": timedelta(seconds=contrib_sec),
        "hands": timedelta(seconds=hands_sec),
        "soft": timedelta(seconds=soft_sec),
        "coding": timedelta(seconds=coding_sec),
        "days_count": days_count,
    }


# ---------- ЛОГИКА АКТИВНОСТЕЙ ----------

def close_current_activity(state: dict, now: datetime) -> str | None:
    code = state["current_activity"]
    start = state["current_start"]
    if not code or not start:
        return None

    delta = now - start
    if delta.total_seconds() < 0:
        delta = timedelta(0)

    state["totals"][code] += delta
    state["current_activity"] = None
    state["current_start"] = None
    return code


def build_daily_report_text(state: dict, now: datetime) -> str:
    wake = state["wake_time"]
    sleep = state["sleep_time"]
    if wake is None:
        return "First mark 🟢 Woke up."

    totals = dict(state["totals"])

    # учёт текущей активности
    if state["current_activity"] and state["current_start"]:
        extra = now - state["current_start"]
        if extra.total_seconds() < 0:
            extra = timedelta(0)
        totals[state["current_activity"]] += extra

    end = sleep if sleep else now
    alive = end - wake
    if alive.total_seconds() < 0:
        alive = timedelta(0)

    lines: list[str] = []
    lines.append("📊 Daily Report")
    lines.append(f"Woke up: {wake.strftime('%H:%M')}")
    if sleep:
        lines.append(f"Sleep time: {end.strftime('%H:%M')}")
    else:
        lines.append("Sleep time: not yet")
    lines.append(f"Alive: {format_timedelta(alive)}")
    lines.append("")
    lines.append("Work:")

    lines.append(f"• Contribution (X/DC): {format_timedelta(totals['contrib'])}")
    lines.append("• Abuse:")
    lines.append(f"  - Soft (Retro/Free): {format_timedelta(totals['soft'])}")
    lines.append(f"  - Hands (Retro/Free): {format_timedelta(totals['hands'])}")
    lines.append(f"• Coding: {format_timedelta(totals['coding'])}")

    if state["current_activity"]:
        lines.append("")
        lines.append(
            f"Now: {ACTIVITY_NAMES[state['current_activity']]} "
            f"(since {state['current_start'].strftime('%H:%M')})"
        )

    return "\n".join(lines)


def build_range_report_text(title: str, stats: dict, start_date: date, end_date: date) -> str:
    if not stats or stats["days_count"] == 0:
        return f"{title}\n\nNo finished days in this period."

    days_count = stats["days_count"]
    lines: list[str] = []
    lines.append(title)
    lines.append(f"Period: {start_date.isoformat()} – {end_date.isoformat()}")
    lines.append(f"Days: {days_count}")
    lines.append("")
    lines.append(f"Alive total: {format_timedelta(stats['alive'])}")
    lines.append("")
    lines.append("Work total:")
    lines.append(f"• Contribution: {format_timedelta(stats['contrib'])}")
    lines.append("• Abuse:")
    lines.append(f"  - Soft: {format_timedelta(stats['soft'])}")
    lines.append(f"  - Hands: {format_timedelta(stats['hands'])}")
    lines.append(f"• Coding: {format_timedelta(stats['coding'])}")
    lines.append("")
    lines.append("Per day (avg):")
    lines.append(f"• Alive: {format_timedelta(stats['alive'] / days_count)}")
    lines.append(f"• Contribution: {format_timedelta(stats['contrib'] / days_count)}")
    lines.append(f"• Abuse Soft: {format_timedelta(stats['soft'] / days_count)}")
    lines.append(f"• Abuse Hands: {format_timedelta(stats['hands'] / days_count)}")
    lines.append(f"• Coding: {format_timedelta(stats['coding'] / days_count)}")
    return "\n".join(lines)


# ---------- КОМАНДЫ ----------

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Daily tracker is on.\n\n"
        "Use /menu to open panel:\n"
        "Woke up / Sleep time / Contribution / Abuse / Coding / Daily Report.\n"
    )
    await update.message.reply_text(text, reply_markup=build_main_menu_keyboard())


async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Menu:", reply_markup=build_main_menu_keyboard())


async def week_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    today = datetime.now().date()
    start = today - timedelta(days=6)
    stats = get_stats_range(user.id, start, today)
    text = build_range_report_text("📊 Last 7 days", stats, start, today)
    await update.message.reply_text(text)


async def month_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args
    today = datetime.now().date()
    year, month = today.year, today.month

    if len(args) == 0:
        pass
    elif len(args) == 1 and "-" in args[0]:
        try:
            y, m = args[0].split("-", 1)
            year = int(y)
            month = int(m)
        except ValueError:
            await update.message.reply_text("Use: /month 2025-12 or /month 12 2025")
            return
    elif len(args) == 2:
        try:
            month = int(args[0])
            year = int(args[1])
        except ValueError:
            await update.message.reply_text("Use: /month 2025-12 or /month 12 2025")
            return
    else:
        await update.message.reply_text("Use: /month 2025-12 or /month 12 2025")
        return

    try:
        first_day = date(year, month, 1)
        last_day = date(year, month, calendar.monthrange(year, month)[1])
    except ValueError:
        await update.message.reply_text("Bad month.")
        return

    stats = get_stats_range(user.id, first_day, last_day)
    title = f"📊 Month {year}-{month:02d}"
    text = build_range_report_text(title, stats, first_day, last_day)
    await update.message.reply_text(text)


# ---------- КНОПКИ ----------

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = query.from_user
    state = get_user_state(user.id)
    data = query.data
    now = datetime.now()
    msg = ""

    # ----- MENU -----
    if data == "show_menu":
        await query.message.reply_text("Menu:", reply_markup=build_main_menu_keyboard())
        return

    # ----- WOKE UP -----
    if data == "woke_up":
        state["wake_time"] = now
        state["sleep_time"] = None
        state["totals"] = {
            "contrib": timedelta(0),
            "soft": timedelta(0),
            "hands": timedelta(0),
            "coding": timedelta(0),
        }
        state["current_activity"] = None
        state["current_start"] = None
        msg = f"🟢 Woke up at {now.strftime('%H:%M')}."
        await query.message.reply_text(msg, reply_markup=build_main_menu_keyboard())
        return

    # ----- SLEEP TIME -----
    if data == "sleep_time":
        if state["wake_time"] is None:
            await query.message.reply_text(
                "First mark 🟢 Woke up.", reply_markup=build_main_menu_keyboard()
            )
            return

        state["sleep_time"] = now
        closed = close_current_activity(state, now)
        msg = f"😴 Sleep time at {now.strftime('%H:%M')}."
        if closed:
            msg += f"\nAuto pause: {ACTIVITY_NAMES[closed]}."
        await query.message.reply_text(msg, reply_markup=build_main_menu_keyboard())

        # финальный отчёт + сохранение
        wake = state["wake_time"]
        end = state["sleep_time"]
        alive = end - wake
        if alive.total_seconds() < 0:
            alive = timedelta(0)

        totals_copy = dict(state["totals"])
        day_date = wake.date()
        save_daily_stats_to_db(user.id, day_date, alive, totals_copy)

        report = build_daily_report_text(state, now)
        await query.message.reply_text(report, reply_markup=build_main_menu_keyboard())
        return

    # ----- ABUSE MENU -----
    if data == "abuse_menu":
        await query.message.reply_text("Abuse mode:", reply_markup=build_abuse_keyboard())
        return

    # helpers
    def start_act(code: str) -> str:
        closed = close_current_activity(state, now)
        state["current_activity"] = code
        state["current_start"] = now
        text = f"▶️ Start {ACTIVITY_NAMES[code]} at {now.strftime('%H:%M')}."
        if closed:
            text += f"\nAuto pause: {ACTIVITY_NAMES[closed]}."
        return text

    def pause_act(code: str) -> str:
        if state["current_activity"] != code:
            return f"{ACTIVITY_NAMES[code]} is not active."
        close_current_activity(state, now)
        return f"⏸ Pause {ACTIVITY_NAMES[code]} at {now.strftime('%H:%M')}."

    def continue_act(code: str) -> str:
        state["current_activity"] = code
        state["current_start"] = now
        return f"▶️ Continue {ACTIVITY_NAMES[code]} at {now.strftime('%H:%M')}."

    # ----- START ACTIVITIES FROM MENU / ABUSE -----
    if data == "start_contrib":
        msg = start_act("contrib")
        await query.message.reply_text(msg, reply_markup=build_activity_keyboard("contrib"))
        return

    if data == "start_soft":
        msg = start_act("soft")
        await query.message.reply_text(msg, reply_markup=build_activity_keyboard("soft"))
        return

    if data == "start_hands":
        msg = start_act("hands")
        await query.message.reply_text(msg, reply_markup=build_activity_keyboard("hands"))
        return

    if data == "start_coding":
        msg = start_act("coding")
        await query.message.reply_text(msg, reply_markup=build_activity_keyboard("coding"))
        return

    # ----- PAUSE -----
    if data.startswith("pause_"):
        code = data.split("_", 1)[1]
        msg = pause_act(code)
        await query.message.reply_text(msg, reply_markup=build_paused_keyboard(code))
        return

    # ----- CONTINUE -----
    if data.startswith("continue_"):
        code = data.split("_", 1)[1]
        msg = continue_act(code)
        await query.message.reply_text(msg, reply_markup=build_activity_keyboard(code))
        return

    # ----- DAILY REPORT -----
    if data == "daily_report":
        msg = build_daily_report_text(state, now)
        await query.message.reply_text(msg, reply_markup=build_main_menu_keyboard())
        return


# ---------- POST INIT (КОМАНДЫ В /) ----------

async def post_init(app: Application):
    await app.bot.set_my_commands(
        [
            BotCommand("start", "Start bot"),
            BotCommand("menu", "Show menu"),
            BotCommand("week", "Stats for last 7 days"),
            BotCommand("month", "Stats for month"),
        ]
    )


# ---------- MAIN ----------

def main():
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()
    app.post_init = post_init

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("menu", menu_cmd))
    app.add_handler(CommandHandler("week", week_cmd))
    app.add_handler(CommandHandler("month", month_cmd))
    app.add_handler(CallbackQueryHandler(callback_handler))

    app.run_polling()


if __name__ == "__main__":
    main()
