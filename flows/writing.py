"""Daily writing + Gemini correction (produce first, AI checks after).

The bot sends a prompt with 2-3 target words and will NOT reveal corrections
until the user has written something. Each mistake Gemini finds auto-becomes a
flashcard.
"""

from __future__ import annotations

import logging
import random

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

import config
import db
import gemini
from auth import owner_only

log = logging.getLogger(__name__)

# (English task, German target words). Produce-first: nothing is revealed yet.
WRITING_PROMPTS = [
    ("Describe what you did yesterday evening.", ["gestern", "danach", "weil"]),
    ("Write about your plans for the weekend.", ["am Wochenende", "vielleicht", "treffen"]),
    ("Describe your morning routine.", ["normalerweise", "zuerst", "dann"]),
    ("Write about a meal you cooked or ate recently.", ["gekocht", "lecker", "Zutaten"]),
    ("Describe the weather today and how it makes you feel.", ["das Wetter", "deshalb", "draußen"]),
    ("Write about a place you would like to travel to and why.", ["reisen", "würde", "weil"]),
    ("Describe a film or series you watched lately.", ["gesehen", "handelt von", "interessant"]),
    ("Write about your work or studies today.", ["gearbeitet", "musste", "danach"]),
]

EASY_PROMPTS = [
    ("Write 3 short sentences about your day.", ["heute", "ich", "gut"]),
    ("Name three things in your room and one sentence each.", ["es gibt", "mein", "neben"]),
    ("Write what you ate today.", ["gegessen", "getrunken", "war"]),
]


def _pick(prompts) -> tuple[str, list[str]]:
    return random.choice(prompts)


def _prompt_text(task: str, targets: list[str]) -> str:
    words = ", ".join(f"*{w}*" for w in targets)
    return (
        f"✍️ *Writing*\n\n{task}\n\n"
        f"Try to use: {words}\n\n"
        f"Write your attempt in German and send it. I won't show corrections "
        f"until you've written something — the struggle is the learning."
    )


def _keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Too hard? Easier prompt \U0001f4c9", callback_data="wr:easier")]]
    )


@owner_only
async def cmd_write(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    task, targets = _pick(WRITING_PROMPTS)
    context.user_data["await"] = {"kind": "writing", "targets": targets}
    await update.effective_message.reply_text(
        _prompt_text(task, targets), reply_markup=_keyboard(), parse_mode="Markdown"
    )


@owner_only
async def on_easier(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer("Here's an easier one.")
    task, targets = _pick(EASY_PROMPTS)
    context.user_data["await"] = {"kind": "writing", "targets": targets}
    await query.edit_message_text(_prompt_text(task, targets), parse_mode="Markdown")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE, targets: list[str]) -> None:
    """Process the user's German attempt. Called by the central dispatcher."""
    context.user_data.pop("await", None)
    text = (update.effective_message.text or "").strip()
    message = update.effective_message

    # Producing counts toward the streak even if Gemini can't correct it.
    streak = db.mark_active()

    if not config.gemini_configured():
        await message.reply_text(
            "Nice — that counts as your output for today. "
            "(Add GEMINI_API_KEY to .env to get AI corrections.)\n"
            f"Streak: {streak['current']} day(s)."
        )
        return

    thinking = await message.reply_text("Checking your German…")
    try:
        result = await gemini.correct_writing(text, targets)
    except gemini.GeminiNotConfigured:
        await thinking.edit_text("Gemini isn't configured, so I can't correct this right now.")
        return
    except gemini.GeminiError as exc:
        log.warning("correction failed: %s", exc)
        await thinking.edit_text("The AI hit an error correcting that. Try again in a moment.")
        return

    corrected = result.get("corrected", "").strip()
    explanation = result.get("explanation", "").strip()
    mistakes = result.get("mistakes", [])

    added = 0
    if mistakes:
        added = db.add_cards([(m["front"], m["back"]) for m in mistakes], source="writing")
        # Feed the mistake-pattern engine: one logged mistake per correction.
        for m in mistakes:
            db.log_mistake(m.get("category", "Other"), m.get("front", ""), source="writing")

    # Plain text: corrected German / explanation are AI-generated and may contain
    # Markdown metacharacters.
    out = ["Corrected:", corrected]
    if explanation:
        out += ["", f"\U0001f4a1 {explanation}"]
    if added:
        out += ["", f"Added {added} card(s) from your mistakes — you'll review them soon."]
    else:
        out += ["", "Clean — no new cards needed. \U0001f3af"]
    out += ["", f"Streak: {streak['current']} day(s)."]
    await thinking.edit_text("\n".join(out))


def handlers() -> list:
    return [
        CommandHandler("write", cmd_write),
        CallbackQueryHandler(on_easier, pattern=r"^wr:easier$"),
    ]
