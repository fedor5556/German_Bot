"""Shared micro-exercise drill engine.

One mechanic, two users:
  - /grammar  -> drills on the day's grammar-cycle point (Feature 3).
  - /mistakes -> "drill my worst category" (Feature 2).

Gemini generates 3-5 short exercises (fill / transform / fix); the learner types
each answer; we grade it (fuzzy-local first, Gemini as a semantic fallback for the
open-ended ones). A wrong answer is banked as a flashcard AND logged to the
mistake-pattern engine, so drilling a weakness also measures it.
"""

from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, ContextTypes

import config
import db
import gemini
import recall

log = logging.getLogger(__name__)

TYPE_LABEL = {"fill": "Fill in the blank", "transform": "Transform", "fix": "Fix the error"}

_CONTROLS = InlineKeyboardMarkup(
    [[InlineKeyboardButton("Skip", callback_data="drl:skip"), InlineKeyboardButton("Stop", callback_data="drl:stop")]]
)


async def _send(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, kb=None) -> None:
    await context.bot.send_message(chat_id, text, reply_markup=kb)


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    topic: str,
    source: str,
    category: str | None = None,
) -> None:
    """Generate a drill set on `topic` and present the first exercise."""
    message = update.effective_message
    chat_id = update.effective_chat.id

    # A fresh drill supersedes any other pending typed step.
    context.user_data.pop("review", None)
    context.user_data.pop("await", None)

    if not config.gemini_configured():
        await message.reply_text(
            "Drills need Gemini (add GEMINI_API_KEY to .env). "
            "Meanwhile, /review and /write still work offline."
        )
        return

    thinking = await message.reply_text(f"Building exercises on “{topic}”…")
    try:
        exercises = await gemini.generate_drills(topic, n=4)
    except (gemini.GeminiError, gemini.GeminiNotConfigured) as exc:
        log.warning("drill generation failed: %s", exc)
        await thinking.edit_text("The AI couldn't build drills just now. Try again in a moment.")
        return

    context.user_data["drill"] = {
        "topic": topic,
        "source": source,
        "category": category or recall.canonical_category(topic),
        "exercises": exercises,
        "pos": 0,
        "correct": 0,
        "made_cards": 0,
        "chat_id": chat_id,
        "gemini_ok": True,
    }
    await thinking.edit_text(
        f"{len(exercises)} exercises on “{topic}”. Type each answer; I'll check it."
    )
    await _present(context)


async def _present(context: ContextTypes.DEFAULT_TYPE) -> None:
    state = context.user_data.get("drill")
    if not state:
        return
    if state["pos"] >= len(state["exercises"]):
        await _finish(context)
        return
    ex = state["exercises"][state["pos"]]
    k, total = state["pos"] + 1, len(state["exercises"])
    label = TYPE_LABEL.get(ex["type"], "Exercise")
    context.user_data["await"] = {"kind": "drill_answer"}
    await _send(
        context, state["chat_id"],
        f"Exercise {k}/{total} — {label}\n\n{ex['prompt']}\n\n(type your answer)",
        _CONTROLS,
    )


async def handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Grade a typed drill answer. Routed from the central text dispatcher."""
    state = context.user_data.get("drill")
    message = update.effective_message
    if not state or state["pos"] >= len(state["exercises"]):
        context.user_data.pop("await", None)
        await message.reply_text("That drill has ended. Try /grammar or /mistakes.")
        return

    ex = state["exercises"][state["pos"]]
    typed = (message.text or "").strip()
    context.user_data.pop("await", None)

    ok = recall.check_answer(ex["answer"], typed, accept=ex.get("accept"))
    feedback = ex.get("explanation", "")
    if not ok and state["gemini_ok"]:
        try:
            res = await gemini.verify_drill_answer(ex["prompt"], ex["answer"], typed)
            ok = res["ok"]
            feedback = res["feedback"] or feedback
        except (gemini.GeminiError, gemini.GeminiNotConfigured) as exc:
            log.info("drill verify failed: %s", exc)

    lines = ["✅ Correct!" if ok else "❌ Not quite."]
    if not ok:
        lines.append(f"Answer: {ex['answer']}")
        db.log_mistake(state["category"], ex["answer"], source="drill")
        made = db.add_cards([(ex["answer"], _card_back(ex))], source="drill")
        state["made_cards"] += made
        if made:
            lines.append("Added to your deck for review.")
    else:
        state["correct"] += 1
    if feedback:
        lines.append(f"\U0001f4a1 {feedback}")

    await _send(context, state["chat_id"], "\n".join(lines))
    state["pos"] += 1
    await _present(context)


def _card_back(ex: dict) -> str:
    note = ex.get("explanation", "").strip()
    return note or "Correct form. (drill)"


async def _finish(context: ContextTypes.DEFAULT_TYPE) -> None:
    state = context.user_data.pop("drill", None)
    context.user_data.pop("await", None)
    if not state:
        return
    streak = db.mark_active()
    total = len(state["exercises"])
    lines = [f"Drill done — {state['correct']}/{total} correct on “{state['topic']}”."]
    if state["made_cards"]:
        lines.append(f"{state['made_cards']} card(s) added from your misses — review them with /review.")
    lines.append(f"Streak: {streak['current']} day(s).")
    await _send(context, state["chat_id"], "\n".join(lines))


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    action = query.data.split(":")[1]
    state = context.user_data.get("drill")

    if action == "stop":
        context.user_data.pop("await", None)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:  # noqa: BLE001 - keyboard may already be gone
            pass
        await _finish(context)
        return

    if action == "skip":
        if not state or state["pos"] >= len(state["exercises"]):
            return
        ex = state["exercises"][state["pos"]]
        context.user_data.pop("await", None)
        note = ex.get("explanation", "")
        text = f"Skipped.\nAnswer: {ex['answer']}"
        if note:
            text += f"\n\U0001f4a1 {note}"
        await query.edit_message_text(text)
        state["pos"] += 1
        await _present(context)
        return


def handlers() -> list:
    return [CallbackQueryHandler(on_callback, pattern=r"^drl:")]
