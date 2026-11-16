"""Published synthetic visual originals, real parent HTTP and original-answer stores."""
from contextlib import contextmanager
from types import SimpleNamespace
import json
from copy import deepcopy
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.learning_obligations import _question_catalog, question_identity
from home_study_agent.learning_rubric import RubricEvidenceService
from home_study_agent.local_supply import validate_item
from test_pet_reviewed_source_actual_http import NoCloud
from test_visual_curriculum_supply_http import supply_case
from test_visual_material_host import setup_case, headers, REGION


@contextmanager
def published(tmp_path, monkeypatch, *, diagonal=False):
    import test_visual_material_host as host_fixture
    from test_visual_material import visual_case
    monkeypatch.setattr(host_fixture, 'visual_case', lambda path: visual_case(path, diagonal=diagonal))
    db, settings, prepared, args, region = setup_case(tmp_path)
    with TestClient(create_app(settings, model_gateway=NoCloud()), client=('127.0.0.1', 42180)) as client:
        h = headers(client)
        client.app.state.visual_materials.stage_prepared('child', 'page', 'figure',
            revision_id=args['revision_id'], region_version=args['region_version'], prepared=prepared)
        r = client.post(REGION+'/storage', headers=h, json={**args, 'client_action_id':'store',
            'purpose':'local_visual_material_review_v1'})
        assert r.status_code == 200, r.text
        case = SimpleNamespace(db=client.app.state.repository.database, settings=settings, client=client,
            headers=h, args=args, region=region, prepared=prepared)
        display = display_image(case, 'publication-display')
        r = client.post(REGION+'/confirm', headers=h, json={**args, 'client_action_id':'confirm',
            'display_receipt_id':display})
        assert r.status_code == 200, r.text
        case.binding = r.json()['detail']['binding']
        source, _, spec = supply_case(case.db, case.binding)
        case.source = source
        base = '/api/parent/children/child/curriculum-supply'
        r = client.post(base+'/generate', headers=h, json={'action_id':'draft', 'expected_version':0,
            'expected_scope_version':1, 'source':spec})
        assert r.status_code == 200, r.text
        case.candidate_id = r.json()['candidate_id']
        for version, operation in enumerate(('validate','review','publish'), 1):
            body = {'action_id':operation, 'expected_version':version}
            if operation == 'review': body.update(decision='approve', note='Synthetic published original, not pupil evidence')
            r = client.post(base+'/'+case.candidate_id+'/'+operation, headers=h, json=body)
            assert r.status_code == 200, r.text
        case.item_id = r.json()['diagnostic_item_id']
        yield case


def display_image(case, key):
    image = case.client.get(REGION+'/image', headers=case.headers,
        params={**case.args, 'sha256':case.region['safe_png_sha256']})
    assert image.status_code == 200, image.text
    r = case.client.post(REGION+'/displayed', headers=case.headers, json={**case.args,
        'client_action_id':key, 'display_token':image.headers['X-HSC-Visual-Display-Token'],
        'sha256':case.region['safe_png_sha256'], 'width':case.prepared.width, 'height':case.prepared.height})
    assert r.status_code == 200, r.text
    return r.json()['display_receipt_id']


def assessment(case, monkeypatch, *, key='original'):
    from home_study_agent.diagnostic_prompt import DiagnosticPromptService
    from home_study_agent.repository import StudyRepository
    from home_study_agent.writing_response_origin import WritingOriginalService
    import test_pet_media_evidence as rubric_fixture
    sid = StudyRepository(case.db).start_session('child', 'answer-session')['session_id']
    prompts = DiagnosticPromptService(case.db)
    p = prompts.prepare(sid, track='school', preferred_item_id=case.item_id, client_action_id=key+'-prompt')['prompt']
    text = 'I observe a closed outline. The visible marks alone do not establish the material or its physical properties.'
    original = WritingOriginalService(case.db).capture_native_response(sid, prompt_id=p['prompt_id'],
        item_version=p['item_version'], text=text, client_action_id=key+'-writing')
    scored = prompts.answer(sid, prompt_id=p['prompt_id'], item_version=p['item_version'], answer=text,
        client_action_id='answer-'+key, writing_original_artifact_id=original['artifact_id'])
    assert scored['attempt']['correctness'] is None
    monkeypatch.setattr(rubric_fixture, 'CHILD', 'child')
    # Existing local evaluation helper creates a synthetic planned evaluation,
    # then uses the real evaluator; it does not insert an assessment or a score.
    value = rubric_fixture.local_review((case.db,None,sid,p,None,None),p,text,approve=False,scored=scored)
    return RubricEvidenceService(case.db), value


