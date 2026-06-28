"""/mistakes -- the personal mistake-pattern engine (Feature 2).

Every correction the bot makes (writing, lessons, drills, use-it-in-a-sentence)
is tagged by category and logged. This surfaces "your top mistake types this
month" and a one-tap "drill my worst category" that generates targeted exercises
— turning generic grammar practice into *yours*.
"""

from __future__ import annotations

import logging
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

import db
from auth import owner_only
from flows import drills

log = logging.getLogger(__name__)


def _month_start() -> datetime:
    now = datetime.now()
    return datetime(now.year, now.month, 1)


@owner_only
async def cmd_mistakes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    since = _month_start()
    top = db.top_mistake_categories(since=since, limit=5)
    total = db.count_mistakes(since=since)
    if not top:
        await update.effective_message.reply_text(
            "No mistakes logged yet this month. \U0001f331\n"
            "Do some /write or /lesson and I'll start spotting your patterns — "
            "then come back to drill your weak spots."
        )
        return

    lines = ["Your top mistake types this month:", ""]
    medals = ["1.", "2.", "3.", "4.", "5."]
    for i, row in enumerate(top):
        lines.append(f"{medals[i]} {row['category']} — {row['count']}")
    lines.append("")
    lines.append(f"{total} correction(s) logged this month.")

    worst = top[0]["category"]
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"Drill my worst: {worst} ▶", callback_data="mis:drill")]]
    )
    await update.effective_message.reply_text("\n".join(lines), reply_markup=kb)


@owner_only
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    worst = db.worst_mistake_category(since=_month_start())
    if not worst:
        await query.edit_message_text("No mistakes logged yet — do some /write or /lesson first.")
        return
    # Drop the consumed button so a stale tap can't spawn a duplicate drill.
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001 - keyboard may already be gone
        pass
    await drills.start(update, context, topic=worst, source="mistake", category=worst)


def handlers() -> list:
    return [
        CommandHandler("mistakes", cmd_mistakes),
        CallbackQueryHandler(on_callback, pattern=r"^mis:"),
    ]
