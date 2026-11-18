import asyncio
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from home_study_agent.database import Database
from home_study_agent.mistake_graph import MistakeGraphMatcher
from home_study_agent.mistake_learning import MistakeLearningService, _source, analysis_context_key, _tutor_route_identity, independent_analysis_context_key
from home_study_agent.mistake_learning_intake import MistakeLearningIntake
from home_study_agent.mistake_learning_privacy import export_learning, purge_learning
from home_study_agent.mistake_topic_learning import MistakeTopicLearning, TopicInput, create_router
from home_study_agent.model_gateway import ModelResult
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent.repository import StudyRepository
from normal_provider_owner import normal_provider_owner


CHILD = 'child-primary-4'
OTHER = 'child-junior-3'
PATH = f'/v1/children/{CHILD}/mistake-learning/topics/explain'
POINT = '异分母分数加法'
GRAPH_VERSION = 'atomic-full-v6.db@' + 'f' * 64 + '+mistake-graph-v1@' + 'a' * 24
ANALYSIS = {'summary': '先通分再把分子相加。',
            'knowledge_points': [{'label': POINT, 'description': '以同一单位份数相加。'}],
            'wrong_causes': [{'label': '直接相加分母', 'explanation': '可能把两个不同单位的份数当成相同。'}],
            'correction_strategy': ['先检查分母。']}


class TeachingModel:
    def __init__(self):
        self.requests = []
        self.hook = None
        self.bad_references = False

    async def complete(self, request):
        request.pre_dispatch()
        self.requests.append(request)
        if self.hook:
            self.hook()
        await asyncio.sleep(0)
        cases = json.loads(request.messages[-1]['content'])['cases']
        result = {'summary': '这几道题的单位份数不同，先判断怎么通分，再考虑分子如何相加。',
                  'patterns': [{'label': '把分母直接相加', 'explanation': '三分之一和四分之一每份大小不同，换到其他分母仍要先统一单位。',
                                'case_refs': ['题1', '题9'] if self.bad_references else [case['case_ref'] for case in cases]}],
                  'learning_steps': ['把两个分数画在等长的数轴上。', '比较通分前后每一份的大小。'],
                  'socratic_question': '如果要把五分之一和十分之一放到同一条数轴上，你准备怎样分格？'}
        return ModelResult(json.dumps(result, ensure_ascii=False), 'deepseek', 'synthetic', 25, 80, 100)


@pytest.fixture
def topic_world(tmp_path):
    db = Database(tmp_path / 'topic.db')
    db.initialize()
    PrivacyLifecycleService(db, tmp_path).initialize()
    repo = StudyRepository(db)
    repo.seed_family()
    session = repo.start_session(CHILD, 'topic-seed-session')['session_id']
    intake, model = MistakeLearningIntake(db), TeachingModel()
    model.settings=SimpleNamespace(tutor_explanation_api_key='synthetic-tutor-key-only',
        tutor_explanation_base_url='https://synthetic-tutor.invalid/v1',tutor_explanation_model='deepseek-v4.1-flash')
    learning = MistakeLearningService(db, model)
    with db.connect() as c:
        c.execute("UPDATE child_profile SET nickname='合成学生名' WHERE child_id=?", (CHILD,))
        c.execute('UPDATE privacy_preference SET cloud_model_text_allowed=1 WHERE singleton_id=1')
        c.commit()

    def add(child, question, answer, subject='数学'):
        card = intake.create(child_id=child, question=question, submitted_answer=answer, subject=subject,
                             client_action_id='seed-' + str(len(question)) + '-' + answer)
        mid = card['mistake_id']
        with db.connect() as c:
            _, revision = _source(c, child, mid)
            learning._state_row(c, mid, child, revision)
            c.execute('UPDATE mistake_learning_state SET analysis_json=?,analysis_context_key=? WHERE mistake_id=?',
                      (json.dumps(ANALYSIS, ensure_ascii=False),analysis_context_key(None,_tutor_route_identity(model)),mid))
            c.commit()
        return {'mistake_id': mid, 'source_revision': revision}

    refs = [add(CHILD, '合成学生名计算三分之一加四分之一。', '2/7'),
            add(CHILD, '计算五分之二加七分之三。', '5/12'),
            add(CHILD, '计算五分之一加八分之三。', '4/13')]
    foreign = add(OTHER, '计算六分之一加八分之三。', '4/14')
    different = add(CHILD, '指出此文使用的修辞。', '反问', subject='语文')
    body = {'subject': '数学', 'knowledge_point': POINT, 'session_id': session, 'sources': refs[:2]}

    def app(*, current_graph_version=None, authorize_graph_label=lambda graph: ([{'ref': 'C1', 'label': graph['alternatives'][0]['title']}], {})):
        application = FastAPI()
        application.include_router(create_router(db, model, authorize_request=lambda req: req.headers.get('X-Fixture') == 'topic',
                                                 current_graph_version=current_graph_version,
                                                 authorize_graph_label=authorize_graph_label))
        return TestClient(application, headers={'X-Fixture': 'topic'})

    return db, model, body, refs, foreign, different, app


