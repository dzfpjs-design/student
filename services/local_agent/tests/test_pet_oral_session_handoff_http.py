import json
import pytest
from test_pet_audio_vertical_http import tcp_client
from home_study_agent.app import create_app
from home_study_agent.voice_control_intent import VoiceControlIntent
from test_pet_oral_supply_http import setup,post
from test_oral_runtime_feedback_http import OralGateway,begin_oral,ANSWER
from test_oral_followup_execution_http import answer
from test_voice_runtime_binding import capture,interpret


def test_answered_oral_identity_does_not_trap_explicit_session_end(tmp_path,monkeypatch):
    db,settings=setup(tmp_path);gateway=OralGateway()
    with tcp_client(create_app(settings,model_gateway=gateway),{'X-HSC-Session':settings.session_token}) as client:
        client.headers['X-HSC-Parent']=post(client,'/v1/parent/setup',{'pin':'7391'})['parent_token']
        sid,prompt,_,_=begin_oral(client,db,gateway)
        answer(client,settings,monkeypatch,sid,prompt,gateway,ANSWER,'saved-before-end')
        gateway.quote='开始复盘';gateway.requested_control='session.request_end'
        voice=capture(client,settings,monkeypatch,sid,gateway.quote,prompt,'end-capture')
        turn=interpret(client,sid,gateway.quote,'end-command',voice)
        assert turn['runtime_capability']=='session.request_end',turn['action']
        assert turn['action']['type']=='request_end'
        assert turn['action']['local_only'] is True
        assert turn['action']['cloud_transmission_allowed'] is False
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]==1
            assert c.execute('SELECT state FROM daily_session WHERE session_id=?',(sid,)).fetchone()[0]=='study'
        # This control requests the existing handler, not a forged completion.
        assert turn['tool_status']=='pending'


@pytest.mark.parametrize('text',['先不要结束学习','我不想开始复盘','如果开始复盘会怎样','老师说“结束学习”','not now, do not end the session'])
def test_session_end_never_infers_consent_from_a_model_control_label(text):
    value={'role':'control','control':'session.request_end','quote':text,'confidence':.99}
    assert VoiceControlIntent.parse(json.dumps(value),text)['status']!='control'
