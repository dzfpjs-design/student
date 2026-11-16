"""C08 real HTTP, monitor/continuation stores and dispatcher; no physical device."""
from dataclasses import replace
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.document_monitoring import DocumentMonitoringService
from home_study_agent.model_gateway import ModelGateway
from home_study_agent.repository import utc_now
from test_diagnostic_prompt_http import environment, CHILD, OTHER
from test_voice_stop_activity_arbitration_http import session, say


QUOTE = '暂停高拍仪，我刚才写了什么？'
TEXT = 'PRIVATE_LOCAL_PAGE: 12 + 8 = 20'


def host(settings, resume_quote=None):
    settings = replace(settings, model_provider='qwen', model_api_key='synthetic-no-network',
                       model_base_url='https://voice.invalid/v1')
    posts = []
    def transport(request):
        posts.append(json.loads(request.content))
        if resume_quote is not None:
            body = json.loads(posts[-1]['messages'][-1]['content'])
            assert body['transcript'] == resume_quote
            content = {'role': 'control', 'control': 'task.resume', 'quote': resume_quote, 'confidence': .98,
                       'selector': {'subject': None, 'task_kind': None, 'relative_order': None}}
            return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(content)}}]})
        raise AssertionError('A saved local page must never be sent to a model')
    return settings, ModelGateway(settings, transport=httpx.MockTransport(transport)), posts


def setup_stop(client, db, stage='study', quote=QUOTE, existing_goal=False, has_page=True, session_id=None):
    sid = session_id or session(client, db)
    assert client.post(f'/v1/sessions/{sid}/document-monitor/start', json={'client_action_id':'open-monitor'}).status_code == 200
    with db.connect() as c:
        c.execute('''INSERT INTO capture_artifact(artifact_id,child_id,session_id,artifact_type,extracted_text,
            confidence,details_json,status,created_at,client_action_id) VALUES('stop-page',?,?,'document_ocr',?,.94,'{}','completed',?,'page-action')''',
            (CHILD, sid, TEXT, utc_now()))
        c.commit()
    DocumentMonitoringService(db).record_revision(sid, 'stop-page', 'signature-before-stop', 'page-revision')
    with db.connect() as c:
        c.execute('UPDATE daily_session SET state=? WHERE session_id=?', (stage,sid))
        c.execute('UPDATE privacy_preference SET cloud_model_text_allowed=0')
        if not has_page:
            c.execute("DELETE FROM document_page_revision WHERE artifact_id='stop-page'")
            c.execute("DELETE FROM capture_artifact WHERE artifact_id='stop-page'")
        if existing_goal:
            c.execute('''INSERT INTO agent_objective(objective_id,child_id,session_id,objective_type,summary,
                success_criteria_json,phase,status,created_at,updated_at)
                VALUES('original-learning',?,?,'learning','原数学目标','["独立完成原题"]','practice','active',?,?)''',
                (CHILD, sid, utc_now(), utc_now()))
        c.commit()
    turn = say(client, sid, quote, 'compound-stop')
    assert turn['action']['type'] == 'control_document_monitor'
    assert TEXT not in json.dumps(turn)
    return sid, turn


def complete_stop(client, sid, tid):
    assert client.post(f'/v1/sessions/{sid}/agent-turns/{tid}/control-admission').json()['status'] == 'admitted'
    assert client.post(f'/v1/sessions/{sid}/document-monitor/pause',json={'client_action_id':'physical-stop'}).status_code == 200
    receipt = dict(capability='document.monitor_control', execution_location='client_device_bridge',
        action_type='control_document_monitor',status='completed',completed_at=utc_now(),
        output=dict(device_type='document_camera',device_status='paused',frame_confirmed=False,
            lease_confirmed=True,persisted_status='paused'))
    result = client.put(f'/v1/agent-turns/{tid}/outcome',json={'final_response':'高拍仪已停止。',
        'tool_status':'completed','result':{'execution_receipt':receipt}})
    assert result.status_code == 200, result.text
    return receipt


