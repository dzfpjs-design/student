"""Actual service/SQLite/HTTP, public synthetic WAV and stub local decoder; no model/audio output."""
import asyncio
import base64
import hashlib
import json
from io import BytesIO
import wave
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from home_study_agent import local_playback_control as control_module
from home_study_agent.pet_media_evidence import PetMediaEvidenceError, PetMediaEvidenceService
from home_study_agent.pet_media_http import create_pet_media_router
from home_study_agent.parent_auth import ParentAuth
from test_pet_media_evidence import media, begin, change, CHILD
from test_pet_material_supply import supply, materialize, bind


def approve(media, row, *, full=False, key='review', operation='approve'):
    return media[1].review_listening_source(media[2], media_id=row['media_id'],
        audio_sha256=row['audio']['sha256'], operation=operation, reviewer_id='public-parent',
        reviewer_role='parent', client_action_id=key, full_spoken_reference=full)


def resume(media, row, key='resume'):
    return media[1].control_listening(media[2], media_id=row['media_id'], generation=row['generation'],
        expected_revision=row['revision'], command='resume', client_action_id=key)


def capsule(media, row):
    return media[1].playback_control_reference(media[2], media_id=row['media_id'], audio_sha256=row['audio']['sha256'])


def stop_payload(reference):
    b=BytesIO()
    with wave.open(b,'wb') as w:
        w.setparams((1,2,16000,0,'NONE','not compressed'));w.writeframes(b'\x01\x00'*8000)
    return control_module.PlaybackControlRequest(child_id=reference['child_id'],
        playback_lease_id='00000000-0000-4000-8000-000000000001', playback_generation=1,
        capture_epoch=1, source_kind='local_pcm', pcm_reference=reference,
        wav_base64=base64.b64encode(b.getvalue()).decode())


class LocalDecoder:
    def __init__(self, sid, child=CHILD):self.sid=sid;self.child=child;self.calls=[];self.after=None;self.text='停一下'
    def _session(self, sid):return dict(session_id=sid,child_id=self.child,status='active')
    def _recognize_wav(self, wav, **kwargs):
        self.calls.append(kwargs)
        if self.after:self.after()
        return self.text,500,'synthetic-local-decoder-no-model',False


def recognize(media, ref, decoder=None):
    decoder=decoder or LocalDecoder(media[2])
    service=control_module.LocalPlaybackControl(decoder, pcm_reference_resolver=media[1].resolve_playback_control_reference)
    return asyncio.run(service.recognize(media[2],stop_payload(ref))),decoder


def counts(db):
    with db.connect() as c:
        return {table:c.execute('SELECT count(*) FROM '+table).fetchone()[0]
            for table in ['voice_input','model_call','diagnostic_attempt','pet_media_event']}


def test_existing_source_approval_is_not_full_audio_attestation(media):
    row=resume(media,approve(media,begin(media)))
    with pytest.raises(PetMediaEvidenceError,match='full_audio_spoken_reference_unavailable'):capsule(media,row)


def test_complete_generated_script_and_audio_hash_qualify_without_parent_text_upload(supply):
    row=resume(supply,approve(supply,bind(supply,materialize(supply))))
    ref=capsule(supply,row); before=counts(supply[0])
    result,decoder=recognize(supply,ref)
    assert result['control']=='stop_playback' and result['persisted'] is False and result['learning_evidence'] is False
    assert decoder.calls==[dict(prefer_cloud=False,session=None,purpose='agent_command')]
    assert counts(supply[0])==before
    assert not any(key in ref for key in ('text','stimulus_text','current_playback_text','expected_answer','local_path'))
    assert ref['spoken_text_sha256']==hashlib.sha256(supply[3]['item']['stimulus_text'].encode()).hexdigest()


def test_explicit_whole_audio_attestation_is_durable_and_retraction_revokes(media):
    row=resume(media,approve(media,begin(media),full=True)); ref=capsule(media,row)
    before=counts(media[0]); assert recognize(media,ref)[0]['control']=='stop_playback';assert counts(media[0])==before
    restarted=PetMediaEvidenceService(media[0],media_root=media[4].parent,runtime_epoch='new-process')
    with pytest.raises(PetMediaEvidenceError,match='owner_changed'):
        restarted.playback_control_reference(media[2],media_id=row['media_id'],audio_sha256=row['audio']['sha256'])
    current=restarted.read(media[2],media_id=row['media_id'])
    newmedia=(media[0],restarted,*media[2:]); newrow=resume(newmedia,current,'explicit-recovery')
    newref=capsule(newmedia,newrow);assert newref['source_review_event_id']==ref['source_review_event_id'];assert newref['generation']!=ref['generation']
    approve(newmedia,newrow,key='retract',operation='retract')
    result,decoder=recognize(newmedia,newref);assert result['control'] is None and decoder.calls==[]


