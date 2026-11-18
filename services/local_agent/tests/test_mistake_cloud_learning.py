import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from home_study_agent.database import Database
from home_study_agent.config import Settings
from home_study_agent.model_gateway import ModelGateway
from home_study_agent.repository import StudyRepository
from home_study_agent.learning_state import LearningStateService
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent.mistake_book import MistakeBookService
from home_study_agent.mistake_learning import MistakeLearningService, native_focus, validate_source, native_receipt
from home_study_agent.mistake_learning_http import create_router
from normal_provider_owner import normal_provider_owner

import home_study_agent.mistake_learning as _main_learning
assert Path(_main_learning.__file__).resolve() == Path(__file__).resolve().parents[1]/"src/home_study_agent/mistake_learning.py", "MAIN_SOURCE_IMPORT_REQUIRED"

CHILD = 'child-junior-3'
ANALYSIS = {'summary':'先核对判别式中的平方和符号。','wrong_causes':[{'label':'符号计算','explanation':'原答可能漏掉了平方项，需要先看原过程。'}], 'knowledge_points':[{'label':'判别式新模型标签','description':'用 b²-4ac 判断实根数量。'}], 'correction_strategy':['分别算 b² 和 4ac，再相减。']}
PRACTICE = {'candidates':[
 {'stage':'near_transfer','prompt':'二次方程 x²-2x-3=0，分别计算判别式，并判断不同实数根的个数。','expected_answer':{'keywords':['16','两个不同实数根']},'explanation':'判别式4+12=16，大于0，所以有两个不同的实数根。','difficulty':1},
 {'stage':'representation_transfer','prompt':'已知一个二次方程的系数为 a=3、b=2、c=1，不求根，说明它是否有实数解。','expected_answer':{'keywords':['-8','没有实数解']},'explanation':'判别式4-12=-8，小于0，所以没有实数根。','difficulty':2},
 {'stage':'discrimination','prompt':'同学说二次方程判别式为0时没有实数根。请判断该结论，并给出你的理由。','expected_answer':{'keywords':['不对','两个相等实数根']},'explanation':'判别式为0说明两个根相等，仍然有实数根。','difficulty':2}]}
FEEDBACK = {'judgement':'correct','feedback':'计算和结论一致。','explanation':'你正确说明了判别式和根的关系。','reference_answer':'16，有两个不同实数根。','next_hint':'下一道题试着先判断符号。'}

@pytest.fixture
def world(tmp_path, normal_provider_owner, monkeypatch):
    import socket
    network=[]
    def deny(*a,**kw):
        network.append(True);raise AssertionError('EXTERNAL_NETWORK_FORBIDDEN')
    monkeypatch.setattr(socket.socket,'connect',deny)
    db = Database(tmp_path/'public.db'); db.initialize()
    repo=StudyRepository(db); repo.seed_family()
    LearningStateService(db).seed_catalog()
    privacy=PrivacyLifecycleService(db,tmp_path);privacy.initialize();privacy.update_preferences({'cloud_model_text_allowed':True})
    sid=repo.start_session(CHILD,'public-session')['session_id']
    book=MistakeBookService(db)
    mid=book.capture(child_id=CHILD,knowledge_node_id='node-j3-math-discriminant',subject='数学',title='判别式复习',prompt_summary='方程 x²-4x+4=0 的判别式是多少？有几个不同的实数根？',submitted_answer='判别式是4，有两个根',source_type='diagnostic',source_ref='diag-j3-discriminant-1',confidence=.6,client_action_id='public-capture',diagnostic_item_id='diag-j3-discriminant-1')['mistake_id']
    calls=[]; controls={'fail':False,'response':None,'coach_response':None,'coach_check_response':None,'practice_response':None,
                        'variant_check_response':None,'practice_sequence':[],'variant_checks':[],
                        'step_checks':[],'revised_coach_response':None,'hook':None,'barrier':None}
    async def transport(req):
        payload=json.loads(req.content);calls.append(payload)
        if controls['barrier']:
            controls['barrier'][0].set();await controls['barrier'][1].wait()
        if controls['hook']: controls['hook']()
        if controls['fail']: return httpx.Response(503,request=req)
        instruction=payload['messages'][0]['content']
        if '中立事实核查员' in instruction:
            value=controls['coach_check_response']
            if value is None:value={'claim_status':'no_claim','facts':'未给出可核实的断言时，应依据实际题干澄清，不补造条件。'}
        else:
            context = json.loads(payload['messages'][1]['content'])
            if '逐步讲解核对员' in instruction:
                value={'valid':controls['step_checks'].pop(0) if controls['step_checks'] else True}
            elif '独立题目核对员' in instruction:
                value = ({'checks':[{'position':1,'valid':controls['variant_checks'].pop(0)}]}
                         if controls['variant_checks'] else controls['variant_check_response'])
            elif 'candidates' in instruction:
                value = (controls['practice_sequence'].pop(0) if controls['practice_sequence'] else controls['practice_response'])
            elif isinstance(context, dict) and 'coaching_step' in context:
                value = (controls['revised_coach_response'] if context.get('revision_instruction')
                         and controls['revised_coach_response'] is not None else controls['coach_response'])
            else:
                value = None
            if value is None:
                value = controls['response']
        if value is None:
            value= {'checks':[{'position':i+1,'valid':True} for i in range(len(json.loads(payload['messages'][1]['content'])))]} if '独立题目核对员' in instruction else ANALYSIS if 'wrong_causes' in instruction else PRACTICE if 'candidates' in instruction else FEEDBACK if 'judgement' in instruction else {'phase':'question','text':'你能先算出平方项吗？'}
            if value is PRACTICE:
                plan = json.loads(payload['messages'][1]['content']).get('graph_practice_plan')
                if plan:
                    value = {'candidates': [candidate | {'checkpoint_ref': target['ref']}
                                            for candidate, target in zip(PRACTICE['candidates'], plan)]}
        return httpx.Response(200,request=req,json={'choices':[{'message':{'content':json.dumps(value,ensure_ascii=False)},'finish_reason':'stop'}],'usage':{'prompt_tokens':40,'completion_tokens':50}})
    gw=ModelGateway(Settings(data_dir=tmp_path,database_path=db.path,model_api_key='public-synthetic',
                             tutor_explanation_api_key='synthetic-tutor-key-only',
                             tutor_explanation_base_url='https://synthetic-tutor.invalid/v1'),transport=httpx.MockTransport(transport))
    service=MistakeLearningService(db,gw)
    app=FastAPI(); app.include_router(create_router(service,authorize_request=lambda r:r.headers.get('x-public-auth')=='yes'))
    client=TestClient(app,headers={'x-public-auth':'yes'})
    yield db,service,client,mid,sid,calls,controls,privacy
    client.close();asyncio.run(gw.close());assert network==[]


def post(w, op, key, **kw):
    _,_,client,mid,sid,*_=w
    return client.post(f'/v1/mistakes/{mid}/learning/{op}',json={'child_id':CHILD,'session_id':sid,'client_action_id':key,**kw})


def native_onset(db, sid, key, utterance, visible_practice_id, *, expected_validation='current', full_witness=False, current_graph_version=None):
    from home_study_agent.realtime_native_mistake_onset import BOUND, record
    from home_study_agent.voice_capture_binding import VoiceCaptureBindingService

    onset = datetime.now(timezone.utc)
    captures = VoiceCaptureBindingService(db)
    captures.begin_realtime(sid, key, 'native_cloud_dialogue', {'child_id': CHILD},
                            model_version='PUBLIC_NATIVE_SYNTHETIC', captured_at_ms=int(onset.timestamp() * 1000))
    result = captures.complete_realtime(sid, key, utterance)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        focus = native_focus(c, sid, current_graph_version=current_graph_version)
        carrier = {field: focus[field] for field in BOUND}
        carrier.update(version=1, question_visible=True, practice_item_id=visible_practice_id)
        assert record(c, result, carrier, allow_new=True, current_graph_version=current_graph_version)['validation'] == expected_validation
        if full_witness:
            from types import SimpleNamespace
            from home_study_agent.privacy_operation_receipt import generation
            from home_study_agent.realtime_native_context import record_native_input_witness
            record_native_input_witness(SimpleNamespace(page_source_store=None), c, sid,
                                        result['voice_input_id'], expected_generation=generation(c))
        voice = c.execute('SELECT * FROM voice_input WHERE voice_input_id=?', (result['voice_input_id'],)).fetchone()
        c.commit()
    return dict(voice), {'speech_started_at': onset.isoformat()}


def test_visible_coach_accepts_its_bound_voice_when_the_model_does_not_select_a_tool(world):
    db, _, _, _, sid, calls, controls, privacy = world
    privacy.update_preferences({'cloud_audio_allowed': True})
    assert post(world, 'focus', 'public-bound-coach-focus').json()['action_status'] == 'completed'
    opened = post(world, 'coach', 'public-bound-coach-open', original_review=True,
                  utterance='请一步一步讲这道题。').json()
    assert opened['coach_turn_id'] == opened['turns'][-1]['turn_id']
    voice, _ = native_onset(db, sid, 'public-bound-coach-reply', '我还是想不出来。', None, full_witness=True)
    body = {'voice_input_id':voice['voice_input_id'], 'source_revision':opened['source_revision'],
            'coach_turn_id':opened['coach_turn_id'], 'original_review':True}
    action = 'mistake-voice:'+voice['voice_input_id']
    assert post(world, 'coach', action, **(body | {'coach_turn_id':'old-or-other-coach'})).status_code == 409
    assert post(world, 'coach', action, **body, utterance='用别的话替换原话').status_code == 422
    controls['coach_response'] = {'phase':'explain_step', 'text':'这里先算负四的平方，两个负数相乘得到正数。'}
    response = post(world, 'coach', action, **body)
    assert response.status_code == 200, response.text
    explained = response.json()
    assert explained['action_status']=='completed', explained.get('error_code')
    assert explained['coach_phase']=='explain_step'
    assert explained['turns'][-2]['text']==voice['transcript']
    assert explained['coach_turn_id']!=opened['coach_turn_id']
    calls_before = len(calls)
    assert post(world, 'coach', action, **body).json() == explained
    assert len(calls) == calls_before