def test_stop_receipt_then_saved_question_survives_http_restart_exactly_once(environment):
    settings, db = environment
    settings, gateway, posts = host(settings)
    with TestClient(create_app(settings,model_gateway=gateway)) as client:
        sid, turn = setup_stop(client, db)
        tid = turn['agent_turn_id']
        assert turn['after_execution'] == 'replan'
        assert client.post(f'/v1/sessions/{sid}/agent-turns/{tid}/continue').json()['status'] == 'none'
        complete_stop(client, sid, tid)
        assert client.get(f'/v1/sessions/{sid}/agent-continuation').json()['status'] == 'pending'
        assert not posts
    with TestClient(create_app(settings,model_gateway=gateway)) as client:
        result = client.post(f'/v1/sessions/{sid}/agent-turns/{tid}/continue')
        assert result.status_code == 200, result.text
        assert result.json()['status'] == 'ready', result.text
        followup = result.json()['turn']
        assert followup['action']['type'] == 'none'
        assert followup['action']['local_only'] and not followup['action']['cloud_transmission_allowed']
        assert TEXT in followup['response_text'] and '停止前' in followup['response_text']
        next_id = followup['agent_turn_id']
        done = client.post(f'/v1/agent-turns/{next_id}/execute')
        assert done.status_code == 200, done.text
        replay = client.post(f'/v1/sessions/{sid}/agent-turns/{tid}/continue').json()
        assert replay['status'] == 'completed' and not posts
        assert client.post(f'/v1/sessions/{sid}/agent-turns/{tid}/control-admission').json()['status'] == 'execution_receipt_required'
        with db.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM agent_step WHERE capability_name='document.monitor_control'").fetchone()[0] == 1
            assert c.execute("SELECT COUNT(*) FROM agent_turn WHERE intent='runtime_continuation'").fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
            assert c.execute("SELECT status FROM agent_objective").fetchone()[0] == 'completed'


@pytest.mark.parametrize('change', ['cancel', 'switch_child', 'ended', 'source_deleted', 'source_changed', 'monitor_resumed', 'ttl', 'arrival_deleted'])
def test_queued_question_rechecks_authority_before_reading(environment, change):
    settings, db = environment
    settings, gateway, posts = host(settings)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, turn = setup_stop(client, db)
        tid = turn['agent_turn_id']
        complete_stop(client, sid, tid)
        if change == 'cancel':
            say(client, sid, '先别执行刚才的操作', 'cancel-question')
        elif change == 'switch_child':
            r = client.post(f'/v1/sessions/{sid}/switch-child', json={'target_child_id': OTHER, 'client_action_id': 'switch-before-question'})
            assert r.status_code == 200, r.text
        elif change == 'monitor_resumed':
            assert client.post(f'/v1/sessions/{sid}/document-monitor/resume', json={'client_action_id': 'new-capture'}).status_code == 200
        else:
            with db.connect() as c:
                # Adversarial source/lifecycle mutations, not fabricated execution receipts.
                if change == 'ended':
                    c.execute("UPDATE daily_session SET status='completed',state='completed' WHERE session_id=?", (sid,))
                elif change == 'source_deleted':
                    c.execute("DELETE FROM document_page_revision WHERE artifact_id='stop-page'")
                elif change == 'source_changed':
                    c.execute("UPDATE capture_artifact SET extracted_text='replacement private source' WHERE artifact_id='stop-page'")
                elif change == 'ttl':
                    c.execute("UPDATE agent_continuation SET created_at='2020-01-01T00:00:00+00:00'")
                elif change == 'arrival_deleted':
                    c.execute("DELETE FROM learning_input_arrival WHERE session_id=?", (sid,))
                c.commit()
        r = client.post(f'/v1/sessions/{sid}/agent-turns/{tid}/continue')
        assert r.status_code in {200, 409}, r.text
        assert r.json().get('status') != 'ready' and TEXT not in r.text
        with db.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM agent_turn WHERE intent='runtime_continuation'").fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
        assert not posts


def test_prepared_question_restart_and_cancel_late_result(environment):
    settings, db = environment
    settings, gateway, posts = host(settings)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, turn = setup_stop(client, db)
        tid = turn['agent_turn_id']
        complete_stop(client, sid, tid)
        ready = client.post(f'/v1/sessions/{sid}/agent-turns/{tid}/continue').json()
        assert ready['status'] == 'ready'
        next_id = ready['turn']['agent_turn_id']
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        ready = client.post(f'/v1/sessions/{sid}/agent-turns/{tid}/continue').json()
        assert ready['status'] == 'ready' and ready['turn']['agent_turn_id'] == next_id
        say(client, sid, '先别执行刚才的操作', 'cancel-after-prepared')
        assert client.post(f'/v1/agent-turns/{next_id}/execute').status_code == 409
        late = client.put(f'/v1/agent-turns/{next_id}/outcome', json={
            'final_response': 'pretend answered', 'tool_status': 'completed', 'result': {'status': 'completed'}})
        assert late.status_code == 409, late.text
        assert late.json()['message'] == '原请求已取消或来源已变化，不再继续旧问题。'
        with db.connect() as c:
            assert c.execute('SELECT tool_status FROM agent_turn WHERE agent_turn_id=?', (next_id,)).fetchone()[0] != 'completed'
            assert c.execute('SELECT status FROM agent_objective').fetchone()[0] == 'cancelled'
        assert not posts