def test_cross_case_result_reads_after_reopen_and_erases_with_learning_scope(topic_world):
    db, model, body, _, _, _, app = topic_world
    with app() as client:
        assert client.post(PATH, json=body, headers={'X-Fixture': 'invalid'}).status_code == 403
        empty = client.post(PATH, json=body | {'reuse_only': True}).json()
        assert empty['status'] == 'missing' and not model.requests
        ready = client.post(PATH, json=body)
        assert ready.status_code == 200, ready.json()
    value = ready.json()
    assert value['status'] == 'ready'
    assert value['sources'] == sorted(body['sources'], key=lambda ref: ref['mistake_id'])
    assert set(value['explanation']['patterns'][0]['mistake_ids']) == {ref['mistake_id'] for ref in body['sources']}
    assert value['explanation']['socratic_question'].endswith('？')
    request = model.requests[0]
    sent = request.messages[-1]['content']
    assert request.request_type == 'mistake_learning_topic' and request.context_manifest['formal_mastery'] is False
    assert '合成学生名' not in sent and '孩子称呼已脱敏' in sent
    assert CHILD not in sent and all(ref['mistake_id'] not in sent for ref in body['sources'])
    with app() as client:
        reopened = client.post(PATH, json=body | {'sources': body['sources'][::-1], 'reuse_only': True})
        assert reopened.json() == value and len(model.requests) == 1
    with db.connect() as c:
        assert export_learning(c, CHILD)['mistake_learning_topic']
        assert export_learning(c, OTHER)['mistake_learning_topic'] == []
        assert purge_learning(c, CHILD)['mistake_learning_topic'] == 1
        c.commit()
        assert export_learning(c, CHILD)['mistake_learning_topic'] == []


def test_topic_uses_only_analysis_from_current_dedicated_route(topic_world):
    from types import SimpleNamespace
    from home_study_agent.mistake_learning import _tutor_route_identity
    db,model,body,refs,foreign,_,app=topic_world
    model.settings=SimpleNamespace(tutor_explanation_api_key='synthetic-tutor-key-only',
        tutor_explanation_base_url='https://synthetic-tutor.invalid/v1',tutor_explanation_model='deepseek-v4.1-flash')
    route=_tutor_route_identity(model)
    with db.connect() as c:
        for ref in refs[:2]:
            c.execute('UPDATE mistake_learning_state SET analysis_context_key=? WHERE mistake_id=?',
                      (analysis_context_key(None,route),ref['mistake_id']))
        c.commit()
    with app() as client:
        response=client.post(PATH,json=body)
        assert response.status_code==200 and response.json()['status']=='ready'
        assert client.post(PATH,json=body | {'sources':[refs[0],foreign]}).status_code != 200
        model.settings.tutor_explanation_model='deepseek-synthetic-b'
        changed=client.post(PATH,json=body)
        assert changed.status_code != 200 and changed.json()['detail']['code']=='MISTAKE_TOPIC_SOURCE_MISMATCH'


def test_revoked_graph_label_right_does_not_resend_cached_case_analysis(topic_world):
    db,model,body,refs,_,_,app=topic_world
    for ref in refs[:2]:
        _store_graph(db,ref,bind_analysis=True)
        with db.connect() as c:
            analysis=ANALYSIS | {'wrong_causes':[{'label':'撤销标签哨兵','explanation':'不可再次发送的旧图谱内容'}]}
            c.execute('UPDATE mistake_learning_state SET analysis_json=? WHERE mistake_id=?',(json.dumps(analysis,ensure_ascii=False),ref['mistake_id']));c.commit()
    keyed=body | {'knowledge_node_key':'canonical-unit-addition',
                  'sources':[ref | {'graph_version':GRAPH_VERSION} for ref in refs[:2]]}
    with app(current_graph_version=lambda:GRAPH_VERSION,authorize_graph_label=lambda graph:([],{})) as client:
        result=client.post(PATH,json=keyed)
        assert result.status_code==200
    assert '撤销标签哨兵' not in json.dumps([r.messages for r in model.requests],ensure_ascii=False)


