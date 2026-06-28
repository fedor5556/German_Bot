"""/grammar -- bring the ROUTINE.md grammar cycle into the bot (Feature 3).

The bot already rotates a grammar point per day (scheduler.grammar_point_for).
This command turns that point into 3-5 Gemini micro-exercises; wrong answers
become cards. Synergy with the mistake engine: you can choose to drill your
worst category instead, letting your own weakness set the focus.
"""

from __future__ import annotations

import logging
from datetime import date, datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

import db
import scheduler
from auth import owner_only
from flows import drills

log = logging.getLogger(__name__)


def _month_start() -> datetime:
    now = datetime.now()
    return datetime(now.year, now.month, 1)


@owner_only
async def cmd_grammar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    point = scheduler.grammar_point_for(date.today())
    worst = db.worst_mistake_category(since=_month_start())
    rows = [[InlineKeyboardButton("Start drills ▶", callback_data="gr:start")]]
    if worst:
        rows.append([InlineKeyboardButton(f"Drill my worst: {worst}", callback_data="gr:worst")])
    await update.effective_message.reply_text(
        f"Today's grammar focus: {point}\n\nReady for a few quick exercises?",
        reply_markup=InlineKeyboardMarkup(rows),
    )


@owner_only
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    action = query.data.split(":")[1]
    if action == "start":
        await _strip_keyboard(query)
        point = scheduler.grammar_point_for(date.today())
        await drills.start(update, context, topic=point, source="grammar")
    elif action == "worst":
        worst = db.worst_mistake_category(since=_month_start())
        if not worst:
            await query.edit_message_text("No mistakes logged yet — do some /write or /lesson first.")
            return
        await _strip_keyboard(query)
        await drills.start(update, context, topic=worst, source="grammar", category=worst)


async def _strip_keyboard(query) -> None:
    """Remove the now-consumed buttons so the same drill can't be launched twice."""
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001 - keyboard may already be gone
        pass


def handlers() -> list:
    return [
        CommandHandler("grammar", cmd_grammar),
        CallbackQueryHandler(on_callback, pattern=r"^gr:"),
    ]
