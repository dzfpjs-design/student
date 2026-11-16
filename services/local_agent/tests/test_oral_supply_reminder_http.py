"""Real reminder HTTP/receipt path; synthetic catalog and voice boundaries."""
from datetime import datetime, timedelta, timezone
import json

import pytest

from test_oral_supply_obligation_http import (
    test_missing_question_request_replay_and_restart_keep_original_goal as supply_journey,
)


@pytest.mark.parametrize('subject', ['authored:数学', 'authored-p4:数学'])
def test_supply_only_offers_current_question_and_resumes_after_voice_reply(tmp_path, monkeypatch, subject):
    from home_study_agent import agent_runtime, agent_orchestrator, voice_control_intent
    clock = [datetime.now(timezone.utc)]
    posts = []
    offer_ids = []

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)

    def pending(j, commitment):
        clock[0] = datetime.now(timezone.utc) + timedelta(seconds=70)
        monkeypatch.setattr(agent_runtime, 'datetime', Clock)
        monkeypatch.setattr(agent_orchestrator, 'datetime', Clock)
        monkeypatch.setattr(voice_control_intent, 'datetime', Clock)
        result = j['client'].get(f"/v1/sessions/{j['sid']}/agent/proactive-prompt")
        assert result.status_code == 200, result.text
        assert result.json()['prompt'] is None, result.json()
        with j['db'].connect() as c:
            assert not c.execute('SELECT 1 FROM agent_proactive_prompt WHERE source_id=?',
                (commitment['commitment_id'],)).fetchone()
            c.execute('''INSERT INTO agent_proactive_prompt(prompt_id,child_id,session_id,source_type,source_id,
                message_text,status,created_at,expires_at,delivery_attempts,last_attempt_at)
                VALUES('legacy-gap-reminder',?,?,'due_commitment',?,'Old generic review reminder','delivered',?,?,1,?)''',
                (j['child'],j['sid'],commitment['commitment_id'],clock[0].isoformat(),
                    (clock[0]+timedelta(minutes=10)).isoformat(),clock[0].isoformat()))
            c.commit()
        base = f"/v1/sessions/{j['sid']}/agent"
        assert not j['client'].get(base + '/proactive-prompts/legacy-gap-reminder/current').json()['current']
        assert j['client'].get(base + '/proactive-prompt').json()['status'] == 'expired'

    def available(j, client, commitment, item_id):
        from home_study_agent.oral_supply_obligation import acknowledged_offer
        clock[0] += timedelta(seconds=70)
        base = f"/v1/sessions/{j['sid']}/agent"
        result = client.get(base + '/proactive-prompt')
        assert result.status_code == 200, result.text
        offer = result.json()['prompt']
        assert offer and offer['source_type'] == 'oral_supply_ready', result.json()
        assert '新题' in offer['message'] and '复测时间到了' not in offer['message']
        path = base + '/proactive-prompts/' + offer['prompt_id']
        assert client.get(path + '/current').json()['current']
        with j['db'].connect() as c:
            c.execute('BEGIN')
            assert acknowledged_offer(c, j['child'], j['sid'], clock[0]) is None
        assert client.get(base + '/proactive-prompt').json()['status'] == 'awaiting_ack'
        with j['db'].connect() as c:
            # An offer must not allocate a question, mark delivery complete or
            # answer on the child's behalf before their voice response.
            assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt WHERE child_id=?', (j['child'],)).fetchone()[0] == 1
            assert c.execute('SELECT status FROM agent_commitment WHERE commitment_id=?',
                (commitment['commitment_id'],)).fetchone()[0] == 'pending'
            c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?", (item_id,))
            c.commit()
        assert not client.get(path + '/current').json()['current']
        assert client.get(base + '/proactive-prompt').json()['status'] == 'expired'
        with j['db'].connect() as c:
            c.execute("UPDATE diagnostic_item SET status='active' WHERE diagnostic_item_id=?", (item_id,))
            c.execute("UPDATE agent_objective SET status='paused' WHERE objective_id=?", (commitment['objective_id'],))
            c.commit()
        assert not client.get(path + '/current').json()['current']
        with j['db'].connect() as c:
            c.execute("UPDATE agent_objective SET status='active' WHERE objective_id=?", (commitment['objective_id'],))
            c.commit()
        renewed = client.get(base + '/proactive-prompt').json()['prompt']
        assert renewed and renewed['source_type'] == 'oral_supply_ready'
        assert renewed['prompt_id'] == offer['prompt_id']
        path = base + '/proactive-prompts/' + renewed['prompt_id']
        offer_ids.append(renewed['prompt_id'])
        assert client.post(path + '/acknowledge').json()['status'] == 'acknowledged'
        assert not client.get(path + '/current').json()['current']
        with j['db'].connect() as c:
            c.execute('BEGIN')
            assert acknowledged_offer(c, j['child'], j['sid'], clock[0]) == renewed['prompt_id']
            assert acknowledged_offer(c, 'another-child', j['sid'], clock[0]) is None
        clock[0] += timedelta(minutes=6)
        assert client.get(base + '/proactive-prompt').json()['prompt'] is None
        with j['db'].connect() as c:
            c.execute('BEGIN')
            assert acknowledged_offer(c, j['child'], j['sid'], clock[0]) is None
        clock[0] -= timedelta(minutes=6)
        posts.append(j['posts'])
        return '好'

    # The existing continuation executes the new voice request after the offer,
    # verifies its original objective, atomic receipt and restart-safe answer.
    supply_journey(tmp_path, monkeypatch, subject, None, after_pending=pending, after_available=available)
    contexts = [json.loads(body['messages'][1]['content']) for body in posts[0]
        if len(body.get('messages', [])) > 1 and body['messages'][1]['content'].startswith('{')]
    assert any(context.get('transcript') == '好' and context.get('state', {}).get('has_acknowledged_oral_supply_offer')
        for context in contexts)
    assert all(offer not in json.dumps(posts, ensure_ascii=False) for offer in offer_ids)


@pytest.mark.parametrize('words', ['不做了', '先不', '不要', '不用', '晚点再练', '稍后再说'])
def test_model_cannot_turn_short_rejection_into_new_question(words):
    from home_study_agent.voice_control_intent import VoiceControlIntent
    guessed = json.dumps(dict(role='control', control='oral.independent_check', quote=words, confidence=.99))
    assert VoiceControlIntent.parse(guessed, words)['status'] == 'clarify'