@pytest.mark.parametrize('field', ['child_id','session_id','media_id','prompt_id','item_version','diagnostic_item_id','source_version','source_review_event_id','spoken_text_sha256','audio_sha256','generation'])
def test_forged_or_stale_capsule_never_decodes(media,field):
    row=resume(media,approve(media,begin(media),full=True));ref=capsule(media,row)
    ref[field]=ref[field]+1 if field=='generation' else ('b'*64 if field.endswith('sha256') else 'foreign')
    result,decoder=recognize(media,ref);assert result['control'] is None;assert decoder.calls==[]


def test_source_and_review_changed_during_local_decode_cannot_stop(media):
    row=resume(media,approve(media,begin(media),full=True));ref=capsule(media,row)
    decoder=LocalDecoder(media[2]);decoder.after=lambda:approve(media,row,key='late-retract',operation='retract')
    result,_=recognize(media,ref,decoder);assert result['control'] is None;assert len(decoder.calls)==1


def test_full_reference_echo_is_not_truncated_or_substituted_by_question(media):
    # Actual fixture source is changed before prompt preparation, preserving normal version checks.
    from home_study_agent.diagnostic_prompt import DiagnosticPromptService
    old=media[3]
    service=DiagnosticPromptService(media[0]);service.cancel(media[2],prompt_id=old['prompt_id'],item_version=old['item_version'])
    text='完整原录音内容。'*400+'停一下'
    change(media[0],'UPDATE diagnostic_item SET stimulus_text=? WHERE diagnostic_item_id=?',(text,old['diagnostic_item_id']))
    prompt=service.prepare(media[2],track='pet',preferred_item_id=old['diagnostic_item_id'],client_action_id='new') ['prompt']
    actual=(media[0],media[1],media[2],prompt,*media[4:])
    row=resume(actual,approve(actual,begin(actual),full=True));ref=capsule(actual,row)
    assert recognize(actual,ref)[0]['status']=='echo_rejected'


def test_real_http_parent_auth_full_attestation_and_loopback_stop(media):
    auth=ParentAuth(media[0]);token=auth.setup('123456')
    def local(req:Request):
        if req.headers.get('X-HSC-Session')!='local-test-token':raise HTTPException(401)
    decoder=LocalDecoder(media[2]);app=FastAPI()
    app.include_router(create_pet_media_router(media[1],auth,local_session_dependency=local))
    app.include_router(control_module.create_router(decoder,local,media[1]))
    client=TestClient(app,client=('127.0.0.1',45000));client.headers['X-HSC-Session']='local-test-token'
    row=begin(media);base=f'/v1/sessions/{media[2]}/pet-media'
    review=dict(audio_sha256=row['audio']['sha256'],operation='approve',client_action_id='http-full',full_spoken_reference=True)
    assert client.post(base+'/'+row['media_id']+'/source-review',json=review).status_code==401
    headers={'X-HSC-Parent':token}
    assert client.post(base+'/'+row['media_id']+'/source-review',json={**review,'stimulus_text':'caller text'},headers=headers).status_code==422
    approved=client.post(base+'/'+row['media_id']+'/source-review',json=review,headers=headers)
    assert approved.status_code==200
    assert client.post(base+'/'+row['media_id']+'/source-review',json=review,headers=headers).json()==approved.json()
    row=resume(media,approved.json()['media']);ref=client.get(base+'/'+row['media_id']+'/control-reference',params={'audio_sha256':row['audio']['sha256']})
    assert ref.status_code==200 and ref.headers['cache-control']=='no-store'
    before=counts(media[0]);result=client.post(f'/v1/sessions/{media[2]}/voice/local-playback-control',json=stop_payload(ref.json()).model_dump(mode='json'))
    assert result.status_code==200 and result.json()['control']=='stop_playback';assert counts(media[0])==before
    remote=TestClient(app,client=('192.0.2.1',4000));remote.headers.update(client.headers)
    assert remote.get(base+'/'+row['media_id']+'/control-reference',params={'audio_sha256':row['audio']['sha256']}).status_code==403

