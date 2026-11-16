"""Actual local ASGI/SQLite and declared synthetic issuer, zero OS/cloud credit."""
import asyncio
from contextlib import contextmanager
from dataclasses import replace
import httpx
import pytest
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.model_gateway import ModelGateway
from home_study_agent import money_egress as money
from home_study_agent.money_r4.money_budget import BudgetHold
from test_learning_runtime_http import NoCloud
from test_model_gateway import request_for
from test_native_privacy_permit_http import certificate,signed_header
from privacy_restore_operation import restore_operation
from normal_provider_owner import normal_provider_owner,normal_provider_scope
KEY='a'*64 # Explicit public synthetic native key, not a renderer token/real secret.


def app_client(root):
    return TestClient(create_app(Settings(data_dir=root,database_path=root/'test.db',maintenance_enabled=False,
        data_encryption_key=b'0'*32,native_launch_attestation_key=KEY),model_gateway=NoCloud()))


def test_00_original_app_owner_closes_before_standalone_provider(tmp_path):
    with app_client(tmp_path)as client:
        owner=money._owner
        assert owner.closed is False and owner.active is False
    assert owner.closed is True and money._owner is owner and money._slot_mode=='NORMAL'
    with normal_provider_scope()as provider:
        assert provider is money._owner and provider is not owner and provider.check()is False
    assert provider.closed is True


def test_explicit_scope_cannot_take_over_live_app_owner(tmp_path):
    with app_client(tmp_path)as client:
        owner=money._owner
        with pytest.raises(AssertionError,match='TEST_PROVIDER_OWNER_ALREADY_LIVE'):
            with normal_provider_scope():raise AssertionError('must never enter')
        assert money._owner is owner and owner.closed is False


def test_closed_owner_still_refuses_late_physical_send_zero_posts(tmp_path,normal_provider_owner):
    posts=[]
    def transport(request):posts.append(request);return httpx.Response(200,request=request,json={})
    gateway=ModelGateway(Settings(data_dir=tmp_path,database_path=tmp_path/'test.db',model_api_key='synthetic'),transport=httpx.MockTransport(transport))
    normal_provider_owner.close()
    try:
        with pytest.raises(BudgetHold,match='MONEY_OWNER_CLOSED'):
            asyncio.run(gateway.complete(request_for([{'role':'user','content':'public synthetic transport'}])))
    finally:asyncio.run(gateway.close())
    assert posts==[] and money._owner is normal_provider_owner


@contextmanager
def restore_fixture(root):
    with app_client(root)as client:
        setup=client.post('/v1/parent/setup',json={'pin':'5837'});assert setup.status_code==200,setup.text
        headers={'X-HSC-Parent':setup.json()['parent_token']}
        backup=client.post('/v1/parent/backups',headers=headers);assert backup.status_code==200,backup.text
        path='/v1/parent/backups/restore'
        body={'backup_id':backup.json()['backup_id'],'confirmation':'确认恢复','operation':restore_operation(client,headers=headers)}
        proof=certificate(path,body,body['operation'],kind='restore',child_id=None,scope=None)
        db=Database(root/'test.db')
        with db.connect()as c:c.execute("UPDATE child_profile SET nickname='keep-current-public'WHERE child_id='child-primary-4'");c.commit()
        yield client,db,path,body,proof,headers


@pytest.mark.parametrize('case,expected',[
    ('unsigned','NATIVE_PRIVACY_PROOF_REQUIRED'),
    ('signature','NATIVE_PRIVACY_PROOF_INVALID'),
    ('body','NATIVE_PRIVACY_SCOPE_CHANGED'),
    ('generation','NATIVE_PRIVACY_GENERATION_CHANGED'),
    ('missing_parent',None),
    ('wrong_pin',None),
])
def test_synthetic_issuer_does_not_weaken_auth_body_or_generation(tmp_path,case,expected):
    with restore_fixture(tmp_path)as(c,db,path,body,proof,parent):
        send_body=body;send_proof=proof;headers={**parent,**signed_header(proof)}
        if case=='unsigned':headers=parent
        if case=='signature':headers={**parent,**signed_header({**proof,'signature':'0'*64})}
        if case=='body':send_body={**body,'confirmation':'different-public-command'}
        if case=='generation':
            send_body={**body,'operation':{**body['operation'],'expected_generation':'wrong-public-generation'}}
            send_proof=certificate(path,send_body,send_body['operation'],kind='restore',child_id=None,scope=None)
            headers={**parent,**signed_header(send_proof)}
        if case in('missing_parent','wrong_pin'):
            headers=signed_header(proof)
            if case=='wrong_pin':assert c.post('/v1/parent/unlock',json={'pin':'0000'}).status_code in{401,403}
        result=c.post(path,json=send_body,headers=headers)
        assert result.status_code==(409 if expected else 401),result.text
        if expected:assert expected in result.text
        with db.connect()as d:assert d.execute("SELECT nickname FROM child_profile WHERE child_id='child-primary-4'").fetchone()[0]=='keep-current-public'
        # Rejecting the bad request must not consume any original valid nonce.
        read=c.post('/v1/parent/native-privacy-permits/read',headers=parent,json=proof)
        assert read.status_code==200 and read.json()['status']=='missing',read.text


def test_normal_environment_has_no_implicit_native_key(tmp_path):
    assert Settings(data_dir=tmp_path,database_path=tmp_path/'never-open.db').native_launch_attestation_key is None
