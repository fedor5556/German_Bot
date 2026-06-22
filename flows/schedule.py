"""/schedule -- the standout feature.

Lets the user view/edit which weekdays are lesson days (and their time), and --
when a lesson moves -- asks Permanent vs Just-this-week. Temporary changes are
stored as one-off date overrides that auto-revert simply because future dates
fall back to the recurring weekly schedule (and past overrides get cleaned up).

The day-type resolver (resolve_day_type) is PURE and unit-tested in
tests/test_schedule.py; the DB-backed wrappers sit below it.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

import db
from auth import owner_only
from db import WEEKDAY_NAMES

log = logging.getLogger(__name__)

# Day types drive which flow runs each day.
LESSON, FULL, REVIEW = "lesson", "full", "review"


# --- pure logic (unit-tested) --------------------------------------------------

def resolve_day_type(day: date, weekly_schedule: dict[int, dict], override: dict | None = None) -> str:
    """Resolve a date to 'lesson' | 'full' | 'review'.

    A lesson day (recurring or via a one-off override) is always 'lesson'.
    Otherwise Sunday is the weekly 'review' day; every other day is 'full'.
    """
    weekday = day.weekday()  # 0=Mon ... 6=Sun
    if override is not None:
        is_lesson = bool(override["is_lesson_day"])
    else:
        is_lesson = bool(weekly_schedule[weekday]["is_lesson_day"])

    if is_lesson:
        return LESSON
    if weekday == 6:  # Sunday
        return REVIEW
    return FULL


def date_for_weekday_this_week(weekday: int, today: date) -> date:
    """The next occurrence of `weekday` counting today as itself (this-week semantics)."""
    start = today - timedelta(days=today.weekday())  # Monday of this week
    target = start + timedelta(days=weekday)
    if target < today:
        target += timedelta(days=7)
    return target


# --- DB-backed wrappers --------------------------------------------------------

def resolve_for(day: date) -> str:
    weekly = db.get_weekly_schedule()
    override = db.get_override(day)
    return resolve_day_type(day, weekly, override)


def resolve_today(today: date | None = None) -> str:
    return resolve_for(today or date.today())


# --- rendering -----------------------------------------------------------------

def _fmt_day_line(wd: int, info: dict) -> str:
    label = WEEKDAY_NAMES[wd]
    if info["is_lesson_day"]:
        t = info["time"] or "time not set"
        return f"{label}: Lesson ({t})"
    if wd == 6:
        return f"{label}: Weekly review"
    return f"{label}: Full study day"


def render_home(today: date) -> tuple[str, InlineKeyboardMarkup]:
    weekly = db.get_weekly_schedule()
    lines = ["*Your weekly schedule*", ""]
    for wd in range(7):
        lines.append("• " + _fmt_day_line(wd, weekly[wd]))

    overrides = db.get_upcoming_overrides(today)
    if overrides:
        lines.append("")
        lines.append("*One-off changes (just this week):*")
        for ov in overrides:
            kind = "Lesson" if ov["is_lesson_day"] else "Full/Review"
            t = f" ({ov['time']})" if ov["time"] else ""
            lines.append(f"• {ov['date'].isoformat()} → {kind}{t}")

    lines.append("")
    lines.append("Tap a day to change it.")

    rows = []
    weekly = db.get_weekly_schedule()
    for wd in range(0, 7, 2):
        row = [InlineKeyboardButton(WEEKDAY_NAMES[wd], callback_data=f"sch:day:{wd}")]
        if wd + 1 < 7:
            row.append(
                InlineKeyboardButton(WEEKDAY_NAMES[wd + 1], callback_data=f"sch:day:{wd + 1}")
            )
        rows.append(row)
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _day_menu(wd: int) -> tuple[str, InlineKeyboardMarkup]:
    weekly = db.get_weekly_schedule()
    info = weekly[wd]
    text = f"*{WEEKDAY_NAMES[wd]}* — currently: {_fmt_day_line(wd, info)}\n\nChange it to:"
    rows = [
        [
            InlineKeyboardButton("Lesson day", callback_data=f"sch:choose:{wd}:1"),
            InlineKeyboardButton("Full day", callback_data=f"sch:choose:{wd}:0"),
        ],
        [InlineKeyboardButton("Set lesson time", callback_data=f"sch:time:{wd}")],
        [InlineKeyboardButton("← Back", callback_data="sch:home")],
    ]
    return text, InlineKeyboardMarkup(rows)


def _scope_menu(wd: int, val: int) -> tuple[str, InlineKeyboardMarkup]:
    kind = "a lesson day" if val else ("a full/review day")
    text = (
        f"Make *{WEEKDAY_NAMES[wd]}* {kind} — permanently every week, "
        f"or just this week?"
    )
    rows = [
        [InlineKeyboardButton("Permanently ♾️", callback_data=f"sch:apply:{wd}:{val}:perm")],
        [InlineKeyboardButton("Just this week \U0001f4c5", callback_data=f"sch:apply:{wd}:{val}:temp")],
        [InlineKeyboardButton("← Back", callback_data=f"sch:day:{wd}")],
    ]
    return text, InlineKeyboardMarkup(rows)


# --- handlers ------------------------------------------------------------------

@owner_only
async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text, kb = render_home(date.today())
    await update.effective_message.reply_text(text, reply_markup=kb, parse_mode="Markdown")


@owner_only
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    action = parts[1] if len(parts) > 1 else "home"

    if action == "home":
        text, kb = render_home(date.today())
        await query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")
        return

    if action == "day":
        wd = int(parts[2])
        text, kb = _day_menu(wd)
        await query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")
        return

    if action == "choose":
        wd, val = int(parts[2]), int(parts[3])
        text, kb = _scope_menu(wd, val)
        await query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")
        return

    if action == "time":
        wd = int(parts[2])
        context.user_data["await"] = {"kind": "time", "weekday": wd}
        await query.edit_message_text(
            f"Send the lesson time for *{WEEKDAY_NAMES[wd]}* as HH:MM "
            f"(e.g. `17:30`). Send /cancel to abort.",
            parse_mode="Markdown",
        )
        return

    if action == "apply":
        wd, val, scope = int(parts[2]), int(parts[3]), parts[4]
        is_lesson = bool(val)
        weekly = db.get_weekly_schedule()
        existing_time = weekly[wd]["time"]
        if scope == "perm":
            db.set_weekday(wd, is_lesson_day=is_lesson, time=existing_time if is_lesson else None)
            note = f"Done — *{WEEKDAY_NAMES[wd]}* is now a {'lesson' if is_lesson else 'full'} day every week."
        else:  # temp
            target = date_for_weekday_this_week(wd, date.today())
            db.set_override(target, is_lesson_day=is_lesson, time=existing_time if is_lesson else None)
            note = (
                f"Done — just for {target.isoformat()}, "
                f"{WEEKDAY_NAMES[wd]} is a {'lesson' if is_lesson else 'full/review'} day. "
                f"It reverts next week."
            )
        text, kb = render_home(date.today())
        await query.edit_message_text(note + "\n\n" + text, reply_markup=kb, parse_mode="Markdown")
        return


async def apply_time_text(update: Update, context: ContextTypes.DEFAULT_TYPE, weekday: int) -> None:
    """Handle a free-text HH:MM reply after 'Set lesson time'. Called by the dispatcher."""
    raw = (update.effective_message.text or "").strip()
    parts = raw.split(":")
    valid = False
    if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
        h, m = int(parts[0]), int(parts[1])
        if 0 <= h < 24 and 0 <= m < 60:
            valid = True
    if not valid:
        await update.effective_message.reply_text(
            "That doesn't look like a time. Send it as HH:MM, e.g. 17:30 (or /cancel)."
        )
        return
    hhmm = f"{h:02d}:{m:02d}"
    db.set_weekday(weekday, is_lesson_day=True, time=hhmm)
    context.user_data.pop("await", None)
    text, kb = render_home(date.today())
    await update.effective_message.reply_text(
        f"Set {WEEKDAY_NAMES[weekday]} lesson time to {hhmm}.\n\n" + text,
        reply_markup=kb,
        parse_mode="Markdown",
    )


def handlers() -> list:
    return [
        CommandHandler("schedule", cmd_schedule),
        CallbackQueryHandler(on_callback, pattern=r"^sch:"),
    ]
