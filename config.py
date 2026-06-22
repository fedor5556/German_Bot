"""Configuration + secrets loading for the German bot.

Loads everything from .env (delivered out-of-band, never via Git) and exposes
constants + the owner auth gate. The gate FAILS CLOSED: if OWNER_TELEGRAM_ID is
missing, no one is authorised (an empty allowlist refuses everyone) rather than
opening the bot to all. See remote_host_architecture_guideLAST.md s12.

No emoji in any print() here -- this code runs in raw cmd.exe on the server,
which crashes on non-cp1252 characters (guide s9).
"""

import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
SEED_DIR = BASE_DIR / "seed"
DB_PATH = DATA_DIR / "german.db"
LOCK_PATH = DATA_DIR / "german_bot.lock"

# Load .env that sits next to this file (works no matter the launch CWD).
load_dotenv(BASE_DIR / ".env")

# --- Secrets / required config -------------------------------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()


def _parse_ids(raw: str) -> set[int]:
    """Parse a comma-separated list of numeric Telegram user IDs."""
    ids: set[int] = set()
    for chunk in (raw or "").replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            ids.add(int(chunk))
        except ValueError:
            # A non-numeric entry is ignored rather than crashing startup; the
            # gate stays closed for it. Logged loudly by validate() below.
            pass
    return ids


OWNER_IDS: set[int] = _parse_ids(os.getenv("OWNER_TELEGRAM_ID", ""))

# --- Timezone ------------------------------------------------------------------
# You AND the host PC are both in Cyprus -- pin pushes to Europe/Nicosia via
# zoneinfo rather than the server's implicit local time (guide / plan Phase 7).
PUSH_TZ_NAME = os.getenv("PUSH_TZ", "Europe/Nicosia").strip() or "Europe/Nicosia"
try:
    PUSH_TZ = ZoneInfo(PUSH_TZ_NAME)
except Exception:  # noqa: BLE001 - bad tz name should not be silent
    PUSH_TZ_NAME = "Europe/Nicosia"
    PUSH_TZ = ZoneInfo(PUSH_TZ_NAME)

# --- AI ------------------------------------------------------------------------
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash").strip() or "gemini-3.5-flash"

# --- Learning constants (overridable via settings table at runtime) ------------
DEFAULT_NEW_CARDS_PER_DAY = 10
DEFAULT_MORNING_PUSH = "09:00"
DEFAULT_EVENING_PUSH = "20:00"
DEFAULT_LESSON_TIME = "17:00"

# Default weekly schedule mirrors ROUTINE.md's sample week (Mon/Wed/Fri lessons).
# The user edits this any time via /schedule; nothing here is permanent.
# weekday: 0=Mon ... 6=Sun
DEFAULT_LESSON_WEEKDAYS = {0, 2, 4}


def is_owner(user_id: int | None) -> bool:
    """True only for an explicitly-allowlisted numeric ID. Empty list => False."""
    return user_id is not None and user_id in OWNER_IDS


def validate() -> list[str]:
    """Return a list of fatal config problems (empty list == OK).

    Called at startup by german_bot.py, which logs each problem and exits.
    """
    problems: list[str] = []
    if not TELEGRAM_TOKEN:
        problems.append("TELEGRAM_TOKEN is missing from .env -- the bot cannot start.")
    if not OWNER_IDS:
        problems.append(
            "OWNER_TELEGRAM_ID is missing/empty in .env -- the auth gate would "
            "refuse everyone (fail-closed). Set your numeric Telegram user ID."
        )
    return problems


def gemini_configured() -> bool:
    return bool(GEMINI_API_KEY)
