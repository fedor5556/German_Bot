"""Unit tests for the SM-2 engine (pure logic)."""

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import srs  # noqa: E402

TODAY = date(2026, 6, 22)


def _new():
    return {"interval": 0.0, "ease_factor": srs.START_EASE, "repetitions": 0}


def test_good_three_times_grows_interval():
    card = _new()
    intervals = []
    for _ in range(3):
        st = srs.rate(rating="good", today=TODAY, **card)
        intervals.append(st.interval)
        card = {"interval": st.interval, "ease_factor": st.ease_factor, "repetitions": st.repetitions}
    # First Good graduates to its step (3d); thereafter it grows by the ease factor.
    assert intervals[0] == srs.GRADUATING_INTERVALS["good"]
    assert intervals[1] > intervals[0]
    assert intervals[2] > intervals[1]
    assert intervals == sorted(intervals)


def test_new_card_ratings_are_distinct():
    # Regression for the "everything says (1d)" confusion: a brand-new card's
    # four buttons must map to four different next-review dates.
    card = _new()
    by_rating = {
        r: srs.rate(rating=r, today=TODAY, **card).interval
        for r in ("again", "hard", "good", "easy")
    }
    assert by_rating["again"] == 0  # due again today
    assert by_rating["hard"] < by_rating["good"] < by_rating["easy"]
    # All four are genuinely different values.
    assert len(set(by_rating.values())) == 4


def test_again_resets():
    # A mature card lapses: repetitions reset to 0 and it's due again today.
    st = srs.rate(interval=30.0, ease_factor=2.6, repetitions=5, rating="again", today=TODAY)
    assert st.repetitions == 0
    assert st.interval == 0
    assert st.is_lapse is True
    assert st.due_date == TODAY


def test_ease_factor_floor():
    # Repeated "again" must not push ease below 1.3.
    ef = srs.START_EASE
    for _ in range(20):
        st = srs.rate(interval=1.0, ease_factor=ef, repetitions=1, rating="again", today=TODAY)
        ef = st.ease_factor
    assert ef >= srs.MIN_EASE


def test_easy_grows_faster_than_hard():
    base = {"interval": 10.0, "ease_factor": 2.5, "repetitions": 3}
    easy = srs.rate(rating="easy", today=TODAY, **base)
    hard = srs.rate(rating="hard", today=TODAY, **base)
    assert easy.interval > hard.interval


def test_due_filtering():
    cards = [
        {"repetitions": 2, "due_date": TODAY - timedelta(days=1)},  # overdue
        {"repetitions": 2, "due_date": TODAY},                      # due today
        {"repetitions": 2, "due_date": TODAY + timedelta(days=3)},  # future
        {"repetitions": 0, "due_date": TODAY},                      # new, not "due" rotation
    ]
    due = srs.get_due_cards(cards, TODAY)
    assert len(due) == 2
