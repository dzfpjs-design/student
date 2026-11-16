"""Current real Runtime/HTTP preparation; public fixture WAV, stub planner/decoder only."""
import json
import pytest
from test_exam_listening_delivery_integration import case, sufficient_full_school_fixture
from test_pet_listening_resource_admission import bind_audio
from test_pet_vocabulary_prompt_integration import turn
from home_study_agent.pet_media_evidence import PetMediaEvidenceService
from test_pcm_reference import stop_payload, counts


def test_full_school_exam_waits_for_whole_reference_then_first_actual_resume_and_canonical_stop(case,monkeypatch):
    db,sid,service,old,client,planner,oldprompt,source,mono,root=case
    service.cancel(old['run_id']);sufficient_full_school_fixture(case,monkeypatch)
    planner.select('exam.start','开始英语模拟卷',arguments={})
    planned=turn(client,sid,'开始英语模拟卷','full-school-control-reference-start')
    started=client.post(f"/v1/agent-turns/{planned['agent_turn_id']}/execute")
    assert started.status_code==200,started.text
    run=started.json()['execution']['result'];assert run['total_items']==6 and run['started_at'] is None
    token=client.post('/v1/parent/unlock',json={'pin':'2468'}).json()['parent_token']
    prompts=client.get(f"/v1/parent/children/{oldprompt['child_id']}/pet-prompts",headers={'X-HSC-Parent':token}).json()['prompts']
    prompt,=[p for p in prompts if p.get('exam_slot',{}).get('run_id')==run['run_id'] and p['status']=='waiting_answer']
    row,_=bind_audio(client,db,root,sid,prompt,key='full-reference-media',full_reference=False)
    assert row['qualification']['full_audio_control_reference_available'] is False
    preparing=service.status(run['run_id'])
    assert preparing['delivery_status']=='preparing' and preparing['started_at'] is None and preparing['elapsed_seconds']==0
    assert any(b['reason']=='full_audio_spoken_reference_unavailable' for b in preparing['delivery_blockers'])
    base=f'/v1/sessions/{sid}/pet-media'
    full=client.post(base+'/'+row['media_id']+'/source-review',headers={'X-HSC-Parent':token},json={'audio_sha256':row['audio']['sha256'],'operation':'approve','full_spoken_reference':True,'client_action_id':'complete-whole-audio-reference'})
    assert full.status_code==200,full.text
    # No incidental GET status between final parent review and the child's first resume.
    planner.select('exam.resume','继续模拟卷',arguments={})
    resumed_turn=turn(client,sid,'继续模拟卷','first-reference-ready-resume')
    resumed=client.post(f"/v1/agent-turns/{resumed_turn['agent_turn_id']}/execute")
    assert resumed.status_code==200,resumed.text
    current=resumed.json()['execution']['result'];assert current['status']=='active'
    assert current['total_items']==6 and current['started_at'] is not None
    assert source['stimulus_text'] not in json.dumps(current,ensure_ascii=False)
    future=client.get(base+'/'+row['media_id']+'/control-reference',params={'audio_sha256':row['audio']['sha256']})
    assert future.status_code==409  # Not the current slot/playing generation.
    assert counts(db)['diagnostic_attempt']==0