def test_graph_topic_cache_is_bound_to_dedicated_model_even_without_case_analysis(topic_world):
    from types import SimpleNamespace
    db,model,body,refs,_,_,app=topic_world
    model.settings=SimpleNamespace(tutor_explanation_api_key='synthetic-tutor-key-only',
        tutor_explanation_base_url='https://synthetic-tutor.invalid/v1',tutor_explanation_model='deepseek-synthetic-a')
    for ref in refs[:2]:
        _store_graph(db,ref)
    keyed=body | {'knowledge_node_key':'canonical-unit-addition',
                  'sources':[ref | {'graph_version':GRAPH_VERSION} for ref in refs[:2]]}
    with app(current_graph_version=lambda:GRAPH_VERSION) as client:
        assert client.post(PATH,json=keyed).json()['status']=='ready'
        assert client.post(PATH,json=keyed | {'reuse_only':True}).json()['status']=='ready'
        model.settings.tutor_explanation_model='deepseek-synthetic-b'
        assert client.post(PATH,json=keyed | {'reuse_only':True}).json()['status']=='missing'
        model.settings=None
        assert client.post(PATH,json=keyed | {'reuse_only':True}).json()['status']=='missing'


def test_unidentified_legacy_topic_cache_is_not_used_when_model_is_unconfigured(topic_world):
    from home_study_agent.mistake_learning import _hash
    from home_study_agent.mistake_topic_learning import PROMPT_VERSION
    db,model,body,_,_,_,app=topic_world
    with app() as client:
        assert client.post(PATH,json=body).json()['status']=='ready'
        service=MistakeTopicLearning(db,model)
        with db.connect() as c:
            snapshot=service._snapshot(c,CHILD,TopicInput(**body))
            legacy_key=_hash([PROMPT_VERSION,snapshot.topic_key,snapshot.manifest,snapshot.model_context])
            c.execute('UPDATE mistake_learning_topic SET source_key=? WHERE child_id=?',(legacy_key,CHILD))
            c.commit()
        model.settings=None
        result=client.post(PATH,json=body | {'reuse_only':True})
        assert result.status_code==422 and result.json()['detail']['code']=='MISTAKE_TOPIC_SOURCE_MISMATCH'


def test_bad_scope_or_model_output_is_never_persisted(topic_world):
    db, model, body, refs, foreign, different, app = topic_world
    with app() as client:
        assert client.post(PATH, json=body | {'sources': [refs[0], foreign]}).status_code == 404
        assert client.post(PATH, json=body | {'sources': [refs[0], different]}).json()['detail']['code'] == 'MISTAKE_TOPIC_SOURCE_MISMATCH'
        assert client.post(PATH, json=body | {'sources': [refs[0], refs[0]]}).json()['detail']['code'] == 'MISTAKE_TOPIC_REQUEST_INVALID'
        assert client.post(PATH, json=body | {'sources': [refs[0], refs[1] | {'source_revision': '0'*64}]}).json()['detail']['code'] == 'MISTAKE_TOPIC_SOURCE_CHANGED'
        assert not model.requests
        model.bad_references = True
        assert client.post(PATH, json=body).json()['detail']['code'] == 'MISTAKE_TOPIC_MODEL_OUTPUT_INVALID'
        assert client.post(PATH, json=body | {'reuse_only': True}).json()['status'] == 'missing'
        with db.connect() as c:
            c.execute('UPDATE privacy_preference SET cloud_model_text_allowed=0')
            c.commit()
        assert client.post(PATH, json=body).status_code == 403 and len(model.requests) == 1


def test_revocation_while_model_answers_and_independent_concurrent_source_sets(topic_world):
    db, model, body, refs, _, _, app = topic_world

    def revoke():
        with db.connect() as c:
            c.execute('UPDATE privacy_preference SET cloud_model_text_allowed=0')
            c.commit()

    model.hook = revoke
    with app() as client:
        assert client.post(PATH, json=body).status_code == 403
        assert client.post(PATH, json=body | {'reuse_only': True}).json()['status'] == 'missing'
    with db.connect() as c:
        c.execute('UPDATE privacy_preference SET cloud_model_text_allowed=1')
        c.commit()
    model.hook = None
    service = MistakeTopicLearning(db, model)
    first, second = TopicInput(**body), TopicInput(**(body | {'sources': refs[1:]}))

    async def generate():
        return await asyncio.gather(service.explain(CHILD, first), service.explain(CHILD, second), service.explain(CHILD, first))

    a, b, duplicate = asyncio.run(generate())
    assert a == duplicate and a['sources'] != b['sources']
    assert len(model.requests) == 3  # One revoked call and two distinct generations.
    with app() as client:
        assert client.post(PATH, json=body | {'reuse_only': True}).json() == a
        assert client.post(PATH, json=body | {'sources': refs[1:], 'reuse_only': True}).json() == b


