"""Actual create_app/parent review/SQLite lifecycle with synthetic local ASR/PCM.

The new projection is called with the host's borrowed read transaction, not a
pretend public route. Runtime delivery remains a separate integration gate.
"""
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.database import Database
from test_agent_continuation import Gateway
from test_oral_source_revision_combined_http import author, record_and_review, get, facts
from test_pet_oral_supply_http import setup, post, BASE, CHILD, OTHER
from test_oral_teaching import project


@pytest.mark.parametrize('withdraw', ['review', 'source'])
def test_actual_published_oral_original_low_feedback_reopen_and_withdraw(tmp_path, monkeypatch, withdraw):
    initial, settings = setup(tmp_path)
    settings = replace(settings, data_encryption_key=b'O' * 32)
    db = Database(initial.path, media_root=settings.data_dir, data_encryption_key=settings.data_encryption_key)
    gateway = Gateway({})
    gateway.configured = False
    headers = {'X-HSC-Session': settings.session_token}
    with TestClient(create_app(settings, model_gateway=gateway), headers=headers,
                    client=('127.0.0.1', 41920)) as client:
        client.headers['X-HSC-Parent'] = post(client, '/v1/parent/setup', {'pin': '7391'})['parent_token']
        assert client.put('/v1/parent/privacy', json={'raw_media_storage_allowed': True,
            'cloud_model_text_allowed': False, 'cloud_audio_allowed': False,
            'model_improvement_allowed': False}).status_code == 200
        original, source, other_source, text, published = author(client, db, legacy=False)
        sid, prompt, saved, path, body = record_and_review(client, db, settings, monkeypatch, published)
        high = project(db, sid, prompt)
        assert high['status'] == 'reviewed_audio_feedback'
        low_body = {**body, 'expected_revision': 1, 'client_action_id': 'specific-low-review',
            'dimensions': [{**d, 'score': 0,
                'rationale': 'Try connecting your chosen place to one concrete reason.'}
                for d in body['dimensions']]}
        reviewed = post(client, path + '/review', low_body)
        assert not reviewed['qualification']['eligible_for_mastery_gate']
        low = project(db, sid, prompt)
        assert low['task_points'] == [dict(task_point=p, status='not_assessed') for p in original['task_points']]
        assert low['review_ref']['review_revision'] == 2
        assert 'one concrete reason' in low['spoken_feedback']
        assert not low['mastery_claim_allowed']
        assert low['next_action']['binding']['prompt_id'] == prompt['prompt_id']
        assert low['next_action']['binding']['feedback_version'] == low['feedback_version']
        # A lost review response reuses its exact receipt, not a new attempt/review.
        assert post(client, path + '/review', low_body)['event'] == reviewed['event']
        original_facts = facts(db, CHILD)
        other_before = facts(db, OTHER)
        assert client.get(path, headers={'X-HSC-Parent': ''}).status_code == 401
        assert client.get(path.replace(CHILD, OTHER)).status_code in {404, 409}
    with TestClient(create_app(settings, model_gateway=gateway), headers=headers,
                    client=('127.0.0.1', 41921)) as client:
        client.headers['X-HSC-Parent'] = post(client, '/v1/parent/unlock', {'pin': '7391'})['parent_token']
        assert project(db, sid, prompt) == low
        assert post(client, path + '/review', low_body)['event'] == reviewed['event']
        if withdraw == 'review':
            post(client, path + '/review', dict(expected_revision=2, client_action_id='withdraw-oral-review',
                operation='retract', reason='Synthetic reviewer withdrew the old judgment.'))
        else:
            post(client, BASE + '/' + published['candidate_id'] + '/withdraw', dict(
                action_id='withdraw-oral-source', expected_version=published['version'], note='Source withdrawn.'))
        invalid = project(db, sid, prompt)
        assert invalid['status'] == ('content_self_check' if withdraw == 'review' else 'unavailable')
        assert invalid['feedback'] == [] and invalid['review_ref'] is None
        assert invalid['feedback_version'] != low['feedback_version']
        # Old exact approve key can only recover its historical receipt, never authority.
        post(client, path + '/review', low_body)
        assert project(db, sid, prompt) == invalid
        assert facts(db, CHILD)['diagnostic_attempt'] == original_facts['diagnostic_attempt']
        assert facts(db, CHILD)['learning_evidence'] == original_facts['learning_evidence']
        assert facts(db, OTHER) == other_before
    with TestClient(create_app(settings, model_gateway=gateway), headers=headers,
                    client=('127.0.0.1', 41922)) as client:
        client.headers['X-HSC-Parent'] = post(client, '/v1/parent/unlock', {'pin': '7391'})['parent_token']
        assert project(db, sid, prompt) == invalid
        assert get(client, path)['history']
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0] == 0
    assert not gateway.requests