def test_published_same_text_different_pixels_have_different_public_exposure_identity(tmp_path, monkeypatch):
    identities = []
    for diagonal in (False, True):
        with published(tmp_path/str(diagonal), monkeypatch, diagonal=diagonal) as case:
            with case.db.connect() as c:
                item = validate_item(c, 'child', case.item_id)
                _, index = _question_catalog(c)
                identities.append(index[case.item_id])
                assert item['stimulus_asset'] is None
    assert identities[0] != identities[1]


def test_real_rubric_get_stops_claiming_current_after_original_image_retracted(tmp_path, monkeypatch):
    with published(tmp_path, monkeypatch) as case:
        service, snap = assessment(case, monkeypatch)
        url = '/v1/parent/children/child/learning-rubrics/'+snap['assessment_id']
        before = case.client.get(url, headers=case.headers)
        assert before.status_code == 200, before.text
        assert before.json()['assessment']['source_status'] == 'current'
        r = case.client.post(REGION+'/retract', headers=case.headers,
            json={**case.args,'client_action_id':'withdraw-original-image'})
        assert r.status_code == 200, r.text
        after = case.client.get(url, headers=case.headers)
        assert after.status_code == 200, after.text
        assert after.json()['assessment']['source_status'] == 'changed'
        assert not service.qualification('child', snap['assessment_id'])['eligible_for_mastery_gate']


def review_body(case, snap, display_id):
    context = RubricEvidenceService(case.db).writing_context('child', snap['assessment_id'])
    return dict(expected_revision=0, client_action_id='review-answer', operation='approve',
        reason='Synthetic parent review of the original answer and displayed original figure.',
        independent=True, hint_level=0, review_profile=context['profile'],
        dimensions=[dict(dimension_id=d['dimension_id'], score=0, response_quotes=[snap['response']],
            rationale='The response does not justify the requested conclusion.') for d in snap['dimensions']],
        criterion_evidence=[dict(criterion=point, response_quote=snap['response']) for point in snap['criteria']],
        writing_evidence=dict(binding_version=context['binding']['binding_version'],
            task_point_evidence=[dict(task_point=point, response_quote=snap['response'])
                                for point in context['binding']['task_points']]),
        visual_evidence=dict(assessment_id=snap['assessment_id'], assessment_fingerprint=snap['fingerprint'],
            binding_version=snap['material']['visual']['binding_version'],
            displays=[dict(material_key=snap['material']['visual']['materials'][0]['material_key'],
                           display_receipt_id=display_id)]))


@pytest.mark.parametrize('fault', ['missing', 'wrong_assessment', 'wrong_fingerprint', 'old_display', 'wrong_binding'])
def test_visual_approval_requires_original_assessment_bound_display(tmp_path, monkeypatch, fault):
    with published(tmp_path, monkeypatch) as case:
        _, snap = assessment(case, monkeypatch)
        body = review_body(case, snap, display_image(case, 'answer-display'))
        if fault == 'missing':
            body.pop('visual_evidence')
        elif fault == 'old_display':
            with case.db.connect() as c:
                row = c.execute("SELECT audit_id FROM audit_event WHERE json_extract(summary_json,'$.client_action_id')='publication-display'").fetchone()
            body['visual_evidence']['displays'][0]['display_receipt_id'] = row[0]
        elif fault == 'wrong_assessment':
            body['visual_evidence']['assessment_id'] = 'another-answer'
        elif fault == 'wrong_fingerprint':
            body['visual_evidence']['assessment_fingerprint'] = '0'*64
        else:
            body['visual_evidence']['binding_version'] = '0'*64
        result = case.client.post('/v1/parent/children/child/learning-rubrics/'+snap['assessment_id']+'/review',
            headers=case.headers, json=body)
        assert result.status_code == 409, result.text
        assert result.json()['detail']['code'].startswith('RUBRIC_VISUAL_')
        with case.db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM rubric_review_event').fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1


