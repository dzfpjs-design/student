"""Pin a future family evening after a real report's creation, not host midnight."""
from datetime import datetime, time, timedelta, timezone

from home_study_agent.family_clock import FAMILY_ZONE, family_today


def next_family_evening(monkeypatch):
    from home_study_agent import repository
    reference = datetime.now(timezone.utc)
    day = family_today(reference) + timedelta(days=1)
    instant = datetime.combine(day, time(18), FAMILY_ZONE).astimezone(timezone.utc)
    monkeypatch.setattr(repository, 'utc_now', lambda: instant.isoformat())
    return instant, day.isoformat()
