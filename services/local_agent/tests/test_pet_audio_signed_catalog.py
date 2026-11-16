import asyncio
import base64
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import subprocess
import shutil
from pathlib import Path
import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from home_study_agent import pet_audio_catalog as catalog
from home_study_agent.pet_audio_gateway import AudioEndpoint, PetAudioGateway, from_settings
from home_study_agent.config import Settings
from home_study_agent.pet_audio_work import PetAudioTeachingService
from home_study_agent.pet_audio_source import PetAudioAuthorityError
from test_pet_audio_work import audio_case, loopback
from test_pet_media_evidence import media, CHILD
from test_pet_audio_gateway import feedback, stream

NOW = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
PRIVATE = Ed25519PrivateKey.from_private_bytes(bytes([17])*32)  # Public synthetic test seed only.
KEYS = {'synthetic-only-key': {'issuer': 'Synthetic test maintainer', 'publicKeyPem': PRIVATE.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()}}


def payload(revision=2):
    return {'schema': 'pet_audio_catalog_v1', 'revision': revision, 'id': f'synthetic-catalog-{revision}',
        'verified_at': '2026-10-24T05:04:30Z', 'review_before': '2026-11-23T05:04:30Z',
        'region': 'cn-beijing', 'currency': 'CNY', 'endpoint_family': 'aliyun-compatible-cn-beijing-v1',
        'protocol': 'qwen_omni_audio_json_sse_v1', 'model': 'qwen3.5-omni-plus-2026-03-15',
        'text_input_cny_per_million': '7', 'audio_input_cny_per_million': '53', 'text_output_with_audio_cny_per_million': '40',
        'audio_tokens_per_second': 7, 'source_urls': ['https://help.aliyun.com/zh/model-studio/qwen3-5-omni-plus',
            'https://help.aliyun.com/zh/model-studio/qwen-omni'], 'evidence_sha256': 'a'*64}