def test_visual_review_low_score_exact_replay_revoke_restart_and_retract(tmp_path, monkeypatch):
    with published(tmp_path, monkeypatch) as case:
        service, snap = assessment(case, monkeypatch)
        body = review_body(case, snap, display_image(case, 'answer-display'))
        url = '/v1/parent/children/child/learning-rubrics/'+snap['assessment_id']+'/review'
        result = case.client.post(url, headers=case.headers, json=body)
        assert result.status_code == 200, result.text
        event = result.json()['event']
        assert event['visual_evidence'] == body['visual_evidence']
        assert not event['passed']
        with case.db.connect() as c:
            receipt = service.reviewed_receipt(c, 'child', snap['step_id'], now=datetime.now(timezone.utc))
            assert receipt is not None and receipt['correctness'] == 0
            assert receipt['question_identity'].startswith('exact_visual_question_v1:')
        r = case.client.post(REGION+'/retract', headers=case.headers,
            json={**case.args,'client_action_id':'withdraw-image-after-review'})
        assert r.status_code == 200, r.text
        # A real service reopen reads only the original review receipt, never reapproves.
        reopened = RubricEvidenceService(case.db)
        replay = reopened.review('child', snap['assessment_id'], reviewer_id='household-parent',
            reviewer_role='parent', **body)
        assert replay == event
        with case.db.connect() as c:
            assert reopened.reviewed_receipt(c, 'child', snap['step_id'], now=datetime.now(timezone.utc)) is None
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM rubric_assessment').fetchone()[0] == 1
        withdrawn = case.client.post(url, headers=case.headers, json=dict(expected_revision=1,
            client_action_id='withdraw-review', operation='retract', reason='Original image withdrawn.'))
        assert withdrawn.status_code == 200, withdrawn.text
        assert withdrawn.json()['event']['score_status'] == 'withdrawn'


def test_same_visible_pixels_in_separate_publications_are_not_new_questions(tmp_path, monkeypatch):
    identities = []
    for n in range(2):
        with published(tmp_path/str(n), monkeypatch) as case:
            with case.db.connect() as c:
                item = validate_item(c, 'child', case.item_id)
                identities.append(question_identity(item, connection=c))
    assert identities[0] == identities[1]


def test_reconfirm_same_figure_does_not_make_historical_question_distinct(tmp_path, monkeypatch):
    with published(tmp_path, monkeypatch) as case:
        with case.db.connect() as c:
            original = _question_catalog(c)[1][case.item_id]
        second = case.client.post(REGION+'/confirm', headers=case.headers, json={**case.args,
            'client_action_id':'confirm-same-pixels-again',
            'display_receipt_id':display_image(case, 'reconfirm-display')})
        assert second.status_code == 200, second.text
        assert second.json()['detail']['binding']['confirmation_id'] != case.binding['confirmation_id']
        with case.db.connect() as c:
            assert _question_catalog(c)[1][case.item_id] == original
            with pytest.raises(ValueError):
                validate_item(c, 'child', case.item_id)


