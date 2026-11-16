"""Review scheduling through actual voice, planning, execution and local storage."""
import json
from datetime import datetime, timedelta, timezone
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.document_review_schedule import parse
from test_diagnostic_prompt_http import environment
from test_document_goal_http import journey, teach, BODY
from test_voice_runtime_binding import capture, interpret


@pytest.mark.parametrize('confidence,when', [
    (.5, {'kind':'relative', 'minutes':30}),
    (.99, {'kind':'calendar', 'date':'2026-01-01', 'time':'12:00'}),
])
def test_uncertain_or_past_time_is_replayable_clarification(confidence, when):
    raw = dict(meaning='propose', quote='稍后再看', time=when, confidence=confidence)
    saved = parse(json.dumps(raw), raw['quote'], '2026-09-13T10:00:00+00:00')
    assert saved['meaning'] == 'unclear' and saved['due_at'] is None
    assert parse(json.dumps({k:saved[k] for k in raw}), raw['quote'], '2026-09-13T10:00:00+00:00') == saved


def spoken(j, monkeypatch, text, meaning, when=None, *, confidence=.99, key, execute=True):
    j['responses'].append(json.dumps(dict(meaning=meaning, quote=text, time=when, confidence=confidence)))
    voice = capture(j['client'], j['settings'], monkeypatch, j['sid'], text, key=key+'-voice')
    turn = interpret(j['client'], j['sid'], text, key, voice)
    assert turn['runtime_capability'] == 'learning.review_schedule', turn
    if not execute:
        return turn, None
    response = j['client'].post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
    assert response.status_code == 200, response.text
    saved = response.json()['turn']
    assert saved['tool_status'] == 'completed', saved
    return turn, saved


def commitments(j):
    with j['db'].connect() as c:
        assert c.execute('SELECT count(*) FROM learning_evidence').fetchone()[0] == 0
        return [dict(r) for r in c.execute('SELECT * FROM agent_commitment')]


def test_voice_propose_confirm_restart_change_cancel(journey, monkeypatch):
    j = journey
    teach(j)
    turn, proposed = spoken(j, monkeypatch, '半小时后再看这道题', 'propose',
        {'kind':'relative', 'minutes':30}, key='review-propose')
    assert not commitments(j)
    assert '北京时间' in proposed['response_text'] and '对吗' in proposed['response_text']
    request = j['calls'][-1]
    serialized = json.dumps(request, ensure_ascii=False)
    assert BODY not in serialized and j['root']['agent_turn_id'] not in serialized
    assert set(json.loads(request['messages'][-1]['content'])) == {'transcript','phase','current_time','timezone'}
    turn, saved = spoken(j, monkeypatch, '对，就照你说的这个时间安排', 'confirm', key='review-confirm')
    rows = commitments(j)
    assert len(rows) == 1 and rows[0]['status'] == 'pending' and rows[0]['commitment_type'] == 'reminder'
    old_due, cid = rows[0]['due_at'], rows[0]['commitment_id']
    assert '已保存' in saved['response_text']
    j['client'].__exit__(None, None, None)
    with TestClient(create_app(j['settings'], model_gateway=j['gateway'])) as restarted:
        j['client'] = restarted
        replay = restarted.post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
        assert replay.status_code == 200, replay.text
        assert replay.json()['turn']['response_text'] == saved['response_text']
        assert commitments(j) == rows
        observed = subprocess.run([sys.executable, '-c',
            "import sqlite3,sys,json; c=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True); "
            "print(json.dumps(c.execute('select commitment_id,due_at,status from agent_commitment').fetchall()))",
            str(j['settings'].database_path)], check=True, capture_output=True, text=True)
        assert json.loads(observed.stdout) == [[cid, old_due, 'pending']]
        spoken(j, monkeypatch, '改到一小时后吧', 'propose', {'kind':'relative','minutes':60}, key='review-change')
        assert commitments(j)[0]['due_at'] == old_due
        spoken(j, monkeypatch, '这个新时间可以，确定吧', 'confirm', key='review-change-confirm')
        changed = commitments(j)
        assert len(changed) == 1 and changed[0]['commitment_id'] == cid and changed[0]['due_at'] > old_due
        _, cancelled = spoken(j, monkeypatch, '取消这道题的复习提醒，别停掉学习', 'cancel', key='review-cancel')
        assert commitments(j)[0]['status'] == 'cancelled'
        assert '已取消' in cancelled['response_text']


@pytest.mark.parametrize('meaning,when,confidence', [
    ('unclear', None, .99), ('propose', {'kind':'relative','minutes':30}, .4),
    ('confirm', None, .99),
])
def test_unclear_or_unoffered_confirmation_never_schedules(journey, monkeypatch, meaning, when, confidence):
    teach(journey)
    _, saved = spoken(journey, monkeypatch, '我再想一下', meaning, when, confidence=confidence, key='uncertain-time')
    assert not commitments(journey)
    assert '还没有确定' in saved['response_text']


