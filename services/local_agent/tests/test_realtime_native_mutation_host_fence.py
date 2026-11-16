"""Real original task writes recheck current native owner inside the write.

HTTP cases run the actual execute coroutine on a copied-context worker so a
new owner can arrive after preflight and before the original mutation.
"""
import asyncio
import pytest

from home_study_agent.agent_runtime import AgentRuntimeStore
from home_study_agent.agent_capability_executor import AgentCapabilityExecutor
from home_study_agent.realtime_native_authority import inspect_native_control
from test_content_free_voice_control_http import task_fixture
from test_native_runtime_hmac_integration import native, env, admit, proposal, success, counts
from test_realtime_capability_tools import scenario
from test_ordinary_learning_real_source_execution import real_source


def prepare_task(native):
    client, db, gateway, sid, parent, request, calls, faults = native
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,)); c.commit()
    task_fixture(db, sid)
    return client.app.state.agent_capability_executor.repository


def threaded_original(native, monkeypatch):
    service = native[0].app.state.native_realtime_capabilities
    original = service.execute_turn

    async def execute(turn_id):
        # asyncio.to_thread carries the actual purpose-HMAC ContextVar. This
        # invokes the original app route/Runtime/executor, without a fake receipt.
        return await asyncio.to_thread(lambda: asyncio.run(original(turn_id)))

    monkeypatch.setattr(service, 'execute_turn', execute)


def test_new_hmac_generation_after_preflight_fences_original_pause_write(native, monkeypatch):
    client, db, gateway, sid, parent, request, calls, faults = native
    repo = prepare_task(native)
    threaded_original(native, monkeypatch)
    original = repo.pause_plan_item
    paused_attempts = []

    def advance_owner_before_write(*args, **kwargs):
        assert kwargs.get('authorized_turn_id') is not None
        response = request('catalog', connection='synthetic-new-owned-host', generation=2)
        assert response.status_code == 200, response.text
        paused_attempts.append(kwargs['authorized_turn_id'])
        return original(*args, **kwargs)

    monkeypatch.setattr(repo, 'pause_plan_item', advance_owner_before_write)
    voice, _ = admit(native, words='手头这段先留在这里，我想腾出手来。')
    body = proposal(voice, 'pause-generation-fence', 'task.pause', {})
    first = request('proposal', body)
    assert paused_attempts and len(calls) == 1
    # No response from a retired old owner can authorize a fresh export.
    assert first.status_code == 409, first.text
    assert repo._get_plan_item_by_id('control-task-1')['status'] == 'active'
    assert repo._get_plan_item_by_id('control-task-2')['status'] == 'pending'
    with db.connect() as c:
        assert c.execute("SELECT count(*) FROM plan_item_event WHERE event_type='paused'").fetchone()[0] == 0
    identity = {key: body[key] for key in
                ('version', 'voice_input_id', 'supplier_session_id', 'input_item_id', 'response_id', 'call_id')}
    recovered = success(request('receipt', identity, connection='synthetic-new-owned-host', generation=2))
    assert recovered['execution']['status'] != 'completed'
    assert counts(db)['agent_outcome'] == 0 and gateway.requests == []


def test_current_hmac_context_survives_real_threaded_executor_and_actual_pause(native, monkeypatch):
    client, db, gateway, sid, parent, request, calls, faults = native
    repo = prepare_task(native)
    threaded_original(native, monkeypatch)
    voice, _ = admit(native, words='手上的计时先暂停，我想换口气。')
    body = proposal(voice, 'pause-current-thread', 'task.pause', {})
    result = success(request('proposal', body))
    assert result['execution']['status'] == 'completed', result
    assert result['public_result']['task_state'] == 'paused'
    assert repo._get_plan_item_by_id('control-task-1')['status'] == 'paused'
    assert len(calls) == 1 and gateway.requests == []


def test_original_injected_authorizer_is_strictly_scoped_and_missing_scope_has_no_native_grant(scenario):
    client, db, gateway, root, old, parent, service, propose, seen, authorize = scenario
    sid = root.split('/')[-1]
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,)); c.commit()
    repo = task_fixture(db, sid)
    service.execute_turn = AgentCapabilityExecutor(db, repo).execute
    body, _ = propose('手头这一件先保留', {}, reference=old['reference'], cap='task.pause')
    admitted = service.admit(sid, body)

    async def run_then_probe():
        completed = await service.execute(sid, admitted['action_id'])
        assert completed['execution']['status'] == 'completed', completed
        assert repo._get_plan_item_by_id('control-task-1')['status'] == 'paused'
        # Same coroutine, same DB and still-true original callback: finally must
        # nevertheless remove ambient dispatch authority after execution.
        with db.connect() as c:
            with pytest.raises(ValueError, match='NATIVE_RUNTIME_TARGET_CHANGED'):
                inspect_native_control(c, admitted['agent_turn_id'], allow_planned=True)

    asyncio.run(run_then_probe())
