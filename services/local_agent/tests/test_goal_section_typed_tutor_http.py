"""Real local oral HTTP/reviewer protocol, synthetic PCM; no acoustic quality claim."""
from dataclasses import replace
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.database import Database
from home_study_agent.goal_program import GoalProgramService
from home_study_agent.repository import StudyRepository
from home_study_agent.teaching_question_target import resolve_question_target
from test_agent_continuation import Gateway
from test_diagnostic_prompt_http import start
from test_goal_roadmap import configure
from test_goal_section_evidence import section
from test_goal_section_oral_http import ROUTE
from test_oral_teaching_families_http import original_answer
from test_pet_oral_supply_http import setup, authored, post, BASE, CHILD


def sample(name, response, enabled):
    assert response.status_code == 200, response.text[:400]
    packet = response.json()
    if enabled and os.environ.get('SECTION_HTTP_SAMPLE_DIR'):
        target = Path(os.environ['SECTION_HTTP_SAMPLE_DIR'])
        assert str(target.resolve()).startswith('/private/tmp/hsc-c07-section-20260909/')
        target.mkdir(parents=True, exist_ok=True)
        safe = {key:packet[key] for key in ('version','child_id','program_id','track','source_version','as_of_date','section_evidence')}
        (target/(name+'.json')).write_text(json.dumps(safe,ensure_ascii=False,indent=2)+'\n')
    return packet['section_evidence']


