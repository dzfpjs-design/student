"""Learning fixtures must not depend on the machine's time of day."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from home_study_agent import learning_input_replan, repository, supply_gap_resume
from home_study_agent.app import create_app
from test_pet_media_evidence import media, CHILD
from test_writing_stage_review_consumers import advance_clock


@pytest.fixture(autouse=True)
def near_bedtime(monkeypatch):
    # Nanjing 23:59. Creating a new plan here must remain disallowed.
    return advance_clock(monkeypatch, datetime(2026, 9, 10, 15, 59, 10, tzinfo=timezone.utc))


def test_repeated_clock_advance_keeps_datetime_and_utc_consumers_together(monkeypatch, near_bedtime):
    next_day = near_bedtime.current + timedelta(days=1)
    clock = advance_clock(monkeypatch, next_day)
    values = [repository.utc_now(), learning_input_replan.utc_now().isoformat(),
        supply_gap_resume.datetime.now(timezone.utc).isoformat()]
    assert all(next_day <= datetime.fromisoformat(value) <= clock.current for value in values)


def test_fresh_listening_review_waits_24_hours_then_uses_evening_not_bedtime(media, tmp_path, monkeypatch):
    from test_listening_review_journey import next_day_case
    from test_legacy_listening_review_resume import legacy_plan
    settings, db, gateway, _, _, clock = next_day_case(media, tmp_path, monkeypatch, ready_source=True)
    assert clock.current.astimezone(timezone(timedelta(hours=8))).hour == 18
    with db.connect() as c:
        latest_answer = c.execute('SELECT MAX(created_at) FROM diagnostic_attempt').fetchone()[0]
        assert clock.current - datetime.fromisoformat(latest_answer) > timedelta(hours=24)
    with TestClient(create_app(settings, model_gateway=gateway),
                    headers={'X-HSC-Session': settings.session_token}) as client:
        response = client.post('/v1/sessions', json={
            'child_id': CHILD, 'client_action_id': 'after-bedtime-reference'})
        assert response.status_code == 201, response.text
        item = legacy_plan(client, db, monkeypatch, response.json()['session_id'])
        assert item['status'] == 'pending' and item['started_at'] is None
