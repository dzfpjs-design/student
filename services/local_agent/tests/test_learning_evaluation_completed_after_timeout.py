# coding: utf-8
"""Synthetic transport; real timeout, original thread drain and durable receipts."""
import asyncio
from contextlib import contextmanager
import threading

import pytest

from home_study_agent import local_async_work
from home_study_agent.learning_outcome_evaluator import LearningOutcomeEvaluator, LearningEvaluationUnknownError
from test_learning_outcome_evaluator import EvaluationGateway, evaluation_fixture, persist_evaluation_turn
from test_agent_runtime import seed_session
from test_formative_evaluator import evaluation
from test_writing_question_evaluator import projection
from test_writing_question_authorization import published_question, grant, CHILD


@contextmanager
def original_store_after_cancellation(monkeypatch, after_store=None):
    original_store = LearningOutcomeEvaluator._store_response
    original_drain = local_async_work._drain
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    evidence = {'original_store_calls': 0, 'real_thread_drain_observed': 0}

    def controlled_store(self, *args, **kwargs):
        evidence['original_store_calls'] += 1
        entered.set()
        try:
            assert release.wait(25), 'original store was never cancelled and drained'
            result = original_store(self, *args, **kwargs)
            if after_store is not None:
                after_store(self)
            return result
        finally:
            finished.set()

    async def observe_original_drain(task):
        code = getattr(task.get_coro(), 'cr_code', None)
        if entered.is_set() and not release.is_set() and code is not None and code.co_name == 'to_thread':
            # Observe genuine run_local cancellation, then let its ORIGINAL
            # synchronous store finish. No result, clock or wait_for is mocked.
            evidence['real_thread_drain_observed'] += 1
            release.set()
        return await original_drain(task)

    monkeypatch.setattr(LearningOutcomeEvaluator, '_store_response', controlled_store)
    monkeypatch.setattr(local_async_work, '_drain', observe_original_drain)
    try:
        yield entered, finished, evidence
    finally:
        release.set()
        if entered.is_set():
            assert finished.wait(5), 'original local store did not finish'


def saved_summary(database):
    with database.connect() as c:
        execution = c.execute('SELECT state, evaluation_json IS NOT NULL, result_json IS NOT NULL FROM learning_evaluation_execution').fetchone()
        calls = c.execute("SELECT status FROM model_call WHERE purpose='learning_outcome_evaluation'").fetchall()
        return tuple(execution), [row[0] for row in calls]


def test_actual_http_timeout_drains_original_store_and_returns_saved_without_audio(tmp_path, monkeypatch, record_property):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from home_study_agent.database import Database

    settings = Settings(data_dir=tmp_path, database_path=tmp_path/'evaluation.db',
        data_encryption_key=b'0'*32, maintenance_enabled=False,
        agent_runtime_enabled=True, model_timeout_seconds=15)
    gateway = EvaluationGateway()
    with original_store_after_cancellation(monkeypatch) as (entered, finished, evidence):
        with TestClient(create_app(settings, model_gateway=gateway)) as client:
            database = Database(settings.database_path)
            sid = seed_session(database)
            persist_evaluation_turn(database, sid, 'eval', 'eval-action', 'new', 'diagnose')
            response = client.post('/v1/agent-turns/eval/execute')
            assert entered.is_set() and finished.is_set()
            assert evidence == {'original_store_calls': 1, 'real_thread_drain_observed': 1}
            assert saved_summary(database) == (('completed', 1, 1), ['completed'])
            assert len(gateway.requests) == 1
            for key, value in {**evidence, 'model_calls': 1, 'durable_state': 'completed',
                               'http_status': response.status_code, 'deadline_seconds': 15}.items():
                record_property(key, value)
            assert response.status_code == 200, response.text
            assert response.json()['execution']['replay_audio_allowed'] is False
            replay = client.post('/v1/agent-turns/eval/execute')
            assert replay.status_code == 200 and replay.json()['execution']['replay_audio_allowed'] is False
            assert len(gateway.requests) == 1


def test_human_cancellation_during_original_store_is_not_converted_to_success(tmp_path, monkeypatch):
    database, _, service, gateway, _ = evaluation_fixture(tmp_path)
    with original_store_after_cancellation(monkeypatch) as (entered, finished, evidence):
        async def run():
            task = asyncio.create_task(service.evaluate('eval', include_delivery=True))
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        asyncio.run(run())
        assert finished.is_set() and evidence['real_thread_drain_observed'] == 1
    assert saved_summary(database) == (('completed', 1, 1), ['completed'])
    assert len(gateway.requests) == 1


def test_timeout_with_only_response_ready_still_holds_without_model_retry(tmp_path, monkeypatch):
    database, _, service, gateway, settings = evaluation_fixture(tmp_path)
    settings.model_timeout_seconds = 1
    def stop_before_commit(*args, **kwargs):
        raise RuntimeError('synthetic stop after the original response receipt')
    monkeypatch.setattr(service, '_persist', stop_before_commit)
    with original_store_after_cancellation(monkeypatch) as (_, finished, evidence):
        with pytest.raises(LearningEvaluationUnknownError):
            asyncio.run(service.evaluate('eval', include_delivery=True))
        assert finished.is_set() and evidence['real_thread_drain_observed'] == 1
    assert saved_summary(database) == (('response_ready', 1, 0), ['completed'])
    assert len(gateway.requests) == 1


@pytest.mark.parametrize('revoked', ['parent_permission', 'privacy', 'source'])
def test_completed_after_timeout_rechecks_current_permission_and_source(evaluation, published_question, monkeypatch, revoked):
    writing, _, service, _, calls, turn = evaluation
    database, permission, _, published, privacy = published_question
    grant(published_question)
    service.settings.model_timeout_seconds = 2

    def revoke_after_original_commit(_):
        assert saved_summary(database) == (('completed', 1, 1), ['completed'])
        if revoked == 'parent_permission':
            permission.revoke(CHILD, diagnostic_item_id=published['diagnostic_item_id'], expected_revision=1,
                actor_id='authenticated-parent', client_action_id='revoke-after-drain')
        elif revoked == 'privacy':
            privacy.update_preferences({'cloud_model_text_allowed': False})
        else:
            with database.connect() as c:
                c.execute("UPDATE knowledge_source SET review_status='quarantined'")
                c.commit()

    with original_store_after_cancellation(monkeypatch, revoke_after_original_commit) as (_, finished, evidence):
        result = asyncio.run(service.evaluate(turn, include_delivery=True))
        assert finished.is_set() and evidence['real_thread_drain_observed'] == 1
    assert result['replay_audio_allowed'] is False
    assert result['evaluation']['feedback_status'] == 'blocked'
    assert 'Make the meeting time easier to find.' not in result['response_text']
    assert len(calls) == 1
    assert saved_summary(database) == (('completed', 1, 1), ['completed'])
