"""Review session flow -- the SRS in chat, now with an active-recall variety pack.

Same SM-2 scheduling underneath (srs.py is untouched): we only vary how each card
is *presented*, picked from its SRS state by recall.choose_mode:

  - recognition   DE -> EN, self-rated reveal (new + hard cards)
  - cloze         the example sentence with the word blanked; you type it
  - production    English clue -> you type the German word
  - use           write your own sentence with the word; Gemini checks it
  - multiple choice  low-effort "bad day" mode that still keeps the streak

Typed/produced recall is far stickier than a self-rated reveal, and rotating modes
is the anti-boredom win. A card rated "Again" is requeued to the end of the session.
"""

from __future__ import annotations

import logging
import random
from datetime import date

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

import config
import db
import gemini
import recall
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


def _install_session(
    context: ContextTypes.DEFAULT_TYPE,
    queue: list[int],
    *,
    due_count: int,
    new_count: int,
    low_energy: bool,
) -> int:
    context.user_data["review"] = {
        "queue": queue,
        "pos": 0,
        "reviewed": 0,
        "new_count": new_count,
        "due_count": due_count,
        "low_energy": low_energy,
        "deck_size": db.count_cards(),
        "gemini_ok": config.gemini_configured(),
        "current": None,
    }
    return len(queue)


def build_session(context: ContextTypes.DEFAULT_TYPE, *, low_energy: bool = False) -> int:
    """Populate context.user_data['review'] with today's queue. Returns its size."""
    today = date.today()
    due = db.get_due_cards(today)
    new = db.get_new_cards(db.get_new_cards_per_day())
    queue = [c["id"] for c in due] + [c["id"] for c in new]
    return _install_session(
        context, queue, due_count=len(due), new_count=len(new), low_energy=low_energy
    )


async def begin_over_ids(message, context: ContextTypes.DEFAULT_TYPE, card_ids: list[int]) -> None:
    """Start a review session over an explicit list of card ids and present the first.

    Used by /learn's 'Test these now' so words you just had introduced go straight
    into the SRS through the normal review path (no separate scheduling code).
    """
    ids = [cid for cid in card_ids if db.get_card(cid) is not None]
    if not ids:
        await message.reply_text("Those cards aren't available to review. Try /review.")
        return
    _install_session(context, ids, due_count=0, new_count=len(ids), low_energy=False)
    await _present(_sender_from_message(message), context)


# --- keyboards -----------------------------------------------------------------

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
            InlineKeyboardButton(
                f"{label} ({_fmt_interval(st.interval)})", callback_data=f"rev:rate:{rating}"
            )
        )
    return InlineKeyboardMarkup([btns[:2], btns[2:], [InlineKeyboardButton("Stop", callback_data="rev:stop")]])


_STOP_ROW = [InlineKeyboardButton("Stop", callback_data="rev:stop")]


# --- senders (plain text: card content may contain Markdown metacharacters) -----

def _sender_from_message(message):
    async def send(text, kb):
        await message.reply_text(text, reply_markup=kb)
    return send


def _sender_from_query(query):
    async def send(text, kb):
        await query.edit_message_text(text, reply_markup=kb)
    return send


# --- enrichment ----------------------------------------------------------------

async def _ensure_enriched(card: dict, gemini_ok: bool) -> dict | None:
    """Return the card's recall metadata, generating + caching it on first need."""
    enrich = card.get("enrich")
    if enrich and enrich.get("word") and enrich.get("clue_en") and enrich.get("cloze"):
        return enrich
    if not gemini_ok:
        return None
    try:
        enrich = await gemini.enrich_card(card["front"], card["back"])
    except (gemini.GeminiError, gemini.GeminiNotConfigured) as exc:
        log.info("enrich failed for card %s: %s", card["id"], exc)
        return None
    db.set_card_enrichment(card["id"], enrich)
    card["enrich"] = enrich
    return enrich


def _mc_options(card: dict) -> tuple[list[str], int, str] | None:
    """Build (options, correct_index, correct_text) for multiple choice, or None."""
    correct = recall.clean_english(card["back"])
    if not correct:
        return None
    pool: list[str] = []
    for back in db.random_distractor_backs(card["id"], limit=15):
        opt = recall.clean_english(back)
        if opt and opt.casefold() != correct.casefold() and opt not in pool:
            pool.append(opt)
    if len(pool) < 3:
        return None
    options = random.sample(pool, 3) + [correct]
    random.shuffle(options)
    return options, options.index(correct), correct