def test_socratic_original_coach_guides_one_step_and_freezes_one_independent_variant(world):
    from home_study_agent.mistake_learning_execution import learning_response
    from home_study_agent.realtime_native_mistake_learning import compose

    db, service, _, mid, sid, calls, controls, privacy = world
    assert service.graph_matcher is None
    with db.connect() as c:
        evidence_before = c.execute('SELECT count(*) FROM learning_evidence').fetchone()[0]
    assert post(world, 'focus', 'public-socratic-focus').json()['action_status'] == 'completed'

    def coach(key, reply, *, phase, text, check_status='no_claim', focus_step=None, expected_text=None):
        controls['coach_response'] = {'phase': phase, 'text': text}
        if focus_step is not None:
            controls['coach_response']['focus_step'] = focus_step
        controls['coach_check_response'] = {'claim_status': check_status, 'facts': '负四的平方是十六。'}
        result = post(world, 'coach', key, original_review=True, style='socratic', utterance=reply)
        assert result.status_code == 200, result.text
        value = result.json()
        assert value['action_status'] == 'completed', value.get('error_code')
        assert value['turns'][-1]['text'] == (expected_text if expected_text is not None else text)
        return value

    opening_question = '你先单独算一下，负四的平方是多少？'
    opening = coach('public-socratic-open', '带我一步一步讲解这道错题。', phase='question', text=opening_question)
    assert opening['turns'][-1]['text'].count('？') == 1
    assert not opening.get('coach_variant')
    opening_payload = json.loads(calls[-1]['messages'][1]['content'])
    assert opening_payload['original_answer'] == '判别式是4，有两个根'
    assert opening_payload['coaching_step'] == 'opening'
    assert not opening_payload.get('graph_targets')

    narrower_question = '想一想，负四乘负四，乘积的符号是什么？'
    incorrect = coach('public-socratic-try', '负四的平方是负十六。', phase='question', text=narrower_question,
                      check_status='incorrect')
    assert not incorrect.get('coach_variant')
    assert narrower_question.count('？') == 1
    followup_payload = json.loads(calls[-1]['messages'][1]['content'])
    assert followup_payload['model_check']['claim_status'] == 'incorrect'
    assert followup_payload['coaching_step'] == 'follow_up'
    assert followup_payload['utterance'] == '负四的平方是负十六。'
    assert any(turn['text'] == opening_question for turn in followup_payload['history'])

    explained = coach('public-socratic-stuck', '我还是想不出来。', phase='explain_step',
                      text='这个平方就是把负四乘负四。两个负数相乘得正数，所以这一步得到十六。')
    assert not explained.get('coach_variant')
    terminal_text = '现在请独立做下面这一道变式题。'
    model_invitation = '你接下来就重新做原题。'
    final = coach('public-socratic-variant', '负数乘负数得到正数，平方项应当是十六。',
                  phase='independent_practice', text=model_invitation, check_status='correct', focus_step='算判别式中的负数平方',
                  expected_text=terminal_text)
    variant = final['coach_variant']
    assert variant['status'] == 'ready'
    assert variant['question'] == PRACTICE['candidates'][0]['prompt']
    assert 'expected_answer' not in variant and 'expected_json' not in variant and 'explanation' not in variant
    assert learning_response('coach', final) == terminal_text + '\n' + variant['question']
    assert all(turn['text'] != model_invitation for turn in final['turns'])
    assert not final['practice_items']
    assert final['today'] == opening['today']
    variant_requests = [payload for payload in calls if 'candidates' in payload['messages'][0]['content']]
    assert len(variant_requests) == 1
    assert json.loads(variant_requests[0]['messages'][1]['content'])['count'] == 1
    assert '算判别式中的负数平方' in variant_requests[0]['messages'][1]['content']
    checks = [payload for payload in calls if '独立题目核对员' in payload['messages'][0]['content']]
    assert len(checks) == 1
    checked = json.loads(checks[0]['messages'][1]['content'])
    assert len(checked) == 1 and checked[0]['question'] == variant['question']
    assert checked[0]['learning_target'] == '算判别式中的负数平方'

    call_count = len(calls)
    assert post(world, 'coach', 'public-socratic-variant', original_review=True, style='socratic',
                utterance='负数乘负数得到正数，平方项应当是十六。').json() == final
    assert len(calls) == call_count
    reopened = MistakeLearningService(Database(db.path), service.gateway)
    saved = reopened.state(mid, child_id=CHILD, session_id=sid)
    assert saved['coach_variant'] == variant
    assert saved['turns'][-1]['text'] == terminal_text

    privacy.update_preferences({'cloud_audio_allowed': True})
    voice, onset = native_onset(db, sid, 'public-socratic-spoken-answer', '十六，有两个不同的实数根。', variant['practice_item_id'], full_witness=True)
    with db.connect() as c:
        spoken_answer = compose(c, {'session_id': sid, 'child_id': CHILD}, voice, onset, {'operation': 'answer'},
                                turn_id='public-socratic-signed-answer')['action']
    assert spoken_answer['mistake_learning_source']['practice_item_id'] == variant['practice_item_id']
    response = post(world, 'answer', 'mistake-voice:'+voice['voice_input_id'], practice_item_id=variant['practice_item_id'],
                    voice_input_id=voice['voice_input_id'], source_revision=final['source_revision'])
    assert response.status_code==200, response.text
    answered = response.json()
    assert answered['action_status'] == 'completed', answered.get('error_code')
    assert answered['coach_variant']['status'] == 'answered'
    assert answered['coach_variant']['feedback']['judgement'] == 'correct'
    assert answered['today'] == opening['today']
    assert learning_response('answer', answered, variant['practice_item_id']) == FEEDBACK['feedback']
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM learning_evidence').fetchone()[0] == evidence_before


def test_visible_original_can_be_coached_when_a_daily_practice_is_ready_but_not_answered_as_that_practice(world):
    from home_study_agent.agent_runtime import InvalidAgentPlanError
    from home_study_agent.realtime_native_mistake_learning import compose

    db, _, _, _, sid, _, _, privacy = world
    privacy.update_preferences({'cloud_audio_allowed': True})
    assert post(world, 'focus', 'public-native-original-focus').json()['action_status'] == 'completed'
    practice = post(world, 'practice', 'public-native-daily-practice', count=2).json()
    assert practice['focus']['practice_item_id'] == practice['practice_items'][0]['practice_item_id']
    voice, onset = native_onset(db, sid, 'public-native-original-coach', '带我一步一步讲解眼前这道错题。', None)
    scope = {'session_id': sid, 'child_id': CHILD}
    with db.connect() as c:
        action = compose(c, scope, voice, onset, {'operation': 'coach'}, turn_id='public-native-coach')['action']
        assert action['mistake_learning_source']['practice_item_id'] is None
        with pytest.raises(InvalidAgentPlanError, match='NATIVE_MISTAKE_ONSET_STALE'):
            compose(c, scope, voice, onset, {'operation': 'answer'}, turn_id='public-native-wrong-surface')


def test_spoken_answers_bind_to_the_visible_variant_or_daily_item_when_both_are_ready(world):
    from home_study_agent.agent_runtime import InvalidAgentPlanError
    from home_study_agent.realtime_native_mistake_learning import compose

    db, _, _, _, sid, _, controls, privacy = world
    privacy.update_preferences({'cloud_audio_allowed': True})
    scope = {'session_id': sid, 'child_id': CHILD}
    assert post(world, 'focus', 'public-dual-item-focus').json()['action_status'] == 'completed'
    controls['practice_response'] = {'candidates': PRACTICE['candidates'][1:]}
    daily = post(world, 'practice', 'public-dual-item-daily', count=2).json()
    assert daily['action_status'] == 'completed'
    daily_ids = [row['practice_item_id'] for row in daily['practice_items']]
    controls['practice_response'] = None
    opening = post(world, 'coach', 'public-dual-item-opening', original_review=True,
                   utterance='请一步一步讲解原题。').json()
    assert opening['action_status'] == 'completed'
    controls['coach_response'] = {'phase': 'independent_practice', 'text': '现在请独立试一道。', 'focus_step': '负数的平方'}
    controls['coach_check_response'] = {'claim_status': 'correct', 'facts': '负四的平方是十六。'}
    closed = post(world, 'coach', 'public-dual-item-closing', original_review=True,
                  utterance='负四的平方是十六。').json()
    assert closed['action_status'] == 'completed'
    variant_id = closed['coach_variant']['practice_item_id']
    assert closed['focus']['practice_item_id'] == variant_id

    with db.connect() as c:
        assert native_focus(c, sid, prefer_coach_variant=False)['practice_item_id'] == daily_ids[0]
    for index, item_id in enumerate((daily_ids[0], variant_id), start=1):
        voice, onset = native_onset(db, sid, f'public-dual-item-voice-{index}', '我已经算好了。', item_id)
        with db.connect() as c:
            plan = compose(c, scope, voice, onset, {'operation': 'answer'}, turn_id=f'public-dual-item-answer-{index}')
            assert plan['action']['mistake_learning_source']['practice_item_id'] == item_id

    voice, onset = native_onset(db, sid, 'public-dual-item-too-early', '我已经算好了。', daily_ids[1], expected_validation='stale')
    with db.connect() as c, pytest.raises(InvalidAgentPlanError, match='NATIVE_MISTAKE_ONSET_STALE'):
        compose(c, scope, voice, onset, {'operation': 'answer'}, turn_id='public-dual-item-unbound')


@pytest.mark.parametrize('model_reply', [
    {'phase': 'independent_practice', 'text': '来做一道新题。', 'focus_step': '算判别式中的负数平方'},
    {'phase': 'explain_step', 'text': '把负四乘负四，得到十六。'},
    {'phase': 'question', 'text': '负四的平方是多少？你能说说整题怎么做吗？'},
])
def test_socratic_opening_can_publish_only_one_question(world, model_reply):
    _, _, _, _, _, calls, controls, _ = world
    controls['coach_response'] = model_reply
    result = post(world, 'coach', 'public-socratic-premature', original_review=True, style='socratic',
                  utterance='带我一步一步讲解这道错题。')
    assert result.status_code == 200
    assert result.json()['action_status'] == 'failed'
    assert not result.json().get('coach_variant')
    assert all(turn['role'] != 'assistant' for turn in result.json()['turns'])
    assert not any('candidates' in payload['messages'][0]['content'] for payload in calls)


def test_socratic_opening_repairs_two_questions_once_before_publishing(world):
    db, _, _, _, _, calls, controls, _ = world
    rejected = '负四的平方是多少？你能再说说整题有几个实根吗？'
    accepted = '负四的平方是多少？'
    controls['coach_response'] = {'phase':'question','text':rejected}
    controls['revised_coach_response'] = {'phase':'question','text':accepted,'focus_step':'核对负数平方'}

    result = post(world, 'coach', 'public-opening-format-repair', original_review=True, style='socratic',
                  utterance='带我一步一步讲解这道错题。').json()

    assert result['action_status'] == 'completed'
    assert result['coach_phase'] == 'question'
    assert [t['text'] for t in result['turns'] if t['role']=='assistant'] == [accepted]
    coach_calls = [json.loads(r['messages'][1]['content']) for r in calls
                   if 'coaching_step' in json.loads(r['messages'][1]['content'])]
    assert len(calls) == 3 and len(coach_calls) == 2
    assert not coach_calls[0].get('revision_instruction') and coach_calls[1].get('revision_instruction')
    with db.connect() as c:
        saved = [row[0] for row in c.execute("SELECT text FROM mistake_learning_turn WHERE role='assistant'")]
    assert saved == [accepted]


def test_failed_variant_verification_never_exposes_the_candidate_or_a_ready_claim(world):
    db, _, _, _, _, calls, controls, _ = world
    opening = post(world, 'coach', 'public-socratic-verification-open', original_review=True, style='socratic',
                   utterance='带我一步一步讲解这道错题。').json()
    assert opening['action_status'] == 'completed'
    ready_text = '可以开始独立巩固。'
    controls['coach_response'] = {'phase': 'independent_practice', 'text': ready_text, 'focus_step': '负数的平方'}
    controls['coach_check_response'] = {'claim_status': 'correct', 'facts': '负四的平方是十六。'}
    controls['variant_check_response'] = {'checks': [{'position': 1, 'valid': False}]}
    result = post(world, 'coach', 'public-socratic-verification-rejected', original_review=True, style='socratic',
                  utterance='负四乘负四，所以平方项是十六。').json()
    assert result['action_status'] == 'failed'
    assert not result.get('coach_variant')
    assert all(turn['text'] != ready_text for turn in result['turns'])
    assert sum('独立题目核对员' in payload['messages'][0]['content'] for payload in calls) == 2
    assert sum('candidates' in payload['messages'][0]['content'] for payload in calls) == 2
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM mistake_learning_item').fetchone()[0] == 0


def test_independent_variant_retries_once_and_only_persists_the_verified_new_question(world):
    db, service, _, mid, sid, calls, controls, _ = world
    opened = post(world, 'coach', 'public-first-variant-turn', original_review=True,
                  utterance='带我一步一步讲解这道错题。').json()
    assert opened['action_status'] == 'completed'
    first = PRACTICE['candidates'][0]
    revised = {'stage':'near_transfer','prompt':'在 x²-6x+5=0 中，先算负六的平方，再判断方程的不同实根数量。',
               'expected_answer':{'keywords':['三十六','两个不同实数根']},
               'explanation':'负六的平方是三十六，减去二十得十六，所以有两个不同实数根。','difficulty':1}
    controls['practice_sequence'] = [{'candidates':[first]},{'candidates':[revised]}]
    controls['variant_checks'] = [False, True]
    controls['coach_response'] = {'phase':'independent_practice','focus_step':'负数的平方'}
    controls['coach_check_response'] = {'claim_status':'correct','facts':'负四的平方是十六。'}

    response = post(world, 'coach', 'public-variant-repaired', original_review=True,
                    utterance='负四乘负四，所以平方项是十六。').json()

    assert response['action_status'] == 'completed'
    variant = response['coach_variant']
    assert variant['question'] == revised['prompt'] and variant['status'] == 'ready' and variant['feedback'] is None
    assert first['prompt'] not in json.dumps(response, ensure_ascii=False)
    assert 'expected_answer' not in variant and 'explanation' not in variant
    assert revised['explanation'] not in json.dumps(response, ensure_ascii=False)
    offered = [payload for payload in calls if 'candidates' in payload['messages'][0]['content']]
    checks = [json.loads(payload['messages'][1]['content'])[0]['question'] for payload in calls
              if '独立题目核对员' in payload['messages'][0]['content']]
    assert len(offered) == 2 and checks == [first['prompt'],revised['prompt']]
    with db.connect() as c:
        items = [row[0] for row in c.execute('SELECT question FROM mistake_learning_item')]
    assert items == [revised['prompt']]
    assert service.state(mid, child_id=CHILD, session_id=sid)['coach_variant'] == variant


