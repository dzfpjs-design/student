"""Authorized human audio assessment in the existing immutable rubric journal."""
import json

import pytest

from home_study_agent.database import Database
from home_study_agent.learning_rubric import RubricEvidenceService
from home_study_agent.pet_skill_gate import pet_skill_gate
from test_pet_media_evidence import media, CHILD, speaking, bind_speaking, local_review, change


def prepared(media):
    prompt, transcript = speaking(media)
    binding = bind_speaking(media, prompt)
    snapshot = local_review(media, prompt, transcript, approve=False)
    database = Database(media[0].path, media_root=media[4].parent)
    service = RubricEvidenceService(database)
    context = service.audio_context(CHILD, snapshot['assessment_id'])
    packet = context['bindings'][0]
    review = {'schema': 'pet_speaking_audio_references_v1', 'binding_version': packet['binding_version'],
        'audio_sha256': packet['audio']['sha256'], 'observations': [dict(dimension_id=name, start_frame=0,
            end_frame=packet['audio']['frames'], heard_text='Synthetic fixture annotation, not real speech outcome',
            rationale='Human fixture checked original frames') for name in packet['dimensions_required']]}
    body = dict(expected_revision=0, client_action_id='audio-review', reviewer_id='authorized-parent', reviewer_role='parent',
        operation='approve', reason='Isolated human review fixture, not a child assessment', independent=True, hint_level=0,
        review_profile='pet_speaking_audio_v1', audio_evidence={'media_id': binding['media_id'], 'review': review},
        dimensions=[dict(dimension_id=d['dimension_id'], score=3, response_quotes=[transcript] if d['dimension_id'] in {'task','coherence','language'} else [],
                         rationale='Checked this dimension from original material') for d in context['dimensions']],
        criterion_evidence=[dict(criterion=c, response_quote=transcript) for c in snapshot['criteria']])
    return database, service, snapshot, body, prompt


def test_audio_review_is_atomic_idempotent_recoverable_and_qualified_by_original_source(media):
    db, service, snapshot, body, prompt = prepared(media)
    aid = snapshot['assessment_id']
    result = service.review(CHILD, aid, **body)
    assert result['review_profile'] == 'pet_speaking_audio_v1' and result['passed']
    assert result['audio_evidence'] == body['audio_evidence']
    assert service.review(CHILD, aid, **body) == result
    restarted = RubricEvidenceService(Database(db.path, media_root=media[4].parent))
    assert restarted.qualification(CHILD, aid)['eligible_for_mastery_gate']
    from home_study_agent.pet_media_evidence import PetMediaEvidenceService
    projected = PetMediaEvidenceService(db, media_root=media[4].parent).read(media[2], media_id=body['audio_evidence']['media_id'])
    assert projected['qualification']['skill_qualified']
    assert projected['qualification']['assessment_scope'] == 'reviewed_human_original_audio'
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM rubric_review_event').fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1
        attempt = c.execute('SELECT * FROM diagnostic_attempt').fetchone()
        gate = pet_skill_gate(c, CHILD, prompt['diagnostic_item_id'], session_id=media[2], prompt_id=prompt['prompt_id'],
            item_version=prompt['item_version'], attempt_id=attempt['attempt_id'], evidence_id=attempt['evidence_id'], media_root=media[4].parent)
        assert gate['skill_scoring_allowed'] and gate['assessment_scope'] == 'reviewed_human_original_audio'
    with pytest.raises(ValueError, match='IDEMPOTENCY'):
        restarted.review(CHILD, aid, **{**body, 'reason': 'different payload'})
    restarted.review(CHILD, aid, expected_revision=1, client_action_id='withdraw', reviewer_id='parent', reviewer_role='parent', operation='retract', reason='Correcting human judgment')
    assert not restarted.qualification(CHILD, aid)['eligible_for_mastery_gate']


