from normal_provider_owner import normal_provider_owner
import asyncio
import json

import pytest

from home_study_agent.database import Database
from home_study_agent.model_gateway import ModelResult, requires_json_object
from home_study_agent.paper_printed_score_execution import PaperPrintedScoreExecutionService, WORK_TYPE
from home_study_agent.paper_scoring import PaperScoringService
from home_study_agent.repository import StudyRepository
from test_paper_response_origin import prepared_visual


class Gateway:
    configured = True

    def __init__(self, hook=None, error=None):
        self.requests = []
        self.hook = hook
        self.error = error

    async def complete(self, request):
        self.requests.append(request)
        if self.hook:
            self.hook('before', request)
        request.pre_dispatch()
        request.pre_dispatch.authorize_physical({'provider': 'fixture', 'requested_model': 'fixture-model'})
        if self.error:
            raise self.error
        body = json.loads(request.messages[1]['content'])
        result = ModelResult(content=json.dumps({'points': [dict(point_id=p['point_id'], status='met',
            score=p['maximum_score'], response_quote=body['student_answer'], reason='作答逐字依据')
            for p in body['criteria']]}, ensure_ascii=False), provider='fixture', model='fixture-model',
            latency_ms=10, prompt_tokens=50, completion_tokens=30)
        if self.hook:
            self.hook('after', request)
        return result


def setup(tmp_path, gateway=None, permission=lambda: True):
    db, request, aid = prepared_visual(tmp_path, unmarked=True)
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    privacy = PrivacyLifecycleService(db, tmp_path)
    privacy.initialize()
    privacy.update_preferences({'cloud_model_text_allowed': True})
    gateway = gateway or Gateway()
    return db, request, aid, gateway, PaperPrintedScoreExecutionService(db, gateway, StudyRepository(db), permission)


def work(db):
    with db.connect() as c:
        return dict(c.execute('SELECT * FROM capture_artifact WHERE artifact_type=?', (WORK_TYPE,)).fetchone())


def test_automatic_exact_scale_candidate_is_durable_without_becoming_a_grade(tmp_path):
    db, request, aid, gateway, service = setup(tmp_path)
    result = asyncio.run(service.process_next())
    assert result['status'] == 'completed' and result['candidate_artifact_id']
    assert asyncio.run(service.process_next()) is None and len(gateway.requests) == 1
    view = PaperScoringService(Database(db.path, runtime_mode=db.runtime_mode)).for_candidate(request['candidate_id'], 'child')
    candidate = view['authority_supply']['printed_score_candidate']
    assert candidate['total_score'] == '3.5' and candidate['points'][0]['score'] == '2.5'
    assert candidate['model_call']['output_sha256'] and candidate['model_call']['safety_status'] == 'passed'
    assert view['decision_id'] is None and candidate['score_authority_granted'] is False
    assert view['authority_supply']['printed_score_execution']['status'] == 'completed'
    outbound = json.loads(gateway.requests[0].messages[1]['content'])
    assert set(outbound) == {'question', 'student_answer', 'criteria'}
    assert set(outbound['criteria'][0]) == {'point_id', 'criterion', 'maximum_score'}
    assert outbound['criteria'][0]['point_id'] == '1'
    assert aid not in json.dumps(outbound) and 'original_response' not in json.dumps(outbound)
    assert requires_json_object(gateway.requests[0].request_type)
    calls = StudyRepository(db).list_model_calls('child')
    assert calls[0]['prompt_tokens'] == 50 and calls[0]['completion_tokens'] == 30


@pytest.mark.parametrize('stage', ['before', 'after'])
@pytest.mark.parametrize('mutation', ['source', 'permission', 'owner'])
def test_source_permission_and_ownership_change_never_accepts_late_result(tmp_path, stage, mutation):
    allowed = [True]
    def hook(where, request):
        if where != stage:
            return
        with db.connect() as c:
            if mutation == 'source':
                c.execute("UPDATE capture_artifact SET extracted_text='source changed' WHERE artifact_id='page'")
            elif mutation == 'owner':
                row = work(db)
                details = json.loads(row['details_json']); details['owner_token'] = 'new-owner'
                c.execute('UPDATE capture_artifact SET details_json=? WHERE artifact_id=?', (json.dumps(details), row['artifact_id']))
            else:
                allowed[0] = False
            c.commit()
    db, _, _, gateway, service = setup(tmp_path, Gateway(hook), lambda: allowed[0])
    asyncio.run(service.process_next())
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM capture_artifact WHERE artifact_type='paper_printed_score_candidate'").fetchone()[0] == 0
    if mutation != 'owner':
        assert work(db)['status'] == 'blocked'


