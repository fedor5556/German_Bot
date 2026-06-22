"""German bot -- entrypoint.

Runtime guarantees baked in from the deployment guide:
- Fail-closed config check before anything starts (no token / no owner => exit).
- init_db() at startup, every boot (idempotent) -- a fresh DB must not crash.
- Single-instance guard + run_polling(drop_pending_updates=True): one token,
  one poller, no silent 409 (guide s7.5).
- No emoji in any print()/log -- this runs in raw cmd.exe on the server (guide s9).
- All ops/admin (update, restart, logs, backup) come from the Admin Hub; this
  bot implements ONLY learning commands.
"""

from __future__ import annotations

import logging
import sys
from datetime import date
from logging.handlers import RotatingFileHandler

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
import db
import scheduler
import seedloader
from auth import owner_only
from flows import lesson, review
from flows import schedule as schedule_flow
from flows import writing

log = logging.getLogger("german_bot")

# --- single-instance guard (Windows file lock) ---------------------------------
_LOCK_FH = None
try:
    import msvcrt  # Windows only -- the target host
except ImportError:  # pragma: no cover - dev convenience on non-Windows
    msvcrt = None


def acquire_single_instance_lock() -> bool:
    """Hold an OS file lock for the process lifetime; False if another holds it."""
    global _LOCK_FH
    if msvcrt is None:
        return True
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    _LOCK_FH = open(config.LOCK_PATH, "a+")
    try:
        _LOCK_FH.seek(0)
        if not _LOCK_FH.read(1):
            _LOCK_FH.write("x")
            _LOCK_FH.flush()
        _LOCK_FH.seek(0)
        msvcrt.locking(_LOCK_FH.fileno(), msvcrt.LK_NBLCK, 1)
        return True
    except OSError:
        return False


# --- logging -------------------------------------------------------------------

def setup_logging() -> None:
    """The bot OWNS its log file -- a single rotating handler writes logs/german_bot.log.

    Do NOT let the launcher tee stdout into the log (the old guide s8 approach): under
    the Admin Hub runner this process is spawned with stdout=DEVNULL, so a tee captures
    nothing and /logs goes blank. A sole in-process writer logs identically whether we
    are launched by COMPLETE_LAUNCH.bat or adopted by runner.py (Windows log-discipline
    rule). stdout is still echoed for the interactive .bat case; UTF-8 so non-cp1252
    glyphs in user content never crash a write."""
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    file_handler = RotatingFileHandler(
        config.LOG_DIR / "german_bot.log",
        maxBytes=2_000_000, backupCount=5, encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()  # idempotent: never stack handlers on a re-init
    root.addHandler(file_handler)
    root.addHandler(stream_handler)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


# --- basic commands ------------------------------------------------------------

HELP_TEXT = (
    "*German tutor bot* \U0001f1e9\U0001f1ea\n\n"
    "Your single daily home for German. Commands:\n"
    "• /review — spaced-repetition cards (due + new)\n"
    "• /write — a writing prompt, then AI correction\n"
    "• /lesson — paste lesson notes → flashcards\n"
    "• /schedule — set which days are lesson days\n"
    "• /today — today's plan\n"
    "• /stats — streak & deck size\n"
    "• /seed — import the Goethe B1 starter deck\n"
    "• /backup — DM yourself a copy of the SRS database\n"
    "• /cancel — abort the current step\n\n"
    "Daily push: morning plan + evening check-in (Cyprus time).\n"
    "Rule of thumb: *produce first, AI checks after.* A bad day = Core only "
    "still keeps the streak."
)


@owner_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Capture the owner's chat id so daily pushes have a target.
    chat = update.effective_chat
    if chat:
        db.set_setting("owner_chat_id", str(chat.id))
    await update.effective_message.reply_text(
        "Willkommen! I'm your German study bot.\n\n" + HELP_TEXT, parse_mode="Markdown"
    )


@owner_only
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP_TEXT, parse_mode="Markdown")


@owner_only
async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        scheduler._morning_message(date.today()), parse_mode="Markdown"
    )


@owner_only
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    today = date.today()
    streak = db.get_streak()
    text = (
        f"*Deck:* {db.count_cards()} cards "
        f"({db.count_due(today)} due, {db.count_new()} new)\n"
        f"*Streak:* {streak['current']} day(s) — best {streak['longest']}"
    )
    await update.effective_message.reply_text(text, parse_mode="Markdown")


