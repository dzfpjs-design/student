"""Delayed teaching must still belong to the exact child dialogue."""
import json

import pytest

from test_foundation_teaching_journey import teaching_chain, say
from test_goal_foundation_execution import foundation
from test_goal_roadmap import flow, ROOT


@pytest.mark.parametrize('change', ['paused', 'pause_resume', 'closed', 'replaced', 'budget', 'withdrawn'])
def test_late_tutor_does_not_persist_or_attach_to_changed_dialogue(teaching_chain, monkeypatch, change):
    db, sid, settings, gateway, client, task, _, case, _ = teaching_chain
    complete = gateway.complete
    changed = []

    async def delayed(request):
        result = await complete(request)
        if change == 'pause_resume':
            from home_study_agent.repository import StudyRepository
            repository = StudyRepository(db)
            repository.pause_plan_item(task['plan_item_id'], 'during-model-pause')
            repository.resume_plan_item(task['plan_item_id'], 'during-model-resume')
            changed.append(change)
            return result
        with db.connect() as c:
            if change == 'paused':
                c.execute("UPDATE plan_item SET status='paused',active_segment_started_at=NULL WHERE plan_item_id=?", (task['plan_item_id'],))
            elif change == 'closed':
                c.execute("UPDATE mistake_dialogue SET phase='closed' WHERE session_id=?", (sid,))
            elif change == 'replaced':
                c.execute("UPDATE mistake_dialogue SET state_json=json_set(state_json,'$.latest_question','A different question') WHERE session_id=?", (sid,))
            elif change == 'budget':
                c.execute('UPDATE plan_item SET active_seconds=expected_minutes*60 WHERE plan_item_id=?', (task['plan_item_id'],))
            else:
                c.execute("UPDATE knowledge_node SET status='inactive' WHERE knowledge_node_id=?", (ROOT,))
            c.commit()
        changed.append(change)
        return result

    monkeypatch.setattr(gateway, 'complete', delayed)
    result = say(client, sid, settings, monkeypatch, gateway, '答案是-999', 'late-'+change)
    assert gateway.requests, 'Must cross the real Tutor generation boundary'
    assert changed == [change], 'The intended state change must succeed, not throw inside the gateway'
    assert not result['execution'].get('tutor_turn'), result['execution']
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM tutor_turn WHERE session_id=?', (sid,)).fetchone()[0] == 0
        row = c.execute('SELECT phase,state_json FROM mistake_dialogue WHERE session_id=?', (sid,)).fetchone()
        state = json.loads(row['state_json'])
        assert not state.get('teaching_turn_id')
        if change == 'closed':
            assert row['phase'] == 'closed'
        if change == 'replaced':
            assert state['latest_question'] == 'A different question'
        assert c.execute('SELECT count(*) FROM mistake_case WHERE mistake_id=?', (case,)).fetchone()[0] == 1


def test_successful_teaching_replay_keeps_one_saved_turn(teaching_chain, monkeypatch):
    db, sid, settings, gateway, client, *_ = teaching_chain
    result = say(client, sid, settings, monkeypatch, gateway, '答案是-999', 'teaching-replay')
    taught = result['execution']['tutor_turn']
    with db.connect() as c:
        row = c.execute("SELECT agent_turn_id FROM agent_turn WHERE session_id=? ORDER BY rowid DESC LIMIT 1", (sid,)).fetchone()
    count = len(gateway.requests)
    replay = client.post(f'/v1/agent-turns/{row[0]}/execute')
    assert replay.status_code == 200, replay.text
    assert replay.json()['execution']['tutor_turn']['turn_id'] == taught['turn_id']
    assert len(gateway.requests) == count
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM tutor_turn WHERE session_id=?', (sid,)).fetchone()[0] == 1


@pytest.mark.parametrize('boundary', ['before_save', 'after_save', 'attachment_failure'])
def test_teaching_commit_and_delivery_boundaries(teaching_chain, monkeypatch, boundary):
    import sqlite3
    from home_study_agent.repository import StudyRepository
    from home_study_agent.mistake_teaching_authority import MistakeTeachingAuthority
    db, sid, settings, gateway, client, *_ = teaching_chain
    save = StudyRepository.save_tutor_turn
    crossed = []

    def change():
        with db.connect() as c:
            c.execute("UPDATE mistake_dialogue SET phase='closed',state_json='{}' WHERE session_id=?", (sid,))
            c.commit()
        crossed.append(boundary)

    def guarded_save(repository, *args, **kwargs):
        if boundary == 'before_save':
            change()
        result = save(repository, *args, **kwargs)
        if boundary == 'after_save':
            change()
        return result

    def fail_attachment(_authority, _connection, _turn):
        crossed.append(boundary)
        raise sqlite3.OperationalError('synthetic attachment failure')

    monkeypatch.setattr(StudyRepository, 'save_tutor_turn', guarded_save)
    if boundary == 'attachment_failure':
        monkeypatch.setattr(MistakeTeachingAuthority, 'attach', fail_attachment)
    result = say(client, sid, settings, monkeypatch, gateway, '答案是-999', 'commit-'+boundary)
    assert crossed == [boundary]
    assert not result['execution'].get('tutor_turn')
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM tutor_turn WHERE session_id=?', (sid,)).fetchone()[0] == int(boundary == 'after_save')
        focus = c.execute('SELECT phase,state_json FROM mistake_dialogue WHERE session_id=?', (sid,)).fetchone()
        assert not json.loads(focus['state_json']).get('teaching_turn_id')
        if boundary != 'attachment_failure':
            assert focus['phase'] == 'closed' and focus['state_json'] == '{}'
        else:
            assert c.execute("SELECT count(*) FROM tutor_request_reservation WHERE session_id=? AND status='completed'", (sid,)).fetchone()[0] == 0


@pytest.mark.parametrize('already_begun', [False, True])
def test_saved_teaching_cannot_start_or_resume_after_dialogue_changed(teaching_chain, monkeypatch, already_begun):
    db, sid, settings, gateway, client, *_ = teaching_chain
    result = say(client, sid, settings, monkeypatch, gateway, '答案是-999', 'playback-source')
    taught = result['execution']['tutor_turn']
    url = f'/v1/sessions/{sid}/tutor-playback'
    body = {'turn_id': taught['turn_id'], 'client_action_id': 'guard-playback'}
    if already_begun:
        begun = client.post(url+'/begin', json=body)
        assert begun.status_code == 200, begun.text
    with db.connect() as c:
        c.execute("UPDATE mistake_dialogue SET phase='closed' WHERE session_id=?", (sid,))
        c.commit()
    if already_begun:
        read = client.get(url)
        assert read.status_code == 200, read.text
        assert read.json()['playback']['status'] == 'invalidated'
    else:
        started = client.post(url+'/begin', json=body)
        assert started.status_code == 409, started.text
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM tutor_turn WHERE turn_id=?', (taught['turn_id'],)).fetchone()[0] == 1