@pytest.mark.parametrize('change', ['arrival', 'proposal', 'source', 'tutor', 'voice'])
def test_late_or_changed_confirmation_cannot_create_commitment(journey, monkeypatch, change):
    j = journey
    teach(j)
    spoken(j, monkeypatch, '明天这个时候再看', 'propose', {'kind':'relative','minutes':1440}, key='offer-time')
    turn, _ = spoken(j, monkeypatch, '行，就这个时间', 'confirm', key='confirm-time', execute=False)
    if change == 'arrival':
        capture(j['client'], j['settings'], monkeypatch, j['sid'], '先别安排，我有新问题', key='newer-arrival')
    else:
        with j['db'].connect() as c:
            if change == 'proposal':
                c.execute("""UPDATE agent_plan SET context_manifest_json=json_set(context_manifest_json,
                    '$.document_goal_handoff_v1.review_time_proposal.due_at','2028-01-01T00:00:00+00:00') WHERE agent_turn_id=?""", (j['root']['agent_turn_id'],))
            elif change == 'source':
                c.execute("UPDATE capture_artifact SET extracted_text='changed source' WHERE artifact_id=?", (j['source']['artifact_id'],))
            elif change == 'tutor':
                c.execute("UPDATE tutor_turn SET answer='changed teaching' WHERE session_id=?", (j['sid'],))
            else:
                c.execute("UPDATE agent_turn SET transcript='another utterance' WHERE agent_turn_id=?", (turn['agent_turn_id'],))
            c.commit()
    response = j['client'].post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
    assert response.status_code in {409, 422}, response.text
    assert not commitments(j)


def test_unavailable_model_preserves_prior_schedule(journey, monkeypatch):
    j = journey
    teach(j)
    spoken(j, monkeypatch, '一会儿半小时后再看', 'propose', {'kind':'relative','minutes':30}, key='before-unavailable')
    spoken(j, monkeypatch, '确认这个安排', 'confirm', key='confirmed-before-unavailable')
    rows = commitments(j)
    j['responses'].append('not a valid model response')
    text = '帮我改到一小时后'
    voice = capture(j['client'], j['settings'], monkeypatch, j['sid'], text, key='failed-model-voice')
    turn = interpret(j['client'], j['sid'], text, 'failed-model', voice)
    response = j['client'].post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
    assert response.status_code == 200, response.text
    assert '理解没有成功' in response.json()['turn']['response_text']
    assert commitments(j) == rows


def test_written_appointment_survives_failure_before_executor_receipt(journey, monkeypatch):
    from home_study_agent.agent_capability_executor import AgentCapabilityExecutor
    j = journey
    teach(j)
    spoken(j, monkeypatch, '二十分钟以后提醒', 'propose', {'kind':'relative','minutes':20}, key='receipt-offer')
    turn, _ = spoken(j, monkeypatch, '嗯，确认', 'confirm', key='receipt-confirm', execute=False)
    original = AgentCapabilityExecutor._dispatch
    with monkeypatch.context() as patch:
        def interrupted(self, step):
            original(self, step)
            raise RuntimeError('synthetic receipt interruption')
        patch.setattr(AgentCapabilityExecutor, '_dispatch', interrupted)
        with pytest.raises(RuntimeError, match='synthetic receipt interruption'):
            j['client'].post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
    rows = commitments(j)
    assert len(rows) == 1 and rows[0]['status'] == 'pending'
    j['client'].__exit__(None, None, None)
    with TestClient(create_app(j['settings'], model_gateway=j['gateway'])) as restarted:
        response = restarted.post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
        assert response.status_code == 200, response.text
        assert response.json()['turn']['tool_status'] == 'completed'
    assert commitments(j) == rows


def test_changed_commitment_cannot_be_overwritten_by_next_confirmation(journey, monkeypatch):
    j = journey
    teach(j)
    spoken(j, monkeypatch, '二十分钟以后提醒', 'propose', {'kind':'relative','minutes':20}, key='payload-offer')
    spoken(j, monkeypatch, '好的，安排吧', 'confirm', key='payload-confirm')
    with j['db'].connect() as c:
        c.execute("UPDATE agent_commitment SET payload_json=json_set(payload_json,'$.artifact_id','different-page')")
        c.commit()
    rows = commitments(j)
    turn, _ = spoken(j, monkeypatch, '取消这个复习安排', 'cancel', key='payload-cancel', execute=False)
    response = j['client'].post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
    assert response.status_code == 409, response.text
    assert commitments(j) == rows


def test_review_appointment_is_not_misrepresented_as_qualified_retention(journey, monkeypatch):
    from home_study_agent import agent_runtime
    j = journey
    teach(j)
    spoken(j, monkeypatch, '一分钟以后再看', 'propose', {'kind':'relative','minutes':1}, key='due-offer')
    spoken(j, monkeypatch, '确定这个时间', 'confirm', key='due-confirm')
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(timezone.utc) + timedelta(minutes=2)
    monkeypatch.setattr(agent_runtime, 'datetime', Clock)
    with j['db'].connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (j['sid'],))
        c.commit()
    response = j['client'].get(f'/v1/sessions/{j["sid"]}/agent/proactive-prompt')
    assert response.status_code == 200, response.text
    assert response.json()['status'] != 'waiting_child', response.text
    assert '开始复测' not in response.text
    assert commitments(j)[0]['status'] == 'pending'