# --- presentation --------------------------------------------------------------

async def _present(send, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the current card in the mode chosen for its SRS state."""
    state = context.user_data.get("review")
    if not state or state["pos"] >= len(state["queue"]):
        await _finish(send, context)
        return
    card = db.get_card(state["queue"][state["pos"]])
    if card is None:  # deleted mid-session; skip
        state["pos"] += 1
        await _present(send, context)
        return

    context.user_data.pop("await", None)  # default: this card takes button taps
    enrich = await _ensure_enriched(card, state["gemini_ok"])
    enriched = enrich is not None
    mode = recall.choose_mode(
        card_id=card["id"],
        repetitions=card["repetitions"],
        ease_factor=card["ease_factor"],
        low_energy=state["low_energy"],
        gemini_ok=state["gemini_ok"],
        enriched=enriched,
        deck_size=state["deck_size"],
    )

    n_left = len(state["queue"]) - state["pos"]
    head = f"Card ({n_left} left)"

    if mode == recall.MULTIPLE_CHOICE:
        built = _mc_options(card)
        if built is None:
            mode = recall.RECOGNITION
        else:
            options, correct_idx, correct_text = built
            state["current"] = {"mode": mode, "card_id": card["id"], "answer": correct_text, "answered": False}
            rows = [
                [InlineKeyboardButton(opt[:90], callback_data=f"rev:mc:{1 if i == correct_idx else 0}")]
                for i, opt in enumerate(options)
            ]
            rows.append(_STOP_ROW)
            await send(f"{head}\n\nWhat does this mean?\n\n{card['front']}", InlineKeyboardMarkup(rows))
            return

    if mode == recall.CLOZE and enriched:
        state["current"] = {
            "mode": mode, "card_id": card["id"],
            # 'answer' (surface form) may be absent on a hand-edited/partial cached
            # row; fall back to the word, which the enrichment guard guarantees.
            "answer": enrich.get("answer") or enrich["word"], "accept": [enrich["word"]],
            "article_ok": True, "answered": False,
        }
        context.user_data["await"] = {"kind": "review_answer"}
        rows = [[InlineKeyboardButton("Show answer", callback_data="rev:show")]]
        if state["gemini_ok"]:
            rows.append([InlineKeyboardButton("\U0001f504 New sentence", callback_data="rev:fresh")])
        rows.append(_STOP_ROW)
        # Always show the sentence's English meaning -- a cloze with no translation
        # leaves you stuck if you don't already know the sentence.
        meaning = recall.clean_english(card["back"])
        hint = f"\n\n(Meaning: {meaning})" if meaning else ""
        await send(
            f"{head}\n\nFill in the blank — type the missing word:\n\n{enrich['cloze']}{hint}",
            InlineKeyboardMarkup(rows),
        )
        return

    if mode == recall.PRODUCTION and enriched:
        state["current"] = {
            "mode": mode, "card_id": card["id"],
            "answer": enrich["word"], "accept": [enrich.get("answer", "")],
            "article_ok": True, "answered": False,
        }
        context.user_data["await"] = {"kind": "review_answer"}
        rows = [[InlineKeyboardButton("Show answer", callback_data="rev:show")], _STOP_ROW]
        await send(
            f"{head}\n\nSay it in German — type the word for:\n\n“{enrich['clue_en']}”",
            InlineKeyboardMarkup(rows),
        )
        return

    if mode == recall.USE_SENTENCE and enriched:
        state["current"] = {
            "mode": mode, "card_id": card["id"],
            "word": enrich["word"], "clue_en": enrich["clue_en"],
            "answer": enrich["word"], "answered": False,
        }
        context.user_data["await"] = {"kind": "review_answer"}
        rows = [[InlineKeyboardButton("Skip", callback_data="rev:skip")], _STOP_ROW]
        await send(
            f"{head}\n\nUse this word in your own German sentence:\n\n"
            f"{enrich['word']}  ({enrich['clue_en']})",
            InlineKeyboardMarkup(rows),
        )
        return

    # Default / fallback: recognition (DE -> EN, self-rated reveal).
    state["current"] = {"mode": recall.RECOGNITION, "card_id": card["id"], "answered": False}
    rows = [[InlineKeyboardButton("Show answer", callback_data="rev:show")], _STOP_ROW]
    await send(f"{head}\n\n{card['front']}", InlineKeyboardMarkup(rows))


# --- reveal / grading ----------------------------------------------------------

def _reveal_text(card: dict, *, result: str, extra: str = "") -> str:
    body = f"{result}\n\n{card['front']}\n———\n{card['back']}"
    if extra:
        body += f"\n\n{extra}"
    # The time on each button is the next-review date that rating schedules --
    # spell it out so the "(1d)" labels aren't a mystery.
    return body + "\n\nHow well did you recall it? (each button shows when you'll see this card again)"


async def _reveal_and_rate(send, context: ContextTypes.DEFAULT_TYPE, *, result: str, extra: str = "") -> None:
    state = context.user_data["review"]
    cur = state["current"]
    cur["answered"] = True
    context.user_data.pop("await", None)
    card = db.get_card(cur["card_id"])
    if card is None:  # vanished; just move on
        state["pos"] += 1
        await _present(send, context)
        return
    await send(_reveal_text(card, result=result, extra=extra), _rating_keyboard(card))


async def _apply_and_advance(send, context: ContextTypes.DEFAULT_TYPE, rating: str) -> None:
    state = context.user_data["review"]
    cur = state.get("current") or {}
    card = db.get_card(cur.get("card_id")) if cur.get("card_id") else None
    if card is not None:
        st = srs.rate(
            interval=card["interval"], ease_factor=card["ease_factor"],
            repetitions=card["repetitions"], rating=rating, today=date.today(),
        )
        db.update_card_srs(
            card["id"], interval=st.interval, ease_factor=st.ease_factor,
            repetitions=st.repetitions, due_date=st.due_date,
        )
        state["reviewed"] += 1
        if st.is_lapse:
            state["queue"].append(card["id"])  # see it again this session
    state["pos"] += 1
    state["current"] = None
    await _present(send, context)


async def _finish(send, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = context.user_data.pop("review", None)
    context.user_data.pop("await", None)
    if state is None:
        # A second Stop tap (double-tap race) after the session already ended --
        # don't clobber the real summary with a misleading "no cards rated".
        await send("Session already ended.", None)
        return
    reviewed = state["reviewed"]
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


# --- typed-answer handling (routed from the central text dispatcher) ------------

async def handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = context.user_data.get("review")
    cur = state.get("current") if state else None
    message = update.effective_message
    if not state or not cur or cur.get("answered"):
        context.user_data.pop("await", None)
        await message.reply_text("That review step has passed. Send /review to continue.")
        return

    typed = (message.text or "").strip()
    send = _sender_from_message(message)

    if cur["mode"] == recall.USE_SENTENCE:
        await _grade_use(update, context, typed)
        return

    correct = recall.check_answer(
        cur["answer"], typed, accept=cur.get("accept"), article_insensitive=cur.get("article_ok", False)
    )
    if correct:
        result = "✅ Richtig!"
    else:
        result = f"❌ Not quite — you wrote: {typed}\nAnswer: {cur['answer']}"
    await _reveal_and_rate(send, context, result=result)


async def _grade_use(update: Update, context: ContextTypes.DEFAULT_TYPE, typed: str) -> None:
    state = context.user_data["review"]
    cur = state["current"]
    message = update.effective_message
    send = _sender_from_message(message)
    word = cur.get("word", "")

    if not state["gemini_ok"]:
        await _reveal_and_rate(send, context, result="Saved your sentence (AI check unavailable).")
        return

    thinking = await message.reply_text("Checking your sentence…")
    try:
        res = await gemini.check_usage(word, typed)
    except (gemini.GeminiError, gemini.GeminiNotConfigured) as exc:
        log.info("usage check failed: %s", exc)
        await thinking.delete()
        await _reveal_and_rate(send, context, result="Couldn't AI-check that — rate it yourself.")
        return

    await thinking.delete()
    if not res["ok"] and res.get("category"):
        db.log_mistake(res["category"], res.get("corrected", ""), source="review")
    mark = "✅" if res["ok"] else "❌"
    extra = f"\U0001f4a1 {res['feedback']}" if res.get("feedback") else ""
    if res.get("corrected") and res["corrected"].strip() != typed:
        extra += (f"\nBetter: {res['corrected']}" if extra else f"Better: {res['corrected']}")
    await _reveal_and_rate(send, context, result=f"{mark} (your sentence: {typed})", extra=extra)


# --- commands & callbacks ------------------------------------------------------

@owner_only
async def cmd_review(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    low_energy = bool(context.args) and context.args[0].lower() in ("easy", "mc", "tired", "bad")
    size = build_session(context, low_energy=low_energy)
    if size == 0:
        await update.effective_message.reply_text(
            "Nothing due right now, and no new cards waiting. \U0001f389\n"
            "Add cards with /write or /lesson, or seed a starter deck."
        )
        context.user_data.pop("review", None)
        return
    st = context.user_data["review"]
    mode_note = " (easy mode: multiple choice)" if low_energy else ""
    rows = []
    if not low_energy:
        rows.append([InlineKeyboardButton("Bad day? Easy mode \U0001f4c9", callback_data="rev:easy")])
    await update.effective_message.reply_text(
        f"Review time: {st['due_count']} due + {st['new_count']} new = {size} cards{mode_note}.",
        reply_markup=InlineKeyboardMarkup(rows) if rows else None,
    )
    await _present(_sender_from_message(update.effective_message), context)


@owner_only
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    parts = query.data.split(":")
    action = parts[1]
    state = context.user_data.get("review")

    if action == "stop":
        await query.answer()
        await _finish(_sender_from_query(query), context)
        return

    if state is None:
        await query.answer()
        await query.edit_message_text("That session has ended. Start a new one with /review.")
        return

    cur = state.get("current") or {}
    send = _sender_from_query(query)

    if action == "easy":
        # Flip the flag only -- the current card finishes in its mode; everything
        # from the next card on becomes multiple choice. (Re-presenting here would
        # collide with the already-sent first card, which shares state["current"].)
        state["low_energy"] = True
        await query.answer("Easy mode on — next cards are multiple choice.")
        return

    if action == "mc":  # multiple-choice answer -> one-tap auto-rate
        if cur.get("answered") or cur.get("mode") != recall.MULTIPLE_CHOICE:
            await query.answer()
            return
        cur["answered"] = True
        is_correct = parts[2] == "1"
        await query.answer("✅ Richtig!" if is_correct else f"❌ {cur.get('answer', '')}"[:200])
        await _apply_and_advance(send, context, "good" if is_correct else "again")
        return

    if action == "show":
        await query.answer()
        if cur.get("answered"):
            return
        mode = cur.get("mode")
        if mode in (recall.CLOZE, recall.PRODUCTION):
            ans = cur.get("answer", "")
            await _reveal_and_rate(send, context, result=f"Answer: {ans}")
        else:  # recognition
            await _reveal_and_rate(send, context, result="Answer:")
        return

    if action == "skip":  # use-in-a-sentence: reveal without grading
        await query.answer()
        if not cur.get("answered"):
            await _reveal_and_rate(send, context, result=f"Skipped. Word: {cur.get('word', '')}")
        return

    if action == "fresh":  # regenerate the cloze sentence (anti-memorisation)
        await query.answer("New sentence…")
        await _refresh_cloze(query, context)
        return

    if action == "rate":
        await query.answer()
        if not cur.get("answered"):
            # Guard: only rate after a reveal (recognition/cloze/production/use).
            return
        await _apply_and_advance(send, context, parts[2])
        return

    await query.answer()


async def _refresh_cloze(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = context.user_data["review"]
    cur = state.get("current") or {}
    if cur.get("mode") != recall.CLOZE or cur.get("answered"):
        return
    card = db.get_card(cur["card_id"])
    enrich = (card or {}).get("enrich") or {}
    word, clue = enrich.get("word", ""), enrich.get("clue_en", "")
    if not word:
        return
    try:
        fresh = await gemini.fresh_cloze(word, clue)
    except (gemini.GeminiError, gemini.GeminiNotConfigured) as exc:
        log.info("fresh cloze failed: %s", exc)
        return
    cur["answer"] = fresh["answer"]
    cur["accept"] = [word]
    n_left = len(state["queue"]) - state["pos"]
    rows = [
        [InlineKeyboardButton("Show answer", callback_data="rev:show")],
        [InlineKeyboardButton("\U0001f504 New sentence", callback_data="rev:fresh")],
        _STOP_ROW,
    ]
    await query.edit_message_text(
        f"Card ({n_left} left)\n\nFill in the blank — type the missing word:\n\n{fresh['cloze']}",
        reply_markup=InlineKeyboardMarkup(rows),
    )


def handlers() -> list:
    return [
        CommandHandler("review", cmd_review),
        CallbackQueryHandler(on_callback, pattern=r"^rev:"),
    ]