def test_current_exam_capsule_is_canonical_private_and_stop_only(case,monkeypatch):
    db,sid,service,run,client,planner,prompt,source,mono,root=case
    row,_=bind_audio(client,db,root,sid,prompt,key='current-full-reference-media')
    token=client.post('/v1/parent/unlock',json={'pin':'2468'}).json()['parent_token']
    base=f'/v1/sessions/{sid}/pet-media'
    full=client.post(base+'/'+row['media_id']+'/source-review',headers={'X-HSC-Parent':token},json={'audio_sha256':row['audio']['sha256'],'operation':'approve','full_spoken_reference':True,'client_action_id':'current-whole-audio-reference'})
    assert full.status_code==200,full.text
    planner.select('exam.resume','继续模拟卷',arguments={})
    resumed_turn=turn(client,sid,'继续模拟卷','current-reference-ready-resume')
    resumed=client.post(f"/v1/agent-turns/{resumed_turn['agent_turn_id']}/execute")
    assert resumed.status_code==200,resumed.text
    current=resumed.json()['execution']['result'];assert current['status']=='active'
    cue=current['audio_delivery'];assert cue['media_prompt']['item']['stimulus_text'] is None
    row=client.get(base+'/'+row['media_id']).json()['media']
    admitted=client.post(base+'/listening/control',json=dict(media_id=row['media_id'],generation=row['generation'],expected_revision=row['revision'],command='resume',client_action_id='actual-new-PCM-play-admission'))
    assert admitted.status_code==200,admitted.text
    row=admitted.json()['media'];ref=client.get(base+'/'+row['media_id']+'/control-reference',params={'audio_sha256':row['audio']['sha256']})
    assert ref.status_code==200,ref.text
    ref=ref.json();assert ref['prompt_id']==cue['media_prompt']['prompt_id'];assert ref['diagnostic_item_id']==cue['media_item_id'];assert ref['item_version']==cue['media_prompt']['item_version']
    assert source['stimulus_text'] not in json.dumps(ref,ensure_ascii=False)
    calls=[]
    monkeypatch.setattr(client.app.state.media_ai,'_recognize_wav',lambda wav,**kwargs:(calls.append(kwargs) or ('停一下',500,'synthetic-test-decoder-no-model',False)))
    before=counts(db)
    recognized=client.post(f'/v1/sessions/{sid}/voice/local-playback-control',json=stop_payload(ref).model_dump(mode='json'))
    assert recognized.status_code==200 and recognized.json()['control']=='stop_playback',recognized.text
    assert calls==[dict(prefer_cloud=False,session=None,purpose='agent_command')]
    assert counts(db)==before and counts(db)['diagnostic_attempt']==0
    # Recognition is a stop-output grant, never an exam.pause or a completed listening receipt.
    assert service.status(run['run_id'])['status']=='active'

from test_exam_listening_delivery_integration import group_case, activate, answer, play

def test_shared_second_slot_uses_full_canonical_original_not_current_slot_or_its_version(group_case):
    case,published,supply,headers=group_case
    db,sid,service,run,client,planner,prompt,source,mono,root=case
    row,_=bind_audio(client,db,root,sid,prompt,key='shared-complete-reference')
    activate(case)
    base=f'/v1/sessions/{sid}/pet-media'
    row=client.get(base+'/'+row['media_id']).json()['media']
    play(client,sid,row,mono,'shared-real-service-receipts-not-native')
    first,_=answer(case,'first-shared-original-answer')
    assert first.status_code==200,first.text
    current=service.status(run['run_id']);cue=current['audio_delivery']
    assert current['current_position']==2 and cue['item_id']!=cue['media_item_id']
    # An explicit, existing original-audio restart is a current unit permission,
    # not a forced replay or a manufactured ended event/score.
    row=client.get(base+'/'+row['media_id']).json()['media']
    restarted=client.post(base+'/listening/control',json=dict(media_id=row['media_id'],generation=row['generation'],expected_revision=row['revision'],command='restart',client_action_id='explicit-shared-repeat'))
    assert restarted.status_code==200,restarted.text
    row=restarted.json()['media'];ref=client.get(base+'/'+row['media_id']+'/control-reference',params={'audio_sha256':row['audio']['sha256']})
    assert ref.status_code==200,ref.text
    ref=ref.json();assert ref['diagnostic_item_id']==cue['media_item_id'] and ref['prompt_id']==cue['prompt_id'] and ref['item_version']==cue['item_version']
    with db.connect() as c:
        current_version=c.execute('SELECT item_version FROM diagnostic_prompt WHERE prompt_id=?',(f"exam-media:{run['run_id']}:2",)).fetchone()[0]
    assert current_version!=ref['item_version']
    service.pause(run['run_id'])
    with pytest.raises(ValueError):client.app.state.pet_media.resolve_playback_control_reference(sid,prompt['child_id'],ref)
