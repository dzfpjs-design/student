"""Published listening -> real resource wait -> fresh-authority original resume."""
import json
from synthetic_unclean_app_process import unclean_app

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app

from test_supply_gap_resume_http import supplied, facts, execute, CHILD
from test_pet_typed_source_import import packet, values, encode
from test_pet_listening_resource_admission import bind_audio
from test_voice_runtime_binding import capture, interpret


def publish_listening(case):
    post,client,base=case['post'],case['client'],case['base']
    source=post(client,f'/v1/parent/children/{CHILD}/knowledge-sources',values(encode(packet('listening'))))
    response=client.get(base+f"/sources/{source['source_id']}/options")
    assert response.status_code==200,response.text
    options=response.json()
    seed=next(s for s in options['seeds'] if s['seed_item_id']=='diag-pet-listening-1')
    standard=next(s for s in options['standards'] if s['requirement_id']=='c03-req')
    selected=post(client,base+'/select',{'action_id':'listen-source-select','expected_version':case['selected']['version'],
        'scope':{'grade':'四年级','subject':'英语','edition':'isolated-c03-v1','semester':'上学期',
        'standard_scope':standard['stage_scope'],'textbook_source_id':source['source_id']}})
    draft=post(client,base+'/generate',{'action_id':'listen-source-draft','expected_version':0,
        'expected_scope_version':selected['version'],'source':{'seed_item_id':seed['seed_item_id'],
        'stage':'parallel','excerpt_id':options['excerpts'][0]['excerpt_id'],'requirement_id':standard['requirement_id'],
        'standard_page':standard['page_number'],'standard_quote':standard['quote'],'validator':seed['validator'],
        'difficulty':1,'explanation':'Synthetic reviewed original listening; no official recording or child result.'}})
    for version,operation in enumerate(('validate','review','publish'),1):
        body={'expected_version':version,'action_id':'listen-'+operation}
        if operation=='review':
            body.update(decision='approve',note='Checked full original text, listening count and exact answer.')
        published=post(client,base+f"/{draft['candidate_id']}/{operation}",body)
    assert published['eligible']
    return published