@pytest.mark.parametrize('with_hint', [False, True], ids=['original-only','exact-typed-tutor-hint'])
def test_reviewed_read_aloud_material_has_current_receipt_but_tutor_help_is_not_independence(tmp_path, monkeypatch, with_hint):
    initial, settings = setup(tmp_path)
    settings = replace(settings, data_encryption_key=b'T'*32)
    db = Database(initial.path,media_root=settings.data_dir,data_encryption_key=settings.data_encryption_key)
    configure(GoalProgramService(db),child=CHILD,track='pet_path',target_date='2026-12-01')
    gateway = Gateway({})
    gateway.configured = False
    headers = {'X-HSC-Session':settings.session_token}
    with TestClient(create_app(settings,model_gateway=gateway),headers=headers,client=('127.0.0.1',41951)) as client:
        client.headers['X-HSC-Parent'] = post(client,'/v1/parent/setup',{'pin':'7391'})['parent_token']
        assert client.put('/v1/parent/privacy',json={'raw_media_storage_allowed':True,
            'cloud_audio_allowed':False,'cloud_model_text_allowed':False,'model_improvement_allowed':False}).status_code == 200
        original, _, candidate = authored(client,db,'read_aloud')
        for revision,operation in enumerate(('validate','review','publish'),1):
            body = dict(expected_version=revision,action_id='section-'+operation)
            if operation=='review':
                body.update(decision='approve',note='Synthetic task source; no official part mapping')
            published = post(client,BASE+'/'+candidate['candidate_id']+'/'+operation,body)
        item = published['diagnostic_item_id']
        sample('published_reference',client.get(ROUTE),not with_hint)
        sid = start(client)
        prompt = post(client,f'/v1/sessions/{sid}/diagnostic-prompt/prepare',dict(track='pet',
            preferred_item_id=item,client_action_id='section-read-aloud'))['prompt']
        if with_hint:
            with db.connect() as c:
                binding = resolve_question_target(c,sid,subject='英语',question=prompt['item']['prompt'],
                    prompt_id=prompt['prompt_id'],item_version=prompt['item_version'])
            assert binding['status']=='resolved'
            # A real exact-source Tutor receipt before the actual answer. No
            # hint mutation on the answer/reviewer and no consumer-specific veto.
            StudyRepository(db).save_tutor_turn(sid,None,'英语',prompt['item']['prompt'],
                'First practise joining the middle two sentences.', 'local','synthetic','section-exact-hint',
                hint_level=1,output_envelope={'local_teaching_control':{'question_target':binding}})
        answer = original['stimulus_text']
        assessment = original_answer(client,db,settings,monkeypatch,sid,prompt,answer)
        pending = sample('answer_pending_review',client.get(ROUTE),not with_hint)
        assert section(pending,'speaking')['opportunity_count']==1
        assert section(pending,'speaking')['qualified_evidence_count']==0
        path = f"/v1/parent/children/{CHILD}/learning-rubrics/{assessment['assessment_id']}"
        detail = client.get(path).json()
        audio = detail['audio_review']['bindings'][0]
        assert client.get(path+'/audio',params={'media_id':audio['media_id'],
            'audio_sha256':audio['audio']['sha256']}).status_code==200
        review = dict(expected_revision=0,client_action_id='section-qualified-audio',operation='approve',
            reason='Synthetic reviewer protocol, not physical pronunciation calibration',independent=True,hint_level=0,
            review_profile='pet_speaking_audio_v1',
            dimensions=[dict(dimension_id=d['dimension_id'],score=3,
                response_quotes=[answer] if d['dimension_id'] in {'task','coherence','language'} else [],
                rationale='Synthetic original audio annotation') for d in detail['audio_review']['dimensions']],
            criterion_evidence=[dict(criterion=p,response_quote=answer) for p in assessment['criteria']],
            audio_evidence={'media_id':audio['media_id'],'review':{
                'schema':'pet_speaking_audio_references_v1','binding_version':audio['binding_version'],
                'audio_sha256':audio['audio']['sha256'],'observations':[dict(dimension_id=name,
                    start_frame=0,end_frame=audio['audio']['frames'],heard_text='Synthetic recording',
                    rationale='Synthetic protocol fixture, not an acoustic score') for name in audio['dimensions_required']]}})
        post(client,path+'/review',review)
        view = sample('reviewed',client.get(ROUTE),not with_hint)
        speaking = section(view,'speaking')
        assert speaking['opportunity_count']==1 and speaking['qualified_evidence_count']==1
        assert speaking['qualified_evidence'][0]['correctness']==1
        assert speaking['independent_correct_count']==(0 if with_hint else 1)
        assert speaking['official_part_mapping']=='missing'
        with db.connect() as c:
            facts = [tuple(r) for r in c.execute('SELECT * FROM diagnostic_attempt')]
            assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0]==0
    with TestClient(create_app(settings,model_gateway=gateway),headers=headers,client=('127.0.0.1',41952)) as client:
        client.headers['X-HSC-Parent'] = post(client,'/v1/parent/unlock',{'pin':'7391'})['parent_token']
        restored = sample('restarted',client.get(ROUTE),not with_hint)
        assert section(restored,'speaking')['independent_correct_count']==(0 if with_hint else 1)
        post(client,path+'/review',dict(expected_revision=1,client_action_id='section-review-retract',
            operation='retract',reason='Withdraw synthetic review, not original source or response'))
        retracted = sample('review_retracted',client.get(ROUTE),not with_hint)
        speaking = section(retracted,'speaking')
        assert speaking['opportunity_count']==1 and speaking['qualified_evidence_count']==0
        assert item in {source['diagnostic_item_id'] for source in speaking['sources']}
    with TestClient(create_app(settings,model_gateway=gateway),headers=headers,client=('127.0.0.1',41953)) as client:
        client.headers['X-HSC-Parent'] = post(client,'/v1/parent/unlock',{'pin':'7391'})['parent_token']
        retracted_restart = sample('review_retracted_restart',client.get(ROUTE),not with_hint)
        speaking = section(retracted_restart,'speaking')
        assert speaking['opportunity_count']==1 and speaking['qualified_evidence_count']==0
        assert item in {source['diagnostic_item_id'] for source in speaking['sources']}
        post(client,BASE+'/'+candidate['candidate_id']+'/withdraw',dict(expected_version=4,
            action_id='section-withdraw',note='Withdraw original source, preserve response history'))
        withdrawn = sample('withdrawn',client.get(ROUTE),not with_hint)
        assert not section(withdrawn,'speaking')['qualified_evidence']
        with db.connect() as c:
            assert [tuple(r) for r in c.execute('SELECT * FROM diagnostic_attempt')]==facts
    assert not gateway.requests
