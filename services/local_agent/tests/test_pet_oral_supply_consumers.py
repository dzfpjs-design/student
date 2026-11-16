"""Real HTTP answer/lifecycle boundaries for reviewed oral sources in isolation."""
from contextlib import contextmanager
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.local_supply import validate_item, LocalSupplyError
from home_study_agent.pet_audio_rubric import scoped_speaking_gate
from test_pet_oral_supply_http import setup, authored, post, BASE, CHILD, OTHER, FAMILIES
from test_agent_continuation import Gateway
from test_voice_runtime_binding import capture, interpret, submit
from test_diagnostic_prompt_http import start


@contextmanager
def published(tmp_path, family='topic_response'):
    db, settings = setup(tmp_path)
    gateway = Gateway({})
    gateway.configured = False
    with TestClient(create_app(settings, model_gateway=gateway),
            client=('127.0.0.1', 41902), headers={'X-HSC-Session': settings.session_token}) as client:
        client.headers['X-HSC-Parent'] = post(client, '/v1/parent/setup', {'pin': '7391'})['parent_token']
        original, source, draft = authored(client, db, family)
        cid = draft['candidate_id']
        for version, operation in enumerate(('validate', 'review', 'publish'), 1):
            body = dict(action_id=operation, expected_version=version)
            if operation == 'review': body.update(decision='approve', note='Synthetic source review only')
            result = post(client, BASE + f'/{cid}/{operation}', body)
        assert result['eligible'], result['blockers']
        yield client, db, settings, gateway, original, source, result


@pytest.mark.parametrize('family', ['topic_response', 'read_aloud'])
def test_actual_capture_original_answer_remains_reference_and_replays(tmp_path, monkeypatch, family):
    with published(tmp_path, family) as (client, db, settings, gateway, original, source, result):
        sid = start(client)
        item_id = result['diagnostic_item_id']
        prepared = post(client, f'/v1/sessions/{sid}/diagnostic-prompt/prepare', dict(
            track='pet', preferred_item_id=item_id, client_action_id='ask-published-oral'))
        prompt = prepared['prompt']
        assert prompt['item']['prompt'] == original['prompt']
        assert prompt['item']['answer_type'] == original['answer_type']
        assert (prompt['item']['stimulus_text'] or None) == original['stimulus_text']
        answer = original['stimulus_text'] or 'I would like to visit the park because there are flowers and trees. I would walk there with my family and enjoy the fresh air.'
        voice = capture(client, settings, monkeypatch, sid, answer, prompt, 'oral-original')
        candidate = interpret(client, sid, answer, 'oral-answer', voice)
        if candidate['intent'] == 'pending_answer_clarification':
            assert client.post(f"/v1/agent-turns/{candidate['agent_turn_id']}/execute").status_code == 200
            text = '刚才那句就是我的答案'
            confirm_voice = capture(client, settings, monkeypatch, sid, text, prompt, 'oral-confirmation')
            candidate = interpret(client, sid, text, 'oral-answer-confirmed', confirm_voice)
        assert candidate['action']['source_voice_input_id'] == voice
        saved = submit(client, sid, prompt, candidate, answer=answer, key='oral-save')
        assert saved.status_code == 200, saved.text
        assert saved.json()['status'] == 'answered'
        assert saved.json()['attempt']['submitted_answer'] == answer
        assert saved.json()['attempt']['score_status'] != 'verified_local'
        assert submit(client, sid, prompt, candidate, answer=answer, key='oral-save').json()['attempt']['attempt_id'] == saved.json()['attempt']['attempt_id']
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 1
            assert c.execute('SELECT count(*) FROM rubric_review_event').fetchone()[0] == 0
            assert not scoped_speaking_gate(c, CHILD, item_id)['skill_scoring_allowed']
            assert c.execute('SELECT count(*) FROM model_call').fetchone()[0] == 0
        assert not gateway.requests
    with TestClient(create_app(settings, model_gateway=gateway), headers={'X-HSC-Session': settings.session_token}) as client:
        replay = submit(client, sid, prompt, candidate, answer=answer, key='oral-save')
        assert replay.status_code == 200, replay.text
        assert replay.json()['attempt']['attempt_id'] == saved.json()['attempt']['attempt_id']


