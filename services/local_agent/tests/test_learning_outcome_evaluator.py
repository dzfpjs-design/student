from pathlib import Path
import asyncio
import json
from types import SimpleNamespace
import pytest
from concurrent.futures import ThreadPoolExecutor
import subprocess
import sys
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from home_study_agent.agent_capability_executor import AgentCapabilityExecutor
from home_study_agent.agent_runtime import AgentRuntimeStore, CapabilityRegistry, context_manifest, parse_agent_plan_output
from home_study_agent.database import Database
import home_study_agent.learning_outcome_evaluator as evaluator_module
from home_study_agent.learning_outcome_evaluator import (
    LearningOutcomeEvaluator, LearningEvaluationUnknownError, LearningEvaluationInProgressError,
)
from home_study_agent.model_gateway import ModelResult
from home_study_agent.repository import StudyRepository
from test_agent_runtime import seed_session


class EvaluationGateway:
    configured = True

    def __init__(self):
        self.requests = []

    async def complete(self, request):
        assert request.request_type == "learning_outcome_evaluation"
        self.requests.append(request)
        return ModelResult(
            content=json.dumps({
                "understanding": "independent",
                "evidence_summary": "孩子能独立区分两个概念并说明适用方向。",
                "spoken_feedback": "你的区分是对的，而且理由是你自己说出来的。",
                "next_check": "如果直角边换了位置，你会选正弦还是余弦？为什么？",
                "confidence": 0.91,
            }, ensure_ascii=False),
            provider="test", model="test-evaluator", latency_ms=8,
            prompt_tokens=50, completion_tokens=40,
        )


def evaluation_plan(mode: str = "new", phase: str = "diagnose") -> str:
    return json.dumps({
        "objective": {
            "mode": mode, "type": "learning", "summary": "理解正弦和余弦的区别",
            "success_criteria": ["能独立说明区别", "完成一道近迁移检查"], "phase": phase,
        },
        "next_step": {
            "capability": "learning.evaluate_response",
            "arguments": {
                "response": "正弦看对边，余弦看邻边，都要除以斜边。",
                "subject": "数学",
                "question_asked": "在同一个直角三角形里，已知角的对边和斜边时应用哪个比值？",
            },
            "reason": "检查孩子自己的解释", "expected_evidence": ["independent_response"],
        },
        "spoken_transition": "我先听你的解释，再给你一道变化题。",
        "requires_child_reply": True, "confidence": 0.94,
    }, ensure_ascii=False)