@pytest.mark.parametrize('fault', ['missing_image','bad_proof','source_version','legacy_snapshot'])
def test_visual_source_failure_is_explicit_not_text_only_rubric(tmp_path, monkeypatch, fault):
    with published(tmp_path, monkeypatch) as case:
        if fault == 'legacy_snapshot':
            original_material = RubricEvidenceService._material
            def pre_visual_material(*args, **kwargs):
                value = original_material(*args, **kwargs)
                value.pop('visual', None)
                return value
            # Simulate the old producer before immutable INSERT, not by rewriting history.
            with monkeypatch.context() as patch:
                patch.setattr(RubricEvidenceService, '_material', staticmethod(pre_visual_material))
                _, snap = assessment(case, patch)
        else:
            _, snap = assessment(case, monkeypatch)
        with case.db.connect() as c:
            if fault == 'bad_proof':
                row = c.execute('SELECT source_snapshot_json FROM curriculum_supply_package WHERE candidate_id=?',
                                (case.candidate_id,)).fetchone()
                proof = json.loads(row[0])
                proof['open_response_materials']['materials'][0]['visual']['region']['safe_png_sha256'] = '0'*64
                c.execute('UPDATE curriculum_supply_package SET source_snapshot_json=? WHERE candidate_id=?',
                          (json.dumps(proof),case.candidate_id))
                assert case.item_id not in _question_catalog(c)[1]
            elif fault == 'source_version':
                c.execute("UPDATE knowledge_source SET version_label='revised' WHERE source_id=?",
                    (case.source['material_bindings'][0]['source_id'],))
            c.commit()
        if fault == 'missing_image':
            paths = list((case.settings.data_dir/'media').rglob('*.enc'))
            assert paths
            for path in paths:
                path.unlink()
        result = case.client.get('/v1/parent/children/child/learning-rubrics/'+snap['assessment_id'],headers=case.headers)
        assert result.status_code == 200, result.text
        current = result.json()['assessment']
        assert current['source_status'] == 'changed'
        assert current['material']['visual']['status'] == 'unavailable'
        assert not current['material']['visual']['source_current']
        assert current['material']['visual']['image_availability'] == 'unavailable'


def test_borrowed_visual_identity_propagates_database_error(tmp_path, monkeypatch):
    import sqlite3
    from home_study_agent.visual_question_evidence import current_visual
    with published(tmp_path, monkeypatch) as case:
        with case.db.connect() as c:
            item = validate_item(c, 'child', case.item_id)
            with pytest.raises(ValueError, match='CONNECTION_REQUIRED'):
                question_identity(item)
            class BrokenConnection:
                def execute(self, *args):
                    raise sqlite3.OperationalError('isolated SQL failure')
            with pytest.raises(sqlite3.OperationalError, match='isolated SQL failure'):
                current_visual(BrokenConnection(), 'child', item)


def test_corrupted_frozen_caption_cannot_mint_new_identity_by_rehashing_proof(tmp_path, monkeypatch):
    from home_study_agent.local_supply import digest
    with published(tmp_path, monkeypatch) as case:
        with case.db.connect() as c:
            assert case.item_id in _question_catalog(c)[1]
            row = c.execute('SELECT source_snapshot_json FROM curriculum_supply_package WHERE candidate_id=?',
                            (case.candidate_id,)).fetchone()
            proof = json.loads(row[0])
            saved = proof['open_response_materials']
            material = saved['materials'][0]
            material['text'] += ' A corrupted extra geometric condition.'
            saved['stimulus_text'] = '['+material['material_key']+']\n'+material['text']
            saved['binding_version'] = digest(dict(version='open_response_visual_materials_v3', materials=saved['materials']))
            c.execute('UPDATE curriculum_supply_package SET source_snapshot_json=? WHERE candidate_id=?',
                      (json.dumps(proof),case.candidate_id))
            c.commit()
            # This tests the historical catalog, not the current source gate.
            assert case.item_id not in _question_catalog(c)[1]


def test_missing_display_time_is_controlled_unavailable_not_attribute_error(tmp_path, monkeypatch):
    from home_study_agent.visual_question_evidence import validate_display_evidence
    with published(tmp_path, monkeypatch) as case:
        _, snap = assessment(case, monkeypatch)
        display = display_image(case, 'answer-display')
        body = review_body(case, snap, display)
        with case.db.connect() as c:
            class MissingHistoricalTime:
                def execute(self, sql, args=()):
                    result = c.execute(sql, args)
                    if sql.startswith('SELECT * FROM audit_event WHERE audit_id=') and args[0] == display:
                        row = dict(result.fetchone())
                        row['occurred_at'] = None
                        return SimpleNamespace(fetchone=lambda: row)
                    return result
                def __getattr__(self, name):
                    return getattr(c, name)
            with pytest.raises(ValueError, match='RUBRIC_VISUAL_DISPLAY_CHANGED'):
                validate_display_evidence(MissingHistoricalTime(), 'child', snap, body['visual_evidence'])
