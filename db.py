"""SQLite persistence layer.

The DB at data/german.db is the irreplaceable SRS history -- guard it (guide
s13). init_db() is idempotent and MUST be called at the start of german_bot.py
(not just when running this module directly): a fresh/empty DB otherwise crashes
'no such table' and kills the loop (guide s11).

Connections are opened per-operation (sqlite3 connect is cheap) to stay safe
across PTB's event loop + JobQueue callbacks. WAL mode is enabled for durability.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Iterator, Optional

import config
import srs

# Weekday helpers: 0=Mon ... 6=Sun (matches datetime.date.weekday()).
WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """Create the schema if absent and seed defaults. Safe to call every boot."""
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS cards (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                front       TEXT NOT NULL,
                back        TEXT NOT NULL,
                interval    REAL    NOT NULL DEFAULT 0,
                ease_factor REAL    NOT NULL DEFAULT 2.5,
                repetitions INTEGER NOT NULL DEFAULT 0,
                due_date    TEXT,                 -- ISO date; NULL/today => new
                source      TEXT,                 -- seed | writing | lesson | manual
                created_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS schedule (
                weekday       INTEGER PRIMARY KEY,  -- 0=Mon ... 6=Sun
                is_lesson_day INTEGER NOT NULL DEFAULT 0,
                time          TEXT                  -- HH:MM, lesson start (info)
            );

            CREATE TABLE IF NOT EXISTS schedule_overrides (
                date          TEXT PRIMARY KEY,     -- ISO date, one-off change
                is_lesson_day INTEGER NOT NULL,
                time          TEXT
            );

            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS streak (
                id               INTEGER PRIMARY KEY CHECK (id = 1),
                current          INTEGER NOT NULL DEFAULT 0,
                longest          INTEGER NOT NULL DEFAULT 0,
                last_active_date TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_cards_due ON cards (repetitions, due_date);
            """
        )

        _migrate(conn)

        # Seed the single streak row.
        conn.execute(
            "INSERT OR IGNORE INTO streak (id, current, longest) VALUES (1, 0, 0)"
        )

        # Seed default settings (INSERT OR IGNORE keeps any user edits).
        defaults = {
            "new_cards_per_day": str(config.DEFAULT_NEW_CARDS_PER_DAY),
            "push_morning": config.DEFAULT_MORNING_PUSH,
            "push_midday": config.DEFAULT_MIDDAY_PUSH,
            "push_evening": config.DEFAULT_EVENING_PUSH,
            "push_tz": config.PUSH_TZ_NAME,
        }
        for key, value in defaults.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )

        # Seed the weekly schedule once (Mon/Wed/Fri lessons per ROUTINE.md).
        existing = conn.execute("SELECT COUNT(*) AS n FROM schedule").fetchone()["n"]
        if existing == 0:
            for wd in range(7):
                is_lesson = 1 if wd in config.DEFAULT_LESSON_WEEKDAYS else 0
                time_val = config.DEFAULT_LESSON_TIME if is_lesson else None
                conn.execute(
                    "INSERT INTO schedule (weekday, is_lesson_day, time) VALUES (?, ?, ?)",
                    (wd, is_lesson, time_val),
                )


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive, idempotent schema upgrades for features added after v1.

    SQLite has no 'ADD COLUMN IF NOT EXISTS', so we check PRAGMA first. Every
    change here must be safe to run on every boot and must never drop data --
    the DB is the irreplaceable SRS history (guide s13).
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(cards)")}
    if "enrich_json" not in cols:
        # Cached active-recall metadata for a card: {word, clue_en, cloze, answer}.
        # Generated lazily by Gemini the first time a card needs a typed mode.
        conn.execute("ALTER TABLE cards ADD COLUMN enrich_json TEXT")

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS mistakes (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            category   TEXT NOT NULL,     -- one of recall.CATEGORIES
            detail     TEXT,              -- the corrected form / short note
            source     TEXT,              -- writing | lesson | drill | review
            created_at TEXT NOT NULL      -- ISO datetime
        );
        CREATE INDEX IF NOT EXISTS idx_mistakes_created ON mistakes (created_at);
        CREATE INDEX IF NOT EXISTS idx_mistakes_cat ON mistakes (category);
        """
    )


# --- settings ------------------------------------------------------------------

def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def get_new_cards_per_day() -> int:
    raw = get_setting("new_cards_per_day", str(config.DEFAULT_NEW_CARDS_PER_DAY))
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return config.DEFAULT_NEW_CARDS_PER_DAY


# --- cards ---------------------------------------------------------------------

def _row_to_card(row: sqlite3.Row) -> dict:
    card = dict(row)
    if card.get("due_date"):
        try:
            card["due_date"] = date.fromisoformat(card["due_date"])
        except ValueError:
            card["due_date"] = None
    raw = card.pop("enrich_json", None)
    enrich = None
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                enrich = parsed
        except (json.JSONDecodeError, TypeError):
            enrich = None
    card["enrich"] = enrich
    return card


def add_card(front: str, back: str, source: str = "manual", *, today: Optional[date] = None) -> int:
    """Insert a new card, due immediately (a new card starts in today's queue)."""
    today = today or date.today()
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO cards (front, back, interval, ease_factor, repetitions, "
            "due_date, source, created_at) VALUES (?, ?, 0, ?, 0, ?, ?, ?)",
            (
                front.strip(),
                back.strip(),
                srs.START_EASE,
                today.isoformat(),
                source,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        return int(cur.lastrowid)


def add_cards(pairs: list[tuple[str, str]], source: str) -> int:
    """Bulk-add (front, back) pairs, skipping exact-duplicate fronts. Returns count added."""
    added = 0
    today = date.today().isoformat()
    now = datetime.now().isoformat(timespec="seconds")
    with _connect() as conn:
        existing = {
            r["front"].strip().lower()
            for r in conn.execute("SELECT front FROM cards").fetchall()
        }
        for front, back in pairs:
            front = (front or "").strip()
            back = (back or "").strip()
            if not front or not back:
                continue
            if front.lower() in existing:
                continue
            conn.execute(
                "INSERT INTO cards (front, back, interval, ease_factor, repetitions, "
                "due_date, source, created_at) VALUES (?, ?, 0, ?, 0, ?, ?, ?)",
                (front, back, srs.START_EASE, today, source, now),
            )
            existing.add(front.lower())
            added += 1
    return added


def get_card(card_id: int) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
    return _row_to_card(row) if row else None


def get_due_cards(today: date, limit: Optional[int] = None) -> list[dict]:
    """Cards already in rotation (repetitions > 0) whose due_date <= today."""
    sql = (
        "SELECT * FROM cards WHERE repetitions > 0 AND due_date IS NOT NULL "
        "AND due_date <= ? ORDER BY due_date ASC, id ASC"
    )
    params: tuple = (today.isoformat(),)
    if limit is not None:
        sql += " LIMIT ?"
        params += (limit,)
    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_card(r) for r in rows]


def get_new_cards(limit: int) -> list[dict]:
    """Never-reviewed cards (repetitions == 0), oldest first."""
    if limit <= 0:
        return []
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM cards WHERE repetitions = 0 ORDER BY id ASC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_row_to_card(r) for r in rows]


def count_due(today: date) -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM cards WHERE repetitions > 0 "
            "AND due_date IS NOT NULL AND due_date <= ?",
            (today.isoformat(),),
        ).fetchone()
    return row["n"]