def persist_evaluation_turn(database: Database, session_id: str, turn_id: str, action_id: str, mode: str, phase: str) -> None:
    from home_study_agent.period_reports import PeriodReportService
    # Match application startup before assembling longitudinal context.
    PeriodReportService(database).initialize()
    store = AgentRuntimeStore(database)
    decision = parse_agent_plan_output(evaluation_plan(mode, phase), "check_in", CapabilityRegistry())
    manifest = context_manifest(store.assemble_context(session_id, ()))
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO agent_turn(
              agent_turn_id, child_id, session_id, transcript, intent, response_text,
              action_json, orchestrator_version, created_at, client_action_id
            ) VALUES (?, 'child-runtime', ?, '正弦看对边，余弦看邻边', 'learning.evaluate_response',
                      '我来检查。', '{}', 'agent_runtime_planner_v1', datetime('now'), ?)
            """,
            (turn_id, session_id, action_id),
        )
        store.persist_plan(
            connection, child_id="child-runtime", session_id=session_id,
            agent_turn_id=turn_id, client_action_id=action_id, decision=decision,
            context_manifest=manifest,
        )
        connection.commit()


def test_unbound_model_rubric_stays_candidate_across_requested_phases(tmp_path: Path) -> None:
    database = Database(tmp_path / "runtime.db")
    database.initialize()
    session_id = seed_session(database)
    repository = StudyRepository(database)
    gateway = EvaluationGateway()
    evaluator = LearningOutcomeEvaluator(
        database, repository, gateway,
        SimpleNamespace(model_provider="test", model_name="test-evaluator"),
    )
    persist_evaluation_turn(database, session_id, "turn-eval-1", "turn-eval-action-1", "new", "diagnose")

    first = asyncio.run(evaluator.evaluate("turn-eval-1"))
    assert first["evaluation"]["outcome_level"] == "understood_now"
    assert first["evaluation"]["mastery_gate"]["status"] == "unknown"
    assert first["evaluation"]["certification_status"] == "candidate"
    assert first["evaluation"]["understanding"] == "independent"  # Preserve the model's candidate, not certification.
    first_context = json.loads(gateway.requests[0].messages[1]["content"])
    assert first_context["question_asked"].startswith("在同一个直角三角形")

    persist_evaluation_turn(database, session_id, "turn-eval-2", "turn-eval-action-2", "continue", "transfer_check")
    second = asyncio.run(evaluator.evaluate("turn-eval-2"))
    assert second["evaluation"]["outcome_level"] == "understood_now"
    assert second["evaluation"]["mastery_gate"]["status"] == "unknown"
    assert "三天后" not in second["response_text"]

    with database.connect() as connection:
        levels = [row[0] for row in connection.execute(
            "SELECT outcome_level FROM agent_outcome ORDER BY created_at, outcome_id"
        ).fetchall()]
        objective = connection.execute(
            "SELECT status, phase FROM agent_objective ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        commitment = connection.execute(
            "SELECT commitment_id, commitment_type, status, payload_json FROM agent_commitment"
        ).fetchone()
    assert levels == ["understood_now", "understood_now"]
    assert tuple(objective) == ("active", "remediate")
    assert commitment is None
    persist_evaluation_turn(
        database, session_id, "turn-eval-3", "turn-eval-action-3", "continue", "retention_check"
    )
    third = asyncio.run(evaluator.evaluate("turn-eval-3"))
    assert third["evaluation"]["outcome_level"] == "understood_now"
    assert third["evaluation"]["objective_phase"] != "completed"
    assert "已掌握" not in third["response_text"]
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM agent_outcome").fetchone()[0] == 3
        assert connection.execute("SELECT COUNT(*) FROM agent_commitment").fetchone()[0] == 0


def evaluation_fixture(tmp_path):
    database = Database(tmp_path / "evaluation.db")
    database.initialize()
    session_id = seed_session(database)
    persist_evaluation_turn(database, session_id, "eval", "eval-action", "new", "diagnose")
    gateway = EvaluationGateway()
    settings = SimpleNamespace(model_provider="test", model_name="test-evaluator")
    evaluator = LearningOutcomeEvaluator(database, StudyRepository(database), gateway, settings)
    return database, session_id, evaluator, gateway, settings


def test_completed_evaluation_replays_without_another_model_call(tmp_path):
    database, _, evaluator, gateway, settings = evaluation_fixture(tmp_path)
    first = asyncio.run(evaluator.evaluate("eval"))
    second = asyncio.run(evaluator.evaluate("eval"))
    reopened = LearningOutcomeEvaluator(Database(database.path), StudyRepository(database), gateway, settings)
    assert asyncio.run(reopened.evaluate("eval")) == first == second
    assert len(gateway.requests) == 1


@pytest.mark.parametrize("legacy", [False, True])
def test_http_wrapped_step_replays_without_another_model_call(tmp_path, legacy):
    database, _, evaluator, gateway, _ = evaluation_fixture(tmp_path)
    first = asyncio.run(evaluator.evaluate("eval"))
    with database.connect() as connection:
        AgentRuntimeStore(database).complete_turn_execution(connection, "eval", True, None, {
            "capability": "learning.evaluate_response", "response_text": first["response_text"],
            "result": first["evaluation"], "evidence": ["independent_response", "model_rubric"],
        })
        if legacy:
            connection.execute("DELETE FROM learning_evaluation_execution")
        connection.commit()
    replay = asyncio.run(evaluator.evaluate("eval"))
    if legacy:
        assert replay != first
        assert replay["evaluation"]["understanding"] == first["evaluation"]["understanding"]
        assert replay["evaluation"]["mastery_gate"]["eligible"] is False
        assert replay["evaluation"]["certification_status"] == "candidate"
        assert replay["evaluation"]["outcome_level"] == "understood_now"
    else:
        assert replay == first
    assert len(gateway.requests) == 1


def test_two_evaluator_instances_share_one_concurrent_claim(tmp_path):
    database, _, evaluator, gateway, settings = evaluation_fixture(tmp_path)
    original = gateway.complete

    async def slow_complete(request):
        await asyncio.sleep(.1)
        return await original(request)

    gateway.complete = slow_complete
    second = LearningOutcomeEvaluator(Database(database.path), StudyRepository(database), gateway, settings)

    async def run():
        return await asyncio.gather(evaluator.evaluate("eval"), second.evaluate("eval"))

    results = asyncio.run(run())
    assert results[0] == results[1]
    assert len(gateway.requests) == 1
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM agent_outcome").fetchone()[0] == 1


def test_threads_with_separate_event_loops_do_not_duplicate_model_call(tmp_path):
    database, _, evaluator, gateway, settings = evaluation_fixture(tmp_path)
    original = gateway.complete

    async def slow_complete(request):
        await asyncio.sleep(.15)
        return await original(request)

    gateway.complete = slow_complete
    second = LearningOutcomeEvaluator(Database(database.path), StudyRepository(database), gateway, settings)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(asyncio.run, instance.evaluate("eval")) for instance in (evaluator, second)]
        results = [future.result() for future in futures]
    assert results[0] == results[1]
    assert len(gateway.requests) == 1


def test_actual_http_execute_replay_survives_app_restart(tmp_path):
    from test_app import client_for
    gateway = EvaluationGateway()
    with client_for(tmp_path, model_gateway=gateway, agent_runtime_enabled=True) as client:
        database = Database(tmp_path / "test.db")
        session_id = seed_session(database)
        persist_evaluation_turn(database, session_id, "eval", "eval-action", "new", "diagnose")
        first = client.post("/v1/agent-turns/eval/execute")
        assert first.status_code == 200, first.text
        again = client.post("/v1/agent-turns/eval/execute")
        assert again.status_code == 200, again.text
        assert first.json()["execution"]["replay_audio_allowed"] is True
        assert again.json()["execution"] == {**first.json()["execution"], "replay_audio_allowed": False}
    with client_for(tmp_path, model_gateway=gateway, agent_runtime_enabled=True) as client:
        restarted = client.post("/v1/agent-turns/eval/execute")
        assert restarted.status_code == 200, restarted.text
        assert restarted.json()["execution"] == {**first.json()["execution"], "replay_audio_allowed": False}
    assert len(gateway.requests) == 1


def test_concurrent_real_http_requests_share_one_evaluation(tmp_path):
    from test_app import client_for
    gateway = EvaluationGateway()
    original = gateway.complete

    async def slow_complete(request):
        await asyncio.sleep(.2)
        return await original(request)

    gateway.complete = slow_complete
    with client_for(tmp_path, model_gateway=gateway, agent_runtime_enabled=True) as client:
        database = Database(tmp_path / "test.db")
        session_id = seed_session(database)
        persist_evaluation_turn(database, session_id, "eval", "eval-action", "new", "diagnose")
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(client.post, "/v1/agent-turns/eval/execute") for _ in range(2)]
            responses = [future.result() for future in futures]
        assert [r.status_code for r in responses] == [200, 200]
        executions=[r.json()["execution"] for r in responses]
        assert sorted(e["replay_audio_allowed"] for e in executions)==[False,True]
        assert {**executions[0],"replay_audio_allowed":False} == {**executions[1],"replay_audio_allowed":False}
    assert len(gateway.requests) == 1


def test_new_python_process_replays_committed_result(tmp_path):
    database, _, evaluator, gateway, _ = evaluation_fixture(tmp_path)
    first = asyncio.run(evaluator.evaluate("eval"))
    code = """
