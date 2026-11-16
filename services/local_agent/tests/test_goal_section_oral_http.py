"""Actual parent/source/ASR/answer/review HTTP; synthetic PCM, no cloud."""
from dataclasses import replace
import json

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.database import Database
from home_study_agent.goal_program import GoalProgramService
from test_agent_continuation import Gateway
from test_diagnostic_prompt_http import start
from test_goal_roadmap import configure
from test_goal_section_evidence import section
from test_pet_oral_supply_http import setup, authored, post, BASE, CHILD, OTHER, FAMILIES
from test_oral_teaching_families_http import original_answer, ANSWERS

ROUTE = f'/v1/parent/children/{CHILD}/goal-programs/pet_path/roadmap'


@pytest.mark.parametrize('family', FAMILIES)
def test_published_oral_reference_is_a_speaking_opportunity_not_official_part_coverage(tmp_path, monkeypatch, family):
    initial, settings = setup(tmp_path)
    settings = replace(settings, data_encryption_key=b'S'*32)
    db = Database(initial.path, media_root=settings.data_dir, data_encryption_key=settings.data_encryption_key)
    configure(GoalProgramService(db), child=CHILD, track='pet_path', target_date='2026-12-01')
    gateway = Gateway({})
    gateway.configured = False
    headers = {'X-HSC-Session': settings.session_token}
    with TestClient(create_app(settings, model_gateway=gateway), headers=headers, client=('127.0.0.1',41940)) as client:
        assert client.get(ROUTE).status_code == 401
        client.headers['X-HSC-Parent'] = post(client, '/v1/parent/setup', {'pin':'7391'})['parent_token']
        assert client.put('/v1/parent/privacy', json={'raw_media_storage_allowed':True,
            'cloud_audio_allowed':False, 'cloud_model_text_allowed':False, 'model_improvement_allowed':False}).status_code == 200
        original, source, draft = authored(client, db, family)
        for version, operation in enumerate(('validate','review','publish'), 1):
            body = {'action_id':operation, 'expected_version':version}
            if operation == 'review':
                body.update(decision='approve', note='Synthetic task source, not official section mapping')
            published = post(client, BASE + '/' + draft['candidate_id'] + '/' + operation, body)
        item = published['diagnostic_item_id']
        route = client.get(ROUTE)
        assert route.status_code == 200, route.text[:600]
        before = route.json()['section_evidence']
        assert next(s for s in section(before,'speaking')['sources'] if s['diagnostic_item_id']==item)['task_family'] == family
        assert item not in {s['diagnostic_item_id'] for s in section(before,'reading')['sources']}
        assert section(before,'speaking')['official_part_mapping'] == 'missing'
        sid = start(client)
        prompt = post(client, f'/v1/sessions/{sid}/diagnostic-prompt/prepare', dict(track='pet',
            preferred_item_id=item, client_action_id='section-oral-prompt'))['prompt']
        response = original['stimulus_text'] if family=='read_aloud' else ANSWERS[family]
        assessment = original_answer(client, db, settings, monkeypatch, sid, prompt, response)
        after = client.get(ROUTE).json()['section_evidence']
        speaking = section(after,'speaking')
        assert speaking['opportunity_count'] == 1
        assert speaking['qualified_evidence_count'] == 0
        assert speaking['independent_correct_count'] == 0
        assert not after['exam_readiness_claim_allowed']
        encoded = json.dumps(after, ensure_ascii=False)
        for private in (original['prompt'], response, *original['task_points']):
            assert private not in encoded
        assert after['local_only'] and not after['cloud_transmission_allowed']
        path = f"/v1/parent/children/{CHILD}/learning-rubrics/{assessment['assessment_id']}"
        text_review = dict(expected_revision=0, client_action_id='section-text-review',operation='approve',
            reason='Text review is not an original voice assessment', independent=True, hint_level=0,
            dimensions=[dict(dimension_id=d['dimension_id'],score=3,response_quotes=[response],rationale='Synthetic text')
                for d in assessment['dimensions']],
            criterion_evidence=[dict(criterion=p,response_quote=response) for p in assessment['criteria']])
        post(client,path+'/review',text_review)
        assert section(client.get(ROUTE).json()['section_evidence'],'speaking')['qualified_evidence_count'] == 0
        if family == 'retell':
            detail = client.get(path).json()
            binding = detail['audio_review']['bindings'][0]
            assert client.get(path+'/audio', params={'media_id':binding['media_id'],
                'audio_sha256':binding['audio']['sha256']}).status_code == 200
            audio_review = {**text_review, 'expected_revision':1, 'client_action_id':'section-audio-review',
                'review_profile':'pet_speaking_audio_v1',
                'dimensions':[dict(dimension_id=d['dimension_id'], score=0,
                    response_quotes=[response] if d['dimension_id'] in {'task','coherence','language'} else [],
                    rationale='Synthetic annotation verifies protocol, not physical voice quality')
                    for d in detail['audio_review']['dimensions']],
                'audio_evidence':{'media_id':binding['media_id'], 'review':{
                    'schema':'pet_speaking_audio_references_v1', 'binding_version':binding['binding_version'],
                    'audio_sha256':binding['audio']['sha256'], 'observations':[dict(dimension_id=name,
                        start_frame=0, end_frame=binding['audio']['frames'], heard_text='Synthetic recording',
                        rationale='Synthetic reviewer annotation, not acoustic calibration')
                        for name in binding['dimensions_required']]}}}
            post(client,path+'/review',audio_review)
            qualified = section(client.get(ROUTE).json()['section_evidence'],'speaking')
            assert qualified['qualified_evidence_count'] == 1
            assert qualified['opportunity_count'] == 1
            assert qualified['independent_correct_count'] == 0
            assert qualified['qualified_evidence'][0]['review_revision'] == 2
        other = client.get(ROUTE.replace(CHILD,OTHER))
        assert other.status_code == 404
        with db.connect() as c:
            original_attempt = [tuple(r) for r in c.execute('SELECT * FROM diagnostic_attempt')]
    with TestClient(create_app(settings,model_gateway=gateway),headers=headers, client=('127.0.0.1',41941)) as client:
        client.headers['X-HSC-Parent'] = post(client,'/v1/parent/unlock',{'pin':'7391'})['parent_token']
        assert section(client.get(ROUTE).json()['section_evidence'],'speaking')['opportunity_count'] == 1
        if family == 'retell':
            assert section(client.get(ROUTE).json()['section_evidence'],'speaking')['qualified_evidence_count'] == 1
            post(client,path+'/review',dict(expected_revision=2,client_action_id='section-retract-review',
                operation='retract',reason='Withdraw synthetic judgment, preserve original response'))
            declined = section(client.get(ROUTE).json()['section_evidence'],'speaking')
            assert declined['opportunity_count'] == 1 and declined['qualified_evidence_count'] == 0
        post(client,BASE+'/'+draft['candidate_id']+'/withdraw',dict(action_id='withdraw',expected_version=4,note='Remove source'))
        withdrawn = client.get(ROUTE).json()['section_evidence']
        assert item not in {s['diagnostic_item_id'] for s in section(withdrawn,'speaking')['sources']}
        assert section(withdrawn,'speaking')['qualified_evidence_count'] == 0
        assert withdrawn['unmapped_or_unavailable_opportunity_count'] == 1
        with db.connect() as c:
            assert [tuple(r) for r in c.execute('SELECT * FROM diagnostic_attempt')] == original_attempt
            assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0] == 0
    assert gateway.requests == []


def test_internal_planning_does_not_rescan_section_calibration_history(tmp_path, monkeypatch):
    db, _ = setup(tmp_path)
    service = GoalProgramService(db)
    configure(service, child=CHILD, track='pet_path', target_date='2026-12-01')
    def forbidden(*args, **kwargs):
        pytest.fail('ordinary planning rescanned the parent-only section audit')
    monkeypatch.setattr('home_study_agent.goal_section_coverage.build_section_evidence', forbidden)
    cycles = service.cycles(CHILD)
    assert cycles