@pytest.mark.parametrize('fault', ['source_revoke', 'excerpt_change', 'node_change', 'item_mode', 'material_change'])
def test_live_read_downgrades_changed_oral_proof_without_rewriting_history(tmp_path, fault):
    family = 'picture_description' if fault == 'material_change' else 'topic_response'
    with published(tmp_path, family) as (client, db, settings, gateway, original, source, result):
        with db.connect() as c:
            historical = c.execute('SELECT payload_json,source_snapshot_json FROM curriculum_supply_package WHERE candidate_id=?',
                                   (result['candidate_id'],)).fetchone()[:]
            if fault == 'source_revoke':
                c.execute("UPDATE knowledge_source SET review_status='pending' WHERE source_id=?", (source['source_id'],))
            elif fault == 'excerpt_change':
                c.execute("UPDATE knowledge_excerpt SET content_text='changed' WHERE source_id=?", (source['source_id'],))
            elif fault == 'node_change':
                c.execute("UPDATE knowledge_node SET code='PET.LISTENING.ORAL_FAULT' WHERE knowledge_node_id=?", (original['knowledge_node_id'],))
            elif fault == 'item_mode':
                c.execute("UPDATE diagnostic_item SET assessment_mode='objective' WHERE diagnostic_item_id=?", (result['diagnostic_item_id'],))
            else:
                c.execute("UPDATE diagnostic_item SET stimulus_asset='wrong-picture' WHERE diagnostic_item_id=?", (FAMILIES[family],))
            c.commit()
        read = client.get(BASE + '/' + result['candidate_id'])
        assert read.status_code == 200 and not read.json()['eligible'], read.text
        with db.connect() as c:
            with pytest.raises(LocalSupplyError): validate_item(c, CHILD, result['diagnostic_item_id'])
            assert c.execute('SELECT payload_json,source_snapshot_json FROM curriculum_supply_package WHERE candidate_id=?',
                             (result['candidate_id'],)).fetchone()[:] == historical


@pytest.mark.parametrize('scope', ['learning', 'knowledge', 'all_history'])
def test_actual_parent_export_delete_source_history_and_restart(tmp_path, scope):
    with published(tmp_path, 'read_aloud') as (client, db, settings, gateway, original, source, result):
        with db.connect() as c:
            other_before = [tuple(r) for r in c.execute('SELECT * FROM child_profile WHERE child_id=?', (OTHER,))]
        exported = post(client, f'/v1/parent/children/{CHILD}/export', {})
        data = json.loads(Path(exported['path']).read_text())
        assert any(json.loads(row['payload_json'])['pet_oral'] == original for row in data['curriculum_supply_packages'])
        deleted = post(client, f'/v1/parent/children/{CHILD}/delete', dict(
            scope=scope, confirmation='确认删除', client_action_id='delete-oral-source'))
        assert deleted['status'] == 'completed', deleted
        with db.connect() as c:
            assert not c.execute('SELECT 1 FROM curriculum_supply_package WHERE child_id=?', (CHILD,)).fetchone()
            assert not c.execute('SELECT 1 FROM curriculum_supply_event WHERE child_id=?', (CHILD,)).fetchone()
            assert not c.execute('SELECT 1 FROM curriculum_supply_selection WHERE child_id=?', (CHILD,)).fetchone()
            assert c.execute('SELECT status FROM diagnostic_item WHERE diagnostic_item_id=?',
                             (result['diagnostic_item_id'],)).fetchone()[0] == 'retired'
            assert [tuple(r) for r in c.execute('SELECT * FROM child_profile WHERE child_id=?', (OTHER,))] == other_before
            assert not c.execute('PRAGMA foreign_key_check').fetchall()
    with TestClient(create_app(settings, model_gateway=gateway), client=('127.0.0.1', 41903),
            headers={'X-HSC-Session': settings.session_token}) as client:
        client.headers['X-HSC-Parent'] = post(client, '/v1/parent/unlock', {'pin': '7391'})['parent_token']
        assert client.get(BASE + '/' + result['candidate_id']).status_code == 404
