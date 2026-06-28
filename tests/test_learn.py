"""Tests for the /learn intake flow: pure helpers + the new-card queue it draws on.

The flow module imports python-telegram-bot; skip cleanly if PTB isn't installed
so the pure-logic suite still runs everywhere.
"""

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytest.importorskip("telegram")

import config  # noqa: E402
from flows import learn  # noqa: E402


class _Ctx:
    """Minimal stand-in for PTB's context (only .args is read by _batch_size)."""

    def __init__(self, args=None):
        self.args = args or []


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    # Keep these tests hermetic: with no key, gemini_configured() is False, so the
    # review presenter never tries to enrich a card over the network.
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    import db  # noqa: E402

    importlib.reload(db)
    db.init_db()
    return db


def test_batch_size_defaults_and_clamps():
    assert learn._batch_size(_Ctx()) == learn.DEFAULT_BATCH
    assert learn._batch_size(_Ctx(["3"])) == 3
    assert learn._batch_size(_Ctx(["999"])) == learn.MAX_BATCH
    assert learn._batch_size(_Ctx(["0"])) == 1
    assert learn._batch_size(_Ctx(["not-a-number"])) == learn.DEFAULT_BATCH


def test_card_text_shows_word_meaning_and_example():
    card = {
        "front": "Ich bin müde.",
        "back": "I am tired.",
        "enrich": {"word": "müde", "clue_en": "tired"},
    }
    text = learn._card_text(card, 2)
    assert "müde" in text          # the key word
    assert "tired" in text          # its clue
    assert "Ich bin müde." in text  # the example sentence
    assert "I am tired." in text    # the full translation -- always present


def test_card_text_without_enrichment_still_has_translation():
    card = {"front": "Hallo", "back": "Hello", "enrich": None}
    text = learn._card_text(card, 1)
    assert "Hallo" in text
    assert "Hello" in text


def test_new_cards_queue_is_oldest_first_and_limited(fresh_db):
    db = fresh_db
    ids = [db.add_card(f"Wort {i}", f"meaning {i}", source="seed") for i in range(8)]
    queue = [c["id"] for c in db.get_new_cards(5)]
    assert queue == ids[:5]  # oldest five, in insertion order


class _FakeMessage:
    """Records reply_text calls so we can assert what got presented."""

    def __init__(self):
        self.sent = []

    async def reply_text(self, text, reply_markup=None):
        self.sent.append(text)


def test_begin_over_ids_filters_missing_and_presents_first(fresh_db):
    import asyncio
    from types import SimpleNamespace

    from flows import review

    db = fresh_db
    cid = db.add_card("Ich bin müde.", "I am tired.", source="seed")
    msg = _FakeMessage()
    ctx = SimpleNamespace(user_data={})
    # One real id + one bogus id -> the bogus one is filtered out.
    asyncio.run(review.begin_over_ids(msg, ctx, [cid, 999999]))

    assert ctx.user_data["review"]["queue"] == [cid]
    assert msg.sent and "müde" in msg.sent[-1]  # first card was presented


def test_begin_over_ids_handles_all_missing(fresh_db):
    import asyncio
    from types import SimpleNamespace

    from flows import review

    msg = _FakeMessage()
    ctx = SimpleNamespace(user_data={})
    asyncio.run(review.begin_over_ids(msg, ctx, [111, 222]))

    assert "review" not in ctx.user_data  # no session installed
    assert any("aren't available" in s for s in msg.sent)


def test_reviewed_cards_drop_out_of_the_new_queue(fresh_db):
    db = fresh_db
    import srs  # noqa: E402

    cid = db.add_card("Ich bin müde.", "I am tired.", source="seed")
    # Once a card is reviewed (repetitions > 0) it's no longer "new" intake.
    st = srs.rate(interval=0.0, ease_factor=srs.START_EASE, repetitions=0, rating="good", today=db.date.today())
    db.update_card_srs(cid, interval=st.interval, ease_factor=st.ease_factor, repetitions=st.repetitions, due_date=st.due_date)
    assert db.get_new_cards(5) == []
