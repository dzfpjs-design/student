import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from home_study_agent.agent_orchestrator import AgentOrchestrator
from home_study_agent.document_monitoring import DocumentMonitoringService
from home_study_agent.model_gateway import ModelResponseError, ModelResult
from home_study_agent.repository import utc_now
from test_app import client_for
from test_cloud_first_agent import Planner


class RecordingGateway:
    configured = True

    def __init__(self):
        self.requests = []
        self.planner_content = None

    async def complete(self, request):
        self.requests.append(request)
        if request.request_type == 'agent_plan' and self.planner_content:
            content = json.dumps(self.planner_content, ensure_ascii=False)
        elif request.request_type == 'voice_agent_intent':
            content = '{"intent":"supportive_chat","confidence":0.98}'
        elif request.request_type == 'companion_dialogue':
            content = '你今天还有什么想分享的吗？'
        else:
            raise ModelResponseError('MODEL_TIMEOUT', fallback_eligible=False)
        return ModelResult(content=content, provider='test', model='recording', latency_ms=1,
                           prompt_tokens=1, completion_tokens=1)


@pytest.fixture
def event_loop():
    try:
        previous = asyncio.get_event_loop()
    except RuntimeError:
        previous = asyncio.new_event_loop()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield loop
    loop.close()
    asyncio.set_event_loop(previous)


@pytest.fixture
def env(tmp_path, event_loop):
    gateway = RecordingGateway()
    # Exercise the real stores and orchestrator without starting unrelated
    # camera/model warmup workers from the HTTP application lifespan.
    client = client_for(tmp_path, model_gateway=gateway, agent_runtime_enabled=True)
    agent = client.app.state.agent_orchestrator
    agent.database.initialize()
    agent.repository.seed_family()
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    PrivacyLifecycleService(agent.database, tmp_path).initialize()
    sid = agent.repository.start_session('child-primary-4', 'document-agent-session')['session_id']
    with agent.database.connect() as connection:
        connection.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
        connection.commit()
    monitor = DocumentMonitoringService(agent.database)
    monitor.start(sid, 'document-agent-monitor')
    source = SimpleNamespace(context_for_session=Mock())
    agent.document_learning = source
    yield SimpleNamespace(agent=agent, sid=sid, child_id='child-primary-4', gateway=gateway, monitor=monitor, source=source, loop=event_loop)
    client.close()


def add_revision(env, text='本地页面私密证据：12+8=20', suffix='one'):
    artifact_id = f'artifact-{suffix}'
    with env.agent.database.connect() as connection:
        connection.execute(
            """INSERT INTO capture_artifact(artifact_id,child_id,session_id,artifact_type,local_path,
                 extracted_text,confidence,details_json,status,created_at,client_action_id)
               VALUES(?,?,?,'document_ocr',NULL,?,.94,'{}','completed',?,?)""",
            (artifact_id, env.child_id, env.sid, text, utc_now(), f'artifact-action-{suffix}'),
        )
        connection.commit()
    revision = env.monitor.record_revision(env.sid, artifact_id, f'signature-{suffix}', f'revision-action-{suffix}')
    context = dict(status='completed', session_id=env.sid, child_id=env.child_id,
                   current_revision_id=revision['revision_id'], latest_revision_id=revision['revision_id'],
                   created_at=revision['created_at'], freshness='fresh', age_seconds=0,
                   cloud_transmission_allowed=False, observations=[], hypotheses=[], questions=[], analyses=[])
    env.source.context_for_session.return_value = context
    return revision, context


def say(env, text, action='observation-request'):
    return env.loop.run_until_complete(env.agent.interpret(env.sid, text, action, cloud_text_allowed=True))


@pytest.mark.parametrize('text', ['你看到我在做什么？', '我刚才写了什么？', '这页哪里可能有问题？'])
def test_explicit_observation_is_local_evidence_not_tutoring(env, text):
    revision, _ = add_revision(env)
    turn = say(env, text)
    assert turn['intent'] == 'document_observation_question'
    assert turn['action']['type'] == 'none'
    assert turn['action']['source'] == 'local_document_learning'
    assert turn['action']['local_only'] is True
    assert turn['action']['cloud_transmission_allowed'] is False
    assert turn['action']['mastery_update_allowed'] is False
    assert f"document_revision:{revision['revision_id']}" in turn['action']['evidence_refs']
    assert '12+8=20' in turn['response_text']
    assert any(word in turn['response_text'] for word in ['不能', '不一定', '没有足够'])
    assert env.gateway.requests == []
    env.source.context_for_session.assert_called_once_with(env.sid)


