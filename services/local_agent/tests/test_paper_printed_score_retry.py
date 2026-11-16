from normal_provider_owner import normal_provider_owner
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest

from home_study_agent.database import Database
from home_study_agent.paper_printed_score_execution import PaperPrintedScoreExecutionService, WORK_TYPE
from test_paper_printed_score_execution import Gateway, setup


class Malformed(Gateway):
    def __init__(self, failures=1):
        super().__init__()
        self.failures = failures

    async def complete(self, request):
        result = await super().complete(request)
        if len(self.requests) <= self.failures:
            return replace(result, content='{"points": []}')
        return result


def retry_setup(tmp_path, failures=1):
    db, request, aid, gateway, _ = setup(tmp_path, Malformed(failures))
    now = [datetime.now(timezone.utc)]
    service = PaperPrintedScoreExecutionService(db, gateway, cloud_text_allowed=lambda: True)
    service.clock = lambda: now[0]
    return db, request, aid, gateway, service, now


def rows(db):
    with db.connect() as c:
        return [dict(r) for r in c.execute('SELECT * FROM capture_artifact WHERE artifact_type=? ORDER BY rowid', (WORK_TYPE,))]


def test_known_rejected_receipt_recovers_after_cooldown_without_rewriting_failed_attempt(tmp_path):
    db, _, _, gateway, service, now = retry_setup(tmp_path)
    first = asyncio.run(service.process_next())
    assert first['status'] == 'blocked'
    original = rows(db)[0]
    assert asyncio.run(service.process_next()) is None
    now[0] += timedelta(seconds=121)
    second = asyncio.run(service.process_next())
    assert second is not None, 'a known received failure remained permanently blocked'
    assert second['status'] == 'completed' and second['attempt_number'] == 2
    assert len(gateway.requests) == 2 and len(rows(db)) == 2
    assert rows(db)[0] == original
    with db.connect() as c:
        calls = [dict(r) for r in c.execute("SELECT * FROM model_call WHERE purpose='paper_printed_score_candidate' ORDER BY rowid")]
    assert len(calls) == 2
    assert calls[0]['verification_status'] == 'rejected' and calls[1]['safety_status'] == 'passed'
    assert all(call['prompt_tokens'] == 50 and call['completion_tokens'] == 30 for call in calls)
    assert asyncio.run(service.process_next()) is None


def test_known_failures_are_bounded_across_restarts(tmp_path):
    db, _, _, gateway, service, now = retry_setup(tmp_path, failures=20)
    for number, seconds in [(1, 0), (2, 121), (3, 601)]:
        now[0] += timedelta(seconds=seconds)
        restarted = PaperPrintedScoreExecutionService(Database(db.path, runtime_mode=db.runtime_mode), gateway,
            cloud_text_allowed=lambda: True)
        restarted.clock = lambda: now[0]
        result = asyncio.run(restarted.process_next())
        assert result is not None, 'a known received failure was not resumed'
        assert result['status'] == 'blocked' and result.get('attempt_number', 1) == number
    now[0] += timedelta(days=1)
    assert asyncio.run(restarted.process_next()) is None
    assert len(gateway.requests) == 3 and len(rows(db)) == 3


