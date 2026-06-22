"""SM-2 spaced-repetition scheduling -- pure logic, no I/O, unit-tested.

A "card" here is just a dict-like with the SRS fields: interval (days),
ease_factor (starts 2.5), repetitions, due_date (datetime.date).

The bot exposes four ratings -- Again / Hard / Good / Easy -- mapped onto the
classic SM-2 quality scale. "Again" is a lapse: repetitions reset, the card
becomes due again today (the review flow also requeues it within the session).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

START_EASE = 2.5
MIN_EASE = 1.3

# Rating -> SM-2 quality (0-5 scale).
QUALITY = {"again": 2, "hard": 3, "good": 4, "easy": 5}
RATINGS = ("again", "hard", "good", "easy")


@dataclass
class SrsState:
    interval: float
    ease_factor: float
    repetitions: int
    due_date: date
    is_lapse: bool  # True when the card was rated "again" (requeue this session)


def rate(
    *,
    interval: float,
    ease_factor: float,
    repetitions: int,
    rating: str,
    today: date,
) -> SrsState:
    """Apply a rating to a card's SRS state and return the new state.

    Implements SM-2 with an Anki-style "Hard" that grows the interval slowly
    instead of by the full ease factor.
    """
    if rating not in QUALITY:
        raise ValueError(f"unknown rating: {rating!r}")

    q = QUALITY[rating]
    ef = ease_factor if ease_factor else START_EASE
    reps = repetitions
    is_lapse = False

    if q < 3:  # "Again" -- lapse
        reps = 0
        new_interval = 0  # due again today; requeued within the session
        is_lapse = True
    else:
        if reps == 0:
            new_interval = 1
        elif reps == 1:
            new_interval = 6
        elif rating == "hard":
            # Hard: grow slowly rather than by the full ease factor.
            new_interval = max(1, round(interval * 1.2))
        else:
            new_interval = max(1, round(interval * ef))
        reps += 1

    # Update the ease factor with the standard SM-2 formula, floored at 1.3.
    ef = ef + (0.1 - (5 - q) * (0.08 + (5 - q) * 0.02))
    ef = max(MIN_EASE, round(ef, 4))

    due = today + timedelta(days=int(new_interval))
    return SrsState(
        interval=new_interval,
        ease_factor=ef,
        repetitions=reps,
        due_date=due,
        is_lapse=is_lapse,
    )


def is_due(card: dict, today: date) -> bool:
    """A card already in rotation (repetitions > 0) is due when due_date <= today."""
    if card.get("repetitions", 0) <= 0:
        return False
    due = card.get("due_date")
    if due is None:
        return True
    if isinstance(due, str):
        due = date.fromisoformat(due)
    return due <= today


def get_due_cards(cards: list[dict], today: date) -> list[dict]:
    """Filter a list of card dicts down to those due for review today."""
    return [c for c in cards if is_due(c, today)]
