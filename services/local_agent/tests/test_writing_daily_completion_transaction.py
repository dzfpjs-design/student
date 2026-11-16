"""Real explicit completion, with faults at the actual write boundary."""
import json

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.agent_capability_executor import AgentCapabilityExecutor
from home_study_agent.repository import StudyRepository as Repository
from home_study_agent.voice_control_intent import VoiceControlIntent
from test_writing_daily_completion_http import environment, next_day_ready, completion_ready


@pytest.mark.parametrize('retry_change',['none','cancel','missing_receipt','wrong_segment','untrusted_failure'])
def test_completion_event_failure_rolls_back_task_and_old_obligation(completion_ready,retry_change):
    case=completion_ready
    with case.db.connect() as c:
        c.execute("""CREATE TRIGGER fail_writing_completion BEFORE INSERT ON plan_item_event
            WHEN NEW.event_type='completed' AND json_type(NEW.payload_json,'$.daily_writing_completion')='object'
            BEGIN SELECT RAISE(ABORT,'synthetic completion save failure'); END""")
        c.commit()
    failed=case.client.post(f"/v1/agent-turns/{case.completion_command['agent_turn_id']}/execute")
    assert failed.status_code==500,failed.text
    with case.db.connect() as c:
        assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?',(case.task['plan_item_id'],)).fetchone()[0]=='active'
        assert c.execute('SELECT status FROM agent_commitment WHERE commitment_id=?',(case.commitment['commitment_id'],)).fetchone()[0] in {'pending','due'}
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='completed'",(case.task['plan_item_id'],)).fetchone()[0]==0
        c.execute('DROP TRIGGER fail_writing_completion')
        c.commit()
    case.stack.close()
    with TestClient(create_app(case.settings,model_gateway=case.journey.gateway)) as client:
        if retry_change=='cancel':
            cancelled=client.post(f'/v1/sessions/{case.sid}/agent-turn',json={
                'transcript':'别讲了','client_action_id':'do-not-retry-completion','cloud_text_allowed':False})
            assert cancelled.status_code==200,cancelled.text
        elif retry_change!='none':
            with case.db.connect() as c:
                if retry_change=='missing_receipt':
                    c.execute("DELETE FROM plan_item_event WHERE plan_item_id=? AND json_extract(payload_json,'$.reason')='runtime_shutdown'",(case.task['plan_item_id'],))
                elif retry_change=='wrong_segment':
                    c.execute("UPDATE plan_item_event SET payload_json=json_set(payload_json,'$.active_segment_started_at','2020-01-01T00:00:00+00:00') WHERE plan_item_id=? AND event_type='paused'",(case.task['plan_item_id'],))
                else:
                    c.execute("UPDATE agent_turn SET tool_error_code='ValueError' WHERE agent_turn_id=?",(case.completion_command['agent_turn_id'],))
                    c.execute("UPDATE agent_step SET error_code='ValueError' WHERE plan_id IN (SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)",(case.completion_command['agent_turn_id'],))
                c.commit()
        retried=client.post(f"/v1/agent-turns/{case.completion_command['agent_turn_id']}/execute")
        if retry_change!='none':
            assert retried.status_code==409,retried.text
            with case.db.connect() as c:
                assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='completed'",(case.task['plan_item_id'],)).fetchone()[0]==0
                assert c.execute('SELECT status FROM agent_commitment WHERE commitment_id=?',(case.commitment['commitment_id'],)).fetchone()[0]!='completed'
            return
        assert retried.status_code==200,retried.text
        repeated=client.post(f"/v1/agent-turns/{case.completion_command['agent_turn_id']}/execute")
        assert repeated.status_code==200,repeated.text
        with case.db.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='completed'",(case.task['plan_item_id'],)).fetchone()[0]==1
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==2