def _store_graph(db, ref, *, key='canonical-unit-addition', version=GRAPH_VERSION, prereq='最小公倍数', analysis_label=None,
                 subject=None, bind_analysis=False):
    graph = {'status': 'matched', 'graph_version': version,
             'primary': {'key': key, 'title': POINT, 'review_status': 'approved', 'confidence': .94},
             'prerequisites': [{'key': 'internal-prerequisite-key', 'title': prereq, 'review_status': 'approved',
                                'relation_review_status': 'approved', 'source_fragment': '不可上云的书页片段'},
                               {'key': 'restricted-key', 'title': '受限前置标签', 'review_status': 'restricted',
                                'relation_review_status': 'approved'}],
             'alternatives': [{'key': 'unselected-alternative', 'title': '未经确认的相邻知识点', 'review_status': 'approved', 'confidence': .53}]}
    if subject:
        graph['subject'] = subject
    with db.connect() as c:
        c.execute('UPDATE mistake_learning_state SET graph_json=? WHERE mistake_id=? AND source_revision=?',
                  (json.dumps(graph, ensure_ascii=False), ref['mistake_id'], ref['source_revision']))
        if analysis_label:
            analysis = ANALYSIS | {'knowledge_points': [{'label': analysis_label, 'description': '旧标签只描述现象。'}]}
            c.execute('UPDATE mistake_learning_state SET analysis_json=? WHERE mistake_id=?',
                      (json.dumps(analysis, ensure_ascii=False), ref['mistake_id']))
        if bind_analysis:
            c.execute('UPDATE mistake_learning_state SET analysis_context_key=? WHERE mistake_id=?',
                      (analysis_context_key(graph, {'provider':'deepseek','endpoint':'https://synthetic-tutor.invalid/v1',
                                                   'model':'deepseek-v4.1-flash'}), ref['mistake_id']))
        else:
            c.execute('UPDATE mistake_learning_state SET analysis_context_key=NULL WHERE mistake_id=?',(ref['mistake_id'],))
        c.commit()


def test_graph_topic_joins_synonyms_only_by_confirmed_canonical_key_and_sends_whitelisted_hints(topic_world):
    db, model, body, refs, _, _, app = topic_world
    _store_graph(db, refs[0], analysis_label='先通分', bind_analysis=True)
    _store_graph(db, refs[1], analysis_label='不同单位的加法')
    _store_graph(db, refs[2], key='same-display-title-different-graph-key')
    keyed = body | {'knowledge_node_key': 'canonical-unit-addition',
                    'sources': [ref | {'graph_version': GRAPH_VERSION} for ref in refs[:2]]}
    with app(current_graph_version=lambda: GRAPH_VERSION) as client:
        assert client.post(PATH, json=body).json()['detail']['code'] == 'MISTAKE_TOPIC_SOURCE_MISMATCH'
        assert client.post(PATH, json=keyed | {'sources': body['sources']}).json()['detail']['code'] == 'MISTAKE_TOPIC_REQUEST_INVALID'
        wrong_graph_key = keyed | {'sources': [keyed['sources'][0], refs[2] | {'graph_version': GRAPH_VERSION}]}
        assert client.post(PATH, json=wrong_graph_key).json()['detail']['code'] == 'MISTAKE_TOPIC_SOURCE_CHANGED'
        assert not model.requests
        ready = client.post(PATH, json=keyed)
        assert ready.status_code == 200, ready.json()
        result = ready.json()
        assert result['knowledge_node_key'] == keyed['knowledge_node_key']
        assert result['sources'] == sorted(keyed['sources'], key=lambda ref: ref['mistake_id'])
        assert client.post(PATH, json=keyed | {'reuse_only': True}).json() == result
    assert len(model.requests) == 1
    request = model.requests[0]
    content = request.messages[-1]['content']
    sent = json.loads(content)
    assert sent['knowledge_point'] == POINT
    assert sorted(len(case['possible_causes']) for case in sent['cases']) == [0, 1]
    assert all(case['graph_hint']['prerequisite_hypotheses'] == [
        {'title': '最小公倍数', 'review_status': 'approved', 'relation_review_status': 'approved'},
    ] for case in sent['cases'])
    assert all(value not in content for value in ('canonical-unit-addition', 'internal-prerequisite-key', GRAPH_VERSION,
               '不可上云的书页片段', '受限前置标签', '未经确认的相邻知识点'))
    assert '不能说明孩子哪里不会' in request.messages[0]['content']


