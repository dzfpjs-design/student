"""The family planning date must not truncate the current UTC instant."""

from datetime import date, datetime, timedelta, timezone

import pytest

from home_study_agent import goal_roadmap
from home_study_agent.family_clock import family_today, calendar_as_of


@pytest.mark.parametrize("offset,hour", [(-7, 2), (8, 20), (0, 12)])
def test_current_family_day_keeps_actual_instant_despite_host_calendar(monkeypatch, offset, hour):
    now = datetime(2026, 9, 8, hour, tzinfo=timezone.utc)
    local_day = now.astimezone(timezone(timedelta(hours=offset))).date()
    family_day = family_today(now)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz)

    class Calendar(date):
        @classmethod
        def today(cls):
            return cls(local_day.year, local_day.month, local_day.day)

    # Host date.today is deliberately wrong; explicit API dates stay real date
    # objects, not a subclass substituted for a protocol type.
    monkeypatch.setattr(goal_roadmap, "datetime", Clock)
    monkeypatch.setattr(goal_roadmap, "date", Calendar)
    assert goal_roadmap._as_of(None) == (family_day, now)
    assert goal_roadmap._as_of(family_day) == (family_day, now)
    assert goal_roadmap._as_of(local_day) == calendar_as_of(local_day, now)