@pytest.mark.parametrize('mutation', ['permission', 'original', 'audit', 'receipt', 'manifest', 'parent_deleted'])
def test_retry_never_bypasses_current_permission_source_or_receipt_integrity(tmp_path, mutation):
    db, _, _, gateway, service, now = retry_setup(tmp_path)
    asyncio.run(service.process_next())
    row = rows(db)[0]
    details = json.loads(row['details_json'])
    with db.connect() as c:
        if mutation == 'permission':
            c.execute('UPDATE privacy_preference SET cloud_model_text_allowed=0')
        elif mutation == 'original':
            c.execute("UPDATE capture_artifact SET extracted_text='changed page' WHERE artifact_id='page'")
        elif mutation == 'audit':
            c.execute("UPDATE model_call SET safety_status='passed' WHERE call_id=?", (details['call_id'],))
        elif mutation == 'receipt':
            details['receipt']['content'] = 'changed'
            c.execute('UPDATE capture_artifact SET details_json=? WHERE artifact_id=?', (json.dumps(details), row['artifact_id']))
        elif mutation == 'manifest':
            c.execute("UPDATE model_call SET context_manifest_json='[]' WHERE call_id=?", (details['call_id'],))
        else:
            # A successor must not recreate a deleted parent receipt.
            now[0] += timedelta(seconds=121)
            claimed = service._claim()
            assert claimed is not None, 'the eligible successor was never created'
            job_id, owner, _ = claimed
            c.execute('DELETE FROM capture_artifact WHERE artifact_id=?', (row['artifact_id'],))
            c.commit()
            with pytest.raises(ValueError):
                service._prepare(job_id, owner)
            return
        c.commit()
    now[0] += timedelta(seconds=121)
    assert asyncio.run(service.process_next()) is None
    assert len(gateway.requests) == 1


def test_unknown_remote_failure_never_gets_a_successor_even_after_a_long_wait(tmp_path):
    db, _, _, gateway, _ = setup(tmp_path, Gateway(error=TimeoutError()))
    now = [datetime.now(timezone.utc)]
    service = PaperPrintedScoreExecutionService(db, gateway, cloud_text_allowed=lambda: True)
    service.clock = lambda: now[0]
    assert asyncio.run(service.process_next())['status'] == 'blocked'
    now[0] += timedelta(days=1)
    assert asyncio.run(service.process_next()) is None
    assert len(gateway.requests) == 1 and len(rows(db)) == 1


def test_parent_deleted_after_dispatch_cannot_accept_a_successor_result(tmp_path):
    db, _, _, gateway, service, now = retry_setup(tmp_path)
    first = asyncio.run(service.process_next())
    now[0] += timedelta(seconds=121)
    def delete(where, request):
        if where == 'after':
            with db.connect() as c:
                c.execute('DELETE FROM capture_artifact WHERE artifact_id=?', (first['execution_id'],)); c.commit()
    gateway.hook = delete
    second = asyncio.run(service.process_next())
    assert second['status'] == 'blocked' and second['error_code'] == 'PRINTED_RETRY_PARENT_CHANGED'
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM capture_artifact WHERE artifact_type='paper_printed_score_candidate'").fetchone()[0] == 0
    assert len(gateway.requests) == 2


def test_two_maintenance_workers_cannot_create_or_charge_two_successors(tmp_path):
    db, _, _, gateway, service, now = retry_setup(tmp_path)
    asyncio.run(service.process_next())
    now[0] += timedelta(seconds=121)
    second = PaperPrintedScoreExecutionService(db, gateway, cloud_text_allowed=lambda: True, clock=lambda: now[0])
    complete = gateway.complete
    async def delay(request):
        await asyncio.sleep(.01)
        return await complete(request)
    gateway.complete = delay
    async def run():
        return await asyncio.gather(service.process_next(), second.process_next())
    results = asyncio.run(run())
    assert sum(result is not None for result in results) == 1
    assert next(result for result in results if result)['status'] == 'completed'
    assert len(gateway.requests) == 2 and len(rows(db)) == 2


def test_reenable_consent_does_not_reauthorize_an_old_failed_attempt(tmp_path):
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    db, _, _, gateway, service, now = retry_setup(tmp_path)
    asyncio.run(service.process_next())
    privacy = PrivacyLifecycleService(db, tmp_path)
    privacy.update_preferences({'cloud_model_text_allowed': False})
    privacy.update_preferences({'cloud_model_text_allowed': True})
    now[0] += timedelta(seconds=121)
    assert asyncio.run(service.process_next()) is None and len(gateway.requests) == 1