def test_graph_topic_rechecks_live_version_and_invalidates_cache_on_any_stored_graph_change(topic_world):
    db, model, body, refs, _, _, app = topic_world
    for ref in refs[:2]:
        _store_graph(db, ref)
    keyed = body | {'knowledge_node_key': 'canonical-unit-addition',
                    'sources': [ref | {'graph_version': GRAPH_VERSION} for ref in refs[:2]]}
    version = [GRAPH_VERSION]
    with app(current_graph_version=lambda: version[0]) as client:
        assert client.post(PATH, json=keyed).json()['status'] == 'ready'
        _store_graph(db, refs[0], prereq='等值分数')
        assert client.post(PATH, json=keyed | {'reuse_only': True}).json()['status'] == 'missing'
        model.hook = lambda: _store_graph(db, refs[1], prereq='分数的单位')
        assert client.post(PATH, json=keyed).json()['detail']['code'] == 'MISTAKE_TOPIC_SOURCE_CHANGED'
        assert client.post(PATH, json=keyed | {'reuse_only': True}).json()['status'] == 'missing'
        model.hook = None
        assert client.post(PATH, json=keyed).json()['status'] == 'ready'
        version[0] = 'graph@v2'
        assert client.post(PATH, json=keyed | {'reuse_only': True}).json()['detail']['code'] == 'MISTAKE_TOPIC_GRAPH_STALE'
        assert client.post(PATH, json=body | {'reuse_only': True}).json()['detail']['code'] == 'MISTAKE_TOPIC_GRAPH_STALE'
    assert len(model.requests) == 3


def test_graph_topic_uses_inferred_subject_for_manually_entered_question_without_analysis(topic_world):
    db, model, body, refs, _, _, app = topic_world
    card = MistakeLearningIntake(db).create(child_id=CHILD, subject='', question='计算二分之一加七分之三。',
                                           submitted_answer='4/9', client_action_id='topic-manual-inferred')
    mid = card['mistake_id']
    learning = MistakeLearningService(db, model)
    with db.connect() as c:
        _, revision = _source(c, CHILD, mid)
        learning._state_row(c, mid, CHILD, revision)
        c.commit()
    manual = {'mistake_id': mid, 'source_revision': revision}
    _store_graph(db, manual, subject='数学')
    _store_graph(db, refs[0])
    keyed = body | {'knowledge_node_key': 'canonical-unit-addition',
                    'sources': [ref | {'graph_version': GRAPH_VERSION} for ref in (refs[0], manual)]}
    with app(current_graph_version=lambda: GRAPH_VERSION) as client:
        assert client.post(PATH, json=keyed).json()['status'] == 'ready'
        assert client.post(PATH, json=keyed | {'subject': '物理'}).json()['detail']['code'] == 'MISTAKE_TOPIC_SOURCE_MISMATCH'
    assert all(not case['possible_causes'] for case in json.loads(model.requests[0].messages[-1]['content'])['cases'])


def test_graph_topic_fails_closed_on_unproven_labels_and_rechecks_rights(topic_world):
    db, model, body, refs, _, _, app = topic_world
    for ref in refs[:2]:
        _store_graph(db, ref)
    keyed = body | {'knowledge_node_key': 'canonical-unit-addition',
                    'sources': [ref | {'graph_version': GRAPH_VERSION} for ref in refs[:2]]}
    active = {'primary': True}

    def authorize(graph):
        title = graph['alternatives'][0]['title']
        if active['primary'] and title == POINT:
            return ([{'ref': 'C1', 'label': title}], {})
        return ([], {})

    with app(current_graph_version=lambda: GRAPH_VERSION, authorize_graph_label=authorize) as client:
        assert client.post(PATH, json=keyed).status_code == 200
        content = model.requests[-1].messages[-1]['content']
        sent = json.loads(content)
        assert sent['knowledge_point'] == POINT
        assert '最小公倍数' not in content
        assert all(not case['graph_hint']['prerequisite_hypotheses'] for case in sent['cases'])
        active['primary'] = False
        assert client.post(PATH, json=keyed | {'reuse_only': True}).json()['status'] == 'missing'
        assert client.post(PATH, json=keyed).status_code == 200
        content = model.requests[-1].messages[-1]['content']
        assert POINT not in content and '最小公倍数' not in content
        assert json.loads(content)['knowledge_point'] == '根据所选错题归纳共同知识点'
        assert all('knowledge_point' not in case['graph_hint'] for case in json.loads(content)['cases'])


def test_graph_topic_unavailable_or_failed_rights_check_never_exports_titles(topic_world):
    db, model, body, refs, _, _, app = topic_world
    for ref in refs[:2]:
        _store_graph(db, ref)
    keyed = body | {'knowledge_node_key': 'canonical-unit-addition',
                    'sources': [ref | {'graph_version': GRAPH_VERSION} for ref in refs[:2]]}
    def failure(_):
        raise RuntimeError('verification unavailable')
    for verify in (None, failure):
        with app(current_graph_version=lambda: GRAPH_VERSION, authorize_graph_label=verify) as client:
            assert client.post(PATH, json=keyed).status_code == 200
            content = model.requests[-1].messages[-1]['content']
            assert POINT not in content and '最小公倍数' not in content