@pytest.mark.parametrize('accept_revision', [True, False])
def test_step_verifier_withholds_the_whole_solution_and_allows_only_one_repair(world, accept_revision):
    db, _, _, _, _, calls, controls, _ = world
    opening = post(world, 'coach', 'public-step-guard-open', original_review=True,
                   utterance='带我一步一步讲解这道错题。').json()
    assert opening['action_status'] == 'completed'
    announced = '先算负四乘负四得到十六，减去十六，判别式就是零，方程有一个实数根。'
    one_step = '这里只算平方项。负四乘负四，两个负数相乘为正，所以这一步得到十六。'
    controls['coach_response'] = {'phase':'explain_step','text':announced}
    controls['revised_coach_response'] = {'phase':'explain_step','text':one_step}
    controls['step_checks'] = [False, accept_revision]
    before = len(calls)
    response = post(world, 'coach', 'public-step-guard-stuck', original_review=True,
                    utterance='这一步我还是想不出来。').json()
    new_calls = calls[before:]
    steps = [json.loads(row['messages'][1]['content']) for row in new_calls
             if '逐步讲解核对员' in row['messages'][0]['content']]
    revisions = [json.loads(row['messages'][1]['content']) for row in new_calls
                 if 'revision_instruction' in row['messages'][1]['content']]
    assert len(steps) == 2 and len(revisions) == 1
    assert announced in json.dumps(steps[0], ensure_ascii=False)
    assert one_step in json.dumps(steps[1], ensure_ascii=False)
    assert response['action_status'] == ('completed' if accept_revision else 'failed')
    assistant_turns = [turn['text'] for turn in response['turns'] if turn['role']=='assistant']
    assert announced not in assistant_turns
    assert (one_step in assistant_turns) is accept_revision
    with db.connect() as c:
        saved = [row[0] for row in c.execute("SELECT text FROM mistake_learning_turn WHERE role='assistant'")]
    assert announced not in saved
    assert (one_step in saved) is accept_revision


def test_unconfigured_tutor_does_not_reuse_or_delete_legacy_analysis(world,request):
    from dataclasses import replace
    from home_study_agent.mistake_learning import analysis_context_key
    db,service,_,mid,sid,_,_,_=world
    configured=service.gateway
    service.gateway=ModelGateway(replace(configured.settings,tutor_explanation_api_key=None),transport=configured._client._transport)
    request.addfinalizer(lambda:asyncio.run(service.gateway.close()))
    assert service.gateway.configured_for('mistake_learning_analyze') is False
    post(world,'focus','legacy-no-route-focus')
    with db.connect() as c:
        c.execute('UPDATE mistake_learning_state SET analysis_json=?,analysis_context_key=? WHERE mistake_id=?',
                  (json.dumps(ANALYSIS),analysis_context_key(None,False),mid))
        c.commit()
        before=tuple(c.execute('SELECT analysis_json,analysis_context_key FROM mistake_learning_state WHERE mistake_id=?',(mid,)).fetchone())
    state=service.state(mid,child_id=CHILD,session_id=sid)
    assert state['analysis'] is None and state['analysis_review_required'] is True and state['tutor_route_configured'] is False
    result=post(world,'analyze','legacy-no-route-analyze').json()
    assert result['action_status']=='failed' and result['analysis'] is None
    with db.connect() as c:
        assert tuple(c.execute('SELECT analysis_json,analysis_context_key FROM mistake_learning_state WHERE mistake_id=?',(mid,)).fetchone())==before


def test_dedicated_tutor_analysis_is_visible_only_for_the_same_route(world,request):
    from dataclasses import replace
    from home_study_agent.realtime_capability_tools import RealtimeCapabilityTools
    from home_study_agent.realtime_native_context import load_native_context
    from home_study_agent.mistake_learning import _tutor_route_identity
    db,service,client,mid,sid,_,controls,privacy=world
    base_gateway=service.gateway
    settings=replace(base_gateway.settings,tutor_explanation_api_key='synthetic-tutor-key-only',
        tutor_explanation_base_url='https://synthetic-tutor.invalid/v1',tutor_explanation_model='deepseek-v4.1-flash')
    gateways=[ModelGateway(settings,transport=base_gateway._client._transport)]
    request.addfinalizer(lambda: [asyncio.run(gateway.close()) for gateway in gateways])
    service.gateway=gateways[0]
    class Matcher(_AuthorizedSyntheticLabels):
        def cache_version(self,*,connection=None): return 'fixture-v1'
        def match(self,child_id,source):
            return {'graph_version':'fixture-v1','status':'matched',
                    'primary':{'key':'p','title':'根的判别式','review_status':'approved','confidence':.99},
                    'prerequisites':[],'alternatives':[]}
    service.graph_matcher=Matcher()
    privacy.update_preferences({'cloud_audio_allowed':True})
    post(world,'focus','dedicated-focus')
    result=post(world,'analyze','dedicated-analysis').json()
    assert result['analysis'] and result['graph_context']['graph_version']=='fixture-v1'
    assert service.state(mid,child_id=CHILD,session_id=sid)['analysis']==result['analysis']
    native=RealtimeCapabilityTools(db,current_graph_version=service.graph_matcher.cache_version,
        tutor_route_identity=lambda:_tutor_route_identity(service.gateway),graph_cloud_authorizer=service._graph_cloud_allowed)
    assert load_native_context(native,sid,authorize=lambda *args:True)['runtime_facts']['mistake_learning']['analysis']
    gateways.append(ModelGateway(replace(settings,tutor_explanation_model='deepseek-synthetic-b'),transport=base_gateway._client._transport))
    service.gateway=gateways[-1]
    assert service.state(mid,child_id=CHILD,session_id=sid)['analysis'] is None
    assert post(world,'analyze','dedicated-analysis').json()['analysis'] is None
    assert service.read_action('dedicated-analysis',child_id=CHILD,mistake_id=mid)['analysis'] is None
    assert 'analysis' not in load_native_context(native,sid,authorize=lambda *args:True)['runtime_facts']['mistake_learning']
    service.gateway=gateways[0]
    with db.connect() as c:
        c.execute('UPDATE mistake_learning_state SET analysis_context_key=NULL WHERE mistake_id=?',(mid,))
        c.commit()
        previous=tuple(c.execute('SELECT analysis_json,analysis_context_key FROM mistake_learning_state WHERE mistake_id=?',(mid,)).fetchone())
    controls['hook']=lambda:setattr(service,'gateway',gateways[1])
    failed=post(world,'analyze','route-changed-in-flight').json()
    assert failed['action_status']=='failed' and failed['analysis'] is None
    with db.connect() as c:
        assert tuple(c.execute('SELECT analysis_json,analysis_context_key FROM mistake_learning_state WHERE mistake_id=?',(mid,)).fetchone())==previous


def test_graph_guides_case_practice_and_next_review_without_promoting_mastery(world):
    db, service, client, mid, sid, calls, controls, _ = world

    class Matcher:
        version = 'fixture-v1'

        def cache_version(self, *, connection=None):
            return self.version

        def semantic_options(self, graph):
            item = graph['alternatives'][0]
            return [{'ref':'C1','label':item['title']}], {'C1':item['key']}

        def match(self, child_id, source):
            assert child_id == CHILD and source['subject'] == '数学'
            return {
                'graph_version': self.version, 'status': 'matched',
                'primary': {'key': 'local-private-main', 'title': '根的判别式', 'review_status': 'approved', 'confidence': .99},
                'prerequisites': [{'key': 'local-private-before', 'title': '平方运算', 'review_status': 'approved',
                                   'relation_review_status': 'approved'}],
                'alternatives': [],
            }

    service.graph_matcher = Matcher()
    library = client.get(f'/v1/children/{CHILD}/mistake-learning').json()
    assert library['graph_pending_count'] == 0 and library['items'][0]['graph_context']['primary']['title'] == '根的判别式'
    controls['response'] = ANALYSIS | {'prerequisite_checks': [
        {'checkpoint_ref': 'P1', 'question': '你能算出负四的平方吗？', 'evidence_quote': '判别式是4'},
    ]}
    analysis = post(world, 'analyze', 'graph-analyze').json()
    assert analysis['analysis']['prerequisite_checks'] == [{'title': '平方运算', 'question': '你能算出负四的平方吗？',
                                                             'evidence_quote': '判别式是4'}]
    assert analysis['graph_review_focus'] == {'title': '平方运算', 'kind': 'prerequisite', 'state': 'due'}
    assert 'local-private' not in json.dumps(calls[-1])
    controls['response'] = None
    practice = post(world, 'practice', 'graph-practice', count=2).json()
    first, second = practice['practice_items']
    assert [first['graph_target']['title'], second['graph_target']['title']] == ['平方运算', '根的判别式']
    assert practice['graph_review_focus']['state'] == 'scheduled'
    answer = post(world, 'answer', 'graph-answer', practice_item_id=first['practice_item_id'], utterance='十六，两个不同的实数根。').json()
    assert answer['graph_review_focus']['title'] == '根的判别式' and answer['next_review_at']
    reopened = MistakeLearningService(Database(db.path), service.gateway, graph_matcher=service.graph_matcher)
    saved = reopened.state(mid, child_id=CHILD, session_id=sid)
    assert saved['graph_review_focus'] == answer['graph_review_focus']
    assert saved['next_review_at'] == answer['next_review_at']
    assert all('local-private' not in json.dumps(call) for call in calls)
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM learning_evidence').fetchone()[0] == 0
    service.graph_matcher.version = 'fixture-v2'
    updated = post(world, 'analyze', 'graph-reanalyze').json()
    assert updated['graph_context']['graph_version'] == 'fixture-v2'
    assert updated['graph_review_focus']['state'] == 'due'
    assert updated['graph_practice_stale'] and updated['practice_items'][1]['graph_target'] is None
    service.graph_matcher.version = 'fixture-v3'
    controls['hook'] = lambda: setattr(service.graph_matcher, 'version', 'fixture-v4')
    revoked = post(world, 'analyze', 'graph-authority-changed').json()
    assert revoked['action_status'] == 'failed'
    assert revoked['graph_context']['graph_version'] == 'fixture-v4' and revoked['analysis'] is None


class _AuthorizedSyntheticLabels:
    def semantic_options(self, graph):
        item = graph['alternatives'][0]
        return [{'ref': 'C1', 'label': item['title']}], {'C1': item['key']}


def test_graph_change_keeps_old_feedback_without_finishing_new_review(world):
    db, service, client, mid, sid, calls, controls, privacy = world

    class Matcher(_AuthorizedSyntheticLabels):
        version = 'fixture-v1'

        def cache_version(self, *, connection=None):
            return self.version

        def match(self, child_id, source):
            return {
                'graph_version': self.version, 'status': 'matched',
                'primary': {'key': 'same-key', 'title': '根的判别式', 'review_status': 'approved', 'confidence': .99},
                'prerequisites': [], 'alternatives': [],
            }

    service.graph_matcher = Matcher()
    post(world, 'analyze', 'revision-analysis')
    practice = post(world, 'practice', 'revision-practice', count=2).json()
    item, second = practice['practice_items']
    assert post(world, 'answer', 'revision-answer', practice_item_id=item['practice_item_id'],
                utterance='十六，两个不同的实数根。').json()['today']['completed_count'] == 1
    assert post(world, 'answer', 'revision-answer-2', practice_item_id=second['practice_item_id'],
                utterance='十六，两个不同的实数根。').json()['today']['completed_count'] == 2
    with db.connect() as c:
        before = c.execute('SELECT feedback_json,validation_json FROM mistake_learning_item WHERE practice_item_id=?',
                           (item['practice_item_id'],)).fetchone()
    service.graph_matcher.version = 'fixture-v2'
    from home_study_agent.realtime_capability_tools import RealtimeCapabilityTools
    from home_study_agent.realtime_native_context import load_native_context
    privacy.update_preferences({'cloud_audio_allowed': True})
    native = RealtimeCapabilityTools(db)
    native.current_graph_version = service.graph_matcher.cache_version
    from home_study_agent.mistake_learning import _tutor_route_identity
    native.tutor_route_identity = lambda: _tutor_route_identity(service.gateway)
    native.graph_cloud_authorizer=service._graph_cloud_allowed
    due = load_native_context(native, sid, authorize=lambda *args: True)['runtime_facts']['mistake_review_today']
    assert due['due_count'] == 1 and due['remaining_questions'] == 2
    updated = service.state(mid, child_id=CHILD, session_id=sid)
    assert updated['graph_review_focus']['state'] == 'due'
    assert updated['review_due'] is True
    assert updated['today']['completed_count'] == 0
    assert updated['today']['status'] == 'not_started'
    assert all(item['status'] == 'stale' and item['feedback'] for item in updated['practice_items'])
    with db.connect() as c:
        after = c.execute('SELECT feedback_json,validation_json FROM mistake_learning_item WHERE practice_item_id=?',
                          (item['practice_item_id'],)).fetchone()
    assert tuple(before) == tuple(after)
    controls['practice_response'] = {'candidates': [
        candidate | {'prompt': candidate['prompt'] + ' 请再独立说明。', 'checkpoint_ref': 'MAIN'}
        for candidate in PRACTICE['candidates'][:2]
    ]}
    new_practice = post(world, 'practice', 'revision-new-practice', count=2).json()
    assert new_practice['action_status'] == 'completed'
    assert len(new_practice['practice_items']) == 2
    assert all(new['practice_item_id'] not in {item['practice_item_id'], second['practice_item_id']}
               for new in new_practice['practice_items'])
    assert all(new['status'] == 'ready' for new in new_practice['practice_items'])
    with db.connect() as c:
        saved = c.execute('SELECT feedback_json,validation_json FROM mistake_learning_item WHERE practice_item_id=?',
                          (item['practice_item_id'],)).fetchone()
    assert tuple(saved) == tuple(before)