def bundle(p=None):
    raw = json.dumps(p or payload(), sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()
    return json.dumps({'schema': 'pet_audio_catalog_signed_v1', 'key_id': 'synthetic-only-key',
        'payload_base64': base64.b64encode(raw).decode(), 'signature_base64': base64.b64encode(PRIVATE.sign(raw)).decode()}, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def test_dedicated_empty_root_blocks_import_but_original_compiled_tariff_still_works():
    with pytest.raises(ValueError, match='SIGNER_NOT_CONFIGURED'):
        catalog.verify_signed_catalog(bundle(), at=NOW)
    endpoint = AudioEndpoint('https://dashscope.aliyuncs.com/compatible-mode/v1', catalog.MODEL, 'synthetic-secret',
        input_second_micros=371, input_text_token_micros=7, output_token_micros=40,
        tariff_version=catalog.CATALOG_ID, catalog_id=catalog.CATALOG_ID, configuration_id='existing-configuration')
    assert catalog.current(endpoint, NOW)
    checked = catalog.verify_signed_catalog(bundle(), at=NOW, _trusted_keys=KEYS)
    golden=(Path(__file__).parents[3]/'src/test-fixtures/pet-reviewed-synthetic.hscatalog').read_bytes()
    assert catalog.verify_signed_catalog(golden,at=NOW,_trusted_keys=KEYS)['sha256']==hashlib.sha256(bundle()).hexdigest()
    assert checked['rates'] == (371, 7, 40)
    assert catalog.verify_signed_catalog(bundle({**payload(), 'text_input_cny_per_million': '7.000001'}), at=NOW, _trusted_keys=KEYS)['rates'][1] == 8


def test_manual_environment_prices_without_reviewed_catalogue_cannot_enable_production_gateway(tmp_path):
    settings=Settings(data_dir=tmp_path,database_path=tmp_path/'synthetic.db',pet_audio_enabled=True,
        pet_audio_base_url='https://dashscope.aliyuncs.com/compatible-mode/v1',pet_audio_model=payload()['model'],
        pet_audio_api_key='synthetic-secret',pet_audio_input_second_micros=1,pet_audio_input_text_token_micros=1,
        pet_audio_output_token_micros=1,pet_audio_tariff_version='unreviewed-self-reported')
    assert from_settings(settings) is None


@pytest.mark.parametrize('change', [{'region': 'us-east-1'}, {'currency': 'USD'}, {'protocol': 'unknown'},
    {'review_before': '2026-11-24T05:04:30Z'}, {'text_input_cny_per_million': '0'},
    {'text_input_cny_per_million': 7}, {'model': 'qwen-latest'}, {'audio_tokens_per_second': True}])
def test_signed_does_not_allow_unapproved_protocol_unknown_price_region_or_lifetime(change):
    with pytest.raises((ValueError, TypeError)):
        catalog.verify_signed_catalog(bundle({**payload(), **change}), at=NOW, _trusted_keys=KEYS)


def test_real_gateway_and_database_changed_catalog_revokes_old_grant_saves_reply_and_keeps_reserved_cost(audio_case, loopback, tmp_path, monkeypatch):
    monkeypatch.setattr(catalog, 'TRUSTED_KEYS', KEYS)
    class CatalogClock(datetime):
        @classmethod
        def now(cls,tz=None): return NOW
    monkeypatch.setattr(catalog,'datetime',CatalogClock)
    service, assessment, _, grant, original, _ = audio_case
    data = bundle(); file = tmp_path/'signed.hscatalog'; file.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    endpoint = AudioEndpoint('https://dashscope.aliyuncs.com/compatible-mode/v1', payload()['model'], 'synthetic-secret',
        input_second_micros=371, input_text_token_micros=7, output_token_micros=40, tariff_version=payload()['id'],
        catalog_id=payload()['id'], configuration_id='signed-configuration-first', catalog_file=str(file), catalog_sha256=digest)
    posts=[]
    def respond(request):
        body=json.loads(request.content); posts.append(body)
        assert 'enable_thinking' not in body and body['max_tokens']==1024 and body['modalities']==['text']
        assert hashlib.sha256(base64.b64decode(body['messages'][1]['content'][1]['input_audio']['data'].split(',',1)[1])).digest() == hashlib.sha256(original).digest()
        return httpx.Response(200, headers={'content-type':'text/event-stream'}, content=stream(json.dumps(feedback())).replace(b'synthetic-omni',endpoint.model.encode()))
    parsed=Settings(data_dir=tmp_path,database_path=tmp_path/'unused-configuration.db',pet_audio_enabled=True,
        pet_audio_base_url=endpoint.base_url,pet_audio_model=endpoint.model,pet_audio_api_key=endpoint.api_key,
        pet_audio_input_second_micros=371,pet_audio_input_text_token_micros=7,pet_audio_output_token_micros=40,
        pet_audio_catalog_id=endpoint.catalog_id,pet_audio_tariff_version=endpoint.tariff_version,
        pet_audio_configuration_id=endpoint.configuration_id,pet_audio_catalog_file=str(file),pet_audio_catalog_sha256=digest,
        pet_audio_control_file=str(tmp_path.resolve()/'pet-audio-catalog-control.bin'),pet_audio_control_state='ABSENT')
    configured=from_settings(parsed)
    assert configured and configured.endpoint.identity()==endpoint.identity()
    gateway=PetAudioGateway(configured.endpoint, transport=httpx.MockTransport(respond))
    service=PetAudioTeachingService(service.database,media_root=service.media_root,data_encryption_key=service.data_encryption_key,gateway=gateway,runtime_epoch='signed-process')
    with service.database.connect() as c:
        with pytest.raises(PetAudioAuthorityError, match='PURPOSE_CHANGED'): service._grant(c, CHILD)
    service.grant(CHILD,**{**grant,'expected_revision':1,'action_id':'new-signed-catalog-consent','request_limit_micros':200000,'day_limit_micros':500000})
    with service.database.connect() as c:
        c.execute('BEGIN')
        bound=service._source(c,CHILD,assessment['assessment_id'],live=True)
    expected_cost=endpoint.reserve_cost(duration_ms=bound['binding']['duration_ms'],question=bound['question'])
    work=service.enqueue(CHILD,assessment['assessment_id'])
    result=asyncio.run(service.run(CHILD,work['work_id']))
    assert result['state']=='response_saved' and len(posts)==1
    reopened=PetAudioTeachingService(service.database,media_root=service.media_root,data_encryption_key=service.data_encryption_key,gateway=gateway,runtime_epoch='reopened-process')
    assert asyncio.run(reopened.run(CHILD,work['work_id']))==result and len(posts)==1
    with service.database.connect() as c:
        row=c.execute('SELECT * FROM pet_audio_dispatch').fetchall()[-1]
        assert row['cost_micros']==expected_cost and row['state']=='settled'
        assert c.execute('SELECT count(*) FROM pet_audio_grant').fetchone()[0]==2
    next_file=tmp_path/'next.hscatalog'; next_bytes=bundle(payload(3)); next_file.write_bytes(next_bytes)
    changed=replace(endpoint,catalog_id='synthetic-catalog-3',tariff_version='synthetic-catalog-3',configuration_id='next-configuration',catalog_file=str(next_file),catalog_sha256=hashlib.sha256(next_bytes).hexdigest())
    newer=PetAudioTeachingService(service.database,media_root=service.media_root,data_encryption_key=service.data_encryption_key,gateway=PetAudioGateway(changed,transport=httpx.MockTransport(respond)),runtime_epoch='changed-catalog')
    with service.database.connect() as c:
        with pytest.raises(PetAudioAuthorityError,match='PURPOSE_CHANGED'): newer._grant(c,CHILD)
    assert len(posts)==1 and not loopback['posts']


@pytest.fixture(scope="module")
def actual_main_failure_bridge(tmp_path_factory):
    # Normal repository layout, never an operation-directory compiled artifact.
    root = Path(__file__).resolve().parents[3]
    fixture = root / 'services/local_agent/tests/fixtures/pet_catalog_fault/build.mjs'
    output = (tmp_path_factory.mktemp('pet-catalog-current-main') / 'fault-main.mjs').resolve()
    node = shutil.which('node')
    assert node is not None and fixture.is_file()
    built = subprocess.run([node, str(fixture), str(output)], cwd=root,
        capture_output=True, text=True, timeout=30)
    assert built.returncode == 0 and built.stderr == '', built.stderr[-1500:]
    receipt = json.loads(built.stdout)
    output.with_suffix('.build-receipt.json').write_text(built.stdout, encoding='utf-8')
    assert receipt['synthetic_only'] and receipt['bundle_source'] == 'CURRENT_CHECKOUT_TYPESCRIPT'
    assert receipt['output_sha256'] == hashlib.sha256(output.read_bytes()).hexdigest()
    for name in ('src/main/pet-audio-catalog.ts', 'src/main/pet-audio-settings.ts', 'src/shared/petAudioTariffs.ts'):
        assert receipt['production_inputs_sha256'][name] == hashlib.sha256((root/name).read_bytes()).hexdigest()
    return node, output, root/'src/test-fixtures/pet-reviewed-synthetic.hscatalog'


@pytest.mark.parametrize('failure', ['encrypt','rename'])
@pytest.mark.parametrize('already_sent', [True,False])
def test_live_original_gateway_sees_actual_main_failure_fence_without_new_posts_or_reserves_but_sent_work_settles(audio_case, loopback, tmp_path, failure, already_sent, actual_main_failure_bridge):
    service, assessment, _, grant, _, _=audio_case
    desktop=tmp_path.resolve()/'desktop';desktop.mkdir()
    control=desktop/'pet-audio-catalog-control.bin'
    endpoint=AudioEndpoint('https://dashscope.aliyuncs.com/compatible-mode/v1',catalog.MODEL,'synthetic-secret',
        input_second_micros=371,input_text_token_micros=7,output_token_micros=40,tariff_version=catalog.CATALOG_ID,
        catalog_id=catalog.CATALOG_ID,configuration_id='existing-compiled-config',control_file=str(control),control_state='ABSENT')
    node, bridge, golden = actual_main_failure_bridge
    posts=[];main_facts=[]
    def fail_main():
        child=subprocess.run([node,str(bridge),str(desktop),failure,str(golden)],capture_output=True,text=True,timeout=10)
        assert child.returncode==0 and child.stderr==''
        fact=json.loads(child.stdout);main_facts.append(fact)
        assert fact['original_error']=='SYNTHETIC_SETTINGS_'+failure.upper()+'_FAILED'
        assert fact['revoked']==1 and fact['applied']==0 and fact['floor_exists'] and not fact['summary_enabled']
    async def respond(request):
        raw=await request.aread();posts.append(hashlib.sha256(raw).hexdigest())
        fail_main() # Actual Main store/handler encrypt or rename exception, after this body was sent.
        return httpx.Response(200,headers={'content-type':'text/event-stream'},content=stream(json.dumps(feedback())).replace(b'synthetic-omni',endpoint.model.encode()))
    gateway=PetAudioGateway(endpoint,transport=httpx.MockTransport(respond))
    service=PetAudioTeachingService(service.database,media_root=service.media_root,data_encryption_key=service.data_encryption_key,gateway=gateway,runtime_epoch='already-running-controlled-gateway')
    service.grant(CHILD,**{**grant,'expected_revision':1,'action_id':'explicit-current-catalog-consent','request_limit_micros':200000,'day_limit_micros':500000})
    work=service.enqueue(CHILD,assessment['assessment_id'])
    with service.database.connect() as c:
        c.execute('BEGIN');original=service._source(c,CHILD,assessment['assessment_id'],live=True)
    original_identity=endpoint.identity();expected=endpoint.reserve_cost(duration_ms=original['binding']['duration_ms'],question=original['question'])
    if already_sent:
        result=asyncio.run(service.run(CHILD,work['work_id']))
        assert result['state']=='response_saved' and len(posts)==1
        # The opaque revocation gate does not alter old immutable identity or the original save/settle checks.
        assert endpoint.identity()==original_identity
        assert asyncio.run(service.run(CHILD,work['work_id']))==result and len(posts)==1
    else:
        claim=service._claim(CHILD,work['work_id']);fail_main()
        with pytest.raises(ValueError,match='CONTROL_CHANGED') as caught:
            asyncio.run(gateway.feedback(question=claim['original']['question'],audio=claim['original']['audio'],duration_ms=claim['original']['binding']['duration_ms'],authorize=lambda phase: service._authorize(CHILD,work['work_id'],claim,phase)))
        service._failed(CHILD,work['work_id'],claim,caught.value)
        assert not posts # A reservation without a sent body confers no continued-dispatch right.
    for operation in (lambda: service.require_new_use(),lambda: service.enqueue(CHILD,assessment['assessment_id']),
                      lambda: endpoint.reserve_cost(duration_ms=1000,question='new reserve'),
                      lambda: service.grant(CHILD,**{**grant,'expected_revision':2,'action_id':'must-not-grant-after-fence'})):
        with pytest.raises(ValueError,match='CONTROL_CHANGED'):operation()
    from fastapi import FastAPI
    from home_study_agent.parent_auth import ParentAuth
    from home_study_agent.pet_audio_http import create_router
    parent=ParentAuth(service.database);token=parent.setup('7391')
    app=FastAPI();app.include_router(create_router(service,parent,local_session_dependency=lambda: None))
    async def original_permission_http():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://synthetic-local-session',headers={'X-HSC-Parent':token}) as client:
            route=f'/v1/parent/children/{CHILD}/pet-audio/authorization'
            status=await client.get(route);assert status.status_code==200 and status.json()['current'] is False
            denied=await client.post(route,json={**grant,'expected_revision':2,'action_id':'http-must-not-grant-after-fence'})
            assert denied.status_code==409 and denied.json()['detail']['code']=='PET_CATALOG_CONTROL_CHANGED'
    asyncio.run(original_permission_http())
    with service.database.connect() as c:
        rows=c.execute('SELECT cost_micros,state FROM pet_audio_dispatch').fetchall()
        assert len(rows)==1 and rows[0]['cost_micros']==expected and rows[0]['state']==('settled' if already_sent else 'released')
        assert c.execute('SELECT count(*) FROM pet_audio_grant').fetchone()[0]==2
    assert len(main_facts)==1 and not loopback['posts']


def test_managed_guard_absent_changed_deleted_corrupt_or_aliased_is_revocation_not_catalog_trust(tmp_path):
    root=tmp_path.resolve();control=root/'pet-audio-catalog-control.bin'
    catalog.require_control(str(control),'ABSENT')
    control.write_bytes(b'opaque-synthetic-dpapi')
    digest=hashlib.sha256(control.read_bytes()).hexdigest()
    catalog.require_control(str(control),digest)
    with pytest.raises(ValueError):catalog.require_control(str(control),'ABSENT')
    control.write_bytes(b'changed')
    with pytest.raises(ValueError):catalog.require_control(str(control),digest)
    control.unlink()
    with pytest.raises(ValueError):catalog.require_control(str(control),digest)
    control.symlink_to(root/'missing')
    with pytest.raises(ValueError):catalog.require_control(str(control),'ABSENT')
    with pytest.raises(ValueError):catalog.require_control(str(root/'wrong-leaf'),'ABSENT')
    with pytest.raises(ValueError):catalog.verify_signed_catalog(bundle({**payload(),'id':12345678}),at=NOW,_trusted_keys=KEYS)
    settings=Settings(data_dir=tmp_path,database_path=tmp_path/'unstarted.db',pet_audio_enabled=True,
        pet_audio_base_url='https://dashscope.aliyuncs.com/compatible-mode/v1',pet_audio_model=catalog.MODEL,pet_audio_api_key='synthetic-secret',
        pet_audio_catalog_id=catalog.CATALOG_ID,pet_audio_configuration_id='existing-compiled-config',pet_audio_tariff_version=catalog.CATALOG_ID,
        pet_audio_input_second_micros=371,pet_audio_input_text_token_micros=7,pet_audio_output_token_micros=40)
    assert from_settings(settings) is None # Even a legal compiled tariff needs its managed desktop revocation marker.