def test_topic_real_matcher_checks_live_label_rights(topic_world, tmp_path):
    db, model, body, refs, _, _, app = topic_world
    build = tmp_path / 'topic-rights.db'
    with sqlite3.connect(build) as c:
        c.execute('CREATE TABLE kg_atomic_definition_draft(draft_id TEXT, canonical_signature TEXT, title TEXT, review_status TEXT)')
        c.execute('CREATE TABLE kg_resource(resource_id TEXT, review_status TEXT)')
        c.execute('CREATE TABLE kg_atomic_draft_evidence(draft_id TEXT, resource_id TEXT, exact_quote TEXT)')
        c.execute('CREATE TABLE kg_atomic_label_cloud_right(draft_id TEXT, title_sha256 TEXT, origin TEXT, status TEXT, basis TEXT)')
        c.execute('INSERT INTO kg_atomic_definition_draft VALUES(?,?,?,?)', ('d1', 'canonical-unit-addition', POINT, 'approved'))
        c.execute('INSERT INTO kg_resource VALUES(?,?)', ('r1', 'restricted'))
        c.execute('INSERT INTO kg_atomic_draft_evidence VALUES(?,?,?)', ('d1', 'r1', '合成出处，不含标签'))
        c.execute('INSERT INTO kg_atomic_label_cloud_right VALUES(?,?,?,?,?)',
                  ('d1', hashlib.sha256(POINT.encode()).hexdigest(), 'project_authored', 'active', '人工核对'))
    for ref in refs[:2]:
        _store_graph(db, ref)
    keyed = body | {'knowledge_node_key': 'canonical-unit-addition',
                    'sources': [ref | {'graph_version': GRAPH_VERSION} for ref in refs[:2]]}
    matcher = MistakeGraphMatcher(db, build)
    with app(current_graph_version=lambda: GRAPH_VERSION, authorize_graph_label=matcher.semantic_options) as client:
        assert client.post(PATH, json=keyed).status_code == 200
        content = model.requests[-1].messages[-1]['content']
        assert POINT in content and '最小公倍数' not in content
        with sqlite3.connect(build) as c:
            c.execute("UPDATE kg_atomic_label_cloud_right SET status='revoked' WHERE draft_id='d1'")
        assert client.post(PATH, json=keyed | {'reuse_only': True}).json()['status'] == 'missing'
        assert client.post(PATH, json=keyed).status_code == 200
        assert POINT not in model.requests[-1].messages[-1]['content']


def test_unreviewed_graph_cannot_start_or_reuse_topic_even_with_cloud_label_right(topic_world):
    db, model, body, refs, foreign, _, app = topic_world
    for ref in refs[:2]:
        _store_graph(db, ref, bind_analysis=True)
        with db.connect() as c:
            row = c.execute('SELECT graph_json FROM mistake_learning_state WHERE mistake_id=?', (ref['mistake_id'],)).fetchone()
            graph = json.loads(row['graph_json'])
            graph['primary']['review_status'] = 'unreviewed'
            c.execute('UPDATE mistake_learning_state SET graph_json=? WHERE mistake_id=?', (json.dumps(graph), ref['mistake_id']))
            c.commit()
    keyed = body | {'knowledge_node_key': 'canonical-unit-addition',
                    'sources': [ref | {'graph_version': GRAPH_VERSION} for ref in refs[:2]]}
    with app(current_graph_version=lambda: GRAPH_VERSION) as client:
        for reuse in (False, True):
            result = client.post(PATH, json=keyed | {'reuse_only': reuse})
            assert result.status_code >= 400
            assert result.json()['detail']['code'] == 'MISTAKE_TOPIC_SOURCE_CHANGED'
        cross_child = keyed | {'sources': [keyed['sources'][0], foreign | {'graph_version': GRAPH_VERSION}]}
        assert client.post(PATH, json=cross_child).status_code >= 400
    assert not model.requests


