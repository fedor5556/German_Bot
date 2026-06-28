"""Lesson capture -- "feed me your lesson".

Paste lesson notes / teacher corrections; Gemini extracts new vocab + your
corrected mistakes into flashcards. This is how every teacher correction gets
banked -- a ROUTINE.md hard rule (your own mistakes are your highest-value cards).
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

import config
import db
import gemini
from auth import owner_only

log = logging.getLogger(__name__)


@owner_only
async def cmd_lesson(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["await"] = {"kind": "lesson"}
    await update.effective_message.reply_text(
        "\U0001f4dd *Lesson capture*\n\n"
        "Paste your lesson notes, vocabulary, and every correction your teacher "
        "made. I'll turn them into flashcards. Send /cancel to abort.",
        parse_mode="Markdown",
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Process pasted lesson notes. Called by the central dispatcher."""
    context.user_data.pop("await", None)
    notes = (update.effective_message.text or "").strip()
    message = update.effective_message

    if len(notes) < 5:
        await message.reply_text("That looks empty — paste your lesson notes and I'll process them.")
        return

    streak = db.mark_active()  # processing a lesson is real study

    if not config.gemini_configured():
        await message.reply_text(
            "I can't extract cards without Gemini (add GEMINI_API_KEY to .env). "
            "Your notes are saved in this chat though.\n"
            f"Streak: {streak['current']} day(s)."
        )
        return

    thinking = await message.reply_text("Reading your lesson…")
    try:
        result = await gemini.extract_lesson(notes)
    except gemini.GeminiNotConfigured:
        await thinking.edit_text("Gemini isn't configured, so I can't extract cards right now.")
        return
    except gemini.GeminiError as exc:
        log.warning("lesson extraction failed: %s", exc)
        await thinking.edit_text("The AI hit an error reading that. Try again in a moment.")
        return

    summary = result.get("summary", "").strip()
    cards = result.get("cards", [])
    added = db.add_cards([(c["front"], c["back"]) for c in cards], source="lesson") if cards else 0
    # Bank the teacher's corrections into the mistake-pattern engine (vocab cards
    # carry no category and are skipped).
    for c in cards:
        if c.get("kind") == "correction" and c.get("category"):
            db.log_mistake(c["category"], c.get("front", ""), source="lesson")

    out = []
    if summary:
        out.append(f"\U0001f4d8 {summary}")
    out.append(f"Banked {added} card(s) from this lesson.")
    out.append("")
    out.append(f"Streak: {streak['current']} day(s). Review them with /review.")
    await thinking.edit_text("\n".join(out))


def handlers() -> list:
    return [CommandHandler("lesson", cmd_lesson)]