def test_restart_recovers_received_result_without_another_paid_request(tmp_path, monkeypatch):
    db, request, _, gateway, service = setup(tmp_path)
    def crash(*args, **kwargs):
        raise SystemExit('crash after durable response receipt')
    monkeypatch.setattr(service, '_accept_ready', crash)
    with pytest.raises(SystemExit):
        asyncio.run(service.process_next())
    assert work(db)['status'] == 'response_ready'
    StudyRepository(db).recover_interrupted_model_calls()
    restarted = PaperPrintedScoreExecutionService(Database(db.path, runtime_mode=db.runtime_mode), gateway,
        StudyRepository(db), lambda: True)
    restarted.recover()
    assert asyncio.run(restarted.process_next())['status'] == 'completed'
    assert len(gateway.requests) == 1
    assert PaperScoringService(db).for_candidate(request['candidate_id'], 'child')['authority_supply']['printed_score_candidate']


def test_unknown_remote_outcome_is_not_silently_billed_again(tmp_path):
    db, _, _, gateway, service = setup(tmp_path, Gateway(error=SystemExit('lost process after dispatch')))
    with pytest.raises(SystemExit):
        asyncio.run(service.process_next())
    service.recover()
    assert work(db)['status'] == 'blocked'
    assert json.loads(work(db)['details_json'])['error_code'] == 'PRINTED_REMOTE_OUTCOME_UNKNOWN'
    assert asyncio.run(service.process_next()) is None and len(gateway.requests) == 1


def test_pre_dispatch_restart_can_resume_and_two_workers_cannot_send_twice(tmp_path):
    db, _, _, gateway, service = setup(tmp_path)
    assert service._claim()
    service.recover()
    results = asyncio.run(service.process_batch(2))
    assert len(results) == 1 and results[0]['status'] == 'completed' and len(gateway.requests) == 1


def test_consent_revoke_reenable_is_not_bypassed(tmp_path):
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    def hook(stage, request):
        if stage == 'after':
            privacy = PrivacyLifecycleService(db, tmp_path)
            privacy.update_preferences({'cloud_model_text_allowed': False})
            privacy.update_preferences({'cloud_model_text_allowed': True})
    db, _, _, _, service = setup(tmp_path, Gateway(hook))
    asyncio.run(service.process_next())
    assert work(db)['status'] == 'blocked'
    assert json.loads(work(db)['details_json'])['error_code'] == 'PRINTED_PERMISSION_CHANGED'


@pytest.mark.parametrize('metadata', [dict(cloud_text_allowed=False), dict(local_only=True),
    dict(cloud_transmission_allowed=False), dict(nested=[dict(local_only=True)])])
def test_explicit_source_text_denial_never_sends(tmp_path, metadata):
    db, _, _, gateway, service = setup(tmp_path)
    with db.connect() as c:
        c.execute("UPDATE kg_resource SET metadata_json=?", (json.dumps(metadata),)); c.commit()
    assert asyncio.run(service.process_next())['status'] == 'blocked'
    assert json.loads(work(db)['details_json'])['error_code'] == 'PRINTED_SOURCE_TEXT_DENIED'
    assert not gateway.requests


def test_sensitive_text_never_leaves_and_redaction_is_not_misquoted(tmp_path):
    db, _, _, gateway, service = setup(tmp_path)
    with db.connect() as c:
        c.execute("UPDATE child_profile SET nickname='80' WHERE child_id='child'"); c.commit()
    assert asyncio.run(service.process_next())['status'] == 'blocked'
    assert not gateway.requests
    assert json.loads(work(db)['details_json'])['error_code'] == 'PRINTED_LOCAL_TEXT_REVIEW_REQUIRED'


def test_receipt_corruption_and_changed_original_after_restart_are_blocked(tmp_path, monkeypatch):
    db, _, _, gateway, service = setup(tmp_path)
    monkeypatch.setattr(service, '_accept_ready', lambda *args: (_ for _ in ()).throw(SystemExit()))
    with pytest.raises(SystemExit):
        asyncio.run(service.process_next())
    row = work(db)
    details = json.loads(row['details_json']); details['receipt']['content'] = '{"points":[]}'
    with db.connect() as c:
        c.execute('UPDATE capture_artifact SET details_json=? WHERE artifact_id=?', (json.dumps(details), row['artifact_id'])); c.commit()
    service = PaperPrintedScoreExecutionService(db, gateway, StudyRepository(db), lambda: True)
    service.recover()
    assert asyncio.run(service.process_next())['status'] == 'blocked'
    assert len(gateway.requests) == 1


