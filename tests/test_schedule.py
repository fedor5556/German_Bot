"""Unit tests for the day-type resolver + this-week date math (pure logic)."""

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flows import schedule as sched  # noqa: E402


def _weekly(lesson_days: set[int]):
    return {
        wd: {"is_lesson_day": wd in lesson_days, "time": "17:00" if wd in lesson_days else None}
        for wd in range(7)
    }


def test_lesson_day_resolves_to_lesson():
    weekly = _weekly({0, 2, 4})  # Mon/Wed/Fri
    monday = date(2026, 6, 22)   # a Monday
    assert sched.resolve_day_type(monday, weekly) == sched.LESSON


def test_full_day_resolves_to_full():
    weekly = _weekly({0, 2, 4})
    tuesday = date(2026, 6, 23)
    assert sched.resolve_day_type(tuesday, weekly) == sched.FULL


def test_sunday_without_lesson_is_review():
    weekly = _weekly({0, 2, 4})
    sunday = date(2026, 6, 28)
    assert sched.resolve_day_type(sunday, weekly) == sched.REVIEW


def test_sunday_with_lesson_is_lesson():
    weekly = _weekly({6})  # Sunday is a lesson day
    sunday = date(2026, 6, 28)
    assert sched.resolve_day_type(sunday, weekly) == sched.LESSON


def test_temporary_override_changes_day_type():
    weekly = _weekly({0, 2, 4})  # Wed is normally a lesson day
    wednesday = date(2026, 6, 24)
    # Move this Wednesday's lesson away -> a one-off override makes it a full day.
    override = {"is_lesson_day": False, "time": None}
    assert sched.resolve_day_type(wednesday, weekly, override) == sched.FULL
    # And Thursday becomes the lesson this week only.
    thursday = date(2026, 6, 25)
    override_thu = {"is_lesson_day": True, "time": "17:00"}
    assert sched.resolve_day_type(thursday, weekly, override_thu) == sched.LESSON


def test_date_for_weekday_this_week():
    today = date(2026, 6, 22)  # Monday
    # Wednesday of this week is the 24th.
    assert sched.date_for_weekday_this_week(2, today) == date(2026, 6, 24)
    # A weekday already passed rolls to next week. From Wed 24th, asking for Mon
    # (already gone) gives next Monday.
    wed = date(2026, 6, 24)
    assert sched.date_for_weekday_this_week(0, wed) == date(2026, 6, 29)
    # Today itself counts as itself.
    assert sched.date_for_weekday_this_week(0, today) == today