def test_graph_change_does_not_accept_old_unanswered_practice_as_current(world):
    db, service, client, mid, sid, calls, controls, _ = world

    class Matcher(_AuthorizedSyntheticLabels):
        version = 'fixture-v1'

        def cache_version(self, *, connection=None):
            return self.version

        def match(self, child_id, source):
            return {
                'graph_version': self.version, 'status': 'matched',
                'primary': {'key': 'same-key', 'title': '根的判别式', 'review_status': 'approved', 'confidence': .99},
                'prerequisites': [], 'alternatives': [],
            }

    service.graph_matcher = Matcher()
    post(world, 'analyze', 'stale-analysis')
    practice = post(world, 'practice', 'stale-practice', count=2).json()
    first, second = practice['practice_items']
    post(world, 'answer', 'stale-answer-1', practice_item_id=first['practice_item_id'],
         utterance='十六，两个不同的实数根。')
    service.graph_matcher.version = 'fixture-v2'
    before_calls = len(calls)
    response = post(world, 'answer', 'stale-answer-2', practice_item_id=second['practice_item_id'],
                    utterance='没有实数解。')
    assert response.status_code == 409
    assert len(calls) == before_calls
    current = service.state(mid, child_id=CHILD, session_id=sid)
    assert current['today']['completed_count'] == 0
    assert all(item['status'] == 'stale' for item in current['practice_items'])
    with db.connect() as c:
        rows = c.execute('SELECT practice_item_id,answered_at FROM mistake_learning_item WHERE practice_item_id IN (?,?)',
                         (first['practice_item_id'], second['practice_item_id'])).fetchall()
    assert len(rows) == 2 and sum(row['answered_at'] is not None for row in rows) == 1


def test_native_context_hides_only_stale_graph_derived_content(world):
    from home_study_agent.realtime_capability_tools import RealtimeCapabilityTools
    from home_study_agent.realtime_native_context import load_native_context

    db, service, client, mid, sid, calls, controls, privacy = world

    class Matcher(_AuthorizedSyntheticLabels):
        version = 'fixture-v1'

        def cache_version(self, *, connection=None):
            return self.version

        def match(self, child_id, source):
            return {
                'graph_version': self.version, 'status': 'matched',
                'primary': {'key': 'same-key', 'title': '根的判别式', 'review_status': 'approved', 'confidence': .99},
                'prerequisites': [], 'alternatives': [],
            }

    service.graph_matcher = Matcher()
    privacy.update_preferences({'cloud_audio_allowed': True})
    post(world, 'focus', 'native-revision-focus')
    post(world, 'analyze', 'native-revision-analysis')
    practice = post(world, 'practice', 'native-revision-practice', count=2).json()
    old_item = practice['practice_items'][0]
    native = RealtimeCapabilityTools(db)
    native.current_graph_version = service.graph_matcher.cache_version
    from home_study_agent.mistake_learning import _tutor_route_identity
    native.tutor_route_identity = lambda: _tutor_route_identity(service.gateway)
    native.graph_cloud_authorizer=service._graph_cloud_allowed
    current = load_native_context(native, sid, authorize=lambda *args: True)
    before = current['runtime_facts']['mistake_learning']
    assert before['question'] and before['analysis'] and before['practice']['question'] == old_item['question']
    with db.connect() as c:
        saved = tuple(c.execute('SELECT analysis_json,graph_json,analysis_context_key FROM mistake_learning_state WHERE mistake_id=?', (mid,)).fetchone())
        c.execute('UPDATE mistake_learning_state SET analysis_context_key=NULL WHERE mistake_id=?', (mid,))
        c.commit()
    unknown_version = load_native_context(native, sid, authorize=lambda *args: True)['runtime_facts']['mistake_learning']
    assert unknown_version['question'] == before['question']
    assert 'analysis' not in unknown_version and unknown_version['practice']['question'] == old_item['question']
    screen_state = service.state(mid, child_id=CHILD, session_id=sid)
    assert screen_state['source']['question'] == before['question'] and screen_state['analysis'] is None
    with db.connect() as c:
        c.execute('UPDATE mistake_learning_state SET analysis_context_key=? WHERE mistake_id=?', (saved[2], mid))
        c.commit()
    service.graph_matcher.version = 'fixture-v2'
    after = load_native_context(native, sid, authorize=lambda *args: True)['runtime_facts']['mistake_learning']
    assert after['question'] == before['question']
    assert 'analysis' not in after and 'practice' not in after
    with db.connect() as c:
        assert tuple(c.execute('SELECT analysis_json,graph_json,analysis_context_key FROM mistake_learning_state WHERE mistake_id=?', (mid,)).fetchone()) == saved
        c.execute('UPDATE mistake_learning_state SET graph_json=? WHERE mistake_id=?', ('{broken', mid))
        c.commit()
    invalid_graph = load_native_context(native, sid, authorize=lambda *args: True)['runtime_facts']['mistake_learning']
    assert invalid_graph['question'] == before['question']
    assert 'analysis' not in invalid_graph and 'practice' not in invalid_graph
    with db.connect() as c:
        c.execute('UPDATE mistake_learning_state SET graph_json=? WHERE mistake_id=?', (saved[1], mid))
        # An analysis and practice with no graph binding remain valid across a graph change.
        validation = json.loads(c.execute('SELECT validation_json FROM mistake_learning_item WHERE practice_item_id=?',
                                          (old_item['practice_item_id'],)).fetchone()[0])
        validation.pop('graph_target')
        c.execute('UPDATE mistake_learning_item SET validation_json=? WHERE practice_item_id=?',
                  (json.dumps(validation), old_item['practice_item_id']))
        c.execute('UPDATE mistake_learning_state SET analysis_context_key=? WHERE mistake_id=?',
                  (_main_learning.analysis_context_key(None, _tutor_route_identity(service.gateway)), mid))
        c.commit()
    unbound = load_native_context(native, sid, authorize=lambda *args: True)['runtime_facts']['mistake_learning']
    assert unbound['question'] == before['question']
    assert 'analysis' not in unbound and 'practice' not in unbound
    refreshed = service.state(mid, child_id=CHILD, session_id=sid)
    assert refreshed['graph_context']['graph_version'] == 'fixture-v2' and refreshed['analysis']
    restored = load_native_context(native, sid, authorize=lambda *args: True)['runtime_facts']['mistake_learning']
    assert restored['analysis'] and 'practice' not in restored


def test_native_mixed_legacy_batch_skips_stale_question_before_graph_refresh(world):
    from home_study_agent.realtime_capability_tools import RealtimeCapabilityTools
    from home_study_agent.realtime_native_context import load_native_context
    from home_study_agent.realtime_native_mistake_learning import compose

    db, service, _, mid, sid, _, _, privacy = world

    class Matcher(_AuthorizedSyntheticLabels):
        version = 'fixture-v1'
        def cache_version(self, *, connection=None): return self.version
        def match(self, child_id, source):
            return {'graph_version': self.version, 'status': 'matched',
                    'primary': {'key': 'same-key', 'title': '根的判别式', 'review_status': 'approved', 'confidence': .99},
                    'prerequisites': [], 'alternatives': []}

    service.graph_matcher = Matcher()
    privacy.update_preferences({'cloud_audio_allowed': True})
    post(world, 'focus', 'mixed-legacy-focus')
    initial_view = post(world, 'practice', 'mixed-legacy-practice', count=2).json()
    items = initial_view['practice_items']
    first, second = items
    with db.connect() as c:
        validation = json.loads(c.execute('SELECT validation_json FROM mistake_learning_item WHERE practice_item_id=?',
                                          (second['practice_item_id'],)).fetchone()[0])
        validation.pop('graph_target')
        c.execute('UPDATE mistake_learning_item SET validation_json=? WHERE practice_item_id=?',
                  (json.dumps(validation), second['practice_item_id']))
        before = [tuple(r) for r in c.execute('SELECT practice_item_id,validation_json,answered_at FROM mistake_learning_item WHERE mistake_id=? ORDER BY position', (mid,))]
        c.commit()
    service.graph_matcher.version = 'fixture-v2'
    native = RealtimeCapabilityTools(db)
    native.current_graph_version = service.graph_matcher.cache_version
    before_refresh = load_native_context(native, sid, authorize=lambda *args: True)['runtime_facts']['mistake_learning']
    assert 'practice' not in before_refresh
    with db.connect() as c:
        focus = native_focus(c, sid, current_graph_version='fixture-v2')
        assert focus['practice_item_id'] is None
        after = [tuple(r) for r in c.execute('SELECT practice_item_id,validation_json,answered_at FROM mistake_learning_item WHERE mistake_id=? ORDER BY position', (mid,))]
        assert after == before
    response = post(world,'answer','mixed-legacy-answer',practice_item_id=second['practice_item_id'],utterance='我的答案是没有实数解。')
    assert response.status_code==409 and response.json()['detail']['code']=='MISTAKE_LEARNING_PRACTICE_CHANGED'


def test_actual_cloud_http_analysis_practice_answers_persist_and_no_mastery(world):
    db,service,client,mid,sid,calls,controls,privacy=world
    with db.connect() as c:
        before={t:c.execute(f'SELECT count(*) FROM {t}').fetchone()[0] for t in ('learning_evidence','mistake_attempt','mistake_diagnosis')}
    focused=post(world,'focus','focus').json(); snapshot=focused['focus']; assert snapshot['focus_generation']==1
    a=post(world,'analyze','analysis',expected_revision=0);assert a.status_code==200,a.text
    assert a.json()['analysis']['knowledge_points'][0]['label']=='判别式新模型标签'
    assert a.json()['today']['completed_count']==0
    assert post(world,'analyze','analysis',expected_revision=0).json()==a.json();assert len(calls)==1
    # Existing source analysis is shared across UI/native IDs, not another cloud payment.
    assert post(world,'analyze','analysis-reused').json()['action_status']=='completed';assert len(calls)==1
    p=post(world,'practice','practice');assert p.status_code==200,p.text
    items=p.json()['practice_items'];assert len(items)==3, p.text
    assert all('expected_answer' not in item and item['feedback'] is None for item in items)
    for i,item in enumerate(items):
        reply=post(world,'answer',f'answer-{i}',practice_item_id=item['practice_item_id'],utterance='16，两个不同实数根')
        assert reply.status_code==200,reply.text
        assert reply.json()['action_status']=='completed',reply.text
    done=reply.json();assert done['today']['status']=='done';assert done['today']['completed_count']==3
    with db.connect() as c:
        assert {t:c.execute(f'SELECT count(*) FROM {t}').fetchone()[0] for t in before}==before
        assert native_focus(c,sid)['focus_generation']==1
        assert native_focus(c,sid)['practice_item_id'] is None
        assert validate_source(c,sid,snapshot,operation='analyze') is True
        assert native_receipt(c,sid,'answer-2')['status']=='completed'
    reopened=MistakeLearningService(Database(db.path),service.gateway)
    assert reopened.state(mid,child_id=CHILD)['today']==done['today']
    assert reopened.state(mid,child_id=CHILD)['turns']==done['turns']


def test_unknown_saved_original_never_retry_same_action_or_other_action(world):
    db,service,client,mid,sid,calls,controls,privacy=world
    post(world,'practice','practice')
    item=service.state(mid,child_id=CHILD)['practice_items'][0]['practice_item_id']
    controls['fail']=True
    failed=post(world,'answer','answer-unknown',practice_item_id=item,utterance='这是必须保留的公开原话')
    assert failed.status_code==200,failed.text
    assert failed.json()['action_status']=='unknown',failed.text
    number=len(calls)
    for key in ('answer-unknown','different-action'):
        retry=post(world,'answer',key,practice_item_id=item,utterance='这是必须保留的公开原话')
        assert retry.json()['action_status']=='unknown',retry.text
    assert len(calls)==number
    state=MistakeLearningService(Database(db.path),service.gateway).state(mid,child_id=CHILD)
    assert sum(t['text']=='这是必须保留的公开原话' for t in state['turns'])==1
    assert state['today']['completed_count']==0
    # A distinct explicit request may continue; the uncertain old action stays read only.
    controls['fail']=False;controls['response']={'phase':'question','text':'先看这道题，你能说出已知的条件吗？'}
    other=post(world,'coach','independent-coach',utterance='请改为讲解这个概念')
    assert other.json()['action_status']=='completed',other.text
    with db.connect() as c: assert native_receipt(c,sid,'answer-unknown')['status']=='unknown'