@pytest.mark.parametrize('stage', ['check_in', 'classroom_recall', 'reflection'])
def test_compound_stop_is_local_in_nonstudy_stage(environment, stage):
    settings, db = environment
    settings, gateway, posts = host(settings)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, turn = setup_stop(client, db, stage)
        complete_stop(client, sid, turn['agent_turn_id'])
        ready = client.post(f"/v1/sessions/{sid}/agent-turns/{turn['agent_turn_id']}/continue")
        assert ready.json()['status'] == 'ready', ready.text
        assert client.post(f"/v1/agent-turns/{ready.json()['turn']['agent_turn_id']}/execute").status_code == 200
        with db.connect() as c:
            assert c.execute('SELECT state FROM daily_session WHERE session_id=?', (sid,)).fetchone()[0] == stage
        assert not posts


def test_local_planning_crash_recovers_same_question_after_lease_and_restart(environment, monkeypatch):
    settings, db = environment
    settings, gateway, posts = host(settings)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, turn = setup_stop(client, db)
        tid = turn['agent_turn_id']
        complete_stop(client, sid, tid)
        def crash(work):
            raise RuntimeError('synthetic crash after claim, before any response write')
        monkeypatch.setattr(client.app.state.agent_orchestrator, '_continue_document_stop_question', crash)
        with pytest.raises(RuntimeError, match='synthetic crash'):
            client.post(f'/v1/sessions/{sid}/agent-turns/{tid}/continue')
        with db.connect() as c:
            c.execute("UPDATE agent_continuation SET lease_until='2020-01-01T00:00:00+00:00'")
            c.commit()
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        ready = client.post(f'/v1/sessions/{sid}/agent-turns/{tid}/continue')
        assert ready.json()['status'] == 'ready', ready.text
        assert TEXT in ready.json()['turn']['response_text']
        assert client.post(f"/v1/agent-turns/{ready.json()['turn']['agent_turn_id']}/execute").status_code == 200
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM agent_continuation').fetchone()[0] == 1
        assert not posts


def test_cancel_during_actual_reply_dispatch_cannot_publish_late_completion(environment, monkeypatch):
    settings, db = environment
    settings, gateway, posts = host(settings)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, turn = setup_stop(client, db)
        tid = turn['agent_turn_id']
        complete_stop(client, sid, tid)
        next_id = client.post(f'/v1/sessions/{sid}/agent-turns/{tid}/continue').json()['turn']['agent_turn_id']
        executor = client.app.state.agent_capability_executor
        dispatch = executor._dispatch
        stages = []
        # Separate ASGI transport models a concurrent input, without invoking
        # another startup or recursively entering the executing client's portal.
        interrupt_client = TestClient(client.app)
        def cancel_after_reply(step):
            result = dispatch(step)
            stages.append('replied')
            say(interrupt_client, sid, '先别执行刚才的操作', 'cancel-during-dispatch')
            stages.append('cancelled')
            return result
        monkeypatch.setattr(executor, '_dispatch', cancel_after_reply)
        response = client.post(f'/v1/agent-turns/{next_id}/execute')
        interrupt_client.close()
        assert stages == ['replied', 'cancelled'], response.text
        assert response.status_code == 409 and TEXT not in response.text, response.text
        with db.connect() as c:
            assert c.execute('SELECT status FROM agent_plan WHERE agent_turn_id=?', (next_id,)).fetchone()[0] == 'cancelled'
            assert c.execute('SELECT tool_status FROM agent_turn WHERE agent_turn_id=?', (next_id,)).fetchone()[0] != 'completed'
        assert not posts


def test_stop_question_does_not_complete_or_displace_existing_learning_goal(environment):
    settings, db = environment
    settings, gateway, posts = host(settings)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, turn = setup_stop(client, db, existing_goal=True)
        with db.connect() as c:
            before = dict(c.execute("SELECT * FROM agent_objective WHERE objective_id='original-learning'").fetchone())
        complete_stop(client, sid, turn['agent_turn_id'])
        ready = client.post(f"/v1/sessions/{sid}/agent-turns/{turn['agent_turn_id']}/continue").json()
        assert ready['status'] == 'ready'
        response = client.post(f"/v1/agent-turns/{ready['turn']['agent_turn_id']}/execute")
        assert response.status_code == 200, response.text
        with db.connect() as c:
            assert dict(c.execute("SELECT * FROM agent_objective WHERE objective_id='original-learning'").fetchone()) == before
            assert c.execute('SELECT COUNT(*) FROM agent_objective').fetchone()[0] == 1
        assert not posts


