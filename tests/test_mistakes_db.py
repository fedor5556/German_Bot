"""DB-backed tests for the mistake-pattern engine + enrichment cache.

Each test runs against a throwaway SQLite file so the real SRS history is never
touched. We repoint config's data paths before importing/using db.
"""

import importlib
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    import db  # noqa: E402
    importlib.reload(db)
    db.init_db()
    return db


def test_init_creates_mistakes_table_and_enrich_column(fresh_db):
    db = fresh_db
    cols = {r["name"] for r in _pragma(db, "cards")}
    assert "enrich_json" in cols
    # mistakes table exists and is queryable
    assert db.count_mistakes() == 0


def _pragma(db, table):
    with db._connect() as conn:
        return conn.execute(f"PRAGMA table_info({table})").fetchall()


def test_log_and_aggregate_top_categories(fresh_db):
    db = fresh_db
    for _ in range(3):
        db.log_mistake("Cases (Akkusativ/Dativ)", "den/dem mix-up", "writing")
    db.log_mistake("Word order", "verb position", "lesson")
    db.log_mistake("Prepositions", "an/auf", "drill")

    top = db.top_mistake_categories(limit=5)
    assert top[0] == {"category": "Cases (Akkusativ/Dativ)", "count": 3}
    assert db.worst_mistake_category() == "Cases (Akkusativ/Dativ)"
    assert db.count_mistakes() == 5


def test_since_filter_scopes_to_window(fresh_db):
    db = fresh_db
    old = datetime.now() - timedelta(days=60)
    db.log_mistake("Plurals", "old one", "writing", when=old)
    db.log_mistake("Word order", "recent one", "writing")

    month_ago = datetime.now() - timedelta(days=30)
    assert db.count_mistakes(since=month_ago) == 1
    assert db.worst_mistake_category(since=month_ago) == "Word order"
    # all-time still sees both
    assert db.count_mistakes() == 2


def test_enrichment_roundtrips_through_get_card(fresh_db):
    db = fresh_db
    cid = db.add_card("Ich bin müde.", "I am tired.", source="manual")
    enrich = {"word": "müde", "clue_en": "tired", "answer": "müde", "cloze": "Ich bin _____."}
    db.set_card_enrichment(cid, enrich)

    card = db.get_card(cid)
    assert card["enrich"]["word"] == "müde"
    assert card["enrich"]["cloze"] == "Ich bin _____."


def test_card_without_enrichment_has_none(fresh_db):
    db = fresh_db
    cid = db.add_card("Hallo", "Hello", source="manual")
    assert db.get_card(cid)["enrich"] is None


def test_distractor_backs_excludes_self(fresh_db):
    db = fresh_db
    ids = [db.add_card(f"Wort {i}", f"meaning {i}", source="manual") for i in range(6)]
    backs = db.random_distractor_backs(ids[0], limit=10)
    assert "meaning 0" not in backs
    assert len(backs) == 5