import asyncio,json,sys
from pathlib import Path
from home_study_agent.database import Database
from home_study_agent.learning_outcome_evaluator import LearningOutcomeEvaluator
from home_study_agent.repository import StudyRepository
db=Database(Path(sys.argv[1]))
print(json.dumps(asyncio.run(LearningOutcomeEvaluator(db,StudyRepository(db),None,None).evaluate('eval'))))
"""
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    completed = subprocess.run([sys.executable, "-B", "-c", code, str(database.path)],
                               env=env, text=True, capture_output=True, check=True, timeout=20)
    assert json.loads(completed.stdout) == first
    assert len(gateway.requests) == 1


def test_saved_model_response_recovers_after_persistence_failure_without_rebilling(tmp_path, monkeypatch):
    database, _, evaluator, gateway, settings = evaluation_fixture(tmp_path)

    def interrupted(*args):
        raise RuntimeError("simulated persistence interruption")

    monkeypatch.setattr(evaluator, "_persist", interrupted)
    with pytest.raises(LearningEvaluationUnknownError) as caught:
        asyncio.run(evaluator.evaluate("eval"))
    assert isinstance(caught.value.__cause__, RuntimeError)
    reopened = LearningOutcomeEvaluator(Database(database.path), StudyRepository(database), gateway, settings)
    result = asyncio.run(reopened.evaluate("eval"))
    assert result["evaluation"]["outcome_level"] == "understood_now"
    assert result["evaluation"]["understanding"] == "independent"
    assert result["evaluation"]["mastery_gate"]["status"] == "unknown"
    assert len(gateway.requests) == 1


def test_expired_claim_is_unknown_after_restart_not_an_automatic_retry(tmp_path, monkeypatch):
    database, _, evaluator, gateway, settings = evaluation_fixture(tmp_path)
    evaluator._claim(evaluator._load("eval"))
    clock = evaluation_clock(monkeypatch)
    clock.advance(120)
    reopened = LearningOutcomeEvaluator(Database(database.path), StudyRepository(database), gateway, settings)
    with pytest.raises(LearningEvaluationUnknownError):
        asyncio.run(reopened.evaluate("eval"))
    assert gateway.requests == []


def test_transport_failure_does_not_automatically_repeat_paid_request(tmp_path):
    database, _, evaluator, gateway, settings = evaluation_fixture(tmp_path)

    async def timeout(request):
        gateway.requests.append(request)
        raise TimeoutError("provider response lost")

    gateway.complete = timeout
    with pytest.raises(LearningEvaluationUnknownError) as caught:
        asyncio.run(evaluator.evaluate("eval"))
    assert isinstance(caught.value.__cause__, TimeoutError)
    reopened = LearningOutcomeEvaluator(Database(database.path), StudyRepository(database), gateway, settings)
    with pytest.raises(LearningEvaluationUnknownError):
        asyncio.run(reopened.evaluate("eval"))
    assert len(gateway.requests) == 1


def test_repetition_is_kept_as_evidence_but_never_independent(tmp_path):
    database, session_id, evaluator, gateway, _ = evaluation_fixture(tmp_path)
    with database.connect() as connection:
        connection.execute("DELETE FROM agent_plan WHERE agent_turn_id='eval'")
        connection.execute("DELETE FROM agent_turn WHERE agent_turn_id='eval'")
        connection.execute("DELETE FROM agent_objective")
        connection.commit()
    # A real persisted explanation precedes the evaluated reply on one objective.
    persist_evaluation_turn(database, session_id, "explain", "explain-action", "new", "explain")
    repeated = "正弦看对边，余弦看邻边，都要除以斜边。"
    with database.connect() as connection:
        connection.execute("UPDATE agent_step SET capability_name='learning.explain',status='completed'")
        connection.execute("UPDATE agent_turn SET tool_status='completed',response_text=?", (repeated + " 请你复述。",))
        connection.commit()
    persist_evaluation_turn(database, session_id, "eval", "eval-action", "continue", "check")
    result = asyncio.run(evaluator.evaluate("eval"))
    assert result["evaluation"]["outcome_level"] == "understood_now"
    assert result["evaluation"]["independence_eligible"] is False
    context = json.loads(gateway.requests[0].messages[1]["content"])
    assert context["child_response"] == repeated
    assert context["previous_explanation"].startswith(repeated)
    with database.connect() as connection:
        saved = connection.execute("SELECT record_json FROM learning_evaluation_execution").fetchone()[0]
    assert json.loads(saved)["child_response"] == repeated


def test_cancelled_coroutine_leaves_unknown_and_does_not_rebill(tmp_path):
    database, _, evaluator, gateway, settings = evaluation_fixture(tmp_path)

    async def run():
        entered = asyncio.Event()

        async def pending(request):
            gateway.requests.append(request)
            entered.set()
            await asyncio.sleep(100)

        gateway.complete = pending
        task = asyncio.create_task(evaluator.evaluate("eval"))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    restarted = LearningOutcomeEvaluator(Database(database.path), StudyRepository(database), gateway, settings)
    with pytest.raises(LearningEvaluationUnknownError):
        asyncio.run(restarted.evaluate("eval"))
    assert len(gateway.requests) == 1


@pytest.mark.parametrize("status", ["paused", "cancelled"])
def test_late_model_result_does_not_reactivate_interrupted_objective(tmp_path, status):
    database, _, evaluator, gateway, _ = evaluation_fixture(tmp_path)
    original = gateway.complete

    async def changed_direction(request):
        with database.connect() as connection:
            connection.execute("UPDATE agent_objective SET status=?,phase='child_changed_direction'", (status,))
            connection.commit()
        return await original(request)

    gateway.complete = changed_direction
    result = asyncio.run(evaluator.evaluate("eval"))
    assert result["evaluation"]["objective_update_applied"] is False
    with database.connect() as connection:
        assert connection.execute("SELECT status FROM agent_objective").fetchone()[0] == status
        assert connection.execute("SELECT COUNT(*) FROM agent_commitment").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM agent_outcome").fetchone()[0] == 1


def evaluation_clock(monkeypatch):
    class Clock(datetime):
        current = datetime.now(timezone.utc)

        @classmethod
        def now(cls, tz=None):
            return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)

        @classmethod
        def advance(cls, seconds):
            cls.current += timedelta(seconds=seconds)

    monkeypatch.setattr(evaluator_module, "datetime", Clock)
    return Clock


def test_configured_total_deadline_covers_gateway_and_persists_lease(tmp_path, monkeypatch):
    database, _, evaluator, gateway, settings = evaluation_fixture(tmp_path)
    settings.model_timeout_seconds = 180
    budgets = []
    wait_for = asyncio.wait_for

    async def capture_budget(awaitable, timeout):
        budgets.append(timeout)
        return await wait_for(awaitable, timeout)

    monkeypatch.setattr(evaluator_module.asyncio, "wait_for", capture_budget)
    asyncio.run(evaluator.evaluate("eval"))
    assert len(budgets) == 1
    assert 175 < budgets[0] <= 180
    assert evaluator.lease_seconds == 210
    with database.connect() as connection:
        row = connection.execute("SELECT * FROM learning_evaluation_execution").fetchone()
    record = json.loads(row["record_json"])
    claimed = datetime.fromisoformat(row["claimed_at"])
    assert (datetime.fromisoformat(record["evaluation_deadline_at"]) - claimed).total_seconds() == 180
    assert (datetime.fromisoformat(record["claim_expires_at"]) - claimed).total_seconds() == 210
    assert len(gateway.requests) == 1


def test_live_claim_crossing_old_expiry_survives_thread_concurrency(tmp_path, monkeypatch):
    database, _, evaluator, gateway, settings = evaluation_fixture(tmp_path)
    settings.model_timeout_seconds = 180
    clock = evaluation_clock(monkeypatch)
    entered, release, observer_entered = threading.Event(), threading.Event(), threading.Event()
    original = gateway.complete

    async def blocked(request):
        entered.set()
        while not release.is_set():
            await asyncio.sleep(.01)
        return await original(request)

    gateway.complete = blocked
    # A second process may restart with a shorter config. The saved lease wins.
    observer = LearningOutcomeEvaluator(Database(database.path), StudyRepository(database), gateway,
        SimpleNamespace(model_provider="test", model_name="test", model_timeout_seconds=1))
    claim = observer._claim

    def observe(record):
        result = claim(record)
        observer_entered.set()
        return result

    monkeypatch.setattr(observer, "_claim", observe)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(asyncio.run, evaluator.evaluate("eval"))
        try:
            assert entered.wait(5)
            clock.advance(40)
            assert observer.execution_status("eval") == {"status": "in_progress"}
            second = pool.submit(asyncio.run, observer.evaluate("eval"))
            assert observer_entered.wait(5)
            assert observer.execution_status("eval") == {"status": "in_progress"}
        finally:
            release.set()
        assert first.result(timeout=5) == second.result(timeout=5)
    assert len(gateway.requests) == 1
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM agent_outcome").fetchone()[0] == 1


@pytest.mark.parametrize('failure', ['deadline_before_dispatch', 'deadline_after_dispatch', 'transport'])
def test_first_uncertain_http_failure_is_409_without_failed_turn_and_replays_after_restart(tmp_path, monkeypatch, failure):
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    gateway = EvaluationGateway()
    clock = evaluation_clock(monkeypatch)
    entered = None
    injected = []

    async def fail(request):
        gateway.requests.append(request)
        if entered is not None:
            entered.set()
        if failure == 'deadline_after_dispatch':
            await asyncio.Event().wait()
        raise TimeoutError('provider response lost')
    gateway.complete = fail
    if failure == 'deadline_before_dispatch':
        claim = LearningOutcomeEvaluator._claim

        def expire_after_claim(self, record):
            result = claim(self, record)
            if result.get('acquired'):
                clock.advance(16)
            return result
        monkeypatch.setattr(LearningOutcomeEvaluator, '_claim', expire_after_claim)
    elif failure == 'deadline_after_dispatch':
        wait_for = asyncio.wait_for
        evaluate_claimed = LearningOutcomeEvaluator._evaluate_claimed

        async def tagged_claimed_evaluation(self, record):
            return await evaluate_claimed(self, record)
        monkeypatch.setattr(LearningOutcomeEvaluator, '_evaluate_claimed', tagged_claimed_evaluation)
        target = tagged_claimed_evaluation.__code__

        async def inject_deadline_after_dispatch(awaitable, timeout):
            nonlocal entered
            if getattr(awaitable, 'cr_code', None) is not target:
                return await wait_for(awaitable, timeout)
            assert timeout == 15
            entered = asyncio.Event()
            task = asyncio.create_task(awaitable)
            try:
                await wait_for(entered.wait(), timeout=timeout)
                injected.append('after_gateway_entry')
                return await wait_for(task, timeout=0.1)
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        monkeypatch.setattr(asyncio, 'wait_for', inject_deadline_after_dispatch)
    settings = Settings(data_dir=tmp_path, database_path=tmp_path / 'test.db', data_encryption_key=b'0' * 32, maintenance_enabled=False, agent_runtime_enabled=True, model_timeout_seconds=15)
    from fastapi.testclient import TestClient
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        db = Database(settings.database_path)
        sid = seed_session(db)
        persist_evaluation_turn(db, sid, 'eval', 'eval-action', 'new', 'diagnose')
        response = client.post('/v1/agent-turns/eval/execute')
        assert response.status_code == 409, response.text
        assert response.json()['detail']['code'] == 'LEARNING_EVALUATION_RESULT_UNKNOWN'
        assert client.get('/v1/agent-turns/eval/execution-status').json() == {'status': 'unknown'}
        with db.connect() as c:
            assert c.execute("SELECT tool_status FROM agent_turn WHERE agent_turn_id='eval'").fetchone()[0] != 'failed'
            assert c.execute('SELECT status FROM agent_step').fetchone()[0] != 'failed'
            assert c.execute('SELECT state FROM learning_evaluation_execution').fetchone()[0] == 'unknown'
            assert c.execute('SELECT COUNT(*) FROM agent_outcome').fetchone()[0] == 0
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        response = client.post('/v1/agent-turns/eval/execute')
        assert response.status_code == 409, response.text
        assert response.json()['detail']['code'] == 'LEARNING_EVALUATION_RESULT_UNKNOWN'
    assert len(gateway.requests) == (0 if failure == 'deadline_before_dispatch' else 1)
    assert injected == (['after_gateway_entry'] if failure == 'deadline_after_dispatch' else [])

@pytest.mark.parametrize("state", ["running", "expired", "failed_step", "response_ready", "completed", "legacy", "absent"])
def test_execution_status_is_strictly_read_only(tmp_path, monkeypatch, state):
    database, _, evaluator, gateway, _ = evaluation_fixture(tmp_path)
    clock = evaluation_clock(monkeypatch)
    result = None
    if state in {"completed", "legacy", "response_ready"}:
        result = asyncio.run(evaluator.evaluate("eval"))
        with database.connect() as connection:
            if state == "legacy":
                connection.execute("DELETE FROM learning_evaluation_execution")
                connection.execute("UPDATE agent_step SET result_json=?", (json.dumps({"result": result["evaluation"]}),))
            elif state == "response_ready":
                connection.execute("UPDATE learning_evaluation_execution SET state='response_ready',result_json=NULL")
                connection.execute("UPDATE agent_step SET status='failed'")
            connection.commit()
    elif state != "absent":
        evaluator._claim(evaluator._load("eval"))
        if state == "expired":
            clock.advance(evaluator.lease_seconds + 1)
        if state == "failed_step":
            with database.connect() as connection:
                connection.execute("UPDATE agent_step SET status='failed',error_code='APP_RESTARTED'")
                connection.commit()
    calls = len(gateway.requests)
    connect = database.connect

    @contextmanager
    def read_only():
        with connect() as connection:
            connection.execute("PRAGMA query_only=ON")
            yield connection

    monkeypatch.setattr(database, "connect", read_only)
    before = None
    with database.connect() as connection:
        before = list(connection.iterdump())
    status = evaluator.execution_status("eval")
    expected = "completed" if state in {"completed", "legacy"} else "in_progress" if state in {"running", "response_ready"} else "unknown"
    assert status["status"] == expected
    if expected == "completed":
        if state == "legacy":
            assert status["result"] != result
            assert status["result"]["evaluation"]["mastery_gate"]["eligible"] is False
            assert status["result"]["evaluation"]["understanding"] == result["evaluation"]["understanding"]
        else:
            assert status["result"] == result
    else:
        assert "result" not in status
    with database.connect() as connection:
        assert list(connection.iterdump()) == before
    assert len(gateway.requests) == calls


def test_persisted_lease_survives_new_process_with_shorter_configuration(tmp_path, monkeypatch):
    database, _, evaluator, gateway, settings = evaluation_fixture(tmp_path)
    settings.model_timeout_seconds = 180
    clock = evaluation_clock(monkeypatch)
    clock.advance(-40)
    evaluator._claim(evaluator._load("eval"))
    code = """