@pytest.mark.parametrize('text,action_type', [
    ('停止全部监听，你看到我在做什么？', 'privacy_pause'),
    ('别讲了，这页哪里可能有问题？', 'stop_speech'),
    ('暂停高拍仪，我刚才写了什么？', 'control_document_monitor'),
    ('换个话题，你看到我在做什么？', 'none'),
])
def test_control_utterances_do_not_read_or_override_with_document_context(env, text, action_type):
    add_revision(env)
    if action_type == 'control_document_monitor':
        env.gateway.planner_content = Planner('document.monitor_control', {'command': 'pause'}, text).payload
    turn = say(env, text)
    assert turn['action']['type'] == action_type
    assert turn['intent'] != 'document_observation_question'
    env.source.context_for_session.assert_not_called()
    assert '12+8=20' not in turn['response_text']
    assert all('12+8=20' not in str(request) for request in env.gateway.requests)


@pytest.mark.parametrize('owner', ['pet', 'exam', 'mistake', 'learning'])
def test_observation_cannot_be_graded_by_waiting_activity(env, monkeypatch, owner):
    add_revision(env)
    if owner == 'pet':
        env.agent.pet_vocabulary = SimpleNamespace(active_prompt=lambda _: {'card_id': 'waiting'})
    elif owner == 'exam':
        env.agent.exam_simulation = SimpleNamespace(active_for_session=lambda _: {'status': 'active'}, arrival_binding=lambda _: None)
    elif owner == 'mistake':
        from home_study_agent.mistake_dialogue import MistakeDialogueService
        monkeypatch.setattr(MistakeDialogueService, 'state', lambda *_: {'phase': 'awaiting_answer'})
    else:
        monkeypatch.setattr(env.agent.agent_runtime, 'pending_child_reply', lambda _: {'phase': 'independent_check'})
    turn = say(env, '这页哪里可能有问题？我卡住了')
    assert turn['intent'] == 'document_observation_question'
    assert turn['action']['type'] == 'none'
    assert env.gateway.requests == []