def test_unreviewed_graph_falls_back_only_to_current_independent_analysis(topic_world):
    db, model, body, refs, foreign, _, app = topic_world
    from types import SimpleNamespace
    model.settings = SimpleNamespace(tutor_explanation_api_key='synthetic-only-tutor-key', tutor_explanation_base_url='https://tutor.invalid/v1', tutor_explanation_model='deepseek-v4.1-flash')
    route = _tutor_route_identity(model)
    assert route
    for ref in refs[:2]:
        _store_graph(db, ref)
        with db.connect() as c:
            row = c.execute('SELECT graph_json FROM mistake_learning_state WHERE mistake_id=?', (ref['mistake_id'],)).fetchone()
            graph = json.loads(row['graph_json'])
            graph['primary']['review_status'] = 'unreviewed'
            graph['primary']['title'] = '不可当依据的图谱候选'
            c.execute('UPDATE mistake_learning_state SET graph_json=?,analysis_context_key=? WHERE mistake_id=?',
                      (json.dumps(graph), independent_analysis_context_key(route), ref['mistake_id']))
            c.commit()
    with app(current_graph_version=lambda: GRAPH_VERSION) as client:
        result = client.post(PATH, json=body)
        assert result.status_code == 200, result.text
        assert result.json()['status'] == 'ready'
        payload = model.requests[-1].messages[-1]['content']
        assert POINT in payload
        assert '不可当依据的图谱候选' not in payload
        assert 'internal-prerequisite-key' not in payload
        assert 'graph_hint' not in payload
        assert client.post(PATH, json=body | {'reuse_only': True}).json()['status'] == 'ready'
        assert client.post(PATH, json=body | {'sources': [refs[0], foreign]}).status_code >= 400
        assert client.post(PATH, json=body | {'sources': [refs[0], refs[1] | {'source_revision':'0'*64}]}).status_code >= 400
        model.settings.tutor_explanation_base_url = 'https://other-tutor.invalid/v1'
        assert client.post(PATH, json=body | {'reuse_only': True}).status_code >= 400
        model.settings.tutor_explanation_base_url = 'https://tutor.invalid/v1'
        with db.connect() as c:
            c.execute("UPDATE mistake_learning_state SET analysis_context_key='stale-route' WHERE mistake_id=?", (refs[1]['mistake_id'],))
            c.commit()
        assert client.post(PATH, json=body | {'reuse_only': True}).status_code >= 400
        with db.connect() as c:
            row = c.execute('SELECT graph_json FROM mistake_learning_state WHERE mistake_id=?', (refs[1]['mistake_id'],)).fetchone()
            graph = json.loads(row['graph_json'])
            c.execute('UPDATE mistake_learning_state SET analysis_context_key=? WHERE mistake_id=?',
                      (analysis_context_key(None, route), refs[1]['mistake_id']))
            c.commit()
        # Older unbound analysis is not proof that historical chat was absent.
        assert client.post(PATH, json=body).status_code >= 400
        with db.connect() as c:
            c.execute('UPDATE mistake_learning_state SET analysis_context_key=? WHERE mistake_id=?',
                      (analysis_context_key(graph, route), refs[1]['mistake_id']))
            c.commit()
        assert client.post(PATH, json=body).status_code >= 400
    assert len(model.requests) == 1


def test_two_unreviewed_cases_independently_analyze_then_build_topic_and_reload(topic_world):
    db, _, body, refs, _, _, _ = topic_world
    class Gateway(TeachingModel):
        def __init__(self):
            super().__init__()
            from types import SimpleNamespace
            self.settings = SimpleNamespace(tutor_explanation_api_key='synthetic-only-tutor-key', tutor_explanation_base_url='https://tutor.invalid/v1', tutor_explanation_model='deepseek-v4.1-flash')
        async def complete(self, request):
            if request.request_type == 'mistake_learning_analyze':
                self.requests.append(request)
                return ModelResult(json.dumps(ANALYSIS, ensure_ascii=False), 'deepseek', 'synthetic', 25, 80, 100)
            return await super().complete(request)
    class Matcher:
        def cache_version(self, *, connection=None): return GRAPH_VERSION
        def match(self, *args, **kwargs):
            return {'graph_version': GRAPH_VERSION, 'status': 'matched', 'subject': '数学',
                    'primary': {'key': 'unreviewed-internal-key', 'title': '未审候选不能入专题', 'review_status': 'unreviewed', 'confidence': .95},
                    'prerequisites': [], 'alternatives': []}
        def semantic_options(self, graph): raise AssertionError('UNREVIEWED_EXPORT_FORBIDDEN')
    gateway = Gateway()
    learning = MistakeLearningService(db, gateway, graph_matcher=Matcher())
    async def analyze():
        for i, ref in enumerate(refs[:2]):
            state = await learning.analyze(ref['mistake_id'], child_id=CHILD, session_id=body['session_id'],
                                           client_action_id='independent-analysis-' + str(i))
            assert state['action_status'] == 'completed', state
            assert state['analysis_independent'] and state['analysis']['knowledge_points'][0]['label'] == POINT
            assert state['graph_review_focus'] is None
    asyncio.run(analyze())
    for ref in refs[:2]:
        state = learning.state(ref['mistake_id'], child_id=CHILD, session_id=body['session_id'])
        assert state['analysis_independent'] and state['analysis']['knowledge_points'][0]['label'] == POINT
    topic = MistakeTopicLearning(db, gateway, current_graph_version=lambda: GRAPH_VERSION)
    result = asyncio.run(topic.explain(CHILD, TopicInput(**body)))
    assert result['status'] == 'ready'
    reopened = MistakeTopicLearning(Database(db.path), gateway, current_graph_version=lambda: GRAPH_VERSION)
    assert asyncio.run(reopened.explain(CHILD, TopicInput(**(body | {'reuse_only': True})))) == result
    assert len(gateway.requests) == 3
    payloads = json.dumps([request.messages for request in gateway.requests], ensure_ascii=False)
    assert '未审候选不能入专题' not in payloads and 'unreviewed-internal-key' not in payloads