@pytest.mark.parametrize('supplied',['listening_wait'],indirect=True)
@pytest.mark.parametrize('change',['none','regrant','reorder','late_regrant','source_withdraw','prompt_cancel','budget_after_audio'])
def test_real_listening_resource_wait_accepts_only_new_request_current_authority(supplied,change,monkeypatch):
    case=supplied
    published=publish_listening(case)
    case['speech']['selector']={'subject':None,'task_kind':'listening','relative_order':None}
    before=facts(case)
    assert any(row['source_ref']=='pet-skill-gap:listening' and row['decision']=='deferred'
        for row in before['plan_constraint_decision'])
    original=case['say']('place-listening-before-audio','题目补好了，继续原来的听力练习。')
    waiting=execute(case,original)
    assert waiting.status_code==200,waiting.text
    receipt=waiting.json()['execution']
    assert receipt['result']['status']=='awaiting_resources',receipt
    prompt=receipt['result']['diagnostic_prompt']['prompt']
    pid=receipt['result']['plan_item_id']
    assert prompt['diagnostic_item_id']==published['diagnostic_item_id']
    assert case['client'].get(f"/v1/sessions/{case['sid']}/diagnostic-prompt").json()['status']=='awaiting_task_start'
    placed=facts(case)
    task=next(row for row in placed['plan_item'] if row['plan_item_id']==pid)
    assert task['status']=='pending' and task['started_at'] is None
    with case['db'].connect() as c:
        original_manifest=c.execute('SELECT context_manifest_json FROM agent_plan WHERE agent_turn_id=?',
            (original['agent_turn_id'],)).fetchone()[0]
    media,_=bind_audio(case['client'],case['db'],case['tmp_path'],case['sid'],prompt,approved=False)
    # Simulated test-parent reference attestation; synthetic signal, no acoustic/content proof.
    reviewed=case['post'](case['client'],f"/v1/sessions/{case['sid']}/pet-media/{media['media_id']}/source-review",
        {'audio_sha256':media['audio']['sha256'],'operation':'approve', 'full_spoken_reference': True,'client_action_id':'actual-parent-listening-review'})
    assert reviewed['media']['source_reviewed'] and not reviewed['media']['qualification']['task_delivery_qualified']
    if change=='regrant':
        for allowed in (False,True):
            response=case['client'].put('/v1/parent/privacy',json={'cloud_model_text_allowed':allowed})
            assert response.status_code==200,response.text
    if change=='reorder':
        # Reorder HTTP is plan_review-only, whereas resume is study-only.
        # Use the real source-revision/replan services, with their existing
        # persisted command fixture; no task, prompt or plan version is inserted.
        from home_study_agent.learning_input_revision import LearningInputRevisionService
        from home_study_agent.learning_input_replan import LearningInputReplanService
        from test_learning_input_revision_voice import propose, confirm
        inputs=(case['db'],case['sid'],LearningInputRevisionService(case['db']))
        proposed,_=propose(inputs,utterance='口算改成10分钟',changes={'expected_minutes':10},
            change_quotes={'expected_minutes':'10分钟'})
        assert proposed['status']=='awaiting_confirmation',proposed
        confirmed,_=confirm(inputs)
        assert confirmed['status']=='applied',confirmed
        replanned=LearningInputReplanService(case['db']).execute_replan(CHILD,confirmed['event_id'],'school-replan-while-audio-waits')
        assert replanned['newitems'] and replanned['receipt']['status']=='scheduled',replanned
        assert replanned['receipt']['to_plan_version']>placed['daily_plan'][0]['version']
        assert next(row for row in facts(case)['plan_item'] if row['plan_item_id']==pid)==task
    if change=='budget_after_audio':
        rule=case['client'].get(f'/v1/parent/children/{CHILD}/family-rule').json()
        changed=case['client'].put(f'/v1/parent/children/{CHILD}/family-rule',json={
            **{k:rule[k] for k in ('latest_end_time','max_learning_minutes','task_segment_minutes','break_minutes',
                'pet_min_minutes','competition_minutes','observation_mode','reminder_budget_per_10m')},
            'task_segment_minutes':10})
        assert changed.status_code==200,changed.text
    before_retry=facts(case)
    old=execute(case,original)
    assert old.status_code==200 and old.json()['execution']==receipt,old.text
    assert facts(case)==before_retry
    fresh=case['say']('fresh-listening-after-'+change,'原音补好了，继续原来的听力练习。',prompt=prompt)
    if change=='late_regrant':
        for allowed in (False,True):
            response=case['client'].put('/v1/parent/privacy',json={'cloud_model_text_allowed':allowed})
            assert response.status_code==200,response.text
    elif change=='source_withdraw':
        case['post'](case['client'],case['base']+f"/{published['candidate_id']}/withdraw",{
            'expected_version':4,'action_id':'late-listening-source-withdraw','note':'Original source withdrawn after new voice'})
    elif change=='prompt_cancel':
        case['post'](case['client'],f"/v1/sessions/{case['sid']}/diagnostic-prompt/cancel",{
            'prompt_id':prompt['prompt_id'],'item_version':prompt['item_version']})
    started=execute(case,fresh)
    if change in {'late_regrant','source_withdraw','prompt_cancel','budget_after_audio'}:
        assert started.status_code==409,started.text
        with case['db'].connect() as c:
            assert c.execute('SELECT started_at FROM plan_item WHERE plan_item_id=?',(pid,)).fetchone()[0] is None
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type IN ('started','resumed')",(pid,)).fetchone()[0]==0
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE diagnostic_item_id=?',(prompt['diagnostic_item_id'],)).fetchone()[0]==0
            assert c.execute('SELECT context_manifest_json FROM agent_plan WHERE agent_turn_id=?',
                (original['agent_turn_id'],)).fetchone()[0]==original_manifest
        return
    assert started.status_code==200,started.text
    result=started.json()['execution']['result']
    assert result['status']=='active' and result['diagnostic_prompt']['prompt']['prompt_id']==prompt['prompt_id']
    with case['db'].connect() as c:
        assert c.execute('SELECT context_manifest_json FROM agent_plan WHERE agent_turn_id=?',
            (original['agent_turn_id'],)).fetchone()[0]==original_manifest
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='started'",(pid,)).fetchone()[0]==1
        assert c.execute("SELECT COUNT(*) FROM diagnostic_attempt WHERE diagnostic_item_id=?",(prompt['diagnostic_item_id'],)).fetchone()[0]==0
        assert c.execute("SELECT COUNT(*) FROM pet_media_event WHERE operation='playback'").fetchone()[0]==0
    after=facts(case)
    assert execute(case,fresh).status_code==200
    assert facts(case)==after
    if change=='none':
        with unclean_app(case, lane='supply_gap', headers=dict(case['client'].headers)) as client:
            paused=facts(case)
            calls=len(case['requests'])
            replay=client.post(f"/v1/agent-turns/{original['agent_turn_id']}/execute")
            assert replay.status_code==200 and replay.json()['execution']==receipt,replay.text
            assert facts(case)==paused and len(case['requests'])==calls
            quote='继续刚才的听力练习，还是原来的题'
            case['speech']['quote']=quote
            voice=capture(client,case['settings'],monkeypatch,case['sid'],quote,prompt,key='listening-after-restart-voice')
            resumed=interpret(client,case['sid'],quote,'listening-after-restart-resume',voice)
            result=client.post(f"/v1/agent-turns/{resumed['agent_turn_id']}/execute")
            assert result.status_code==200,result.text
            assert result.json()['execution']['result']['plan_item_id']==pid
            assert result.json()['execution']['result']['diagnostic_prompt']['prompt']['prompt_id']==prompt['prompt_id']
