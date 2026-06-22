"""Load the Goethe B1 starter deck into the DB (idempotent via dedup)."""

from __future__ import annotations

import json
import logging

import config
import db

log = logging.getLogger(__name__)


def load_starter() -> int:
    """Import seed/b1_starter.json. Returns the number of NEW cards added."""
    path = config.SEED_DIR / "b1_starter.json"
    if not path.exists():
        log.warning("starter deck not found at %s", path)
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        log.error("starter deck is not valid JSON: %s", exc)
        return 0
    pairs = [
        (c["front"], c["back"])
        for c in data
        if isinstance(c, dict) and c.get("front") and c.get("back")
    ]
    return db.add_cards(pairs, source="seed")