def test_gateway_retry_must_not_issue_a_second_physical_request(tmp_path):
    class Retry(Gateway):
        async def complete(self, request):
            self.requests.append(request)
            request.pre_dispatch()
            request.pre_dispatch.authorize_physical({'provider': 'fixture', 'requested_model': 'fixture-model'})
            request.pre_dispatch()
            request.pre_dispatch.authorize_physical({'provider': 'fixture', 'requested_model': 'fixture-model'})
            raise AssertionError('second physical request was allowed')
    db, _, _, gateway, service = setup(tmp_path, Retry())
    assert asyncio.run(service.process_next())['status'] == 'blocked'
    assert json.loads(work(db)['details_json'])['error_code'] == 'PRINTED_DISPATCH_ALREADY_SENT'
    assert len(gateway.requests) == 1


@pytest.mark.parametrize('failure', [None, 503])
def test_real_gateway_projection_and_physical_retry_gate(tmp_path, failure, normal_provider_owner):
    import httpx
    from home_study_agent.config import Settings
    from home_study_agent.model_gateway import ModelGateway
    posts = []
    def transport(request):
        posts.append(request)
        payload = json.loads(request.read())
        assert payload['response_format'] == {'type': 'json_object'}
        assert not {'context_manifest', 'child_id', 'session_id', 'evidence_refs', 'pre_dispatch'} & set(payload)
        body = json.loads(payload['messages'][1]['content'])
        if failure:
            return httpx.Response(failure, request=request)
        content = json.dumps({'points': [dict(point_id=p['point_id'], status='met', score=p['maximum_score'],
            response_quote=body['student_answer'], reason='原答依据') for p in body['criteria']]})
        return httpx.Response(200, json={'choices': [{'message': {'content': content}}],
            'usage': {'prompt_tokens': 21, 'completion_tokens': 17}}, request=request)
    gateway = ModelGateway(Settings(data_dir=tmp_path, database_path=tmp_path/'gateway-unused.db',
        model_provider='deepseek', model_name='fixture-model', model_api_key='synthetic',
        model_base_url='https://primary.invalid', fallback_model_provider='qwen', fallback_model_name='backup',
        fallback_model_api_key='synthetic-backup', fallback_model_base_url='https://backup.invalid'),
        transport=httpx.MockTransport(transport))
    db, _, _, _, service = setup(tmp_path, gateway)
    async def run():
        try:
            return await service.process_next()
        finally:
            await gateway.close()
    outcome = asyncio.run(run())
    assert outcome['status'] == ('blocked' if failure else 'completed')
    assert len(posts) == 1


def test_actual_application_startup_recovers_receipt_and_http_projects_candidate(tmp_path, monkeypatch):
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from test_school_review_start_integrity_http import _http_client
    db, request, _, gateway, service = setup(tmp_path)
    monkeypatch.setattr(service, '_accept_ready', lambda *args: (_ for _ in ()).throw(SystemExit()))
    with pytest.raises(SystemExit):
        asyncio.run(service.process_next())
    app = create_app(Settings(data_dir=tmp_path, database_path=db.path, runtime_mode='trial', maintenance_enabled=False),
                     model_gateway=gateway)
    with _http_client(app) as client:
        outcome = asyncio.run(app.state.paper_printed_score_execution.process_next())
        assert outcome['status'] == 'completed'
        response = client.get('/v1/capture-artifacts/page/mistake-candidates', params={'child_id': 'child'})
        assert response.status_code == 200
        view = PaperScoringService(app.state.paper_printed_score_execution.database).for_candidate(request['candidate_id'], 'child')
        assert view['authority_supply']['printed_score_execution']['status'] == 'completed'
        assert view['authority_supply']['printed_score_candidate']['total_score'] == '3.5'
        assert view['decision_id'] is None
    assert len(gateway.requests) == 1