@pytest.mark.parametrize('fault',['source','cancel','review','audio'])
def test_current_source_control_review_or_audio_change_refuses_old_reference(media,fault):
    row=resume(media,approve(media,begin(media),full=True));ref=capsule(media,row)
    if fault=='source':change(media[0], 'UPDATE diagnostic_item SET stimulus_text=? WHERE diagnostic_item_id=?',('changed actual source',media[3]['diagnostic_item_id']))
    elif fault=='cancel':media[1].control_listening(media[2],media_id=row['media_id'],generation=row['generation'],expected_revision=row['revision'],command='cancel',client_action_id='cancel')
    elif fault=='review':approve(media,row,full=True,key='another-full-review')
    else:media[4].write_bytes(media[4].read_bytes()+b'changed')
    result,decoder=recognize(media,ref);assert result['control'] is None and decoder.calls==[]

@pytest.mark.parametrize('field,value',[('local_only',1),('cloud_transmission_allowed',0),('generation',True)])
def test_reference_numeric_alias_cannot_replace_strict_flags_or_generation(media,field,value):
    from pydantic import ValidationError
    row=resume(media,approve(media,begin(media),full=True));ref=capsule(media,row);ref[field]=value
    with pytest.raises(ValidationError):stop_payload(ref)


def test_readiness_requires_full_reference_before_any_child_clock_or_media_generation(media):
    from home_study_agent.pet_listening_admission import resolve_pet_listening_resource
    from home_study_agent.database import LocalMediaRuntime
    media[0].local_media_runtime=LocalMediaRuntime(media[4].parent,None)
    row=approve(media,begin(media));before=counts(media[0])
    with media[0].connect() as c:
        result=resolve_pet_listening_resource(c,media[2],CHILD,media[3]['prompt_id'],media[3]['item_version'])
    assert result['status']=='awaiting_resources' and result['reason']=='full_audio_spoken_reference_unavailable'
    row=approve(media,row,full=True,key='complete-script')
    with media[0].connect() as c:
        ready=resolve_pet_listening_resource(c,media[2],CHILD,media[3]['prompt_id'],media[3]['item_version'])
    assert ready['status']=='ready';assert media[1].read(media[2],media_id=row['media_id'])['generation']==row['generation']
    assert counts(media[0])['diagnostic_attempt']==before['diagnostic_attempt']==0


def test_new_control_reference_requirement_does_not_rewrite_historical_delivery(media):
    from test_pet_media_evidence import complete
    row=complete(media);before=counts(media[0])
    assert row['qualification']['listening_delivery_qualified'] is True
    assert row['qualification']['full_audio_control_reference_available'] is False
    assert row['status']=='playback_finished'
    with pytest.raises(PetMediaEvidenceError):capsule(media,row)
    assert counts(media[0])==before


def test_preexisting_original_answer_grade_and_all_rows_survive_missing_stop_reference(media):
    from test_pet_scoped_listening import twice
    from home_study_agent.diagnostic_prompt import DiagnosticPromptService
    from home_study_agent.database import LocalMediaRuntime
    media[0].local_media_runtime=LocalMediaRuntime(media[4].parent,None)
    row=twice(media)
    answer=DiagnosticPromptService(media[0]).answer(media[2],prompt_id=media[3]['prompt_id'],item_version=media[3]['item_version'],
        answer='in front of the library',client_action_id='saved-original-before-new-stop-reference')
    with media[0].connect() as c:
        assert c.execute('SELECT correctness FROM diagnostic_attempt WHERE child_id=? AND session_id=? AND diagnostic_item_id=?',(CHILD,media[2],media[3]['diagnostic_item_id'])).fetchone()[0]==1
    def whole_database_digest():
        with media[0].connect() as c:return hashlib.sha256('\n'.join(c.iterdump()).encode()).hexdigest()
    before=whole_database_digest();current=media[1].read(media[2],media_id=row['media_id'])
    assert current['qualification']['listening_delivery_qualified'] is True
    assert current['qualification']['full_audio_control_reference_available'] is False
    with pytest.raises(PetMediaEvidenceError):capsule(media,current)
    assert whole_database_digest()==before
