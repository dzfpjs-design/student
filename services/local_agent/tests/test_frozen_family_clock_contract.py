from datetime import datetime, timezone

import pytest

from home_study_agent.family_clock import family_time
from test_writing_stage_review_consumers import advance_clock


def test_frozen_clock_accepts_aware_instants_from_other_module_parsers(monkeypatch):
    instant = datetime.fromisoformat('2026-09-10T10:00:00+00:00')
    advance_clock(monkeypatch, instant)
    assert family_time(instant).isoformat() == '2026-09-10T18:00:00+08:00'


def test_frozen_clock_does_not_make_naive_instants_valid(monkeypatch):
    advance_clock(monkeypatch, datetime(2026, 9, 10, tzinfo=timezone.utc))
    with pytest.raises(ValueError, match='aware instant'):
        family_time(datetime(2026, 9, 10))