def test_execution_receipt_export_delete_are_durable(tmp_path):
    from pathlib import Path
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    db, _, _, gateway, service = setup(tmp_path)
    result = asyncio.run(service.process_next())
    privacy = PrivacyLifecycleService(db, tmp_path, b'0' * 32)
    exported = json.loads(Path(privacy.export_child('child')['path']).read_text('utf-8'))
    saved = next(row for row in exported['capture_artifacts'] if row['artifact_id'] == result['execution_id'])
    assert json.loads(saved['details_json'])['receipt_sha256']
    privacy.delete_capture_artifact(result['execution_id'], '确认删除', 'delete-execution-receipt')
    restarted = PaperPrintedScoreExecutionService(Database(db.path, runtime_mode=db.runtime_mode), gateway,
        StudyRepository(db), lambda: True)
    assert asyncio.run(restarted.process_next())['error_code'] == 'PRINTED_CANDIDATE_ALREADY_PRESENT'
    assert len(gateway.requests) == 1


def test_default_preference_is_not_parent_consent(tmp_path):
    db, _, _, gateway, service = setup(tmp_path)
    with db.connect() as c:
        c.execute("DELETE FROM consent_record WHERE scope='cloud_model_text_allowed'"); c.commit()
    result = asyncio.run(service.process_next())
    assert result['status'] == 'blocked' and result['error_code'] == 'PRINTED_PARENT_TEXT_CONSENT_REQUIRED'
    assert not gateway.requests


def test_concurrent_workers_share_one_real_execution(tmp_path):
    db, _, _, gateway, service = setup(tmp_path)
    second = PaperPrintedScoreExecutionService(db, gateway, StudyRepository(db), lambda: True)
    async def run():
        return await asyncio.gather(service.process_next(), second.process_next())
    results = asyncio.run(run())
    assert sum(result is not None for result in results) == 1
    assert len(gateway.requests) == 1 and work(db)['status'] == 'completed'


def test_corrupt_pending_work_is_quarantined_without_rewriting_its_bytes(tmp_path):
    db, _, _, gateway, service = setup(tmp_path)
    job_id, _, _ = service._claim()
    with db.connect() as c:
        c.execute("UPDATE capture_artifact SET details_json='broken',status='pending' WHERE artifact_id=?", (job_id,)); c.commit()
    assert asyncio.run(service.process_next())['error_code'] == 'PRINTED_EXECUTION_CORRUPT'
    assert asyncio.run(service.process_next()) is None
    with db.connect() as c:
        assert c.execute('SELECT details_json FROM capture_artifact WHERE artifact_id=?', (job_id,)).fetchone()[0] == 'broken'
    assert not gateway.requests


def test_deleted_original_after_receipt_restart_is_not_reaccepted(tmp_path, monkeypatch):
    db, _, _, gateway, service = setup(tmp_path)
    monkeypatch.setattr(service, '_accept_ready', lambda *args: (_ for _ in ()).throw(SystemExit()))
    with pytest.raises(SystemExit):
        asyncio.run(service.process_next())
    with db.connect() as c:
        c.execute("UPDATE capture_artifact SET extracted_text='new page' WHERE artifact_id='page'"); c.commit()
    restarted = PaperPrintedScoreExecutionService(db, gateway, StudyRepository(db), lambda: True)
    restarted.recover()
    result = asyncio.run(restarted.process_next())
    assert result['status'] == 'blocked' and result['error_code'].startswith('SUBJECTIVE_')
    assert len(gateway.requests) == 1
    with db.connect() as c:
        call = c.execute("SELECT * FROM model_call WHERE purpose='paper_printed_score_candidate'").fetchone()
        assert call['status'] == 'completed' and call['verification_status'] == 'rejected'
        assert call['prompt_tokens'] == 50 and call['completion_tokens'] == 30


def test_application_maintenance_produces_candidate_without_a_child_button(tmp_path):
    import time
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from test_school_review_start_integrity_http import _http_client
    db, request, _, gateway, _ = setup(tmp_path)
    app = create_app(Settings(data_dir=tmp_path, database_path=db.path, runtime_mode='trial',
        maintenance_enabled=True, maintenance_interval_seconds=1), model_gateway=gateway)
    with _http_client(app):
        deadline = time.monotonic() + 12
        result = None
        while time.monotonic() < deadline:
            view = PaperScoringService(db).for_candidate(request['candidate_id'], 'child')
            result = view['authority_supply']['printed_score_candidate']
            if result:
                break
            time.sleep(.1)
        assert result is not None and result['total_score'] == '3.5'
        assert result['score_authority_granted'] is False and view['decision_id'] is None
    printed = [r for r in gateway.requests if r.request_type == 'paper_printed_score_candidate']
    assert len(printed) == 1