def test_missing_original_page_is_answered_honestly_without_new_capture(environment):
    settings, db = environment
    settings, gateway, posts = host(settings)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, turn = setup_stop(client, db, has_page=False)
        complete_stop(client, sid, turn['agent_turn_id'])
        ready = client.post(f"/v1/sessions/{sid}/agent-turns/{turn['agent_turn_id']}/continue").json()
        assert ready['status'] == 'ready'
        assert '没有可核对' in ready['turn']['response_text']
        assert ready['turn']['action']['type'] == 'none'
        response = client.post(f"/v1/agent-turns/{ready['turn']['agent_turn_id']}/execute")
        assert response.status_code == 200, response.text
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM capture_artifact').fetchone()[0] == 0
            assert c.execute('SELECT status FROM agent_objective').fetchone()[0] == 'active'
        assert not posts


def test_stop_question_respects_existing_chain_budget(environment, monkeypatch):
    from home_study_agent import agent_continuation
    settings, db = environment
    settings, gateway, posts = host(settings)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, turn = setup_stop(client, db)
        monkeypatch.setattr(agent_continuation, 'MAX_CHAIN_STEPS', 0)
        complete_stop(client, sid, turn['agent_turn_id'])
        result = client.post(f"/v1/sessions/{sid}/agent-turns/{turn['agent_turn_id']}/continue").json()
        assert result == {'status': 'blocked', 'reason': 'CHAIN_BUDGET_REACHED'}
        assert not posts


def test_cancel_question_preserves_original_learning_goal(environment):
    settings, db = environment
    settings, gateway, posts = host(settings)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, turn = setup_stop(client, db, existing_goal=True)
        complete_stop(client, sid, turn['agent_turn_id'])
        with db.connect() as c:
            before = dict(c.execute("SELECT * FROM agent_objective WHERE objective_id='original-learning'").fetchone())
        say(client, sid, '先别执行刚才的操作', 'cancel-with-learning-goal')
        with db.connect() as c:
            assert dict(c.execute("SELECT * FROM agent_objective WHERE objective_id='original-learning'").fetchone()) == before
        assert client.post(f"/v1/sessions/{sid}/agent-turns/{turn['agent_turn_id']}/continue").json()['status'] != 'ready'
        assert not posts


def test_cancel_question_then_real_asr_task_resume_is_not_blocked_by_temporary_goal(environment, monkeypatch):
    from test_content_free_voice_control_http import task_fixture
    from test_voice_runtime_binding import capture, interpret, prepare
    settings, db = environment
    quote = '刚才手头停下的那项我接着做，计时也继续'
    settings, gateway, posts = host(settings, resume_quote=quote)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid = session(client, db)
        repo = task_fixture(db, sid)
        repo.pause_plan_item('control-task-1', 'paused-before-compound-stop')
        prompt = prepare(client, sid)
        sid, turn = setup_stop(client, db, session_id=sid)
        complete_stop(client, sid, turn['agent_turn_id'])
        assert client.post(f"/v1/sessions/{sid}/agent-turns/{turn['agent_turn_id']}/continue").json()['status'] == 'ready'
        say(client, sid, '先别执行刚才的操作', 'cancel-before-natural-resume')
        with db.connect() as c:
            assert c.execute('SELECT status FROM agent_objective').fetchone()[0] == 'cancelled'
            # Synthetic parent permission applies only to the fresh next input.
            c.execute('UPDATE privacy_preference SET cloud_model_text_allowed=1')
            c.commit()
        before = repo._get_plan_item_by_id('control-task-1')['active_seconds']
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        voice_id = capture(client, settings, monkeypatch, sid, quote, prompt, 'resume-after-c08-voice')
        resumed = interpret(client, sid, quote, 'resume-after-c08', voice_id)
        assert resumed['action']['type'] == 'resume_paused_task', resumed
        response = client.post(f"/v1/agent-turns/{resumed['agent_turn_id']}/execute")
        assert response.status_code == 200, response.text
        assert response.json()['execution']['capability'] == 'task.resume'
        assert response.json()['execution']['result']['plan_item_id'] == 'control-task-1'
        assert repo._get_plan_item_by_id('control-task-1')['status'] == 'active'
        assert repo._get_plan_item_by_id('control-task-1')['active_seconds'] == before
        assert repo._get_plan_item_by_id('control-task-2')['status'] == 'pending'
        assert client.post(f"/v1/agent-turns/{resumed['agent_turn_id']}/execute").status_code == 200
        with db.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='resumed'").fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
        assert len(posts) == 1
        sent = json.dumps(posts[0]['messages'], ensure_ascii=False)
        assert all(secret not in sent for secret in (TEXT, sid, 'stop-page', 'control-task-1', prompt['prompt_id'], prompt['item']['prompt']))
        assert set(json.loads(posts[0]['messages'][-1]['content'])) == {'transcript', 'state'}
