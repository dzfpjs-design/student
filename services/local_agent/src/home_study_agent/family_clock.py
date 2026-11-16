"""Calendar dates for this Nanjing family's plans; evidence instants remain UTC."""
from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo


FAMILY_ZONE = ZoneInfo('Asia/Shanghai')
EVIDENCE_CALENDAR_VERSION = 'nanjing_evidence_calendar_v2'


def family_time(instant: datetime) -> datetime:
    if not isinstance(instant, datetime) or instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError('Family calendar requires an aware instant')
    return instant.astimezone(FAMILY_ZONE)


def family_today(instant: datetime | None = None) -> date:
    return family_time(instant if instant is not None else datetime.now(timezone.utc)).date()


def evidence_calendar_time(value: str) -> datetime:
    """Use Nanjing for instants; retain legacy naive local wall-time semantics."""
    instant = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return family_time(instant) if instant.tzinfo is not None else instant


def evidence_calendar_date(value: str) -> date:
    return evidence_calendar_time(value).date()


def calendar_as_of(reference_date: date | None, instant: datetime) -> tuple[date, datetime]:
    local_today = family_today(instant)
    today = reference_date if reference_date is not None else local_today
    if type(today) is not date:
        raise ValueError('reference_date must be a date')
    now = instant.astimezone(timezone.utc)
    cutoff = (datetime.combine(today, datetime.max.time(), tzinfo=FAMILY_ZONE).astimezone(timezone.utc)
              if today < local_today else now)
    return today, min(now, cutoff)