def test_two_failures_coaching_not_false_done_and_no_endless_generation(world):
    *_,controls,privacy=world
    p=post(world,'practice','practice').json()
    controls['response']=dict(FEEDBACK,judgement='incorrect')
    for n,item in enumerate(p['practice_items'][:2]):
        result=post(world,'answer',f'wrong-{n}',practice_item_id=item['practice_item_id'],utterance='我不确定')
    assert result.json()['today']['status']=='needs_explanation'
    assert post(world,'practice','more').status_code==409
    controls['response']={'phase':'explain_step','text':'先把平方和乘法分开算，看看符号从哪里变化。'}
    coached=post(world,'coach','coaching',utterance='请直接讲一讲',style='direct').json()
    assert coached['today']['status']!='done'
    assert coached['today']['completed_count']==2


def test_fresh_permission_source_child_and_native_guard(world):
    db,service,client,mid,sid,calls,controls,privacy=world
    path=f'/v1/mistakes/{mid}/learning'
    assert client.get(path,params={'child_id':'child-primary-3'}).status_code==404
    assert post(world,'analyze','native-no-guard',native_turn_id='invented').status_code==403
    controls['hook']=lambda:privacy.update_preferences({'cloud_model_text_allowed':False})
    denied=post(world,'analyze','revoked')
    assert denied.status_code==403,denied.text
    assert service.state(mid,child_id=CHILD)['analysis'] is None
    privacy.update_preferences({'cloud_model_text_allowed':True})
    controls['hook']=None
    post(world,'focus','focus')
    with db.connect() as c: snap=native_focus(c,sid)
    with db.connect() as c:
        c.execute('UPDATE mistake_case SET submitted_answer=? WHERE mistake_id=?',('公开更正的原答',mid));c.commit()
    with db.connect() as c:
        with pytest.raises(ValueError):validate_source(c,sid,snap,operation='answer')
    client.headers['x-public-auth']='no'
    assert client.get(path,params={'child_id':CHILD}).status_code==403


def test_focus_generation_changes_only_real_selection_or_leave_and_receipt_stable(world):
    db,service,client,mid,sid,*_=world
    first=post(world,'focus','first').json()['focus']
    second=post(world,'focus','second').json()['focus']
    assert first==second
    post(world,'analyze','analyze')
    with db.connect() as c:
        frozen=native_receipt(c,sid,'analyze')
    post(world,'coach','coach',utterance='先给我解释这个概念')
    with db.connect() as c:
        assert native_receipt(c,sid,'analyze')==frozen
        assert native_focus(c,sid)['focus_generation']==first['focus_generation']
    service.invalidate_focus(sid,CHILD,first['focus_generation'])
    third=post(world,'focus','third').json()['focus']
    assert third['focus_generation']>first['focus_generation']
    with db.connect() as c:
        with pytest.raises(ValueError):validate_source(c,sid,first,operation='answer')


def test_intake_without_graph_original_answer_recovers_in_same_library(world):
    from home_study_agent.mistake_learning_intake import MistakeLearningIntake
    db,service,client,oldmid,sid,calls,controls,privacy=world
    card=MistakeLearningIntake(db).create(child_id=CHILD,session_id=sid,question='地球自转和公转分别会带来哪些现象？我想先理清两个概念。',subject='地理',client_action_id='new-no-graph')
    mid=card['mistake_id']
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM mistake_case WHERE mistake_id=?',(mid,)).fetchone()[0]==0
    result=client.post(f'/v1/mistakes/{mid}/learning/analyze',json={'child_id':CHILD,'session_id':sid,'client_action_id':'no-answer-analysis'})
    assert result.status_code==200,result.text
    assert result.json()['source']['original_answer']==''
    assert result.json()['action_status']=='completed',result.text
    library=service.library(CHILD)
    assert {x['mistake_id'] for x in library['items']}=={oldmid,mid}
    reread=MistakeLearningService(Database(db.path),service.gateway).state(mid,child_id=CHILD)
    assert reread['analysis']==result.json()['analysis']


def test_invalid_generation_verifier_and_strict_args_never_publish_items(world):
    db,service,client,mid,sid,calls,controls,privacy=world
    controls['response']={'candidates':PRACTICE['candidates'][:1]}
    bad=post(world,'practice','bad-generation')
    assert bad.json()['action_status']=='failed'
    assert service.state(mid,child_id=CHILD)['practice_items']==[]
    controls['response']=None
    # Reject an independently checked wrong/ambiguous answer before exposing any exercise.
    original=controls['hook']
    def change_for_second():
        if len(calls)==3: controls['response']={'checks':[{'position':i+1,'valid':False} for i in range(3)]}
    controls['hook']=change_for_second
    failed=post(world,'practice','bad-verifier')
    assert failed.json()['action_status']=='failed',failed.text
    assert service.state(mid,child_id=CHILD)['practice_items']==[]
    path=f'/v1/mistakes/{mid}/learning/analyze'
    for extra in ({'count':True},{'count':4},{'expected_revision':True},{'invented':'field'}):
        response=client.post(path,json={'child_id':CHILD,'client_action_id':'bad-field',**extra})
        assert response.status_code==422,response.text


def test_native_guard_fresh_post_model_and_source_revision_change(world):
    db,service,client,mid,sid,calls,controls,privacy=world
    live=[True]
    app=FastAPI();app.include_router(create_router(service,authorize_request=lambda _:True,native_guard=lambda c,**kw:live[0]))
    guarded=TestClient(app)
    controls['hook']=lambda:live.__setitem__(0,False)
    result=guarded.post(f'/v1/mistakes/{mid}/learning/analyze',json={'child_id':CHILD,'session_id':sid,'client_action_id':'native-guard','native_turn_id':'claimed-by-host'})
    assert result.status_code==403,result.text
    assert service.state(mid,child_id=CHILD)['analysis'] is None
    live[0]=True
    def replace_question():
        with db.connect() as c:
            c.execute('UPDATE mistake_case SET prompt_summary=? WHERE mistake_id=?',('新的公开题干，不应接纳旧的模型结果。',mid));c.commit()
    controls['hook']=replace_question
    result=post(world,'analyze','changed-source')
    assert result.json()['action_status']=='failed',result.text
    assert service.state(mid,child_id=CHILD)['analysis'] is None
    guarded.close()


def test_single_question_requests_quota_uncertain_and_read_only_action(world):
    db,service,client,mid,sid,calls,controls,privacy=world
    # The real model is asked for one; an independently checked one-item result.
    controls['response']={'candidates':PRACTICE['candidates'][:1]}
    def verify_one():
        if len(calls)==2: controls['response']={'checks':[{'position':1,'valid':True}]}
    controls['hook']=verify_one
    p=post(world,'practice','single',count=1)
    assert p.json()['action_status']=='completed',p.text
    assert p.json()['today']['target_count']==2
    item=p.json()['practice_items'][0]['practice_item_id']
    controls['hook']=None;controls['response']=dict(FEEDBACK,judgement='uncertain')
    a=post(world,'answer','uncertain',practice_item_id=item,utterance='这个说法我不确定').json()
    assert a['today']['completed_count']==0
    assert a['practice_items'][0]['attempts']==1
    controls['response']=FEEDBACK
    a=post(world,'answer','certain',practice_item_id=item,utterance='16，有两个不同实根').json()
    assert a['today']['completed_count']==1
    saved=client.get(f'/v1/mistakes/{mid}/learning/actions/certain',params={'child_id':CHILD})
    assert saved.json()==a
    assert service.read_action('certain',child_id=CHILD,mistake_id=mid)==a


def test_reusable_practice_has_own_durable_receipt_without_new_cloud(world):
    db,service,client,mid,sid,calls,controls,privacy=world
    first=post(world,'practice','practice-original').json()
    number=len(calls)
    second=post(world,'practice','practice-voice',expected_revision=first['revision']).json()
    assert second['practice_items']==first['practice_items']
    assert len(calls)==number
    with db.connect() as c:
        assert native_receipt(c,sid,'practice-voice')['status']=='completed'
    assert service.read_action('practice-voice',child_id=CHILD,mistake_id=mid)==second


def test_simultaneous_ui_voice_analysis_single_model_and_read_only_wait(world):
    db,service,client,mid,sid,calls,controls,privacy=world
    async def run():
        started,release=asyncio.Event(),asyncio.Event();controls['barrier']=(started,release)
        app=FastAPI();app.include_router(create_router(service,authorize_request=lambda _:True))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://synthetic') as http:
            path=f'/v1/mistakes/{mid}/learning/analyze'
            body={'child_id':CHILD,'session_id':sid,'client_action_id':'ui-analysis'}
            task=asyncio.create_task(http.post(path,json=body))
            await asyncio.wait_for(started.wait(),3)
            other=await http.post(path,json={**body,'client_action_id':'voice-analysis'})
            assert other.json()['action_status']=='processing'
            assert other.json()['pending_client_action_id']=='ui-analysis'
            observed=await http.get(f'/v1/mistakes/{mid}/learning/actions/ui-analysis',params={'child_id':CHILD})
            assert observed.json()['action_status']=='processing'
            assert len(calls)==1
            release.set();done=await task
            assert done.json()['action_status']=='completed'
            other=await http.post(path,json={**body,'client_action_id':'voice-analysis'})
            assert other.json()['action_status']=='completed'
            assert len(calls)==1
            assert other.json()['analysis']==done.json()['analysis']
    asyncio.run(run())


def test_next_day_review_new_share_preserves_historical_frozen_answers(world,monkeypatch):
    import home_study_agent.mistake_learning as module
    from datetime import datetime,timedelta
    db,service,client,mid,sid,calls,controls,privacy=world
    first=post(world,'practice','day-one').json()
    old=first['practice_items'][0]['practice_item_id']
    post(world,'answer','day-one-answer',practice_item_id=old,utterance='16，有两个不同实根')
    real=datetime
    class Tomorrow(datetime):
        @classmethod
        def now(cls,tz=None):return real.now(tz)+timedelta(days=1)
    monkeypatch.setattr(module,'datetime',Tomorrow)
    assert service.state(mid,child_id=CHILD)['today']['completed_count']==0
    fresh=post(world,'practice','day-two').json()
    assert fresh['action_status']=='completed'
    assert old not in {i['practice_item_id'] for i in fresh['practice_items']}
    assert fresh['today']['completed_count']==0
    assert post(world,'answer','stale-question',practice_item_id=old,utterance='不能串到旧题').status_code==409
    with db.connect() as c:
        assert c.execute('SELECT answered_at FROM mistake_learning_item WHERE practice_item_id=?',(old,)).fetchone()[0]


def test_today_pending_share_stays_due_in_state_library_and_reopen(world):
    db,service,client,mid,sid,calls,controls,privacy=world
    batch=post(world,'practice','today-share').json()
    item=batch['practice_items'][0]['practice_item_id']
    saved=post(world,'answer','today-first',practice_item_id=item,utterance='16，两个不同实根').json()
    assert saved['next_review_at']>saved['today']['date']
    assert saved['today']['remaining_count']==2
    assert saved['review_due'] is True
    library=service.library(CHILD)
    assert library['today']['due_count']==1
    assert library['today']['remaining_questions']==2
    reopened=MistakeLearningService(Database(db.path),service.gateway).state(mid,child_id=CHILD)
    assert reopened['review_due'] is True
    assert reopened['today']==saved['today']


def test_repeated_single_request_cannot_fill_share_with_identical_question(world):
    db,service,client,mid,sid,calls,controls,privacy=world
    controls['response']={'candidates':PRACTICE['candidates'][:1]}
    def verifier():
        if len(calls)==2: controls['response']={'checks':[{'position':1,'valid':True}]}
    controls['hook']=verifier
    item=post(world,'practice','first-single',count=1).json()['practice_items'][0]
    controls['hook']=None;controls['response']=FEEDBACK
    post(world,'answer','first-single-answer',practice_item_id=item['practice_item_id'],utterance='16，两个不同实根')
    controls['response']={'candidates':PRACTICE['candidates'][:1]}
    prior_calls=len(calls)
    duplicate=post(world,'practice','second-identical',count=1).json()
    assert len(calls)==prior_calls+1
    assert duplicate['action_status']=='failed'
    assert len(duplicate['practice_items'])==1
    assert duplicate['today']['completed_count']==1
    actual_input=json.loads(calls[-1]['messages'][1]['content'])
    assert actual_input['previous_questions']==[item['question']]


def test_native_due_context_without_focus_readonly_bounded_no_answers_ids(world):
    from home_study_agent.mistake_learning import native_due_context
    db,service,client,mid,sid,calls,controls,privacy=world
    privacy.update_preferences({'cloud_audio_allowed':True})
    with db.connect() as c:
        before=c.total_changes
        assert native_focus(c,sid) is None
        result=native_due_context(c,sid)
        assert c.total_changes==before
    assert result['due_count']==1
    assert result['remaining_questions']==3
    assert result['items']==[{'title':'判别式复习','subject':'数学','remaining_count':3}]
    assert all(word not in json.dumps(result,ensure_ascii=False) for word in (mid,CHILD,sid,'判别式是4','practice_item_id','source_revision'))
    privacy.update_preferences({'cloud_model_text_allowed':False})
    with db.connect() as c:
        with pytest.raises(ValueError):native_due_context(c,sid)


