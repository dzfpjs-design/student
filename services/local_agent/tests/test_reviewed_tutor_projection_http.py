"""Approved minimal source projection through the real Tutor HTTP boundary."""
import json
import asyncio
from unittest.mock import patch

import httpx
import pytest

from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.model_gateway import ModelGateway
from test_curriculum_first_source_http import CHILD, first_source_fixture, auth, publish
from test_tutor_child_model_controls import RecordingGateway


def prepared_client(tmp_path, gateway, *, replacement_question=None):
    if replacement_question:
        from home_study_agent.knowledge_base import KnowledgeBaseService
        original_import = KnowledgeBaseService.import_source
        def imported(service, child, payload):
            if payload.get('source_ref') != 'isolated:first-source:quadratic':
                return original_import(service, child, payload)
            original = json.loads(payload['content_text'])
            original['prompt'] = replacement_question
            return original_import(service, child, {**payload, 'content_text': json.dumps(original, ensure_ascii=False)})
        with patch.object(KnowledgeBaseService, 'import_source', imported):
            db, source, spec, original = first_source_fixture(tmp_path)
    else:
        db, source, spec, original = first_source_fixture(tmp_path)
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False, session_token="projection-test",
        model_provider='deepseek', model_api_key='synthetic-key', model_base_url='https://primary.invalid',
        fallback_model_provider='qwen', fallback_model_api_key='synthetic-verifier',
        fallback_model_base_url='https://verifier.invalid', fallback_model_name='synthetic-verifier')
    if callable(gateway):
        gateway = gateway(settings)
    client = TestClient(create_app(settings, model_gateway=gateway), client=("127.0.0.1", 41200), headers={"X-HSC-Session": settings.session_token})
    client.__enter__()
    auth(client)
    published = publish(client, spec)
    sid = client.post('/v1/sessions', json=dict(child_id=CHILD, client_action_id='projection-session')).json()['session_id']
    prompt = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/prepare', json=dict(
        track='school', preferred_item_id=published['diagnostic_item_id'], client_action_id='projection-prompt')).json()['prompt']
    consent = client.put('/v1/parent/privacy', json={'cloud_model_text_allowed': True})
    assert consent.status_code == 200, consent.text
    payload = dict(subject='数学', question=prompt['item']['prompt'], prompt_id=prompt['prompt_id'],
                   item_version=prompt['item_version'], client_action_id='projection-help')
    return client, db, settings, source, spec, published, sid, payload


def test_reviewed_owned_original_minimal_tutor_replay_and_restart(tmp_path):
    gateway = RecordingGateway()
    client, db, settings, source, spec, published, sid, payload = prepared_client(tmp_path, gateway)
    try:
        private_context = ['合成孩子私密称呼', '私密地区不可出机', '私密计划标题不可出机', '私密课堂回忆不可出机']
        with db.connect() as c:
            c.execute('UPDATE child_profile SET nickname=?,curriculum_region=? WHERE child_id=?', (*private_context[:2], CHILD))
            c.execute('UPDATE plan_item SET title=? WHERE plan_id IN (SELECT plan_id FROM daily_plan WHERE session_id=?)', (private_context[2], sid))
            c.execute("""INSERT INTO classroom_recall(recall_id,session_id,subject,summary,confidence,
                confirmation_status,source_refs_json,created_at,client_action_id)
                VALUES ('projection-private-recall',?,'数学',?,1,'child_confirmed','[]',datetime('now'),'projection-private-recall')""", (sid, private_context[3]))
            c.commit()
        local_context = client.app.state.repository.tutoring_context(sid)
        assert local_context['recalls'][0]['summary'] == private_context[3]
        response = client.post(f'/v1/sessions/{sid}/tutor', json=payload)
        assert response.status_code == 200, response.text
        saved = response.json()
        assert saved['output_envelope'].get('local_only') is True
        assert saved['output_envelope'].get('cloud_transmission_allowed') is False
        body = json.dumps(gateway.requests[0].messages, ensure_ascii=False)
        assert payload['question'] in body
        for private in (spec['explanation'], source['source_id'], published['candidate_id'],
                        published['diagnostic_item_id'], payload['prompt_id'], payload['item_version'], CHILD, *private_context):
            assert private not in body
        manifest = gateway.requests[0].context_manifest
        assert manifest['privacy_transform']['transmitted_recall_count'] == 0
        assert manifest['privacy_transform']['transmitted_knowledge_excerpt_count'] == 0
        assert manifest['privacy_transform']['source_projection']['policy_version'] == 'reviewed_question_projection_v1'
        repeat = client.post(f'/v1/sessions/{sid}/tutor', json=payload)
        assert repeat.status_code == 200 and repeat.json()['turn_id'] == saved['turn_id']
        assert len(gateway.requests) == 1
    finally:
        client.__exit__(None, None, None)
    with TestClient(create_app(settings, model_gateway=gateway), client=("127.0.0.1", 41200), headers={"X-HSC-Session": settings.session_token}) as restarted:
        response = restarted.post(f'/v1/sessions/{sid}/tutor', json=payload)
        assert response.status_code == 200 and response.json()['turn_id'] == saved['turn_id']
        assert len(gateway.requests) == 1
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM tutor_turn').fetchone()[0] == 1


