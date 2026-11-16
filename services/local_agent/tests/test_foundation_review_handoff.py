"""A fresh planned review is not owned by an already completed foundation task."""
import json
import subprocess
import sys

import pytest

from home_study_agent.mistake_dialogue import MistakeDialogueService
from home_study_agent.repository import StudyRepository, utc_now
from test_foundation_teaching_journey import teaching_chain
from test_goal_foundation_execution import foundation
from test_goal_roadmap import flow
from test_voice_runtime_binding import capture, interpret


@pytest.mark.parametrize('condition', ['valid', 'no_review', 'wrong_case', 'paused_origin', 'pending_answer', 'late_pause',
    'voice_late_pause', 'quoted_request'])
def test_same_day_review_handoff_keeps_explicit_task_ownership(teaching_chain, monkeypatch, condition):
    db, sid, settings, _, client, task, _, case, _ = teaching_chain
    repository = StudyRepository(db)
    if condition == 'paused_origin':
        repository.pause_plan_item(task['plan_item_id'], 'before-independent-review')
    else:
        repository.complete_plan_item(task['plan_item_id'], 1, 'before-independent-review')
    # Isolated planner output for a second, same-day review. This fixture proves
    # handoff behavior, not that the production planner creates this scenario.
    with db.connect() as c:
        row = dict(c.execute('SELECT * FROM plan_item WHERE plan_item_id=?', (task['plan_item_id'],)).fetchone())
        row.update(plan_item_id='same-day-review', source_type='mistake_review', source_ref=case,
            task_type='review', status='active', expected_minutes=5, actual_minutes=None,
            started_at=utc_now(), active_segment_started_at=utc_now(), completed_at=None, active_seconds=0)
        if condition == 'wrong_case':
            row['source_ref'] = 'different-case'
        if condition != 'no_review':
            columns = list(row)
            c.execute(f"INSERT INTO plan_item({','.join(columns)}) VALUES({','.join('?' for _ in columns)})", [row[k] for k in columns])
        original = json.loads(c.execute('SELECT state_json FROM mistake_dialogue WHERE session_id=?', (sid,)).fetchone()[0])
        if condition == 'pending_answer':
            original['pending_operation'] = {'action_id': 'unsettled-original', 'utterance': 'Original saved words'}
            c.execute('UPDATE mistake_dialogue SET state_json=? WHERE session_id=?', (json.dumps(original), sid))
        c.commit()
    service = MistakeDialogueService(db)
    if condition == 'late_pause':
        save = service._save
        def paused(focus, *args):
            with db.connect() as c:
                c.execute("UPDATE plan_item SET status='paused' WHERE plan_item_id='same-day-review'")
                c.commit()
            return save(focus, *args)
        monkeypatch.setattr(service, '_save', paused)
        from home_study_agent.mistake_capture import InvalidMistakeCaptureCandidateError
        with pytest.raises(InvalidMistakeCaptureCandidateError):
            service.open_review(sid, 'open-independent-review', mistake_id=case)
    else:
        if condition in {'valid', 'voice_late_pause', 'quoted_request'}:
            text = '妈妈说打开错题本' if condition == 'quoted_request' else '打开错题本'
            voice = capture(client, settings, monkeypatch, sid, text, key='independent-review-audio')
            turn = interpret(client, sid, text, 'independent-review', voice)
            if condition == 'voice_late_pause':
                repository.pause_plan_item('same-day-review', 'late-voice-pause')
            result = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
            if condition == 'voice_late_pause':
                assert result.status_code == 422, result.text
                opened = service.state(sid)
            elif condition == 'quoted_request':
                assert turn['action'].get('source') != 'saved_review_selection'
                assert result.status_code == 200, result.text
                assert result.json()['execution']['capability'] != 'mistake.open'
                opened = service.state(sid)
            else:
                assert result.status_code == 200, result.text
                assert result.json()['execution']['capability'] == 'mistake.open', result.json()['execution']
                opened = result.json()['execution']['result']
        else:
            opened = service.open_review(sid, 'open-independent-review', mistake_id=case)
        if condition == 'valid':
            assert not opened['state'].get('foundation_origin'), opened['state'].get('foundation_hold')
            assert not opened['state'].get('foundation_hold')
            assert opened['state']['foundation_review_handoff']['from_origin'] == original['foundation_origin']
            assert opened['state']['foundation_review_handoff']['review_plan_item_id'] == 'same-day-review'
        else:
            assert opened['state']['foundation_origin'] == original['foundation_origin']
    restored = MistakeDialogueService(db).state(sid)
    if condition == 'valid':
        assert restored['state']['foundation_review_handoff']['from_origin'] == original['foundation_origin']
        assert not restored['state'].get('foundation_origin')
        code = "import sqlite3,json,sys; c=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True); print(c.execute('SELECT state_json FROM mistake_dialogue WHERE session_id=?',(sys.argv[2],)).fetchone()[0])"
        reader = subprocess.run([sys.executable, '-c', code, str(db.path), sid],
            capture_output=True, text=True, check=True, timeout=30)
        assert json.loads(reader.stdout)['foundation_review_handoff'] == restored['state']['foundation_review_handoff']
    else:
        assert restored['state']['foundation_origin'] == original['foundation_origin']
        if condition == 'pending_answer':
            assert restored['state']['pending_operation'] == original['pending_operation']