import json,sys
from pathlib import Path
from types import SimpleNamespace
from home_study_agent.database import Database
from home_study_agent.learning_outcome_evaluator import LearningOutcomeEvaluator
from home_study_agent.repository import StudyRepository
db=Database(Path(sys.argv[1]))
e=LearningOutcomeEvaluator(db,StudyRepository(db),None,SimpleNamespace(model_timeout_seconds=1))
print(json.dumps(e.execution_status('eval')))
"""
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    run = subprocess.run([sys.executable, "-B", "-c", code, str(database.path)],
                         env=env, text=True, capture_output=True, check=True, timeout=20)
    assert json.loads(run.stdout) == {"status": "in_progress"}
    assert gateway.requests == []


def test_saved_response_recovery_is_local_after_restart(tmp_path, monkeypatch):
    database, _, evaluator, gateway, settings = evaluation_fixture(tmp_path)

    def interrupted(*args):
        raise OSError("database temporarily unavailable")

    monkeypatch.setattr(evaluator, "_persist", interrupted)
    with pytest.raises(LearningEvaluationUnknownError):
        asyncio.run(evaluator.evaluate("eval"))
    restarted = LearningOutcomeEvaluator(Database(database.path), StudyRepository(database), None, settings)
    assert restarted.execution_status("eval") == {"status": "in_progress"}
    result = restarted.recover_ready("eval")
    assert restarted.execution_status("eval") == {"status": "completed", "result": result}
    assert restarted.recover_ready("eval") == result
    assert len(gateway.requests) == 1


@pytest.mark.parametrize("claimed", [False, True])
def test_local_recovery_cannot_invoke_model_without_saved_response(tmp_path, claimed):
    _, _, evaluator, gateway, _ = evaluation_fixture(tmp_path)
    if claimed:
        evaluator._claim(evaluator._load("eval"))
    error = LearningEvaluationInProgressError if claimed else LearningEvaluationUnknownError
    with pytest.raises(error):
        evaluator.recover_ready("eval")
    assert gateway.requests == []