def test_generation_flip_discards_late_failure_cleanup_and_old_action_never_retries(world):
    from uuid import uuid4
    db,service,client,mid,sid,calls,controls,privacy=world
    def new_generation():
        with db.connect() as c:
            c.execute('UPDATE privacy_storage_generation SET generation=? WHERE singleton=1',(str(uuid4()),));c.commit()
    controls['hook']=new_generation
    result=post(world,'analyze','old-generation').json()
    assert result['action_status']=='unknown',result
    assert result['status']=='unknown'
    assert service.read_action('old-generation',child_id=CHILD,mistake_id=mid)['action_status']=='unknown'
    with db.connect() as c:
        row=c.execute('SELECT status FROM mistake_learning_action WHERE client_action_id=?',('old-generation',)).fetchone()
        assert row['status']=='processing'  # Old generation cannot authorize cleanup writes.
    number=len(calls);controls['hook']=None
    assert post(world,'analyze','old-generation').json()['action_status']=='unknown'
    assert len(calls)==number
    fresh=post(world,'analyze','new-generation-explicit').json()
    assert fresh['action_status']=='completed',fresh
    assert len(calls)==number+1


def test_old_source_failed_action_cannot_overwrite_new_source_state(world):
    import home_study_agent.mistake_learning as module
    db,service,client,mid,sid,calls,controls,privacy=world
    def new_source():
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('UPDATE mistake_case SET prompt_summary=? WHERE mistake_id=?',('全新公开更正题干：请比较四个季节的昼夜长短。',mid))
            revision=module._source(c,CHILD,mid)[1]
            service._state_row(c,mid,CHILD,revision)  # Actual new-operation initialization, no fabricated model result.
            c.commit()
    controls['hook']=new_source
    result=post(world,'analyze','old-source-request').json()
    assert result['action_status']=='failed'
    current=service.state(mid,child_id=CHILD)
    assert current['status']=='ready'
    assert current['error_code'] is None
    assert current['analysis'] is None
    with db.connect() as c:
        assert native_receipt(c,sid,'old-source-request')['status']=='failed'


def test_previous_process_processing_is_unknown_not_permanent_case_lock(world,monkeypatch):
    from uuid import uuid4
    import home_study_agent.mistake_learning as module
    db,service,client,mid,sid,calls,controls,privacy=world
    controls['fail']=True
    original_cleanup=service._failed
    # Failure injection models an abrupt process exit before its cleanup write.
    monkeypatch.setattr(service,'_failed',lambda *args:False)
    post(world,'analyze','abandoned-process')
    monkeypatch.setattr(service,'_failed',original_cleanup)
    with db.connect() as c:
        assert c.execute('SELECT status FROM mistake_learning_action WHERE client_action_id=?',('abandoned-process',)).fetchone()[0]=='processing'
    monkeypatch.setattr(module,'_PROCESS_RUN',str(uuid4()))
    assert service.read_action('abandoned-process',child_id=CHILD,mistake_id=mid)['action_status']=='unknown'
    number=len(calls)
    assert post(world,'analyze','abandoned-process').json()['action_status']=='unknown'
    assert len(calls)==number
    controls['fail']=False;controls['response']={'phase':'question','text':'这两个已知量之间的关系是什么？'}
    result=post(world,'coach','new-process-explicit',utterance='先不等刚才的分析，请直接讲基本概念').json()
    assert result['action_status']=='completed',result
    assert len(calls)==number+2
    assert '中立事实核查员' in calls[-2]['messages'][0]['content']
    assert json.loads(calls[-1]['messages'][1]['content'])['model_check']['claim_status']=='no_claim'
    assert result['turns'][-1]['text']==controls['response']['text']


def test_cancelled_cloud_call_preserves_original_then_allows_distinct_new_work(world):
    db,service,client,mid,sid,calls,controls,privacy=world
    async def run():
        started,release=asyncio.Event(),asyncio.Event();controls['barrier']=(started,release)
        task=asyncio.create_task(service.coach(mid,child_id=CHILD,session_id=sid,client_action_id='cancelled-call',utterance='这是取消前已保存的公开原话'))
        await asyncio.wait_for(started.wait(),3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        assert service.read_action('cancelled-call',child_id=CHILD,mistake_id=mid)['action_status']=='unknown'
        assert sum(t['text']=='这是取消前已保存的公开原话' for t in service.state(mid,child_id=CHILD)['turns'])==1
        number=len(calls)
        controls['barrier']=None
        result=await service.coach(mid,child_id=CHILD,session_id=sid,client_action_id='fresh-after-cancel',utterance='现在换个方法解释这个概念')
        assert result['action_status']=='completed'
        assert len(calls)==number+2
        assert '中立事实核查员' in calls[-2]['messages'][0]['content']
        assert json.loads(calls[-1]['messages'][1]['content'])['model_check']['claim_status']=='no_claim'
    asyncio.run(run())


def test_practice_generation_and_verification_share_one_total_deadline(world,monkeypatch):
    import home_study_agent.mistake_learning as module
    seen=[];original=asyncio.wait_for
    async def bounded(coro,timeout):
        if getattr(getattr(coro,'cr_code',None),'co_name',None)=='complete':seen.append(timeout)
        return await original(coro,timeout)
    monkeypatch.setattr(module.asyncio,'wait_for',bounded)
    result=post(world,'practice','shared-deadline')
    assert result.json()['action_status']=='completed'
    assert len(seen)==2
    assert 0<seen[1]<seen[0]<=115


def test_generation_change_between_reservation_commit_and_snapshot_never_dispatches(world,monkeypatch):
    from uuid import uuid4
    from home_study_agent.database import StudyConnection
    db,service,client,mid,sid,calls,controls,privacy=world
    original=StudyConnection.commit;advanced=[False]
    def commit(c):
        original(c)
        if not advanced[0] and c.execute('SELECT 1 FROM mistake_learning_action WHERE client_action_id=?',('commit-boundary',)).fetchone():
            advanced[0]=True
            with db.connect() as other:
                other.execute('UPDATE privacy_storage_generation SET generation=? WHERE singleton=1',(str(uuid4()),));original(other)
    monkeypatch.setattr(StudyConnection,'commit',commit)
    result=post(world,'analyze','commit-boundary').json()
    assert advanced[0]
    assert len(calls)==0
    assert result['action_status']=='unknown',result
    assert result['analysis'] is None



def test_real_matcher_keeps_source_candidate_uncertain_and_persisted(world, tmp_path):
    from home_study_agent.mistake_graph import MistakeGraphMatcher
    from home_study_agent.mistake_graph_study import study_targets
    from test_atomic_companion_context import _fixture_build
    import sqlite3

    db, service, client, _, sid, _, _, _ = world
    build = tmp_path / 'actual-atomic.db'
    _fixture_build(build)
    with sqlite3.connect(build) as connection:
        connection.execute('INSERT INTO kg_atomic_definition_draft VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            ('sign', 'sign', '判断不等式两边乘除负数时不等号方向改变', '初中7-9', '数学', 'concept',
             '{}', '', '{}', '[]', '[]', '[]', .9, 'unreviewed'))
    service.graph_matcher = MistakeGraphMatcher(db, build)
    question = '不等式两边乘负数方向变吗'
    mid = MistakeBookService(db).capture(child_id=CHILD, knowledge_node_id='node-j3-math-discriminant',
        subject='数学', title='原题', prompt_summary=question, submitted_answer='不变', source_type='diagnostic',
        source_ref='synthetic-inequality', confidence=.8, client_action_id='synthetic-inequality-capture',
        diagnostic_item_id='diag-j3-discriminant-1')['mistake_id']
    response = client.get(f'/v1/mistakes/{mid}/learning', params={'child_id': CHILD, 'session_id': sid})
    assert response.status_code == 200
    first = response.json()
    graph = first['graph_context']
    assert graph['status'] == 'ambiguous' and graph['primary'] is None
    assert graph['alternatives'][0]['key'] == 'sign'
    assert study_targets(graph) == []
    reopened = MistakeLearningService(Database(db.path), service.gateway, graph_matcher=service.graph_matcher)
    saved = reopened.state(mid, child_id=CHILD, session_id=sid)
    assert saved['graph_context'] == graph


