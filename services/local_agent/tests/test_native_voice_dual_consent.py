from test_native_runtime_hmac_integration import native, env

def test_actual_parent_text_revocation_stops_the_existing_native_voice_policy(native):
    client,db,gateway,sid,parent,request,calls,faults=native
    assert client.get('/v1/private/money-egress').json()=={'version':1,'capture':'configured'}
    assert client.put('/v1/parent/privacy',headers=parent,json={'cloud_model_text_allowed':False,'cloud_audio_allowed':True}).status_code==200
    assert client.get('/v1/private/money-egress').json()=={'version':1,'capture':'bounded_segmented'}
    assert calls==[]
    assert client.put('/v1/parent/privacy',headers=parent,json={'cloud_model_text_allowed':True,'cloud_audio_allowed':True}).status_code==200
    assert client.get('/v1/private/money-egress').json()=={'version':1,'capture':'configured'}
