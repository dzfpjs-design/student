from dataclasses import replace
from datetime import datetime,timedelta,timezone

from home_study_agent.app import create_app
from test_pet_oral_supply_http import setup,post,CHILD
from test_oral_runtime_feedback_http import OralGateway
from test_pet_audio_vertical_http import tcp_client
from test_pet_audio_work import loopback


def test_real_tcp_grant_commit_lost_receipt_restarts_and_recovers_exact_key(tmp_path,loopback):
    db,settings=setup(tmp_path)
    settings=replace(settings,data_encryption_key=b'P'*32)
    kwargs=dict(model_gateway=OralGateway(),pet_audio_gateway=loopback['gateway'])
    headers={'X-HSC-Session':settings.session_token}
    path=f'/v1/parent/children/{CHILD}/pet-audio/authorization'
    body=dict(allowed=True,expires_at=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat().replace('+00:00','Z'),
        day_limit_micros=20000,request_limit_micros=10000,expected_revision=0,action_id='preview-grant-lost-response')
    with tcp_client(create_app(settings,**kwargs),headers) as client:
        client.headers['X-HSC-Parent']=post(client,'/v1/parent/setup',{'pin':'7391'})['parent_token']
        assert client.put('/v1/parent/privacy',json={'raw_media_storage_allowed':True,'cloud_model_text_allowed':True,'cloud_audio_allowed':True}).status_code==200
        # Actual HTTP POST commits; the caller deliberately discards its body.
        committed=client.post(path,json=body)
        assert committed.status_code==200
        committed.close()
    with tcp_client(create_app(settings,**kwargs),headers) as client:
        client.headers['X-HSC-Parent']=post(client,'/v1/parent/unlock',{'pin':'7391'})['parent_token']
        receipt=client.get(path+'/actions/'+body['action_id'])
        assert receipt.status_code==200
        saved=receipt.json()
        assert saved['state']=='applied' and saved['child_id']==CHILD
        assert saved['grant']['action_id']==body['action_id'] and saved['grant']['revision']==1
        assert saved['grant']['day_limit_micros']==20000 and saved['grant']['request_limit_micros']==10000
        assert client.get(path.replace(CHILD,'child-junior-3')+'/actions/'+body['action_id']).json()['state']=='missing'
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM pet_audio_grant').fetchone()[0]==1
            assert c.execute('SELECT count(*) FROM pet_audio_dispatch').fetchone()[0]==0
    assert not loopback['posts']
