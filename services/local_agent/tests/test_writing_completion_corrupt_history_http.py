"""Optional completion history must not disable a new, unrelated voice command."""
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent import writing_daily_followup as followup
from test_writing_daily_completion_http import environment, next_day_ready, completion_ready
from test_writing_arrival_corrupt_history_http import say


def break_optional(case, fault):
    with case.db.connect() as c:
        if fault.startswith('manifest'):
            value = '{broken' if fault == 'manifest_json' else '[]'
            c.execute('UPDATE agent_plan SET context_manifest_json=? WHERE agent_turn_id=?',
                      (value, case.turn['agent_turn_id']))
        elif fault == 'request_json':
            c.execute('UPDATE diagnostic_prompt SET request_json=? WHERE prompt_id=?',
                      ('{broken', case.completion_prompt['prompt_id']))
        elif fault == 'arguments_json':
            c.execute('UPDATE agent_step SET arguments_json=? WHERE plan_id IN '
                      '(SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)',
                      ('{broken', case.feedback_turn))
        else:
            c.execute('UPDATE agent_step SET result_json=? WHERE plan_id IN '
                      '(SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)',
                      ('[]', case.feedback_turn))
        c.commit()


@pytest.mark.parametrize('fault', ['manifest_json', 'manifest_shape', 'request_json',
                                 'arguments_json', 'result_shape'])
def test_optional_completion_damage_allows_real_voice_and_restart(completion_ready, monkeypatch, fault):
    case = completion_ready
    case.client._transport.raise_server_exceptions = True
    break_optional(case, fault)
    before = len(case.journey.gateway.requests)
    stopped = say(case, monkeypatch, '暂停高拍仪', 'optional-completion-stop')
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()['action']['type'] == 'control_document_monitor'
    assert stopped.json()['action']['command'] == 'pause'
    assert len(case.journey.gateway.requests) == before
    with case.db.connect() as c:
        snapshot = json.loads(c.execute('SELECT snapshot_json FROM learning_input_arrival WHERE client_action_id=?',
                                       ('optional-completion-stop',)).fetchone()[0])
        completion = next(entry['record'] for entry in snapshot if entry['kind'] == followup.COMPLETION_KIND)
        assert completion == {'status': 'blocked', 'reason': 'COMPLETION_SOURCE_OR_ANSWER_UNAVAILABLE'}
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 2
        assert c.execute('SELECT COUNT(*) FROM rubric_assessment').fetchone()[0] == 2
        assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?',
                         (case.task['plan_item_id'],)).fetchone()[0] == 'active'
    # A new connection must see the same damaged source, never a repaired copy.
    with case.db.connect() as c:
        assert followup._capture_completion(c, {'child_id': case.completion_prompt['child_id'],
                                               'session_id': case.sid}) == completion
    case.stack.close()
    with TestClient(create_app(case.settings, model_gateway=case.journey.gateway)) as client:
        case.client = client
        after_startup = len(case.journey.gateway.requests)
        again = say(case, monkeypatch, '暂停高拍仪', 'optional-completion-after-restart')
        assert again.status_code == 200, again.text
        assert again.json()['action']['type'] == 'control_document_monitor'
        assert len(case.journey.gateway.requests) == after_startup
        with case.db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 2
            assert c.execute('SELECT COUNT(*) FROM rubric_assessment').fetchone()[0] == 2
            assert c.execute('SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type=\'completed\'',
                             (case.task['plan_item_id'],)).fetchone()[0] == 0


def test_optional_arguments_damage_is_controlled_in_completion_projection(completion_ready):
    case = completion_ready
    break_optional(case, 'arguments_json')
    with case.db.connect() as c:
        assert followup._capture_completion(c, {'child_id': case.completion_prompt['child_id'],
                                               'session_id': case.sid}) == {
            'status': 'blocked', 'reason': 'COMPLETION_SOURCE_OR_ANSWER_UNAVAILABLE'}


def test_optional_completion_does_not_hide_sqlite_failure():
    class BrokenConnection:
        def execute(self, *args):
            raise sqlite3.OperationalError('synthetic disk read failure')
    with pytest.raises(sqlite3.OperationalError, match='synthetic disk read failure'):
        followup._capture_completion(BrokenConnection(), {'child_id': 'a', 'session_id': 's'})


def test_optional_completion_basis_does_not_hide_sqlite_failure(monkeypatch):
    def failed(*args):
        raise sqlite3.OperationalError('synthetic completion read failure')
    monkeypatch.setattr(followup, '_read_completion_basis', failed)
    with pytest.raises(sqlite3.OperationalError, match='synthetic completion read failure'):
        followup._completion_basis(None, 'original-start')