@pytest.mark.parametrize('problem,expected', [
    ('empty', '还没有'), ('stale', '过时'), ('paused', '暂停'),
    ('different_session', '无法核对'), ('different_child', '无法核对'),
    ('older_analysis', '旧页面'), ('in_progress', '处理中'), ('unknown', '尚未确认'),
    ('blocked', '不能可靠'), ('exception', '无法核对'),
])
def test_missing_stale_paused_or_unconfirmed_context_never_invents_a_lesson(env, problem, expected):
    revision, context = add_revision(env)
    if problem == 'empty':
        with env.agent.database.connect() as connection:
            connection.execute('DELETE FROM document_page_revision WHERE session_id=?', (env.sid,))
            connection.commit()
    elif problem == 'stale':
        context.update(status='stale', freshness='stale', age_seconds=600)
        with env.agent.database.connect() as connection:
            connection.execute('UPDATE document_page_revision SET created_at=? WHERE revision_id=?',
                               ((datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(), revision['revision_id']))
            connection.commit()
    elif problem == 'paused':
        env.monitor.pause(env.sid, 'pause-document-test')
    elif problem == 'different_session':
        context['session_id'] = 'another-session'
    elif problem == 'different_child':
        context['child_id'] = 'child-junior-3'
    elif problem == 'older_analysis':
        context['latest_revision_id'] = 'old-analysis-revision'
    elif problem == 'exception':
        env.source.context_for_session.side_effect = RuntimeError('service unavailable')
    else:
        context['status'] = problem
    turn = say(env, '我刚才写了什么？')
    assert expected in turn['response_text']
    assert '12+8=20' not in turn['response_text']
    assert turn['action']['type'] == 'none'
    assert turn['action']['local_only'] is True
    assert env.gateway.requests == []


@pytest.mark.parametrize('status', ['empty', 'stale'])
def test_current_page_request_acquires_evidence_instead_of_repeating_empty_reply(env, status, monkeypatch):
    monkeypatch.setattr(env.agent, '_local_document_context', lambda *_: {'status': status})
    result = say(env, '你能看到我在写什么作业吗？')
    assert result['action']['type'] == 'request_capture'
    assert result['action']['observation_only'] is True
    assert result['action']['mastery_update_allowed'] is False
    assert result['action']['cloud_transmission_allowed'] is False
    assert env.gateway.requests == []


def test_new_writing_is_a_revision_difference_not_a_claim_about_the_child(env):
    add_revision(env, '题目：计算12+8', 'before')
    revision, _ = add_revision(env, '题目：计算12+8\n我的答案：20', 'after')
    turn = say(env, '我刚才写了什么？')
    assert '新增的识别文字' in turn['response_text']
    assert '我的答案：20' in turn['response_text']
    assert '不一定是你刚写的' in turn['response_text']
    assert turn['action']['document_revision_id'] == revision['revision_id']


@pytest.mark.parametrize('flags', [{'local_only': True}, {'cloud_transmission_allowed': False}])
def test_action_privacy_flags_exclude_both_sides_from_cloud_history_and_repeat(env, flags):
    private = say(env, '你看到我在做什么？')
    with env.agent.database.connect() as connection:
        action = {**private['action'], 'local_only': False, 'cloud_transmission_allowed': True, **flags}
        connection.execute("""UPDATE agent_turn SET intent='supportive_chat',transcript='PRIVATE_OBSERVATION_QUERY_867',
                             response_text='PRIVATE_OCR_REPLY_975',action_json=?,tool_status='completed',completed_at=?
                             WHERE agent_turn_id=?""", (json.dumps(action), utc_now(), private['agent_turn_id']))
        connection.commit()
    repeat = say(env, '再说一遍', 'repeat-private')
    assert 'PRIVATE_' not in repeat['response_text']
    say(env, '今天画画很开心', 'cloud-chat')
    assert env.gateway.requests
    assert all('PRIVATE_' not in str(request) for request in env.gateway.requests)


def test_specific_learning_question_only_carries_local_revision_identity(env):
    revision, _ = add_revision(env, 'OCR_FULL_TEXT_MUST_STAY_LOCAL_596')
    env.gateway.planner_content = Planner('learning.explain', {'question': '数学为什么要学习小数？', 'subject': '数学'},
                                          '数学为什么要学习小数？', role='question', objective_type='learning').payload
    turn = say(env, '数学为什么要学习小数？')
    assert turn['action']['type'] == 'ask_tutor'
    assert 'OCR_FULL_TEXT' not in json.dumps(turn, ensure_ascii=False)
    assert turn['action']['local_document_context'] == {
        'revision_id': revision['revision_id'], 'cloud_transmission_allowed': False,
        'requires_text_confirmation': True, 'mastery_update_allowed': False,
    }
    assert all('OCR_FULL_TEXT' not in str(request) for request in env.gateway.requests)
    assert [request.request_type for request in env.gateway.requests] == ['agent_plan']


@pytest.mark.parametrize('corruption', ['old_revision', 'other_artifact', 'wrong_quote', 'bad_offsets'])
def test_hypothesis_requires_exact_latest_revision_citation(env, corruption):
    revision, context = add_revision(env, '12+18=30')
    evidence = dict(revision_id=revision['revision_id'], artifact_id=revision['artifact_id'],
                    quote='12+18=30', start=0, end=8, kind='ocr_text_unconfirmed')
    if corruption == 'old_revision':
        evidence['revision_id'] = 'old-revision'
    elif corruption == 'other_artifact':
        evidence['artifact_id'] = 'other-child-artifact'
    elif corruption == 'wrong_quote':
        evidence['quote'] = '虚构的错误'
    else:
        evidence['start'] = -1
    context['hypotheses'] = [dict(kind='calculation_step', status='needs_child_confirmation',
                                  statement='你已经掌握所有知识', evidence=evidence)]
    turn = say(env, '这页哪里可能有问题？')
    assert '可能需要核对这一行的计算' not in turn['response_text']
    assert '你已经掌握' not in turn['response_text']
    assert '虚构的错误' not in turn['response_text']
    assert '待核对线索' in turn['response_text']


def test_verified_hypothesis_uses_local_question_not_model_statement(env):
    revision, context = add_revision(env, '12+18=30')
    context['hypotheses'] = [dict(kind='calculation_step', status='needs_child_confirmation',
                                 statement='你已经掌握所有知识', evidence=dict(
                                     revision_id=revision['revision_id'], artifact_id=revision['artifact_id'],
                                     quote='12+18=30', start=0, end=8, kind='ocr_text_unconfirmed'))]
    turn = say(env, '这页哪里可能有问题？')
    assert '可能需要核对这一行的计算' in turn['response_text']
    assert '待确认假设' in turn['response_text']
    assert '你已经掌握' not in turn['response_text']
    assert env.gateway.requests == []


def test_context_checks_actual_artifact_owner(env):
    revision, _ = add_revision(env)
    with env.agent.database.connect() as connection:
        connection.execute('UPDATE capture_artifact SET child_id=? WHERE artifact_id=?',
                           ('child-junior-3', revision['artifact_id']))
        connection.commit()
    turn = say(env, '我刚才写了什么？')
    assert '无法核对' in turn['response_text']
    assert '12+8=20' not in turn['response_text']


def test_real_document_learning_context_connects_without_another_model_call(env):
    from home_study_agent.document_learning import DocumentLearningService
    revision, _ = add_revision(env, '12+18=30')
    service = DocumentLearningService(env.agent.database, env.gateway, settings=env.agent.settings)
    analyzed = env.loop.run_until_complete(service.analyze_revision(env.sid, revision['revision_id']))
    assert analyzed['status'] == 'completed'
    env.agent.document_learning = service
    turn = say(env, '这页哪里可能有问题？')
    assert turn['action']['document_context_status'] == 'completed'
    assert '可能需要核对这一行的计算' in turn['response_text']
    assert env.gateway.requests == []


def test_constructor_remains_compatible_without_document_injection(env):
    agent = AgentOrchestrator(env.agent.database, env.agent.repository, env.gateway, env.agent.settings)
    turn = env.loop.run_until_complete(agent.interpret(env.sid, '我刚才写了什么？', 'no-injection', True))
    assert turn['action']['type'] == 'none'
    assert '无法核对' in turn['response_text']
    assert env.gateway.requests == []


def add_vision_revision(env, ocr=''):
    from home_study_agent.document_vision import DocumentVisionService
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    revision, context = add_revision(env, ocr)
    DocumentVisionService(env.agent.settings, env.agent.database)
    PrivacyLifecycleService(env.agent.database, env.agent.settings.data_dir).initialize()
    payload = dict(status='needs_review', request_id='monitor:artifact-action-one', blocks=[dict(
        text='底边长8厘米，高为5厘米，求面积。', confidence=.6, kind='text', bbox=[.1, .2, .8, .3])])
    with env.agent.database.connect() as connection:
        connection.execute('UPDATE privacy_preference SET cloud_model_text_allowed=1 WHERE singleton_id=1')
        connection.execute('''INSERT INTO document_vision_request
            (child_id,request_id,image_hash,status,reserved_tokens,charged_tokens,created_at,result_json)
            VALUES(?,?,'test-image','succeeded',100,100,?,?)''',
            (env.child_id, payload['request_id'], utc_now(), json.dumps(payload)))
        connection.execute('UPDATE capture_artifact SET details_json=?,confidence=.2 WHERE artifact_id=?',
                           (json.dumps(dict(cloud_vision_fallback=payload)), revision['artifact_id']))
        connection.commit()
    block = payload['blocks'][0]
    evidence = dict(revision_id=revision['revision_id'], artifact_id=revision['artifact_id'],
                    kind='cloud_page_ocr_unconfirmed', cloud_vision_request_id=payload['request_id'],
                    block_index=0, quote=block['text'], bbox=block['bbox'], confidence=block['confidence'])
    context.update(source='cloud_page_ocr', needs_confirmation=True,
                   observations=[dict(kind='text', needs_confirmation=True, evidence=evidence)])
    return revision, context, payload


@pytest.mark.parametrize('ocr', ['', '不可靠的旧识别内容'])
def test_real_vision_context_uses_owned_blocks_even_when_ocr_empty_or_unreliable(env, ocr):
    from home_study_agent.document_learning import DocumentLearningService
    revision, _, payload = add_vision_revision(env, ocr)
    service = DocumentLearningService(env.agent.database, env.gateway, settings=env.agent.settings)
    analyzed = env.loop.run_until_complete(service.analyze_revision(env.sid, revision['revision_id']))
    assert analyzed['source'] == 'cloud_page_ocr', analyzed.get('reason')
    env.agent.document_learning = service
    turn = say(env, '我刚才写了什么？')
    assert turn['action']['document_context_status'] == 'completed'
    assert turn['action']['source'] == 'cloud_page_ocr'
    assert turn['action']['local_only'] is True
    assert turn['action']['cloud_transmission_allowed'] is False
    assert turn['action']['needs_confirmation'] is True
    assert f"cloud_vision_request:{payload['request_id']}:block:0" in turn['action']['evidence_refs']
    assert payload['blocks'][0]['text'] in turn['response_text']
    assert '还没有确认' in turn['response_text']
    assert '题干确认前不会开始讲题' in turn['response_text']
    assert '不可靠的旧识别内容' not in turn['response_text']
    assert env.gateway.requests == []


@pytest.mark.parametrize('mutation', ['request', 'block_index', 'quote', 'bbox', 'confidence', 'revision', 'artifact', 'receipt', 'child', 'confirmed'])
def test_vision_reference_must_match_owned_successful_receipt_exactly(env, mutation):
    _, context, payload = add_vision_revision(env, '不可靠的旧识别内容')
    evidence = context['observations'][0]['evidence']
    if mutation == 'request':
        evidence['cloud_vision_request_id'] = 'another-request'
    elif mutation == 'block_index':
        evidence['block_index'] = 1
    elif mutation == 'quote':
        evidence['quote'] = '伪造的识别文字'
    elif mutation == 'bbox':
        evidence['bbox'] = [0, 0, 1, 1]
    elif mutation == 'confidence':
        evidence['confidence'] = .99
    elif mutation in ('revision', 'artifact'):
        evidence[f'{mutation}_id'] = 'foreign-source'
    elif mutation == 'confirmed':
        context['needs_confirmation'] = False
    else:
        with env.agent.database.connect() as connection:
            if mutation == 'receipt':
                connection.execute("UPDATE document_vision_request SET result_json='{}'")
            else:
                connection.execute("UPDATE document_vision_request SET child_id='child-junior-3'")
            connection.commit()
    turn = say(env, '这页哪里可能有问题？')
    assert turn['action']['document_context_status'] == 'blocked'
    assert payload['blocks'][0]['text'] not in turn['response_text']
    assert '不可靠的旧识别内容' not in turn['response_text']
    assert '伪造的识别文字' not in turn['response_text']
    assert env.gateway.requests == []


@pytest.mark.parametrize('vision', [False, True])
def test_specific_focus_and_question_are_grounded_tentative_and_local(env, vision):
    if vision:
        _, context, _ = add_vision_revision(env)
    else:
        revision, context = add_revision(env, '底边长8厘米，高为5厘米，求面积。')
        context['observations'] = [dict(kind='word_problem', evidence=dict(
            revision_id=revision['revision_id'], artifact_id=revision['artifact_id'],
            kind='ocr_text_unconfirmed', quote='底边长8厘米，高为5厘米，求面积。', start=0, end=len('底边长8厘米，高为5厘米，求面积。')))]
    observation = context['observations'][0]
    observation['learning_focus'] = '底边与高在面积计算中的对应关系'
    context['hypotheses'] = [dict(kind='unit_or_symbol', status='needs_child_confirmation',
        rationale='可能需要核对底边8厘米与高5厘米是否对应同一个图形',
        follow_up='纸上标出的高5厘米对应的是哪一条底边？', evidence=observation['evidence'])]
    activity = say(env, '你看到我在做什么？', 'specific-focus')
    problem = say(env, '这页哪里可能有问题？', 'specific-followup')
    assert observation['learning_focus'] in activity['response_text']
    assert context['hypotheses'][0]['rationale'] in problem['response_text']
    assert context['hypotheses'][0]['follow_up'] in problem['response_text']
    assert '待确认' in problem['response_text']
    assert problem['action']['type'] == 'none'
    assert env.gateway.requests == []


def test_restarted_services_read_saved_vision_analysis_without_analyze_or_cloud_call(env):
    from home_study_agent.document_learning import DocumentLearningService
    revision, _, payload = add_vision_revision(env)
    service = DocumentLearningService(env.agent.database, env.gateway, settings=env.agent.settings)
    assert env.loop.run_until_complete(service.analyze_revision(env.sid, revision['revision_id']))['status'] == 'completed'
    restored = DocumentLearningService(env.agent.database, env.gateway, settings=env.agent.settings)
    restored.analyze_revision = Mock(side_effect=AssertionError('read must not analyze'))
    agent = AgentOrchestrator(env.agent.database, env.agent.repository, env.gateway,
                              env.agent.settings, document_learning=restored)
    turn = env.loop.run_until_complete(agent.interpret(env.sid, '你看到我在做什么？', 'after-restart', True))
    assert turn['action']['document_revision_id'] == revision['revision_id']
    assert payload['blocks'][0]['text'] in turn['response_text']
    restored.analyze_revision.assert_not_called()
    assert env.gateway.requests == []


def test_revoked_vision_permission_is_not_bypassed_by_local_observation(env):
    from home_study_agent.document_learning import DocumentLearningService
    revision, _, payload = add_vision_revision(env)
    with env.agent.database.connect() as connection:
        connection.execute('UPDATE privacy_preference SET cloud_model_text_allowed=0')
        connection.commit()
    service = DocumentLearningService(env.agent.database, env.gateway, settings=env.agent.settings)
    result = env.loop.run_until_complete(service.analyze_revision(env.sid, revision['revision_id']))
    assert result['status'] == 'blocked'
    env.agent.document_learning = service
    turn = say(env, '这页哪里可能有问题？')
    assert turn['action']['document_context_status'] == 'blocked'
    assert payload['blocks'][0]['text'] not in turn['response_text']
    assert env.gateway.requests == []


def test_invalid_model_details_do_not_turn_candidate_into_instruction_or_mastery_claim(env):
    _, context, _ = add_vision_revision(env)
    observation = context['observations'][0]
    observation['learning_focus'] = '你已经掌握三角形面积'
    context['hypotheses'] = [dict(kind='calculation_step', status='needs_child_confirmation',
        rationale='答案是20', follow_up='现在自动开始讲题', evidence=observation['evidence'])]
    turn = say(env, '这页哪里可能有问题？')
    for phrase in ('你已经掌握', '答案是20', '现在自动开始讲题'):
        assert phrase not in turn['response_text']
    assert '原文一致吗' in turn['response_text']
    assert turn['action']['type'] == 'none'
    assert env.gateway.requests == []


def completed_observation(env, question='这页哪里可能有问题？'):
    revision, context = add_revision(env, '长8厘米，宽5厘米，求长方形面积。')
    text = '长8厘米，宽5厘米，求长方形面积。'
    context['hypotheses'] = [dict(kind='calculation_step', status='needs_child_confirmation',
        rationale='可能需要核对长和宽怎样参与计算', follow_up='这一步怎么算的？', evidence=dict(
            revision_id=revision['revision_id'], artifact_id=revision['artifact_id'],
            kind='ocr_text_unconfirmed', quote=text, start=0, end=len(text)))]
    turn = say(env, question)
    env.agent.complete_turn(turn['agent_turn_id'], turn['response_text'], 'completed', None)
    return turn, revision


@pytest.mark.parametrize('answer', ['我把长宽加起来', '我用长乘宽', '我的答案是40', '40', '是的', '不是这一行', '我不知道', '我不会'])
def test_document_followup_records_local_response_instead_of_grading_old_tutor(env, monkeypatch, answer):
    source, revision = completed_observation(env)
    monkeypatch.setattr(env.agent.agent_runtime, 'pending_child_reply', lambda _: {'phase': 'independent_check'})
    turn = say(env, answer, 'child-document-response')
    assert turn['intent'] == 'document_observation_response'
    assert turn['action']['type'] == 'none'
    assert turn['action']['local_only'] is True
    assert turn['action']['cloud_transmission_allowed'] is False
    assert turn['action']['mastery_update_allowed'] is False
    saved = turn['action']['document_response']
    assert saved['source_turn_id'] == source['agent_turn_id']
    assert saved['revision_id'] == revision['revision_id']
    assert saved['child_response'] == answer
    assert saved['status'] == 'unverified'
    assert '?' not in turn['response_text'] and '？' not in turn['response_text']
    assert env.gateway.requests == []
    replay = say(env, answer, 'child-document-response')
    assert replay['agent_turn_id'] == turn['agent_turn_id']


def test_document_followup_survives_restart_and_response_never_enters_cloud_history(env):
    source, _ = completed_observation(env)
    env.agent = AgentOrchestrator(env.agent.database, env.agent.repository, env.gateway,
                                  env.agent.settings, document_learning=env.source)
    turn = say(env, '我把长宽加起来', 'after-observation-restart')
    assert turn['action']['document_response']['source_turn_id'] == source['agent_turn_id']
    env.agent.complete_turn(turn['agent_turn_id'], turn['response_text'], 'completed', None)
    say(env, '今天画画很开心', 'ordinary-next-chat')
    assert env.gateway.requests
    assert all('我把长宽加起来' not in str(request) and '长8厘米' not in str(request) for request in env.gateway.requests)


@pytest.mark.parametrize('problem', ['expired', 'page_changed', 'paused', 'pause_resume', 'analysis_pending'])
def test_invalidated_document_followup_does_not_bind_or_go_to_old_tutor(env, monkeypatch, problem):
    source, _ = completed_observation(env)
    if problem == 'expired':
        action = source['action']
        action['document_followup']['expires_at'] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        with env.agent.database.connect() as connection:
            connection.execute('UPDATE agent_turn SET action_json=? WHERE agent_turn_id=?',
                               (json.dumps(action), source['agent_turn_id']))
            connection.commit()
    elif problem == 'page_changed':
        add_revision(env, '新页面：12+18=30', 'new-page')
    elif problem in ('paused', 'pause_resume'):
        env.monitor.pause(env.sid, 'pause-followup')
        if problem == 'pause_resume':
            env.monitor.resume(env.sid, 'resume-followup')
    else:
        env.source.context_for_session.return_value['status'] = 'in_progress'
    monkeypatch.setattr(env.agent.agent_runtime, 'pending_child_reply', lambda _: {'phase': 'independent_check'})
    turn = say(env, '我把长宽加起来', 'invalidated-response')
    assert turn['intent'] == 'document_observation_response'
    assert 'document_response' not in turn['action']
    assert turn['action']['document_reply_status'] != 'recorded'
    assert turn['action']['local_only'] is True
    assert env.gateway.requests == []


@pytest.mark.parametrize('utterance', ['停止全部监听', '换个话题', '数学为什么要学习小数？', '今天画画很开心', '我喜欢学校的午餐', '妈妈买了水果', '我把书加进书包', '我用学校的电脑玩了30分钟'])
def test_document_followup_does_not_steal_controls_new_questions_or_normal_chat(env, utterance):
    completed_observation(env)
    turn = say(env, utterance, 'new-direction')
    assert turn['intent'] != 'document_observation_response'
    assert 'document_response' not in turn['action']


def test_pending_observation_and_intervening_topic_cannot_reopen_followup(env):
    source, _ = completed_observation(env)
    with env.agent.database.connect() as connection:
        connection.execute("UPDATE agent_turn SET tool_status='pending' WHERE agent_turn_id=?", (source['agent_turn_id'],))
        connection.commit()
    unbound = say(env, '我把长宽加起来', 'before-completion')
    assert unbound['intent'] != 'document_observation_response'
    env.agent.complete_turn(source['agent_turn_id'], source['response_text'], 'completed', None)
    # A later turn exists, even though the old observation completed last.
    changed = say(env, '我把长宽加起来', 'after-intervening-turn')
    assert changed['intent'] != 'document_observation_response'


@pytest.mark.parametrize('invalidation', ['deleted', 'expired', 'pause_resume', 'page_changed'])
@pytest.mark.parametrize('replay_response', [False, True])
def test_document_replay_never_resurrects_invalid_page_or_local_child_response(env, invalidation, replay_response):
    source, revision = completed_observation(env)
    original = source
    transcript, action_id = '这页哪里可能有问题？', 'observation-request'
    if replay_response:
        transcript, action_id = '我把长宽加起来', 'document-reply-to-replay'
        original = say(env, transcript, action_id)
        env.agent.complete_turn(original['agent_turn_id'], original['response_text'], 'completed', None)
    with env.agent.database.connect() as connection:
        original_action_json = connection.execute('SELECT action_json FROM agent_turn WHERE agent_turn_id=?',
                                                 (original['agent_turn_id'],)).fetchone()[0]
        if invalidation == 'deleted':
            connection.execute('DELETE FROM capture_artifact WHERE artifact_id=?', (revision['artifact_id'],))
        elif invalidation == 'expired':
            action = json.loads(original_action_json)
            key = 'document_evidence_context' if replay_response else 'document_followup'
            action[key]['expires_at'] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
            connection.execute('UPDATE agent_turn SET action_json=? WHERE agent_turn_id=?',
                               (json.dumps(action), original['agent_turn_id']))
        connection.commit()
    if invalidation == 'pause_resume':
        env.monitor.pause(env.sid, 'pause-before-replay')
        env.monitor.resume(env.sid, 'resume-before-replay')
    elif invalidation == 'page_changed':
        add_revision(env, '另一个页面内容', 'replay-new-page')
    env.agent = AgentOrchestrator(env.agent.database, env.agent.repository, env.gateway,
                                  env.agent.settings, document_learning=env.source)
    replay = say(env, transcript, action_id)
    assert replay['agent_turn_id'] == original['agent_turn_id']
    assert '不会重播旧页面内容' in replay['response_text']
    assert '长8厘米' not in json.dumps(replay, ensure_ascii=False)
    assert '我把长宽加起来' not in json.dumps(replay, ensure_ascii=False)
    assert replay['action']['local_only'] is True
    assert replay['action']['cloud_transmission_allowed'] is False
    assert env.gateway.requests == []
    with env.agent.database.connect() as connection:
        saved = connection.execute('SELECT response_text FROM agent_turn WHERE agent_turn_id=?',
                                   (original['agent_turn_id'],)).fetchone()[0]
    assert saved == original['response_text']


def test_valid_observation_replay_preserves_current_quote_without_extending_expiry(env):
    source, _ = completed_observation(env)
    replay = say(env, '这页哪里可能有问题？')
    assert replay['response_text'] == source['response_text']
    assert replay['action']['document_followup'] == source['action']['document_followup']


def test_legacy_observation_replay_without_epoch_is_local_generic(env):
    source, _ = completed_observation(env)
    action = source['action']
    action.pop('document_followup')
    with env.agent.database.connect() as connection:
        connection.execute('UPDATE agent_turn SET action_json=? WHERE agent_turn_id=?',
                           (json.dumps(action), source['agent_turn_id']))
        connection.commit()
    replay = say(env, '这页哪里可能有问题？')
    assert '长8厘米' not in replay['response_text']
    assert replay['action']['local_only'] is True


def test_other_child_cannot_claim_an_observation_followup(env):
    source, _ = completed_observation(env)
    with env.agent.database.connect() as connection:
        connection.execute("UPDATE daily_session SET status='completed',state='completed',ended_at=? WHERE session_id=?",
                           (utc_now(), env.sid))
        connection.commit()
    other_sid = env.agent.repository.start_session('child-junior-3', 'other-child-followup')['session_id']
    with env.agent.database.connect() as connection:
        connection.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (other_sid,))
        connection.commit()
    turn = env.loop.run_until_complete(env.agent.interpret(other_sid, '我把长宽加起来', 'foreign-reply', True))
    assert turn['intent'] != 'document_observation_response'
    assert source['agent_turn_id'] not in json.dumps(turn)
    assert all('长8厘米' not in str(request) for request in env.gateway.requests)


def test_document_abstention_does_not_submit_to_an_old_exam(env, monkeypatch):
    completed_observation(env)
    monkeypatch.setattr(env.agent.exam_simulation, 'active_for_session', lambda _: {'status': 'active'})
    turn = say(env, '我不知道', 'document-not-exam')
    assert turn['intent'] == 'document_observation_response'
    assert turn['action']['document_response']['child_response'] == '我不知道'
    assert env.gateway.requests == []