@pytest.mark.parametrize('change', ['unreviewed', 'private', 'unknown', 'withdraw', 'privacy', 'consent_missing', 'source_changed', 'owner'])
def test_reviewed_projection_rejects_unapproved_or_changed_source_before_submission(tmp_path, change):
    gateway = RecordingGateway()
    client, db, _, source, _, published, sid, payload = prepared_client(tmp_path, gateway)
    try:
        if change == 'privacy':
            assert client.put('/v1/parent/privacy', json={'cloud_model_text_allowed': False}).status_code == 200
        else:
            with db.connect() as c:
                if change == 'unreviewed':
                    c.execute("UPDATE knowledge_source SET review_status='quarantined' WHERE source_id=?", (source['source_id'],))
                elif change in ('private', 'unknown'):
                    c.execute('UPDATE knowledge_source SET license_status=? WHERE source_id=?', ('private_note' if change == 'private' else 'unknown', source['source_id']))
                elif change == 'withdraw':
                    c.execute("UPDATE curriculum_first_source SET state='withdrawn' WHERE candidate_id=?", (published['candidate_id'],))
                elif change == 'source_changed':
                    c.execute("UPDATE knowledge_excerpt SET content_text='changed' WHERE source_id=?", (source['source_id'],))
                elif change == 'owner':
                    c.execute("UPDATE curriculum_first_source SET child_id='child-primary-4' WHERE candidate_id=?", (published['candidate_id'],))
                else:
                    c.execute("DELETE FROM consent_record WHERE scope='cloud_model_text_allowed'")
                c.commit()
        response = client.post(f'/v1/sessions/{sid}/tutor', json=payload)
        assert response.status_code in (403, 409), response.text
        assert not gateway.requests
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM tutor_turn').fetchone()[0] == 0
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize('question', ['先阅读材料：' + '这是较长的原始教材文章。' * 60, '姓名：合成学生；用求根公式解方程 x²-5x+6=0。'], ids=['long_source', 'identity'])
def test_reviewed_source_does_not_export_long_or_identifying_question(tmp_path, question):
    gateway = RecordingGateway()
    client, _, _, _, _, _, sid, payload = prepared_client(tmp_path, gateway, replacement_question=question)
    try:
        assert payload['question'] == question
        response = client.post(f'/v1/sessions/{sid}/tutor', json=payload)
        assert response.status_code == 409, response.text
        assert not gateway.requests
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize('change', ['none', 'withdraw', 'consent_cycle', 'review_version'])
def test_real_provider_body_and_result_authority(tmp_path, change):
    posts, mutations = [], []
    async def provider(request):
        body = json.loads(request.content)
        posts.append(body)
        if request.url.host == 'primary.invalid' and change != 'none':
            if change in ('withdraw', 'review_version'):
                with db.connect() as c:
                    if change == 'withdraw':
                        c.execute("UPDATE curriculum_first_source SET state='withdrawn' WHERE candidate_id=?", (published['candidate_id'],))
                    else:
                        c.execute('UPDATE curriculum_first_source SET version=version+1 WHERE candidate_id=?', (published['candidate_id'],))
                    c.commit()
            else:
                # Change actual parent consent during the physical call.
                def revoke_regrant():
                    for allowed in (False, True):
                        response = client.put('/v1/parent/privacy', json={'cloud_model_text_allowed': allowed})
                        assert response.status_code == 200, response.text
                await asyncio.to_thread(revoke_regrant)
            mutations.append(change)
        return httpx.Response(200, json={'choices': [{'message': {'content':
            'VERIFIED' if request.url.host == 'verifier.invalid' else '先看方程的一般形式，找出各项系数。你能指出二次项系数吗？'}}],
            'usage': {'prompt_tokens': 15, 'completion_tokens': 25}})
    client, db, _, source, spec, published, sid, payload = prepared_client(tmp_path,
        lambda settings: ModelGateway(settings, transport=httpx.MockTransport(provider)))
    try:
        response = client.post(f'/v1/sessions/{sid}/tutor', json=payload)
        assert posts, response.text
        wire = json.dumps(posts, ensure_ascii=False)
        assert payload['question'] in wire
        for private in (spec['explanation'], source['source_id'], published['candidate_id'], CHILD,
                        payload['prompt_id'], payload['item_version'], 'source_snapshot_json'):
            assert private not in wire
        if change == 'none':
            assert response.status_code == 200, response.text
        else:
            assert mutations == [change], 'The mutation must occur; swallowed transport errors are not evidence.'
            assert response.status_code == 409, response.text
            with db.connect() as c:
                assert c.execute('SELECT COUNT(*) FROM tutor_turn').fetchone()[0] == 0
    finally:
        client.__exit__(None, None, None)