def test_semantic_graph_choice_uses_existing_analyze_call_and_persists(world, tmp_path):
    from hashlib import sha256
    from home_study_agent.mistake_graph import MistakeGraphMatcher
    from test_atomic_companion_context import _fixture_build
    import sqlite3

    db, service, client, _, sid, calls, controls, _ = world
    build = tmp_path / 'authorized-atomic.db'
    _fixture_build(build)
    title = '判断不等式乘数符号未知时需要分类讨论'
    with sqlite3.connect(build) as connection:
        connection.execute('CREATE TABLE kg_resource(resource_id TEXT PRIMARY KEY,review_status TEXT)')
        connection.execute('CREATE TABLE kg_atomic_draft_evidence(draft_id TEXT,resource_id TEXT,exact_quote TEXT)')
        connection.execute('CREATE TABLE kg_atomic_label_cloud_right(draft_id TEXT PRIMARY KEY,title_sha256 TEXT,origin TEXT,basis TEXT,status TEXT,created_at TEXT)')
        connection.execute("INSERT INTO kg_resource VALUES('private-book','restricted')")
        connection.execute('INSERT INTO kg_atomic_definition_draft VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            ('sign','sign',title,'初中7-9','数学','concept','{}','','{}','[]','[]','[]',.9,'approved'))
        connection.execute('INSERT INTO kg_atomic_definition_draft VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            ('private-pre','private-pre','未授权前置标签仅保留本地','初中7-9','数学','concept','{}','','{}','[]','[]','[]',.9,'unreviewed'))
        connection.execute('INSERT INTO kg_atomic_relation_candidate VALUES(?,?,?,?,?,?,?)',
            ('pre-to-sign','private-pre','sign','prerequisite_of','先学这一项',.9,'unreviewed'))
        connection.execute("INSERT INTO kg_atomic_draft_evidence VALUES('sign','private-book','教材本地私有证据不可外发')")
        connection.execute('INSERT INTO kg_atomic_label_cloud_right VALUES(?,?,?,?,?,?)',
            ('sign',sha256(title.encode()).hexdigest(),'project_authored','synthetic-test','active','now'))
    service.graph_matcher = MistakeGraphMatcher(db, build)
    question = '不等式乘数符号未知怎么办'
    mid = MistakeBookService(db).capture(child_id=CHILD, knowledge_node_id='node-j3-math-discriminant',
        subject='数学', title='原题', prompt_summary=question, submitted_answer='只看正数', source_type='diagnostic',
        source_ref='synthetic-semantic', confidence=.8, client_action_id='synthetic-semantic-capture',
        diagnostic_item_id='diag-j3-discriminant-1')['mistake_id']
    before = client.get(f'/v1/mistakes/{mid}/learning', params={'child_id':CHILD,'session_id':sid}).json()
    assert before['graph_context']['status'] == 'ambiguous'
    controls['response'] = ANALYSIS | {'graph_assessments':[{
        'ref':'C1','verdict':'same_skill','confidence':.96,
        'source_quote':'不等式乘数符号未知','label_quote':'不等式乘数符号未知','missing_qualifiers':[],
    }]}
    response = client.post(f'/v1/mistakes/{mid}/learning/analyze', json={
        'child_id':CHILD, 'session_id':sid, 'client_action_id':'synthetic-semantic-analyze'})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['action_status'] == 'completed', result
    assert result['graph_context']['status'] == 'matched'
    assert result['graph_context']['primary']['key'] == 'sign'
    assert result['graph_context']['primary']['review_status'] == 'approved'
    assert result['graph_context']['verification']['method'] == 'model_semantic_v1'
    assert result['graph_context']['prerequisites'][0]['key'] == 'private-pre'
    assert result['analysis']
    serialized = json.dumps(calls, ensure_ascii=False)
    assert title in serialized and '教材本地私有证据不可外发' not in serialized and 'private-book' not in serialized
    reopened = MistakeLearningService(Database(db.path), service.gateway, graph_matcher=service.graph_matcher)
    saved = reopened.state(mid, child_id=CHILD, session_id=sid)
    assert saved['graph_context'] == result['graph_context'] and saved['analysis'] == result['analysis']
    controls['coach_response'] = {'phase':'question','text':'乘数可能有哪些符号？'}
    verified_coach = client.post(f'/v1/mistakes/{mid}/learning/coach', json={
        'child_id':CHILD,'session_id':sid,'client_action_id':'verified-coach',
        'original_review':True,'style':'socratic','utterance':'请继续讲。'}).json()
    assert verified_coach['action_status'] == 'completed'
    assert verified_coach['graph_context']['prerequisites'][0]['key'] == 'private-pre'
    assert '未授权前置标签仅保留本地' not in calls[-1]['messages'][1]['content']

    def another_case(key):
        return MistakeBookService(db).capture(child_id=CHILD, knowledge_node_id='node-j3-math-discriminant',
            subject='数学', title='原题', prompt_summary=question, submitted_answer='只看正数', source_type='diagnostic',
            source_ref='synthetic-'+key, confidence=.8, client_action_id='capture-'+key,
            diagnostic_item_id='diag-j3-discriminant-1')['mistake_id']

    invalid = another_case('invalid-semantic')
    controls['response'] = ANALYSIS | {'graph_assessments':[{
        'ref':'C9','verdict':'same_skill','confidence':.99,
        'source_quote':'不等式乘数符号未知','label_quote':'不等式乘数符号未知','missing_qualifiers':[],
    }]}
    rejected = client.post(f'/v1/mistakes/{invalid}/learning/analyze', json={
        'child_id':CHILD, 'session_id':sid, 'client_action_id':'invalid-semantic-analyze'}).json()
    assert rejected['action_status'] == 'completed'
    assert rejected['graph_context']['status'] == 'ambiguous' and rejected['analysis']

    coaching = another_case('coach-semantic')
    controls['coach_response'] = {'phase':'question', 'text':'乘数可能有哪些符号？', 'graph_assessments':[{
        'ref':'C1','verdict':'same_skill','confidence':.96,
        'source_quote':'不等式乘数符号未知','label_quote':'不等式乘数符号未知','missing_qualifiers':[],
    }]}
    coached = client.post(f'/v1/mistakes/{coaching}/learning/coach', json={
        'child_id':CHILD, 'session_id':sid, 'client_action_id':'coach-semantic-open',
        'original_review':True, 'style':'socratic', 'utterance':'请一步一步讲。'}).json()
    assert coached['action_status'] == 'completed', coached
    assert coached['graph_context']['status'] == 'matched'
    assert coached['graph_context']['primary']['review_status'] == 'approved'
    assert coached['coach_phase'] == 'question'

    with sqlite3.connect(build) as connection:
        connection.execute("UPDATE kg_atomic_label_cloud_right SET status='revoked' WHERE draft_id='sign'")
    revoked = another_case('revoked-semantic')
    controls['response'] = ANALYSIS | {'graph_assessments':[{
        'ref':'C1','verdict':'same_skill','confidence':.99,
        'source_quote':'不等式乘数符号未知','label_quote':'不等式乘数符号未知','missing_qualifiers':[],
    }]}
    fallback = client.post(f'/v1/mistakes/{revoked}/learning/analyze', json={
        'child_id':CHILD, 'session_id':sid, 'client_action_id':'revoked-semantic-analyze'}).json()
    assert fallback['action_status'] == 'completed' and fallback['graph_context']['status'] == 'ambiguous'
    assert 'graph_options' not in json.loads(calls[-1]['messages'][1]['content'])


def test_persisted_practice_focus_rechecks_label_rights_before_cloud(world):
    db, service, client, mid, sid, calls, controls, _ = world
    label = '本地私有知识标签不得外发'
    class Matcher:
        version = 'fixture-practice-rights'
        allowed = True
        def cache_version(self, *, connection=None): return self.version
        def semantic_options(self, graph):
            item = graph['alternatives'][0]
            return ([{'ref':'C1','label':item['title']}], {'C1':item['key']}) if self.allowed else ([], {})
        def match(self, child_id, source):
            return {'graph_version':self.version,'status':'matched',
                    'primary':{'key':'local-label','title':label,'review_status':'approved','confidence':.9},
                    'prerequisites':[],'alternatives':[]}
    matcher = Matcher()
    service.graph_matcher = matcher
    assert post(world, 'analyze', 'rights-analysis').json()['action_status'] == 'completed'
    practice = post(world, 'practice', 'rights-practice', count=1).json()
    item = practice['practice_items'][0]
    assert item['graph_target']['title'] == label
    matcher.allowed = False
    before=len(calls)
    reply = post(world, 'coach', 'rights-coach', practice_item_id=item['practice_item_id'],
                 utterance='我不明白，请继续引导。')
    assert reply.status_code==409 and reply.json()['detail']['code']=='MISTAKE_LEARNING_CLOUD_LABEL_RIGHT_REQUIRED'
    assert len(calls)==before


def test_unreviewed_graph_stays_hypothesis_across_api_cloud_and_restart(world):
    db, service, client, mid, sid, calls, _, _ = world
    class Matcher:
        def cache_version(self, *, connection=None):
            return 'fixture-unreviewed-v1'
        def semantic_options(self, graph):
            item = graph['alternatives'][0]
            return [{'ref': 'C1', 'label': item['title']}], {'C1': item['key']}
        def match(self, *args, **kwargs):
            return {'graph_version': 'fixture-unreviewed-v1', 'status': 'matched',
                    'primary': {'key': 'main', 'title': '未审主知识点', 'review_status': 'unreviewed', 'confidence': .99},
                    'prerequisites': [{'key': 'pre', 'title': '未审前置', 'review_status': 'unreviewed',
                                       'relation_review_status': 'unreviewed'}], 'alternatives': []}
    service.graph_matcher = Matcher()
    result = post(world, 'analyze', 'unreviewed-analyze').json()
    assert result['action_status'] == 'completed', result
    assert result['graph_context']['primary']['review_status'] == 'unreviewed'
    assert result['graph_review_focus'] is None
    sent = json.dumps(calls, ensure_ascii=False)
    assert '未审主知识点' not in sent and '未审前置' not in sent
    reopened = MistakeLearningService(Database(db.path), service.gateway, graph_matcher=service.graph_matcher)
    saved = reopened.state(mid, child_id=CHILD, session_id=sid)
    assert saved['graph_context']['primary']['review_status'] == 'unreviewed'
    assert saved['graph_review_focus'] is None


@pytest.mark.parametrize('changed',['primary','relation','unknown'])
def test_historical_practice_target_requires_current_review_even_when_graph_hash_matches(world,changed):
    from home_study_agent.mistake_learning import _hash,native_focus,_tutor_route_identity
    from home_study_agent.realtime_capability_tools import RealtimeCapabilityTools
    from home_study_agent.realtime_native_context import load_native_context
    db,service,_,mid,sid,calls,_,privacy=world
    class Matcher(_AuthorizedSyntheticLabels):
        available=True
        def cache_version(self,*,connection=None):return 'historical-review-v1' if self.available else None
        def match(self,*args,**kwargs):
            return {'graph_version':'historical-review-v1','status':'matched',
                'primary':{'key':'main','title':'已审主节点','review_status':'approved','confidence':.99},
                'prerequisites':[{'key':'pre','title':'已审前置','review_status':'approved','relation_review_status':'approved'}],
                'alternatives':[]}
    service.graph_matcher=Matcher()
    post(world,'focus','historical-focus')
    generated=post(world,'practice','historical-practice',count=2).json()
    assert generated['action_status']=='completed'
    first=generated['practice_items'][0]
    with db.connect() as c:
        graph=json.loads(c.execute('SELECT graph_json FROM mistake_learning_state WHERE mistake_id=?',(mid,)).fetchone()[0])
        if changed=='primary': graph['primary']['review_status']='unreviewed'
        elif changed=='relation': graph['prerequisites'][0]['relation_review_status']='unreviewed'
        else: service.graph_matcher.available=False
        c.execute('UPDATE mistake_learning_state SET graph_json=? WHERE mistake_id=?',(json.dumps(graph,ensure_ascii=False),mid))
        for item in c.execute('SELECT practice_item_id,validation_json FROM mistake_learning_item WHERE mistake_id=?',(mid,)).fetchall():
            validation=json.loads(item['validation_json']); validation['graph_target']['graph_sha']=_hash(graph)
            c.execute('UPDATE mistake_learning_item SET validation_json=? WHERE practice_item_id=?',(json.dumps(validation,ensure_ascii=False),item['practice_item_id']))
        c.execute("UPDATE mistake_learning_item SET feedback_json=?,answered_at='2026-01-01T00:00:00+00:00' WHERE practice_item_id=?",
                  (json.dumps(FEEDBACK,ensure_ascii=False),generated['practice_items'][1]['practice_item_id']))
        c.commit()
        preserved=[tuple(row) for row in c.execute('SELECT practice_item_id,question,feedback_json FROM mistake_learning_item WHERE mistake_id=? ORDER BY practice_item_id',(mid,))]
    state=service.state(mid,child_id=CHILD,session_id=sid)
    assert state['practice_items'][0]['status']=='stale' and state['practice_items'][0]['graph_target'] is None
    replay=post(world,'practice','historical-practice',count=2).json()
    assert replay['practice_items'][0]['status']=='stale' and replay['practice_items'][0]['graph_target'] is None
    receipt=service.read_action('historical-practice',child_id=CHILD,mistake_id=mid)
    assert receipt['practice_items'][0]['status']=='stale' and receipt['practice_items'][0]['graph_target'] is None
    with db.connect() as c:
        focus=native_focus(c,sid,current_graph_version=None if changed=='unknown' else 'historical-review-v1')
        assert focus['practice_item_id']!=first['practice_item_id']
    privacy.update_preferences({'cloud_audio_allowed':True})
    native=RealtimeCapabilityTools(db,current_graph_version=service.graph_matcher.cache_version,
        tutor_route_identity=lambda:_tutor_route_identity(service.gateway))
    projected=load_native_context(native,sid,authorize=lambda *args:True)['runtime_facts']['mistake_learning']
    assert 'practice' not in projected
    before_calls=len(calls)
    result=post(world,'answer','historical-answer',practice_item_id=first['practice_item_id'],utterance='我算完了。')
    assert result.status_code==409 and result.json()['detail']['code']=='MISTAKE_LEARNING_PRACTICE_CHANGED'
    coaching=post(world,'coach','historical-coach',practice_item_id=first['practice_item_id'],utterance='讲讲这一题。')
    assert coaching.status_code==409 and coaching.json()['detail']['code']=='MISTAKE_LEARNING_PRACTICE_CHANGED'
    assert len(calls)==before_calls
    with db.connect() as c:
        assert [tuple(row) for row in c.execute('SELECT practice_item_id,question,feedback_json FROM mistake_learning_item WHERE mistake_id=? ORDER BY practice_item_id',(mid,))]==preserved


def test_revoked_label_right_blocks_old_graph_practice_from_cloud_and_native(world):
    from home_study_agent.mistake_learning import _tutor_route_identity
    from home_study_agent.realtime_capability_tools import RealtimeCapabilityTools
    from home_study_agent.realtime_native_context import load_native_context
    db,service,_,mid,sid,calls,controls,privacy=world
    class Matcher:
        allowed=True
        def cache_version(self,*,connection=None):return 'rights-stable-v1'
        def match(self,*args,**kwargs):
            return {'graph_version':'rights-stable-v1','status':'matched','subject':'数学',
                'primary':{'key':'sensitive-label','title':'授权标签哨兵','review_status':'approved','confidence':.99},
                'prerequisites':[],'alternatives':[]}
        def semantic_options(self,graph):
            if not self.allowed:return [],{}
            item=graph['alternatives'][0]
            return [{'ref':'C1','label':item['title']}],{'C1':item['key']}
    matcher=Matcher();service.graph_matcher=matcher
    controls['practice_response']={'candidates':[PRACTICE['candidates'][0] | {'checkpoint_ref':'MAIN',
        'prompt':'授权标签哨兵：方程 x²-2x-3=0 的判别式是多少？',
        'explanation':'授权标签哨兵：判别式4+12=16。'}]}
    privacy.update_preferences({'cloud_audio_allowed':True})
    post(world,'focus','right-focus')
    generated=post(world,'practice','right-practice',count=1).json()
    assert generated['action_status']=='completed' and generated['practice_items'][0]['status']=='ready'
    item=generated['practice_items'][0];matcher.allowed=False
    before=len(calls)
    assert service.state(mid,child_id=CHILD,session_id=sid)['practice_items'][0]['question']==item['question']
    for operation in ('answer','coach'):
        response=post(world,operation,'right-revoked-'+operation,practice_item_id=item['practice_item_id'],utterance='我算好了。')
        assert response.status_code==409 and response.json()['detail']['code']=='MISTAKE_LEARNING_CLOUD_LABEL_RIGHT_REQUIRED'
    native=RealtimeCapabilityTools(db,current_graph_version=matcher.cache_version,
        tutor_route_identity=lambda:_tutor_route_identity(service.gateway),graph_cloud_authorizer=service._graph_cloud_allowed)
    projected=load_native_context(native,sid,authorize=lambda *args:True)['runtime_facts']['mistake_learning']
    assert 'practice' not in projected and '授权标签哨兵' not in json.dumps(projected,ensure_ascii=False)
    assert len(calls)==before
    matcher.allowed=True
    controls['hook']=lambda:setattr(matcher,'allowed',False)
    result=post(world,'answer','right-revoked-in-flight',practice_item_id=item['practice_item_id'],utterance='我算好了。').json()
    assert result['action_status']=='failed'
    with db.connect() as c:
        assert c.execute('SELECT answered_at FROM mistake_learning_item WHERE practice_item_id=?',(item['practice_item_id'],)).fetchone()[0] is None


def test_revoked_label_right_does_not_resend_prior_graph_coaching_history(world):
    _,service,_,_,_,calls,controls,_=world
    class Matcher:
        allowed=True
        def cache_version(self,*,connection=None):return 'coach-right-v1'
        def match(self,*args,**kwargs):
            return {'graph_version':'coach-right-v1','status':'matched','subject':'数学',
                'primary':{'key':'coach-label','title':'撤销讲解标签哨兵','review_status':'approved','confidence':.99},
                'prerequisites':[],'alternatives':[]}
        def semantic_options(self,graph):
            if not self.allowed:return [],{}
            item=graph['alternatives'][0];return [{'ref':'C1','label':item['title']}],{'C1':item['key']}
    matcher=Matcher();service.graph_matcher=matcher
    controls['coach_response']={'phase':'question','text':'撤销讲解标签哨兵是什么？'}
    assert post(world,'coach','rights-history-first',original_review=True,utterance='请讲解。').json()['action_status']=='completed'
    matcher.allowed=False
    controls['coach_response']={'phase':'question','text':'你准备先看哪一步？'}
    before=len(calls)
    result=post(world,'coach','rights-history-second',original_review=True,utterance='继续讲。').json()
    assert result['action_status']=='completed'
    assert '撤销讲解标签哨兵' not in json.dumps(calls[before:],ensure_ascii=False)


def test_unproven_unreviewed_legacy_practice_is_blocked_but_new_source_only_practice_survives(world):
    from home_study_agent.mistake_learning import _tutor_route_identity
    from home_study_agent.realtime_capability_tools import RealtimeCapabilityTools
    from home_study_agent.realtime_native_context import load_native_context
    db,service,_,mid,sid,calls,_,privacy=world
    class Matcher(_AuthorizedSyntheticLabels):
        def cache_version(self,*,connection=None):return 'unreviewed-source-v1'
        def match(self,*args,**kwargs):
            return {'graph_version':'unreviewed-source-v1','status':'matched','subject':'未审核学科哨兵',
                    'primary':{'key':'unreviewed','title':'未审核主节点','review_status':'unreviewed','confidence':.99},
                    'prerequisites':[],'alternatives':[]}
    service.graph_matcher=Matcher()
    post(world,'focus','legacy-unbound-focus')
    first=post(world,'practice','legacy-unbound-original',count=2).json()
    assert first['action_status']=='completed' and first['practice_items'][0]['status']=='ready'
    old_id=first['practice_items'][0]['practice_item_id']
    with db.connect() as c:
        rows=c.execute('SELECT practice_item_id,validation_json FROM mistake_learning_item WHERE mistake_id=?',(mid,)).fetchall()
        for row in rows:
            validation=json.loads(row['validation_json']);validation.pop('graph_free_origin',None)
            c.execute("UPDATE mistake_learning_item SET validation_json=?,question='旧未审学科题面哨兵' WHERE practice_item_id=?",
                      (json.dumps(validation,ensure_ascii=False),row['practice_item_id']))
        c.commit()
        before=[tuple(row) for row in c.execute('SELECT practice_item_id,question,feedback_json FROM mistake_learning_item WHERE mistake_id=? ORDER BY practice_item_id',(mid,))]
    state=service.state(mid,child_id=CHILD,session_id=sid)
    assert all(item['status']=='stale' for item in state['practice_items'])
    assert post(world,'practice','legacy-unbound-original',count=2).json()['practice_items'][0]['status']=='stale'
    privacy.update_preferences({'cloud_audio_allowed':True})
    native=RealtimeCapabilityTools(db,current_graph_version=service.graph_matcher.cache_version,
        tutor_route_identity=lambda:_tutor_route_identity(service.gateway))
    assert 'practice' not in load_native_context(native,sid,authorize=lambda *args:True)['runtime_facts']['mistake_learning']
    voice,_=native_onset(db,sid,'legacy-unbound-voice','我算完了。',old_id,expected_validation='stale',
                         current_graph_version='unreviewed-source-v1')
    from home_study_agent.agent_runtime import InvalidAgentPlanError
    from home_study_agent.realtime_native_mistake_onset import require_current
    with db.connect() as c, pytest.raises(InvalidAgentPlanError,match='NATIVE_MISTAKE_ONSET_STALE'):
        require_current(c,voice,native_focus(c,sid,current_graph_version='unreviewed-source-v1'),
                        current_graph_version='unreviewed-source-v1')
    count=len(calls)
    for operation in ('answer','coach'):
        response=post(world,operation,'legacy-unbound-'+operation,practice_item_id=old_id,utterance='我算完了。')
        assert response.status_code==409 and response.json()['detail']['code']=='MISTAKE_LEARNING_PRACTICE_CHANGED'
    assert len(calls)==count
    new=post(world,'practice','legacy-unbound-regenerate',count=2).json()
    assert new['action_status']=='completed' and all(item['status']=='ready' for item in new['practice_items'])
    assert '旧未审学科题面哨兵' not in json.dumps(calls[count:],ensure_ascii=False)
    with db.connect() as c:
        assert [tuple(row) for row in c.execute('SELECT practice_item_id,question,feedback_json FROM mistake_learning_item WHERE practice_item_id IN (?,?) ORDER BY practice_item_id',tuple(row[0] for row in before))]==before
        from home_study_agent.mistake_graph_study import independently_sourced_practice
        proof_row=dict(c.execute('SELECT * FROM mistake_learning_item WHERE practice_item_id=?',(new['practice_items'][0]['practice_item_id'],)).fetchone())
        assert independently_sourced_practice(proof_row)
        assert not independently_sourced_practice(proof_row | {'child_id':'child-primary-4'})
        assert not independently_sourced_practice(proof_row | {'source_revision':'different-source'})
    reopened=MistakeLearningService(Database(db.path),service.gateway,graph_matcher=service.graph_matcher)
    assert all(item['status']=='ready' for item in reopened.state(mid,child_id=CHILD,session_id=sid)['practice_items'])
    with db.connect() as c:
        c.execute("UPDATE mistake_learning_item SET question='被篡改的题干' WHERE practice_item_id=?",(new['practice_items'][0]['practice_item_id'],));c.commit()
    assert reopened.state(mid,child_id=CHILD,session_id=sid)['practice_items'][0]['status']=='stale'


@pytest.mark.parametrize('review_status',['unreviewed','approved'])
def test_model_requests_and_native_use_only_reviewed_inferred_subject(world,review_status):
    from home_study_agent.realtime_capability_tools import RealtimeCapabilityTools
    from home_study_agent.realtime_native_context import load_native_context
    from home_study_agent.mistake_learning import _tutor_route_identity
    db,service,_,mid,sid,calls,controls,privacy=world
    class Matcher(_AuthorizedSyntheticLabels):
        def cache_version(self,*,connection=None):return 'subject-v1'
        def match(self,*args,**kwargs):
            return {'graph_version':'subject-v1','status':'matched','subject':'图谱学科哨兵',
                    'primary':{'key':'subject-primary','title':'图谱主节点','review_status':review_status,'confidence':.99},
                    'prerequisites':[],'alternatives':[]}
    service.graph_matcher=Matcher()
    privacy.update_preferences({'cloud_audio_allowed':True})
    post(world,'focus','subject-focus')
    analyzed=post(world,'analyze','subject-analyze').json()
    assert analyzed['action_status']=='completed'
    if review_status=='unreviewed':
        from home_study_agent.mistake_learning import analysis_context_key
        contaminated=ANALYSIS | {'summary':'旧图谱污染哨兵'}
        with db.connect() as c:
            c.execute('UPDATE mistake_learning_state SET analysis_json=?,analysis_context_key=? WHERE mistake_id=?',
                (json.dumps(contaminated,ensure_ascii=False),analysis_context_key(analyzed['graph_context'],_tutor_route_identity(service.gateway)),mid))
            c.commit()
    practice=post(world,'practice','subject-practice',count=2).json()
    assert practice['action_status']=='completed' and practice['practice_items']
    answer=post(world,'answer','subject-answer',practice_item_id=practice['practice_items'][0]['practice_item_id'],utterance='我算出零。').json()
    assert answer['action_status']=='completed'
    coached=post(world,'coach','subject-coach',original_review=True,utterance='请给我一个提示。').json()
    assert coached['action_status']=='completed'
    controls['coach_response']={'phase':'independent_practice','text':'现在请独立试一道。','focus_step':'负数的平方'}
    controls['coach_check_response']={'claim_status':'correct','facts':'负四的平方是十六。'}
    controls['practice_response']={'candidates':[PRACTICE['candidates'][0] | {'prompt':'二次方程 x²-6x+5=0，请算判别式并判断不同实数根的个数。',
        'expected_answer':{'keywords':['16','两个不同实数根']},'explanation':'判别式36-20=16，大于零，所以有两个不同的实数根。'}]}
    variant=post(world,'coach','subject-variant',original_review=True,utterance='负四的平方是十六。').json()
    assert variant['action_status']=='completed' and variant['coach_variant']
    assert variant['coach_variant']['status']=='ready'
    native=RealtimeCapabilityTools(db,current_graph_version=service.graph_matcher.cache_version,
        tutor_route_identity=lambda:_tutor_route_identity(service.gateway))
    facts=load_native_context(native,sid,authorize=lambda *args:True)['runtime_facts']['mistake_learning']
    source_subject=service.state(mid,child_id=CHILD,session_id=sid)['source']['subject']
    expected='图谱学科哨兵' if review_status=='approved' else '数学'
    assert facts['subject']=='数学' and source_subject==expected
    sent=json.dumps(calls,ensure_ascii=False)
    if review_status=='unreviewed':
        assert '图谱学科哨兵' not in sent
        assert '旧图谱污染哨兵' not in sent
    else:
        assert '图谱学科哨兵' in sent
        variant_id=variant['coach_variant']['practice_item_id']
        with db.connect() as c:
            old=tuple(c.execute('SELECT question,validation_json FROM mistake_learning_item WHERE practice_item_id=?',(variant_id,)).fetchone())
            graph=json.loads(c.execute('SELECT graph_json FROM mistake_learning_state WHERE mistake_id=?',(mid,)).fetchone()[0])
            graph['primary']['review_status']='unreviewed'
            c.execute('UPDATE mistake_learning_state SET graph_json=? WHERE mistake_id=?',(json.dumps(graph,ensure_ascii=False),mid));c.commit()
        assert service.state(mid,child_id=CHILD,session_id=sid)['coach_variant']['status']=='stale'
        with db.connect() as c:
            assert tuple(c.execute('SELECT question,validation_json FROM mistake_learning_item WHERE practice_item_id=?',(variant_id,)).fetchone())==old


def test_unreviewed_match_produces_independent_analysis_without_graph_or_old_history(world):
    db, service, client, mid, sid, calls, controls, _ = world
    class Matcher:
        def cache_version(self, *, connection=None): return 'unreviewed-independent-v1'
        def match(self, *args, **kwargs):
            return {'graph_version': 'unreviewed-independent-v1', 'status': 'matched', 'subject': '数学',
                    'primary': {'key': 'private-key', 'title': '未审标签不得作为依据', 'review_status': 'unreviewed', 'confidence': .99},
                    'prerequisites': [], 'alternatives': []}
        def semantic_options(self, graph): raise AssertionError('UNREVIEWED_EXPORT_FORBIDDEN')
    service.graph_matcher = Matcher()
    prior = post(world, 'coach', 'prior-coach', original_review=True, utterance='旧对话不应成为本次分析依据').json()
    assert prior['action_status'] == 'completed'
    result = post(world, 'analyze', 'unreviewed-independent').json()
    assert result['action_status'] == 'completed', result
    assert result['analysis_independent'] is True and result['analysis']
    context = json.loads(calls[-1]['messages'][1]['content'])
    assert 'graph_targets' not in context and context['history'] == [] and context['analysis'] is None
    assert '未审标签不得作为依据' not in json.dumps(context, ensure_ascii=False)
    assert '旧对话不应成为本次分析依据' not in json.dumps(context, ensure_ascii=False)
    reopened = MistakeLearningService(Database(db.path), service.gateway, graph_matcher=service.graph_matcher)
    saved = reopened.state(mid, child_id=CHILD, session_id=sid)
    assert saved['analysis_independent'] is True and saved['analysis'] == result['analysis']