def test_mocktransport_independent_cases_survive_restart_and_feed_topic_and_native(topic_world,tmp_path,normal_provider_owner):
    import httpx
    from home_study_agent.config import Settings
    from home_study_agent.model_gateway import ModelGateway
    from home_study_agent.mistake_learning import MistakeLearningError
    from home_study_agent.realtime_capability_tools import RealtimeCapabilityTools
    from home_study_agent.realtime_native_context import load_native_context
    db,_,body,refs,foreign,_,_=topic_world
    calls=[]
    def transport(request):
        payload=json.loads(request.content);calls.append(payload)
        instruction=payload['messages'][0]['content']
        if 'wrong_causes' in instruction:
            value=ANALYSIS
        else:
            cases=json.loads(payload['messages'][-1]['content'])['cases']
            value={'summary':'先统一单位再相加。','patterns':[{'label':'分母混淆','explanation':'统一单位后再相加。',
                'case_refs':[case['case_ref'] for case in cases]}], 'learning_steps':['先找到公分母。','再相加分子。'],
                'socratic_question':'先选哪个公分母？'}
        return httpx.Response(200,request=request,json={'choices':[{'message':{'content':json.dumps(value,ensure_ascii=False)},
            'finish_reason':'stop'}],'usage':{'prompt_tokens':10,'completion_tokens':20}})
    class Matcher:
        def cache_version(self,*,connection=None): return GRAPH_VERSION
        def match(self,*args,**kwargs):
            return {'graph_version':GRAPH_VERSION,'status':'matched','subject':'数学',
                'primary':{'key':'private-unreviewed-key','title':'未审核图谱标签','review_status':'unreviewed','confidence':.95},
                'prerequisites':[],'alternatives':[]}
        def semantic_options(self,graph): raise AssertionError('UNREVIEWED_EXPORT_FORBIDDEN')
    config=Settings(data_dir=tmp_path,database_path=db.path,model_api_key='synthetic-general',
        tutor_explanation_api_key='synthetic-tutor-key-only',tutor_explanation_base_url='https://tutor.invalid/v1')
    gateway=ModelGateway(config,transport=httpx.MockTransport(transport)); matcher=Matcher()
    try:
        learning=MistakeLearningService(db,gateway,graph_matcher=matcher)
        async def generate():
            for i,ref in enumerate(refs[:2]):
                result=await learning.analyze(ref['mistake_id'],child_id=CHILD,session_id=body['session_id'],client_action_id=f'mock-independent-{i}')
                assert result['analysis_independent'] and result['analysis']
        asyncio.run(generate())
        reopened=MistakeLearningService(Database(db.path),gateway,graph_matcher=matcher)
        reopened.focus(refs[0]['mistake_id'],child_id=CHILD,session_id=body['session_id'],client_action_id='mock-focus')
        with db.connect() as c:
            c.execute('UPDATE privacy_preference SET cloud_audio_allowed=1 WHERE singleton_id=1');c.commit()
        native=RealtimeCapabilityTools(db,current_graph_version=matcher.cache_version,tutor_route_identity=lambda:_tutor_route_identity(gateway))
        context=load_native_context(native,body['session_id'],authorize=lambda *args:True)
        assert context['runtime_facts']['mistake_learning']['analysis']
        topic=MistakeTopicLearning(db,gateway,current_graph_version=matcher.cache_version)
        assert asyncio.run(topic.explain(CHILD,TopicInput(**body)))['status']=='ready'
        reopened_topic=MistakeTopicLearning(Database(db.path),gateway,current_graph_version=matcher.cache_version)
        assert asyncio.run(reopened_topic.explain(CHILD,TopicInput(**(body | {'reuse_only':True}))))['status']=='ready'
        with pytest.raises(MistakeLearningError):
            asyncio.run(topic.explain(CHILD,TopicInput(**(body | {'sources':[refs[0],foreign]}))))
        payload=json.dumps(calls,ensure_ascii=False)
        assert '未审核图谱标签' not in payload and 'private-unreviewed-key' not in payload
        assert len(calls)==3
    finally:
        asyncio.run(gateway.close())