@pytest.mark.parametrize('unknown', [False, True])
def test_deleting_failed_execution_does_not_reset_charge_history(tmp_path, unknown):
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    if unknown:
        db, _, _, gateway, service = setup(tmp_path, Gateway(error=TimeoutError()))
    else:
        db, _, _, gateway, service, _ = retry_setup(tmp_path)
    failed = asyncio.run(service.process_next())
    privacy = PrivacyLifecycleService(db, tmp_path, b'0' * 32)
    privacy.delete_capture_artifact(failed['execution_id'], '确认删除', 'delete-failed-execution')
    service.clock = lambda: datetime.now(timezone.utc) + timedelta(days=1)
    assert asyncio.run(service.process_next()) is None
    assert len(gateway.requests) == 1


def test_received_successor_recovers_after_crash_without_a_third_call(tmp_path, monkeypatch):
    db, _, _, gateway, service, now = retry_setup(tmp_path)
    asyncio.run(service.process_next())
    now[0] += timedelta(seconds=121)
    monkeypatch.setattr(service, '_accept_ready', lambda *args: (_ for _ in ()).throw(SystemExit()))
    with pytest.raises(SystemExit):
        asyncio.run(service.process_next())
    restarted = PaperPrintedScoreExecutionService(Database(db.path, runtime_mode=db.runtime_mode), gateway,
        cloud_text_allowed=lambda: True, clock=lambda: now[0])
    assert restarted.recover() == 1
    assert asyncio.run(restarted.process_next())['status'] == 'completed'
    assert len(gateway.requests) == 2


def test_real_http_gateway_retries_only_known_received_invalid_output(tmp_path, normal_provider_owner):
    import httpx
    from home_study_agent.config import Settings
    from home_study_agent.model_gateway import ModelGateway
    posts = []
    def transport(request):
        posts.append(request)
        body = json.loads(json.loads(request.read())['messages'][1]['content'])
        assert set(body) == {'question', 'student_answer', 'criteria'}
        points = [] if len(posts) == 1 else [dict(point_id=p['point_id'], status='met',
            score=p['maximum_score'], response_quote=body['student_answer'], reason='原答依据') for p in body['criteria']]
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps({'points': points})}}],
            'usage': {'prompt_tokens': 21, 'completion_tokens': 17}}, request=request)
    gateway = ModelGateway(Settings(data_dir=tmp_path, database_path=tmp_path/'unused.db',
        model_provider='deepseek', model_name='fixture-model', model_api_key='synthetic',
        model_base_url='https://primary.invalid'), transport=httpx.MockTransport(transport))
    db, _, _, _, service = setup(tmp_path, gateway)
    now = [datetime.now(timezone.utc)]
    service.clock = lambda: now[0]
    async def run():
        try:
            assert (await service.process_next())['status'] == 'blocked'
            now[0] += timedelta(seconds=121)
            assert (await service.process_next())['status'] == 'completed'
        finally:
            await gateway.close()
    asyncio.run(run())
    assert len(posts) == 2 and len(rows(db)) == 2
    with db.connect() as c:
        calls = c.execute("SELECT prompt_tokens,completion_tokens FROM model_call WHERE purpose='paper_printed_score_candidate'").fetchall()
        assert [tuple(row) for row in calls] == [(21, 17), (21, 17)]


def test_application_maintenance_recovers_a_known_failure_without_a_child_button(tmp_path):
    import time
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from test_school_review_start_integrity_http import _http_client
    db, _, aid, gateway, service, now = retry_setup(tmp_path)
    first = asyncio.run(service.process_next())
    now[0] += timedelta(seconds=121)
    app = create_app(Settings(data_dir=tmp_path, database_path=db.path, runtime_mode='trial', maintenance_enabled=True), model_gateway=gateway)
    app.state.paper_printed_score_execution.clock = lambda: now[0]
    with _http_client(app):
        end = time.monotonic() + 8
        while time.monotonic() < end:
            with db.connect() as c:
                view = PaperPrintedScoreExecutionService.for_rubric(c, 'child', aid)
            if view and view['status'] == 'completed':
                break
            time.sleep(.05)
        assert view['status'] == 'completed' and view['attempt_number'] == 2
        assert view['parent_execution_id'] == first['execution_id']
    assert len(gateway.requests) == 2