@owner_only
async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    import tempfile
    from datetime import datetime
    from pathlib import Path

    msg = await update.effective_message.reply_text("Making a backup…")
    dest = Path(tempfile.gettempdir()) / f"german_backup_{datetime.now():%Y%m%d_%H%M%S}.db"
    try:
        db.make_backup(dest)
        with open(dest, "rb") as fh:
            await update.effective_message.reply_document(
                fh, filename=dest.name, caption=f"SRS backup — {db.count_cards()} cards."
            )
        await msg.delete()
    except Exception as exc:  # noqa: BLE001
        log.error("manual backup failed: %s", exc)
        await msg.edit_text("Backup failed — check the logs.")
    finally:
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass


@owner_only
async def cmd_seed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    added = seedloader.load_starter()
    if added:
        await update.effective_message.reply_text(
            f"Imported {added} new starter card(s). Start with /review."
        )
    else:
        await update.effective_message.reply_text(
            "No new cards added — the starter deck is already imported."
        )


@owner_only
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    had = context.user_data.pop("await", None)
    context.user_data.pop("review", None)
    await update.effective_message.reply_text(
        "Cancelled." if had else "Nothing to cancel."
    )


@owner_only
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route free text to whatever step the user is in (writing / lesson / time)."""
    state = context.user_data.get("await")
    if not state:
        await update.effective_message.reply_text(
            "Not sure what to do with that. Try /review, /write, /lesson or /help."
        )
        return
    kind = state.get("kind")
    if kind == "writing":
        await writing.handle_text(update, context, state.get("targets", []))
    elif kind == "lesson":
        await lesson.handle_text(update, context)
    elif kind == "time":
        await schedule_flow.apply_time_text(update, context, state["weekday"])
    else:
        context.user_data.pop("await", None)
        await update.effective_message.reply_text("Lost track of that — try again.")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled exception while handling an update", exc_info=context.error)
    chat_id = scheduler._owner_chat_id()
    if chat_id is not None:
        try:
            await context.bot.send_message(
                chat_id, f"⚠️ Bot error: {type(context.error).__name__}. Logged."
            )
        except Exception:  # noqa: BLE001 - never let the error handler raise
            pass


async def post_init(application: Application) -> None:
    scheduler.register_jobs(application)
    try:
        await application.bot.set_my_commands(
            [
                ("review", "Review due + new cards"),
                ("write", "Writing prompt + AI correction"),
                ("lesson", "Bank a lesson into cards"),
                ("schedule", "View/edit lesson days"),
                ("today", "Today's plan"),
                ("stats", "Streak & deck stats"),
                ("backup", "DM yourself a DB backup"),
                ("help", "Help"),
            ]
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("set_my_commands failed: %s", exc)


def build_application() -> Application:
    application = (
        Application.builder().token(config.TELEGRAM_TOKEN).post_init(post_init).build()
    )

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("today", cmd_today))
    application.add_handler(CommandHandler("stats", cmd_stats))
    application.add_handler(CommandHandler("seed", cmd_seed))
    application.add_handler(CommandHandler("backup", cmd_backup))
    application.add_handler(CommandHandler("cancel", cmd_cancel))

    for handler in review.handlers():
        application.add_handler(handler)
    for handler in writing.handlers():
        application.add_handler(handler)
    for handler in lesson.handlers():
        application.add_handler(handler)
    for handler in schedule_flow.handlers():
        application.add_handler(handler)

    # Catch-all free text (must come after command handlers).
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, on_text)
    )
    application.add_error_handler(on_error)
    return application


def main() -> int:
    setup_logging()

    problems = config.validate()
    if problems:
        for p in problems:
            log.error("CONFIG: %s", p)
        log.error("Refusing to start. Fix .env and relaunch.")
        return 1

    if not acquire_single_instance_lock():
        log.error("Another instance is already running (lock held). Exiting.")
        return 1

    db.init_db()
    removed = db.cleanup_old_overrides(date.today())
    if removed:
        log.info("Cleaned %d expired schedule override(s)", removed)

    log.info("Starting German bot (model=%s, tz=%s)", config.GEMINI_MODEL, config.PUSH_TZ_NAME)
    application = build_application()
    # One token = one poller; drop the backlog so a restart can't hit a 409.
    application.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
    return 0


if __name__ == "__main__":
    sys.exit(main())
