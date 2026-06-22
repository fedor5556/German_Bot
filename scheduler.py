"""Timezone-aware daily pushes + weekly self-backup (JobQueue).

JobQueue jobs are in-memory and the bot restarts on every Hub /update, so jobs
are RE-REGISTERED on every startup from the DB (plan Phase 7). All times are
built from PUSH_TZ via zoneinfo, not the server's implicit local time -- you and
the host are both in Cyprus, but this still handles DST and a future move.

Day-of-week logic uses datetime.weekday() (Mon=0..Sun=6), never PTB's `days`
argument, to avoid its Sunday/Monday index ambiguity.
"""

from __future__ import annotations

import logging
import tempfile
from datetime import date, datetime, time as dtime
from pathlib import Path

from telegram.ext import Application, ContextTypes

import config
import db
from flows import schedule as sched

log = logging.getLogger(__name__)

# Grammar cycle from ROUTINE.md -- rotated deterministically by date.
GRAMMAR_CYCLE = [
    "Present perfect (Perfekt)",
    "Cases & articles (Akkusativ/Dativ)",
    "Adjective endings",
    "Two-way prepositions",
    "Subordinate clauses & word order (weil/dass/wenn)",
    "Praeteritum",
    "Konjunktiv II (wuerde/haette/waere)",
    "Reflexive verbs",
    "Relative clauses",
    "Passive",
    "Genitiv",
    "Connectors (deshalb/trotzdem/obwohl)",
]


def grammar_point_for(day: date) -> str:
    return GRAMMAR_CYCLE[day.toordinal() % len(GRAMMAR_CYCLE)]


def _owner_chat_id() -> int | None:
    cid = db.get_setting("owner_chat_id")
    if cid:
        try:
            return int(cid)
        except ValueError:
            pass
    return next(iter(config.OWNER_IDS)) if config.OWNER_IDS else None


def _parse_hhmm(raw: str | None, default: str) -> dtime:
    raw = (raw or default).strip()
    try:
        h, m = (int(x) for x in raw.split(":"))
    except (ValueError, AttributeError):
        h, m = (int(x) for x in default.split(":"))
    return dtime(hour=h, minute=m, tzinfo=config.PUSH_TZ)


# --- push content --------------------------------------------------------------

def _morning_message(day: date) -> str:
    day_type = sched.resolve_for(day)
    due = db.count_due(day)
    new = min(db.get_new_cards_per_day(), db.count_new())
    head = f"\U0001f305 Guten Morgen! ({due} due, {new} new today)"

    if day_type == sched.REVIEW:
        body = (
            "*Sunday — weekly review.*\n"
            "1. /review your due cards.\n"
            "2. Re-drill this week's mistakes.\n"
            "3. Note anything that overwhelmed you (drop it a level).\n"
            "4. Pick next week's grammar point."
        )
    elif day_type == sched.LESSON:
        body = (
            "*Lesson day — keep it light.*\n"
            "1. /review your due cards (Core).\n"
            "2. After your lesson, /lesson to bank every correction.\n"
            "Your own mistakes are your highest-value cards."
        )
    else:  # FULL
        body = (
            "*Full study day.*\n"
            "1. /review due + new cards (Core).\n"
            f"2. Grammar focus: *{grammar_point_for(day)}*.\n"
            "3. /write a short paragraph using today's grammar + new words."
        )
    return head + "\n\n" + body


def _evening_message() -> str:
    streak = db.get_streak()
    today = date.today()
    done_today = streak.get("last_active_date") == today.isoformat()
    due = db.count_due(today)
    if done_today:
        line = f"Nice work today. \U0001f525 Streak: {streak['current']} day(s)."
    else:
        line = (
            "How did today go? Even a *bad day = Core only* keeps the streak alive — "
            "a quick /review or /write counts."
        )
    tail = f"\n\n{due} card(s) still due." if due else ""
    return "\U0001f319 *Evening check-in*\n\n" + line + tail


# --- jobs ----------------------------------------------------------------------

async def morning_push(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = _owner_chat_id()
    if chat_id is None:
        log.warning("morning_push: no owner chat id known yet")
        return
    await context.bot.send_message(chat_id, _morning_message(date.today()), parse_mode="Markdown")


async def evening_push(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = _owner_chat_id()
    if chat_id is None:
        return
    await context.bot.send_message(chat_id, _evening_message(), parse_mode="Markdown")


async def daily_cleanup(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Drop past one-off overrides so temporary schedule changes auto-revert."""
    removed = db.cleanup_old_overrides(date.today())
    if removed:
        log.info("daily_cleanup: removed %d expired override(s)", removed)


async def weekly_backup(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sunday self-backup: DM the SRS DB to the owner (runs daily, acts on Sun)."""
    if date.today().weekday() != 6:  # Sun=6
        return
    chat_id = _owner_chat_id()
    if chat_id is None:
        return
    stamp = datetime.now(config.PUSH_TZ).strftime("%Y%m%d")
    dest = Path(tempfile.gettempdir()) / f"german_backup_{stamp}.db"
    try:
        db.make_backup(dest)
        with open(dest, "rb") as fh:
            await context.bot.send_document(
                chat_id, fh, filename=dest.name,
                caption=f"Weekly SRS backup ({db.count_cards()} cards).",
            )
    except Exception as exc:  # noqa: BLE001 - backup must never kill the loop
        log.error("weekly_backup failed: %s", exc)
    finally:
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass


def register_jobs(application: Application) -> None:
    """(Re)register all daily jobs from the DB schedule. Idempotent per startup."""
    jq = application.job_queue
    if jq is None:
        log.error("JobQueue is not available -- install python-telegram-bot[job-queue]")
        return

    for name in ("morning", "evening", "daily_cleanup", "weekly_backup"):
        for job in jq.get_jobs_by_name(name):
            job.schedule_removal()

    morning = _parse_hhmm(db.get_setting("push_morning"), config.DEFAULT_MORNING_PUSH)
    evening = _parse_hhmm(db.get_setting("push_evening"), config.DEFAULT_EVENING_PUSH)
    jq.run_daily(morning_push, time=morning, name="morning")
    jq.run_daily(evening_push, time=evening, name="evening")
    jq.run_daily(daily_cleanup, time=dtime(0, 5, tzinfo=config.PUSH_TZ), name="daily_cleanup")
    jq.run_daily(weekly_backup, time=dtime(8, 0, tzinfo=config.PUSH_TZ), name="weekly_backup")
    log.info("Registered daily jobs: morning %s, evening %s (%s)", morning, evening, config.PUSH_TZ_NAME)