@pytest.mark.parametrize('fault', ['missing_audio', 'changed_audio', 'changed_voice', 'wrong_media', 'frames', 'text_profile', 'missing_scores', 'low_score', 'assisted'])
def test_audio_references_alone_or_text_scores_cannot_certify_speaking(media, fault):
    _, service, snapshot, body, _ = prepared(media)
    if fault == 'missing_audio': media[4].unlink()
    elif fault == 'changed_audio': media[4].write_bytes(b'changed')
    elif fault == 'changed_voice': change(media[0], "UPDATE voice_input SET transcript='different'")
    elif fault == 'wrong_media': body['audio_evidence']['media_id'] = 'other'
    elif fault == 'frames': body['audio_evidence']['review']['observations'][0]['end_frame'] = 999999
    elif fault == 'text_profile': body['review_profile'] = 'english_open'
    elif fault == 'missing_scores': body['dimensions'] = []
    elif fault == 'low_score': body['dimensions'][-1]['score'] = 0
    else: body['independent'] = False
    if fault in {'low_score', 'assisted'}:
        service.review(CHILD, snapshot['assessment_id'], **body)
        assert not service.qualification(CHILD, snapshot['assessment_id'])['eligible_for_mastery_gate']
    else:
        with pytest.raises(ValueError): service.review(CHILD, snapshot['assessment_id'], **body)
        with media[0].connect() as c:
            assert c.execute('SELECT COUNT(*) FROM rubric_review_event').fetchone()[0] == 0


def test_recompute_checks_bytes_and_same_transaction_source_revision(media):
    db, service, snapshot, body, _ = prepared(media)
    service.review(CHILD, snapshot['assessment_id'], **body)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute("UPDATE voice_input SET transcript='changed'")
        assert not service.qualification(CHILD, snapshot['assessment_id'], connection=c)['eligible_for_mastery_gate']
        c.rollback()
    assert service.qualification(CHILD, snapshot['assessment_id'])['eligible_for_mastery_gate']
    media[4].unlink()
    assert not service.qualification(CHILD, snapshot['assessment_id'])['eligible_for_mastery_gate']


def test_later_failed_revision_does_not_borrow_older_audio_pass(media):
    db, service, snapshot, body, prompt = prepared(media)
    service.review(CHILD, snapshot['assessment_id'], **body)
    body.update(expected_revision=1, client_action_id='correct-the-review')
    body['dimensions'][-1]['score'] = 0
    service.review(CHILD, snapshot['assessment_id'], **body)
    with db.connect() as c:
        attempt = c.execute('SELECT * FROM diagnostic_attempt').fetchone()
        gate = pet_skill_gate(c, CHILD, prompt['diagnostic_item_id'], session_id=media[2], prompt_id=prompt['prompt_id'],
            item_version=prompt['item_version'], attempt_id=attempt['attempt_id'], evidence_id=attempt['evidence_id'])
    assert not gate['skill_scoring_allowed'] and gate['review_revision'] == 2


def test_original_english_open_review_alone_stays_text_only(media):
    db, service, snapshot, _, _ = prepared(media)
    from test_pet_media_evidence import review_rubric
    review_rubric(service, snapshot)
    assert service.get(CHILD, snapshot['assessment_id'])['profile'] == 'english_open'
    assert not service.qualification(CHILD, snapshot['assessment_id'])['eligible_for_mastery_gate']


def test_failed_event_write_does_not_save_partial_audio_or_score(media):
    db, service, snapshot, body, _ = prepared(media)
    with db.connect() as c:
        c.execute("CREATE TRIGGER fail_audio_review BEFORE INSERT ON rubric_review_event BEGIN SELECT RAISE(ABORT,'audio review failure'); END")
        c.commit()
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError): service.review(CHILD, snapshot['assessment_id'], **body)
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM rubric_review_event').fetchone()[0] == 0
        c.execute('DROP TRIGGER fail_audio_review')
        c.commit()
    assert service.review(CHILD, snapshot['assessment_id'], **body)['review_revision'] == 1


