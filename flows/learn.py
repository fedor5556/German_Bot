"""/learn -- guided intake of brand-new vocabulary (the missing "teach me" step).

The deck already fills with never-reviewed cards -- from the seed deck, lessons,
and your own writing mistakes -- but nothing ever walked you THROUGH them. The
split is deliberate:

  - /review *tests* you (active recall, SRS scheduling).
  - /learn  *introduces* new words: each is shown with its English meaning and an
    example, no pressure, then you can drop the batch straight into a review.

It is purely button-driven (no typed input), so it never competes with the
central text dispatcher. Reaching the end credits the day's streak (seeing your
new words is real effort -- Core-only still keeps the streak going).
"""

from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

import db
from auth import owner_only
from flows import review

log = logging.getLogger(__name__)

DEFAULT_BATCH = 5
MAX_BATCH = 15


def _batch_size(context: ContextTypes.DEFAULT_TYPE) -> int:
    """How many new words this run -- /learn N, clamped to a sane range."""
    n = DEFAULT_BATCH
    if getattr(context, "args", None):
        try:
            n = int(context.args[0])
        except (ValueError, TypeError, IndexError):
            n = DEFAULT_BATCH
    return max(1, min(MAX_BATCH, n))


def _card_text(card: dict, n_left: int) -> str:
    """Render one new word as a low-pressure intake card (meaning always shown)."""
    lines = [f"\U0001f4d6 New word ({n_left} left)", ""]
    enrich = card.get("enrich") or {}
    word = enrich.get("word")
    clue = enrich.get("clue_en")
    if word:
        lines.append(f"{word} — {clue}" if clue else str(word))
        lines.append("")
    lines.append(card["front"])
    lines.append("———")
    lines.append(card["back"])
    return "\n".join(lines)


def _step_controls() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Next ▶", callback_data="lrn:next")],
            [InlineKeyboardButton("Stop", callback_data="lrn:stop")],
        ]
    )


def _end_controls() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Test these now ▶", callback_data="lrn:test")],
            [InlineKeyboardButton("Done", callback_data="lrn:done")],
        ]
    )


# --- senders (plain text: card content may contain Markdown metacharacters) -----

def _sender_from_message(message):
    async def send(text, kb):
        await message.reply_text(text, reply_markup=kb)
    return send


def _sender_from_query(query):
    async def send(text, kb):
        await query.edit_message_text(text, reply_markup=kb)
    return send


# --- presentation --------------------------------------------------------------

async def _present(send, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = context.user_data.get("learn")
    if not state:
        return
    if state["pos"] >= len(state["queue"]):
        await _finish(send, context)
        return
    card = db.get_card(state["queue"][state["pos"]])
    if card is None:  # deleted mid-session; skip it
        state["pos"] += 1
        await _present(send, context)
        return
    n_left = len(state["queue"]) - state["pos"]
    await send(_card_text(card, n_left), _step_controls())


async def _finish(send, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = context.user_data.get("learn")
    if not state:
        await send("Done. Come back with /learn anytime.", None)
        return
    streak = db.mark_active()
    n = len(state["queue"])
    text = (
        f"That's {n} new word(s) seen. \U0001f44d\n"
        f"Streak: {streak['current']} day(s).\n\n"
        f"Lock them in now with a quick review?"
    )
    await send(text, _end_controls())  # state kept so 'Test these now' has the ids


# --- commands & callbacks ------------------------------------------------------

@owner_only
async def cmd_learn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # A fresh intake supersedes any pending typed step / review / drill.
    context.user_data.pop("review", None)
    context.user_data.pop("drill", None)
    context.user_data.pop("await", None)

    cards = db.get_new_cards(_batch_size(context))
    if not cards:
        await update.effective_message.reply_text(
            "No new words waiting right now. \U0001f389\n"
            "Add some with /lesson or /write, or /seed the starter deck. "
            "Meanwhile /review keeps your current cards fresh."
        )
        return

    context.user_data["learn"] = {"queue": [c["id"] for c in cards], "pos": 0}
    await update.effective_message.reply_text(
        f"Let's learn {len(cards)} new word(s). I'll show each with its meaning — no test yet."
    )
    await _present(_sender_from_message(update.effective_message), context)


@owner_only
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    action = query.data.split(":")[1]
    state = context.user_data.get("learn")
    send = _sender_from_query(query)

    if action == "stop":
        await query.answer()
        context.user_data.pop("learn", None)
        await query.edit_message_text("Stopped. Come back with /learn anytime.")
        return

    if state is None:
        await query.answer()
        await query.edit_message_text("That session has ended. Start again with /learn.")
        return

    if action == "next":
        await query.answer()
        state["pos"] += 1
        await _present(send, context)
        return

    if action == "test":
        await query.answer("Starting a quick review…")
        ids = list(state.get("queue") or [])
        context.user_data.pop("learn", None)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:  # noqa: BLE001 - keyboard may already be gone
            pass
        await review.begin_over_ids(query.message, context, ids)
        return

    if action == "done":
        await query.answer()
        context.user_data.pop("learn", None)
        await query.edit_message_text("Nice. Those words are in your deck — /review when you're ready.")
        return

    await query.answer()


def handlers() -> list:
    return [
        CommandHandler("learn", cmd_learn),
        CallbackQueryHandler(on_callback, pattern=r"^lrn:"),
    ]
