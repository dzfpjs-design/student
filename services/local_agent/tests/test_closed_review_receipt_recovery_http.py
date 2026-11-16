"""A lost outer acknowledgement must not create or speak a second review."""
from fastapi.testclient import TestClient

from home_study_agent.agent_capability_executor import AgentCapabilityExecutor
from home_study_agent.app import create_app
from test_closed_review_voice_http import flow, review_clock, settings_for
from test_natural_task_navigation_http import Offline, hear, execute


def test_committed_start_survives_lost_ack_and_restart(flow,monkeypatch):
    review_clock(flow,monkeypatch)
    settings=settings_for(flow)
    dispatch=AgentCapabilityExecutor._dispatch
    def lose_ack(self,step):
        result=dispatch(self,step)
        assert result['result']['status']=='active'
        raise OSError('synthetic lost outer acknowledgement')
    with TestClient(create_app(settings,model_gateway=Offline()),raise_server_exceptions=False) as client:
        heard,_=hear(client,settings,monkeypatch,flow['session'],'开始下一项')
        with monkeypatch.context() as patch:
            patch.setattr(AgentCapabilityExecutor,'_dispatch',lose_ack)
            response=client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute")
            assert response.status_code==500
        with flow['db'].connect() as c:
            original=c.execute('SELECT prompt_id FROM diagnostic_prompt WHERE origin_session_id=?',(flow['session'],)).fetchone()[0]
            assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?',(flow['item']['plan_item_id'],)).fetchone()[0]=='active'
    with TestClient(create_app(settings,model_gateway=Offline())) as client:
        recovered=execute(client,heard)['execution']
        assert recovered['recorded_receipt'] is True
        assert recovered['replay_audio_allowed'] is False
        resume,_=hear(client,settings,monkeypatch,flow['session'],'那我们继续吧','review-resume')
        result=execute(client,resume)['execution']['result']
        assert result['diagnostic_prompt']['prompt']['prompt_id']==original
        with flow['db'].connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_prompt WHERE origin_session_id=?',(flow['session'],)).fetchone()[0]==1
            assert c.execute("SELECT count(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='started'",(flow['item']['plan_item_id'],)).fetchone()[0]==1
