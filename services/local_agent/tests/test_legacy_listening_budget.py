"""PET review uses its PET reservation, while real wall and task limits remain."""
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.legacy_listening_review import candidate, budget
from home_study_agent.task_start_binding import TaskStartBindingError
from test_pet_media_evidence import media, CHILD
from test_listening_review_journey import next_day_case
from test_legacy_listening_review_resume import legacy_plan


def test_exact_pet_review_does_not_double_reserve_pet_but_keeps_other_limits(media, tmp_path, monkeypatch):
    settings, db, gateway, _, _, clock = next_day_case(media, tmp_path, monkeypatch)
    with TestClient(create_app(settings, model_gateway=gateway), headers={'X-HSC-Session': settings.session_token}) as client:
        opened = client.post('/v1/sessions', json={'child_id': CHILD, 'client_action_id': 'tight-budget-day'})
        assert opened.status_code == 201, opened.text
        sid = opened.json()['session_id']
        old = legacy_plan(client, db, monkeypatch, sid)
        with db.connect() as c:
            target = candidate(c, sid, CHILD, old)
            assert target
            rule = c.execute('SELECT pet_min_minutes FROM family_rule WHERE child_id=?', (CHILD,)).fetchone()
            assert rule[0] > 0
            others = c.execute("SELECT expected_minutes FROM plan_item WHERE plan_id=? AND plan_item_id!=? AND status IN ('pending','paused','active','rescheduled')",
                               (old['plan_id'], old['plan_item_id'])).fetchall()
            protected = sum(row[0] for row in others) * 60
            from home_study_agent.learning_input_replan import ZONE
            end = clock.current.astimezone(ZONE).replace(hour=23, minute=59, second=0, microsecond=0)
            available_at = end - timedelta(seconds=protected + old['expected_minutes'] * 60 + 10)
            clock.current = available_at
            receipt = budget(c, target)
            assert receipt['pet_reserved_seconds'] == 0
            assert receipt['protected_remaining_seconds'] == protected
            assert old['expected_minutes'] * 60 <= receipt['remaining_seconds'] < old['expected_minutes'] * 60 + 11
            for fault in ('too_late', 'short_wall', 'daily_cap', 'fixed_commitment'):
                c.execute('SAVEPOINT budget_guard')
                clock.current = available_at
                if fault == 'too_late':
                    clock.current = end
                elif fault == 'short_wall':
                    clock.current += timedelta(seconds=11)
                elif fault == 'daily_cap':
                    c.execute('UPDATE family_rule SET max_learning_minutes=1 WHERE child_id=?', (CHILD,))
                else:
                    c.execute('''INSERT INTO session_planning_context
                        (session_id,energy_level,fixed_commitment_minutes,tomorrow_event_minutes,updated_at)
                        VALUES (?,'medium',1,0,?) ON CONFLICT(session_id)
                        DO UPDATE SET fixed_commitment_minutes=1''', (sid, clock.current.isoformat()))
                with pytest.raises(TaskStartBindingError, match='BUDGET_INSUFFICIENT'):
                    budget(c, target)
                c.execute('ROLLBACK TO budget_guard')
                c.execute('RELEASE budget_guard')
            assert c.execute('SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=?', (old['plan_item_id'],)).fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt WHERE origin_session_id=?', (sid,)).fetchone()[0] == 0
