"""Review session flow -- the SRS in chat.

Walks all due cards, then introduces up to new_cards_per_day new cards. Each card:
show front -> Show answer -> rate Again/Hard/Good/Easy -> schedule next. A card
rated "Again" is requeued to the end of this session and comes back next time too.
"""

from __future__ import annotations

import logging
from datetime import date

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

import db
import srs
from auth import owner_only

log = logging.getLogger(__name__)


def _fmt_interval(days: float) -> str:
    days = int(days)
    if days <= 0:
        return "again today"
    if days == 1:
        return "1d"
    if days < 30:
        return f"{days}d"
    if days < 365:
        return f"{round(days / 30)}mo"
    return f"{round(days / 365, 1)}y"


def build_session(context: ContextTypes.DEFAULT_TYPE) -> int:
    """Populate context.user_data['review'] with today's queue. Returns its size."""
    today = date.today()
    due = db.get_due_cards(today)
    new = db.get_new_cards(db.get_new_cards_per_day())
    queue = [c["id"] for c in due] + [c["id"] for c in new]
    context.user_data["review"] = {
        "queue": queue,
        "pos": 0,
        "reviewed": 0,
        "new_count": len(new),
        "due_count": len(due),
    }
    return len(queue)


def _rating_keyboard(card: dict) -> InlineKeyboardMarkup:
    today = date.today()
    btns = []
    for rating, label in (("again", "Again"), ("hard", "Hard"), ("good", "Good"), ("easy", "Easy")):
        st = srs.rate(
            interval=card["interval"],
            ease_factor=card["ease_factor"],
            repetitions=card["repetitions"],
            rating=rating,
            today=today,
        )
        btns.append(
            InlineKeyboardButton(f"{label} ({_fmt_interval(st.interval)})",
                                 callback_data=f"rev:rate:{rating}")
        )
    return InlineKeyboardMarkup([btns[:2], btns[2:], [InlineKeyboardButton("Stop", callback_data="rev:stop")]])


async def _present_front(send, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the front of the current card with a Show-answer button.

    `send` is an awaitable-returning callable (reply_text or edit_message_text style).
    """
    state = context.user_data.get("review")
    if not state or state["pos"] >= len(state["queue"]):
        await _finish(send, context)
        return
    card = db.get_card(state["queue"][state["pos"]])
    if card is None:  # deleted mid-session; skip
        state["pos"] += 1
        await _present_front(send, context)
        return
    n = len(state["queue"]) - state["pos"]
    text = f"Card ({n} left)\n\n{card['front']}"
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Show answer", callback_data="rev:show")],
            [InlineKeyboardButton("Stop", callback_data="rev:stop")],
        ]
    )
    await send(text, kb)


async def _finish(send, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = context.user_data.pop("review", None)
    reviewed = state["reviewed"] if state else 0
    if reviewed == 0:
        await send("Session ended — no cards rated. Send /review to start again.", None)
        return
    streak = db.mark_active()
    text = (
        f"Session done — {reviewed} card(s) reviewed. \U0001f44d\n"
        f"Streak: {streak['current']} day(s) (best {streak['longest']}).\n\n"
        f"Now write a few sentences? /write"
    )
    await send(text, None)


def _sender_from_message(message):
    # Plain text: card content is user/AI-generated and may contain Markdown
    # metacharacters that would otherwise 400 the request mid-session.
    async def send(text, kb):
        await message.reply_text(text, reply_markup=kb)
    return send


def _sender_from_query(query):
    async def send(text, kb):
        await query.edit_message_text(text, reply_markup=kb)
    return send


@owner_only
async def cmd_review(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    size = build_session(context)
    if size == 0:
        await update.effective_message.reply_text(
            "Nothing due right now, and no new cards waiting. \U0001f389\n"
            "Add cards with /write or /lesson, or seed a starter deck."
        )
        context.user_data.pop("review", None)
        return
    st = context.user_data["review"]
    await update.effective_message.reply_text(
        f"Review time: {st['due_count']} due + {st['new_count']} new = {size} cards."
    )
    await _present_front(_sender_from_message(update.effective_message), context)


@owner_only
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    action = parts[1]
    state = context.user_data.get("review")

    if action == "stop":
        send = _sender_from_query(query)
        await _finish(send, context)
        return

    if state is None:
        await query.edit_message_text("That session has ended. Start a new one with /review.")
        return

    if action == "show":
        card = db.get_card(state["queue"][state["pos"]])
        if card is None:
            state["pos"] += 1
            await _present_front(_sender_from_query(query), context)
            return
        text = f"{card['front']}\n\n———\n{card['back']}\n\nHow well did you recall it?"
        await query.edit_message_text(text, reply_markup=_rating_keyboard(card))
        return

    if action == "rate":
        rating = parts[2]
        card = db.get_card(state["queue"][state["pos"]])
        if card is not None:
            st = srs.rate(
                interval=card["interval"],
                ease_factor=card["ease_factor"],
                repetitions=card["repetitions"],
                rating=rating,
                today=date.today(),
            )
            db.update_card_srs(
                card["id"],
                interval=st.interval,
                ease_factor=st.ease_factor,
                repetitions=st.repetitions,
                due_date=st.due_date,
            )
            state["reviewed"] += 1
            if st.is_lapse:
                state["queue"].append(card["id"])  # see it again this session
        state["pos"] += 1
        await _present_front(_sender_from_query(query), context)
        return


def handlers() -> list:
    return [
        CommandHandler("review", cmd_review),
        CallbackQueryHandler(on_callback, pattern=r"^rev:"),
    ]