def count_new() -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM cards WHERE repetitions = 0"
        ).fetchone()
    return row["n"]


def count_cards() -> int:
    with _connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM cards").fetchone()
    return row["n"]


def update_card_srs(
    card_id: int, *, interval: float, ease_factor: float, repetitions: int, due_date: date
) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE cards SET interval = ?, ease_factor = ?, repetitions = ?, "
            "due_date = ? WHERE id = ?",
            (interval, ease_factor, repetitions, due_date.isoformat(), card_id),
        )


# --- active-recall enrichment cache --------------------------------------------

def set_card_enrichment(card_id: int, enrich: dict) -> None:
    """Cache Gemini-derived recall metadata ({word, clue_en, cloze, answer})."""
    with _connect() as conn:
        conn.execute(
            "UPDATE cards SET enrich_json = ? WHERE id = ?",
            (json.dumps(enrich, ensure_ascii=False), card_id),
        )


def random_distractor_backs(exclude_card_id: int, limit: int = 12) -> list[str]:
    """Random other cards' backs, to build multiple-choice distractors.

    Returns raw backs (the caller cleans them with recall.clean_english); pulls a
    few extra so the caller can drop ones that collide with the right answer.
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT back FROM cards WHERE id != ? ORDER BY RANDOM() LIMIT ?",
            (exclude_card_id, limit),
        ).fetchall()
    return [r["back"] for r in rows]


# --- mistake-pattern engine ----------------------------------------------------

def log_mistake(category: str, detail: str = "", source: str = "", *, when: Optional[datetime] = None) -> None:
    """Record one categorised mistake. Powers /mistakes and 'drill my worst'."""
    ts = (when or datetime.now()).isoformat(timespec="seconds")
    with _connect() as conn:
        conn.execute(
            "INSERT INTO mistakes (category, detail, source, created_at) VALUES (?, ?, ?, ?)",
            (category, (detail or "")[:500], source, ts),
        )


def top_mistake_categories(since: Optional[datetime] = None, limit: int = 5) -> list[dict]:
    """Most frequent mistake categories since `since` (default: all time)."""
    sql = "SELECT category, COUNT(*) AS n FROM mistakes"
    params: tuple = ()
    if since is not None:
        sql += " WHERE created_at >= ?"
        params = (since.isoformat(timespec="seconds"),)
    sql += " GROUP BY category ORDER BY n DESC, category ASC LIMIT ?"
    params += (limit,)
    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [{"category": r["category"], "count": r["n"]} for r in rows]


def worst_mistake_category(since: Optional[datetime] = None) -> Optional[str]:
    top = top_mistake_categories(since=since, limit=1)
    return top[0]["category"] if top else None


def count_mistakes(since: Optional[datetime] = None) -> int:
    sql = "SELECT COUNT(*) AS n FROM mistakes"
    params: tuple = ()
    if since is not None:
        sql += " WHERE created_at >= ?"
        params = (since.isoformat(timespec="seconds"),)
    with _connect() as conn:
        row = conn.execute(sql, params).fetchone()
    return row["n"]


# --- schedule ------------------------------------------------------------------

def get_weekly_schedule() -> dict[int, dict]:
    """Return {weekday: {'is_lesson_day': bool, 'time': str|None}} for 0..6."""
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM schedule").fetchall()
    out = {
        wd: {"is_lesson_day": False, "time": None} for wd in range(7)
    }
    for r in rows:
        out[r["weekday"]] = {
            "is_lesson_day": bool(r["is_lesson_day"]),
            "time": r["time"],
        }
    return out


def set_weekday(weekday: int, *, is_lesson_day: bool, time: Optional[str] = None) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO schedule (weekday, is_lesson_day, time) VALUES (?, ?, ?) "
            "ON CONFLICT(weekday) DO UPDATE SET is_lesson_day = excluded.is_lesson_day, "
            "time = excluded.time",
            (weekday, 1 if is_lesson_day else 0, time),
        )


def set_weekday_time(weekday: int, time: Optional[str]) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO schedule (weekday, is_lesson_day, time) VALUES (?, 0, ?) "
            "ON CONFLICT(weekday) DO UPDATE SET time = excluded.time",
            (weekday, time),
        )


def get_override(day: date) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM schedule_overrides WHERE date = ?", (day.isoformat(),)
        ).fetchone()
    if not row:
        return None
    return {"is_lesson_day": bool(row["is_lesson_day"]), "time": row["time"]}


def set_override(day: date, *, is_lesson_day: bool, time: Optional[str] = None) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO schedule_overrides (date, is_lesson_day, time) VALUES (?, ?, ?) "
            "ON CONFLICT(date) DO UPDATE SET is_lesson_day = excluded.is_lesson_day, "
            "time = excluded.time",
            (day.isoformat(), 1 if is_lesson_day else 0, time),
        )


def get_upcoming_overrides(today: date) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM schedule_overrides WHERE date >= ? ORDER BY date ASC",
            (today.isoformat(),),
        ).fetchall()
    return [
        {
            "date": date.fromisoformat(r["date"]),
            "is_lesson_day": bool(r["is_lesson_day"]),
            "time": r["time"],
        }
        for r in rows
    ]


def cleanup_old_overrides(today: date) -> int:
    """Delete past one-off overrides so temporary changes auto-revert. Returns rows removed."""
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM schedule_overrides WHERE date < ?", (today.isoformat(),)
        )
        return cur.rowcount


# --- streak --------------------------------------------------------------------

def get_streak() -> dict:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM streak WHERE id = 1").fetchone()
    return dict(row) if row else {"current": 0, "longest": 0, "last_active_date": None}


def make_backup(dest_path) -> None:
    """Write a consistent snapshot of the live DB to dest_path.

    Uses SQLite's online-backup API (never a raw file copy of a live DB) and
    checkpoints the WAL first, so the snapshot is a single clean .db with no hot
    -wal/-shm alongside it (guide s13).
    """
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(config.DB_PATH, timeout=30)
    try:
        src.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        dst = sqlite3.connect(str(dest_path))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def mark_active(today: Optional[date] = None) -> dict:
    """Record that the user did at least their Core today. Returns the new streak.

    A bad day = Core only still counts (ROUTINE.md rule 3). Idempotent within a day.
    """
    today = today or date.today()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM streak WHERE id = 1").fetchone()
        current = row["current"] if row else 0
        longest = row["longest"] if row else 0
        last = row["last_active_date"] if row else None

        if last == today.isoformat():
            return dict(row)  # already counted today

        if last is not None:
            last_date = date.fromisoformat(last)
            current = current + 1 if last_date == today - timedelta(days=1) else 1
        else:
            current = 1

        longest = max(longest, current)
        conn.execute(
            "UPDATE streak SET current = ?, longest = ?, last_active_date = ? WHERE id = 1",
            (current, longest, today.isoformat()),
        )
        return {"current": current, "longest": longest, "last_active_date": today.isoformat()}