def test_saved_completion_receipt_survives_failed_dispatch_result(completion_ready):
    case=completion_ready
    with case.db.connect() as c:
        c.execute("""CREATE TRIGGER fail_completion_reply BEFORE UPDATE OF status ON agent_step
            WHEN NEW.capability_name='task.complete' AND NEW.status='completed'
            BEGIN SELECT RAISE(ABORT,'synthetic final receipt failure'); END""")
        c.commit()
    failed=case.client.post(f"/v1/agent-turns/{case.completion_command['agent_turn_id']}/execute")
    assert failed.status_code==500,failed.text
    with case.db.connect() as c:
        assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?',(case.task['plan_item_id'],)).fetchone()[0]=='completed'
        c.execute('DROP TRIGGER fail_completion_reply')
        c.commit()
    case.stack.close()
    with TestClient(create_app(case.settings,model_gateway=case.journey.gateway)) as client:
        retried=client.post(f"/v1/agent-turns/{case.completion_command['agent_turn_id']}/execute")
        assert retried.status_code==200,retried.text
        assert retried.json()['execution']['recorded_receipt'] is True
        assert retried.json()['execution']['replay_audio_allowed'] is False
        with case.db.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='completed'",(case.task['plan_item_id'],)).fetchone()[0]==1
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==2


def test_late_cancel_does_not_revive_command_or_erase_actual_completion(completion_ready,monkeypatch):
    case=completion_ready
    dispatch=AgentCapabilityExecutor._dispatch
    def cancel_after_actual_commit(self,step):
        result=dispatch(self,step)
        if step['agent_turn_id']==case.completion_command['agent_turn_id']:
            with case.db.connect() as c:
                c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id=?",(step['agent_turn_id'],))
                c.execute("UPDATE agent_step SET status='cancelled' WHERE step_id=?",(step['step_id'],))
                c.commit()
        return result
    monkeypatch.setattr(AgentCapabilityExecutor,'_dispatch',cancel_after_actual_commit)
    refused=case.client.post(f"/v1/agent-turns/{case.completion_command['agent_turn_id']}/execute")
    assert refused.status_code==409,refused.text
    case.stack.close()
    with TestClient(create_app(case.settings,model_gateway=case.journey.gateway)) as client:
        retry=client.post(f"/v1/agent-turns/{case.completion_command['agent_turn_id']}/execute")
        assert retry.status_code==409,retry.text
        with case.db.connect() as c:
            assert c.execute('SELECT status FROM agent_plan WHERE agent_turn_id=?',(case.completion_command['agent_turn_id'],)).fetchone()[0]=='cancelled'
            assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?',(case.task['plan_item_id'],)).fetchone()[0]=='completed'
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='completed'",(case.task['plan_item_id'],)).fetchone()[0]==1


@pytest.mark.parametrize('fault',['source','journal'])
def test_completion_write_rechecks_after_dispatch_gate(completion_ready,monkeypatch,fault):
    case=completion_ready
    original=Repository.complete_plan_item
    reached=[]
    def changed(self,*args,**kwargs):
        c=kwargs['connection']
        assert c.in_transaction
        reached.append(True)
        if fault=='source':
            c.execute("UPDATE diagnostic_item SET prompt=prompt||' changed' WHERE diagnostic_item_id=?",(case.completion_prompt['diagnostic_item_id'],))
        else:
            key=c.execute("SELECT json_extract(context_manifest_json,'$.voice_control_binding.request_key') FROM agent_plan WHERE agent_turn_id=?",
                (case.completion_command['agent_turn_id'],)).fetchone()[0]
            c.execute("UPDATE voice_control_intent_request SET status='cancelled' WHERE request_key=?",
                (key,))
        return original(self,*args,**kwargs)
    monkeypatch.setattr(Repository,'complete_plan_item',changed)
    refused=case.client.post(f"/v1/agent-turns/{case.completion_command['agent_turn_id']}/execute")
    assert refused.status_code==409,refused.text
    assert reached
    with case.db.connect() as c:
        assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?',(case.task['plan_item_id'],)).fetchone()[0]=='active'
        assert c.execute('SELECT status FROM agent_commitment WHERE commitment_id=?',(case.commitment['commitment_id'],)).fetchone()[0] in {'pending','due'}
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='completed'",(case.task['plan_item_id'],)).fetchone()[0]==0


@pytest.mark.parametrize('quote',['还没写完','不要完成这项','如果我说做完了呢','“这项做完了”是什么意思','我说错了，先别结束'])
def test_complete_classifier_does_not_authorize_negation_or_quotation(quote):
    result=VoiceControlIntent.parse(json.dumps({'role':'control','control':'task.complete',
        'quote':quote,'confidence':.99}),quote)
    assert result['status']!='control'
