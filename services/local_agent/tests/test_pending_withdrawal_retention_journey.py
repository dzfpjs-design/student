"""Pending answer cancellation inside the paper-to-cross-day learning journey."""
import json
from datetime import datetime, timedelta

import pytest

from home_study_agent.mistake_dialogue import MistakeDialogueService
from home_study_agent.learning_feedback import build_learning_feedback
from test_continuous_marked_mistake_journey import (
    test_continuous_marked_page_to_original_answer_diagnosis_tutor_and_restart as _journey,
)
from test_mistake_pending_answer_http import hear, execute
from test_mistake_voice_journey import focus


@pytest.mark.parametrize('child', ['child-primary-4', 'child-junior-3'])
def test_withdraw_and_reanswer_keeps_qualified_retention_and_daily_plan(tmp_path, monkeypatch, child):
    checkpoints = []

    def interrupt_withdraw(client, sid, db):
        before = focus(client, sid)
        with db.connect() as c:
            attempts = c.execute('SELECT count(*) FROM mistake_attempt').fetchone()[0]
        turn = hear(client, sid, '我选A', 'withdraw-in-verified-practice')

        def unavailable(*args, **kwargs):
            raise RuntimeError('synthetic interruption before scoring')

        with monkeypatch.context() as patch:
            patch.setattr(MistakeDialogueService, '_score_operation', unavailable)
            execute(client, turn)
        pending = focus(client, sid)['state']['pending_operation']
        assert pending['answer'] == 'A'
        execute(client, hear(client, sid, '撤回刚才的回答', 'withdraw-verified-pending'))
        current = focus(client, sid)
        assert current['mistake_id'] == before['mistake_id']
        assert current['state']['next_activity'] == before['state']['next_activity']
        assert 'pending_operation' not in current['state']
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM mistake_attempt').fetchone()[0] == attempts
            saved = c.execute('SELECT result_json FROM mistake_dialogue_event WHERE client_action_id=?',
                              (pending['action_id'],)).fetchone()
            assert json.loads(saved[0])['withdrawn_pending_answer']['operation'] == pending
        checkpoints.append((db, sid, pending))

    _journey(tmp_path, monkeypatch, child, True, before_verified_answer=interrupt_withdraw)
    assert len(checkpoints) == 1
    db, sid, pending = checkpoints[0]
    with db.connect() as c:
        assert not c.execute('SELECT 1 FROM mistake_attempt WHERE client_action_id=?',
                             (pending['action_id'] + ':attempt',)).fetchone()
        event = c.execute('SELECT child_id,session_id,result_json FROM mistake_dialogue_event WHERE client_action_id=?',
                          (pending['action_id'],)).fetchone()
        assert (event['child_id'], event['session_id']) == (child, sid)
        assert json.loads(event['result_json'])['withdrawn_pending_answer']['operation'] == pending
        latest = c.execute('SELECT max(occurred_at) FROM learning_evidence WHERE child_id=?', (child,)).fetchone()[0]
        feedback = build_learning_feedback(c, child, now=datetime.fromisoformat(latest) + timedelta(seconds=1))
        assert sum(node['retention_success_count'] for node in feedback['nodes']) >= 1
        assert pending['action_id'] not in json.dumps(feedback)
        other = 'child-junior-3' if child == 'child-primary-4' else 'child-primary-4'
        assert build_learning_feedback(c, other)['nodes'] == []