def test_actual_host_parent_audio_review_receipt_restart_and_withdraw(media):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from test_pet_reviewed_source_actual_http import NoCloud
    db, service, snapshot, body, _ = prepared(media)
    settings = Settings(data_dir=media[4].parent, database_path=db.path, maintenance_enabled=False, session_token='audio-review-host')
    local = {'X-HSC-Session': settings.session_token}
    path = f"/v1/parent/children/{CHILD}/learning-rubrics/{snapshot['assessment_id']}"
    payload = {k: v for k, v in body.items() if k not in {'reviewer_id', 'reviewer_role'}}
    def persisted_counts():
        with db.connect() as c:
            return tuple(c.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                         for table in ('rubric_review_event', 'diagnostic_attempt', 'pet_media_event', 'model_call'))

    with TestClient(create_app(settings, model_gateway=NoCloud()), raise_server_exceptions=False) as client:
        token = client.post('/v1/parent/setup', json={'pin':'2468'}, headers=local).json()['parent_token']
        headers = {**local, 'X-HSC-Parent': token}
        assert client.post(path + '/review', headers=local, json=payload).status_code == 401
        detail = client.get(path, headers=headers)
        assert detail.status_code == 200, detail.text
        packet = detail.json()['audio_review']['bindings'][0]
        params = {'media_id': packet['media_id'], 'audio_sha256': packet['audio']['sha256']}
        before_get = persisted_counts()
        original = client.get(path + '/audio', params=params, headers=headers)
        assert original.status_code == 200 and original.content == media[4].read_bytes()
        assert original.headers['cache-control'] == 'no-store'
        assert original.headers['x-hsc-audio-sha256'] == params['audio_sha256']
        assert client.get(path + '/audio', params=params, headers=local).status_code == 401
        assert client.get(path + '/audio', params=params, headers={'X-HSC-Parent': token}).status_code == 401
        assert client.get(path.replace('/' + CHILD + '/', '/child-junior-3/') + '/audio', params=params, headers=headers).status_code in {404, 409}
        assert client.get(path + '/audio', params={**params, 'audio_sha256': '0' * 64}, headers=headers).status_code == 409
        assert client.get(path + '/audio', params=params, headers={**headers, 'Range':'bytes=0-1'}).status_code == 416
        assert persisted_counts() == before_get
        assert client.post(path + '/review', headers=headers, json={**payload, 'reviewer_role':'teacher'}).status_code == 422
        result = client.post(path + '/review', json=payload, headers=headers)
        assert result.status_code == 200, result.text
        assert result.json()['qualification']['eligible_for_mastery_gate'], result.json()
        replay = client.post(path + '/review', json=payload, headers=headers)
        assert replay.status_code == 200 and replay.json()['event'] == result.json()['event']
        assert client.post(path + '/review', json={**payload, 'reason': 'different payload'}, headers=headers).status_code == 409
        after_review = persisted_counts()
        assert after_review[0] == 1 and after_review[1:] == before_get[1:]
        with db.connect() as c:
            row = c.execute('SELECT payload_json,request_json FROM rubric_review_event').fetchone()
            assert json.loads(row['payload_json'])['audio_evidence'] == payload['audio_evidence']
            assert json.loads(row['request_json'])['audio_evidence'] == payload['audio_evidence']
    with TestClient(create_app(settings, model_gateway=NoCloud()), raise_server_exceptions=False) as client:
        token = client.post('/v1/parent/unlock', json={'pin':'2468'}, headers=local).json()['parent_token']
        headers = {**local, 'X-HSC-Parent': token}
        restored = client.get(path, headers=headers).json()
        assert restored['history'][0]['audio_evidence'] == payload['audio_evidence']
        assert restored['qualification']['eligible_for_mastery_gate']
        assert restored['qualification']['receipt']['review_revision'] == 1
        assert client.post(path + '/review', json=payload, headers=headers).json()['event'] == result.json()['event']
        assert persisted_counts() == after_review
        # Recheck through actual HTTP must read current bytes, not a stored pass.
        original_bytes = media[4].read_bytes()
        media[4].write_bytes(original_bytes[:-2])
        changed = client.get(path, headers=headers)
        assert changed.status_code == 200, changed.text
        assert not changed.json()['qualification']['eligible_for_mastery_gate']
        assert client.get(path + '/audio', headers=headers, params=params).status_code == 409
        assert persisted_counts() == after_review
        media[4].write_bytes(original_bytes)
        assert client.get(path, headers=headers).json()['qualification']['eligible_for_mastery_gate']
        result = client.post(path + '/review', headers=headers, json={'expected_revision':1, 'client_action_id':'audio-retract', 'operation':'retract', 'reason':'Changed human judgment'})
        assert result.status_code == 200, result.text
        assert not result.json()['qualification']['eligible_for_mastery_gate']
        # Retraction retains original audio for corrected authorized re-review.
        assert client.get(path + '/audio', headers=headers, params=params).status_code == 200
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM rubric_review_event').fetchone()[0] == 2
            assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0] == 0
    with TestClient(create_app(settings, model_gateway=NoCloud()), raise_server_exceptions=False) as client:
        token = client.post('/v1/parent/unlock', json={'pin': '2468'}, headers=local).json()['parent_token']
        headers = {**local, 'X-HSC-Parent': token}
        before_get = persisted_counts()
        restored = client.get(path, headers=headers)
        assert restored.status_code == 200, restored.text
        assert not restored.json()['qualification']['eligible_for_mastery_gate']
        assert [event['operation'] for event in restored.json()['history']] == ['approve', 'retract']
        assert persisted_counts() == before_get
