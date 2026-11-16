"""Signed native input and real Tutor reservations with a public model fixture."""

import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import hmac
import json
import socket
from threading import Event

from fastapi.testclient import TestClient
import httpx

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.curriculum_supply import CurriculumSupplyService
from home_study_agent.model_gateway import ModelGateway
from home_study_agent.realtime_capability_http import DOMAIN, FIELDS
from home_study_agent.realtime_native_curriculum import CLARIFICATION, current_request


CHILD = 'child-junior-3'
KEY = 'ac' * 32
TOKEN = 'synthetic-curriculum-session-token'
NARRATION = '化学研究物质及其变化。水结冰时，水的形态变了。你能举出另一种状态变化吗？'


def signed_post(client, root, operation, body):
    paths = {'input': 'native-input', 'proposal': 'capability-proposals', 'client': 'native-client'}
    raw = json.dumps(body, ensure_ascii=False).encode()
    proof = {'version': 1, 'operation': operation, 'session_id': root.rsplit('/', 1)[1],
             'voice_input_id': body.get('voice_input_id', ''), 'connection_id': 'curriculum-public-host',
             'connection_generation': 1, 'body_sha256': hashlib.sha256(raw).hexdigest(),
             'issued_at': datetime.now(timezone.utc).isoformat()}
    message = json.dumps([DOMAIN] + [proof[k] for k in FIELDS], ensure_ascii=False, separators=(',', ':')).encode()
    proof['signature'] = hmac.new(bytes.fromhex(KEY), message, hashlib.sha256).hexdigest()
    header = base64.urlsafe_b64encode(json.dumps(proof).encode()).decode().rstrip('=')
    response = client.post(root + '/voice/realtime/' + paths[operation], content=raw,
                           headers={'X-HSC-Native-Voice-Proof': header})
    assert response.status_code == 200, response.text
    return response.json()


def native_voice(client, root, key, transcript, reference=None, *, captured_ms=None, with_receipt=False):
    voice = signed_post(client, root, 'input', {
        'version': 1, 'local_input_id': 'input-' + key, 'transcript': transcript,
        'captured_at_ms': captured_ms if captured_ms is not None else int(datetime.now(timezone.utc).timestamp() * 1000),
        'capture_reference': {'child_id': CHILD, 'question_reference': reference},
    })
    assert voice['status'] == 'completed' and 'local_control' not in voice
    identity = {'version': 1, 'voice_input_id': voice['voice_input_id'], 'supplier_session_id': 'public-session',
            'input_item_id': 'item-' + key, 'response_id': 'response-' + key, 'call_id': 'call-' + key}
    return (identity, voice) if with_receipt else identity


def propose(client, root, identity, capability, args, **extra):
    return signed_post(client, root, 'proposal', {**identity, 'capability_name': capability, 'args': args, **extra})


def claim_lesson(client, root, identity, *, scope='prepared_material', **extra):
    args = {'subject': '化学', 'question_source': 'spoken_request', 'support_level': 6}
    if scope is not None:
        args['material_scope'] = scope
    receipt = propose(client, root, identity, 'learning.explain', args, **extra)
    assert receipt['execution']['status'] == 'pending', receipt
    dispatch = receipt['client_dispatch']
    claimed = signed_post(client, root, 'client', {**identity, 'phase': 'claim', 'dispatch_id': dispatch['dispatch_id']})
    assert claimed['status'] == 'claimed', claimed
    return dispatch


def teach(client, root, identity, dispatch):
    action = dispatch['turn']['action']
    response = client.post(root + '/tutor', json={
        'subject': action['subject'], 'question': action['question'], 'hint_level': action['hint_level'],
        'client_action_id': action['tutor_request_action_id'],
    })
    assert response.status_code == 200, response.text
    tutor = response.json()
    receipt = {'capability': 'learning.explain', 'execution_location': 'local_model', 'action_type': 'ask_tutor',
               'status': 'completed', 'completed_at': datetime.now(timezone.utc).isoformat(),
               'output': {k: tutor[k] for k in ('provider', 'model', 'response_kind', 'verification_status', 'safety_status')}}
    receipt['output'].update(tutor_turn_id=tutor['turn_id'], blackboard_present=True)
    saved = signed_post(client, root, 'client', {**identity, 'phase': 'complete', 'dispatch_id': dispatch['dispatch_id'],
        'final_response': tutor['answer'], 'tool_status': 'completed', 'result': {'execution_receipt': receipt}})
    assert saved['execution']['status'] == 'completed', saved
    return tutor


def select_public_reference(db):
    now = datetime.now(timezone.utc).isoformat()
    excerpt = '公开课程样例：实验前先核对可观察条件，比较变化前后的物质。'
    with db.connect() as c:
        c.execute('''INSERT INTO knowledge_source(source_id,child_id,source_type,title,subject,grade_band,
            version_label,source_ref,license_status,review_status,content_hash,imported_at)
            VALUES('public-index',?,'textbook_index','公开合成目录','化学','初三','人教版',
              'public-test','public','approved','public-index-hash',?)''', (CHILD, now))
        c.execute('''INSERT INTO curriculum_corpus(corpus_id,title,corpus_type,source_database_path,
            transcript_root_path,video_root_path,source_ref,license_status,review_status,status,imported_at,updated_at)
            VALUES('public-course','合成课程','seyk','public-sample','','','public-test','public_personal_use',
              'approved_baseline','ready',?,?)''', (now, now))
        c.execute('''INSERT INTO curriculum_resource(corpus_id,external_resource_id,title,description,stage_title,
            grade_title,subject_title,version_title,volume_title,semester,unit_path,transcript_text,transcript_hash,import_job_id)
            VALUES('public-course','first','物质与变化','','初中','初三','化学','人教版','上册','上学期',
                '物质与变化',?,?,'public-import')''', (excerpt, hashlib.sha256(excerpt.encode()).hexdigest()))
        c.commit()
    CurriculumSupplyService(db).select_curriculum(CHILD, {
        'grade': '初三', 'subject': '化学', 'edition': '人教版', 'semester': '上学期',
        'standard_scope': '合成公开课程范围', 'textbook_source_id': 'public-index',
    }, actor='public-parent', action_id='public-select', expected_version=0)
    return excerpt


def test_saved_edition_and_deeper_request_use_the_same_durable_tutor_scope(tmp_path, monkeypatch):
    attempts = []

    def deny_network(*args, **kwargs):
        attempts.append(True)
        raise AssertionError('EXTERNAL_NETWORK_FORBIDDEN')

    monkeypatch.setattr(socket.socket, 'connect', deny_network)
    monkeypatch.setattr(socket.socket, 'connect_ex', deny_network)
    monkeypatch.setenv('HSC_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('HSC_DATABASE_PATH', str(tmp_path / 'public.db'))
    settings = Settings(data_dir=tmp_path, database_path=tmp_path / 'public.db', session_token=TOKEN,
                        native_launch_attestation_key=KEY, maintenance_enabled=False,
                        model_api_key='PUBLIC_SYNTHETIC', model_base_url='https://synthetic.invalid',
                        tutor_explanation_api_key='PUBLIC_SYNTHETIC_TUTOR',
                        tutor_explanation_base_url='https://tutor.example.test:7791/v1')
    requests = []
    quality_requests = []
    intents = []
    topic_words = '物质的结构和性质到底有什么关系？'
    routed_words = '能说得更清楚一点吗？'
    uncertain_words = '我只是提到课堂上的现象。'
    topic_started = Event()
    release_topic = Event()

    async def model(request):
        body = json.loads(request.content)
        if request.url.host == 'synthetic.invalid':
            assert body['model'] == 'qwen3.8-flash' and body['max_tokens'] == 80
            assert body['response_format'] == {'type': 'json_object'}
            source = json.loads(body['messages'][-1]['content'])
            assert {'course', 'utterance'} <= set(source) <= {'course', 'utterance', 'previous_teaching'}
            intents.append(source)
            value = {'intent': 'continue_tutor' if source['utterance'] in {topic_words, routed_words} else 'chat'}
            if source['utterance'] == uncertain_words:
                value = {'intent': 'continue_tutor', 'unexpected': True}
        else:
            assert request.url.host == 'tutor.example.test' and body['model'] == 'deepseek-v4.1-flash'
            if '你是教学呈现核查员' in body['messages'][0]['content']:
                quality_requests.append(body['messages'])
                value = {'applicability': 'uncertain'}
            else:
                requests.append(body['messages'])
                if f'孩子的问题（化学）：{topic_words}' in body['messages'][-1]['content']:
                    topic_started.set()
                    assert await asyncio.to_thread(release_topic.wait, 4), 'previous_tutor_test_timeout'
                value = {'blackboard': {'title': '化学入门', 'segments': [{'narration': NARRATION, 'blocks': [
                    {'kind': 'text', 'text': '物质与变化'}, {'kind': 'question', 'text': '你能举出另一种状态变化吗？'}]}]}}
        return httpx.Response(200, request=request, json={
            'choices': [{'message': {'content': json.dumps(value, ensure_ascii=False)}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 30, 'completion_tokens': 40},
        })

    app = create_app(settings, model_gateway=ModelGateway(settings, transport=httpx.MockTransport(model)))
    with TestClient(app, client=('127.0.0.1', 50000), headers={'X-HSC-Session': TOKEN}) as client:
        parent = client.post('/v1/parent/setup', json={'pin': '8264'}).json()
        consent = client.put('/v1/parent/privacy', headers={'X-HSC-Parent': parent['parent_token']},
                             json={'cloud_model_text_allowed': True, 'cloud_audio_allowed': True})
        assert consent.status_code == 200, consent.text
        session = client.post('/v1/sessions', json={'child_id': CHILD, 'client_action_id': 'public-curriculum-session'})
        assert session.status_code == 201, session.text
        session_id = session.json()['session_id']
        root = '/v1/sessions/' + session_id
        db = app.state.repository.database
        previous_onset = int(datetime.now(timezone.utc).timestamp() * 1000) - 1_000
        initial = native_voice(client, root, 'initial', '请帮我学初三化学第一课。')
        request = propose(client, root, initial, 'learning.prepare_request',
                          {'subject': '化学', 'requested_grade': '初三', 'lesson_title': '第一课'})
        first = {**initial, 'call_id': 'call-initial-explain', 'response_id': 'response-initial-explain'}
        missing_chain = propose(client, root, first, 'learning.explain', {
            'subject': '化学', 'material_scope': 'standalone_concept', 'question_source': 'spoken_request',
        })
        assert missing_chain['public_result']['reason_code'] == 'REALTIME_CAPABILITY_CONTINUATION_RECEIPT_REQUIRED'
        first_dispatch = claim_lesson(client, root, first, scope='standalone_concept', previous_action_id=request['action_id'])
        taught = teach(client, root, first, first_dispatch)
        assert taught['output_envelope']['curriculum_basis'] == 'conceptual'
        assert taught['answer'].startswith('这次先讲相关概念，教材原文还没核对。')
        assert taught['output_envelope']['result']['blackboard']['segments'][0]['narration'].startswith('这次先讲相关概念')
        with db.connect() as c:
            original = current_request(c, CHILD, session_id)
            assert original['prepare_turn_id'] == request['agent_turn_id']
        native_voice(client, root, 'uncertain', '是人教版还是苏教版？')
        native_voice(client, root, 'negated', '不是人教版九年级上册。')
        native_voice(client, root, 'other-grade', '我用人教版八年级上册，就是第一课。')
        old = native_voice(client, root, 'old-onset', '我用人教版九年级上册，就是第一课。', captured_ms=previous_onset)
        stale_update = propose(client, root, old, 'learning.prepare_request', {'textbook_edition': '人教版'})
        assert stale_update['public_result']['reason_code'] == 'REALTIME_PREPARED_CURRICULUM_CHANGED'
        stale_explain = propose(client, root, {**old, 'call_id': 'call-old-explain'}, 'learning.explain', {
            'subject': '化学', 'material_scope': 'prepared_material', 'question_source': 'spoken_request',
        })
        assert stale_explain['public_result']['reason_code'] == 'REALTIME_PREPARED_CURRICULUM_CHANGED'
        with db.connect() as c:
            assert current_request(c, CHILD, session_id) == original
        edition_text = '我用人教版九年级上册，就是第一课。'
        capture_time = int(datetime.now(timezone.utc).timestamp() * 1000)
        edition = native_voice(client, root, 'edition', edition_text, captured_ms=capture_time)
        assert native_voice(client, root, 'edition', edition_text, captured_ms=capture_time) == edition
        with db.connect() as c:
            clarified = current_request(c, CHILD, session_id)
            assert clarified['requested'] == {
                **original['requested'], 'textbook_edition': '人教版', 'textbook_volume': '上册',
            }
            assert clarified['prepare_turn_id'] == original['prepare_turn_id']
            assert clarified['prepare_voice_id'] == edition['voice_input_id']
            assert c.execute('SELECT count(*) FROM audit_event WHERE actor=?', (CLARIFICATION,)).fetchone()[0] == 1
        # An input with no capability proposal must not become this course's context.
        native_voice(client, root, 'interrupted', '我一会儿也想复习物理。')
        runtime = client.get(root + '/agent-runtime').json()
        pending = runtime['pending_learning_capture']
        assert pending is not None, runtime
        deeper = native_voice(client, root, 'deeper', '我想听得再深入一点。', pending['reference'])
        deeper_tutor = teach(client, root, deeper, claim_lesson(client, root, deeper, scope=None))
        assert deeper_tutor['turn_id'] != taught['turn_id']
        assert len(requests) == 2 and len(quality_requests) == 2
        for messages, question in zip(requests, ('请帮我学初三化学第一课。', '我想听得再深入一点。')):
            assert question in messages[-1]['content']
            assert all(word in messages[-1]['content'] for word in ('初三', '化学', '第一课', 'conceptual'))
            assert '物理' not in messages[-1]['content']
            assert '尚未核对该版教材原文' in messages[0]['content']
        assert '人教版' not in requests[0][-1]['content']
        assert all(word in requests[1][-1]['content'] for word in ('人教版', '上册'))
        excerpt = select_public_reference(db)
        late = native_voice(client, root, 'late', '再讲深一点。')
        title = native_voice(client, root, 'title', '这课的课名是物质与变化。')
        updated = propose(client, root, title, 'learning.prepare_request', {'lesson_title': '物质与变化'})
        assert updated['public_result']['requested']['textbook_edition'] == '人教版'
        stale = propose(client, root, late, 'learning.explain', {'subject': '化学', 'material_scope': 'prepared_material'})
        assert stale['not_admitted'] is True
        assert stale['public_result']['reason_code'] == 'REALTIME_PREPARED_CURRICULUM_CHANGED'
        exact = native_voice(client, root, 'verified', '就按这门课程再说明一下。')
        referenced = teach(client, root, exact, claim_lesson(client, root, exact))
        assert referenced['output_envelope']['curriculum_basis'] == 'public_course_reference'
        assert referenced['answer'].startswith('这次参考已核对范围的公开课程。')
        assert excerpt in requests[-1][-1]['content']
        assert '转录不是教材原文' in requests[-1][0]['content']
        route_reference = client.get(root + '/agent-runtime').json()['pending_learning_capture']['reference']
        _, topic_receipt = native_voice(client, root, 'previous-topic', topic_words, route_reference, with_receipt=True)
        topic_payload = {'subject': '化学', 'question': topic_words, 'hint_level': 6,
                         'client_action_id': topic_receipt['curriculum_tutor']['action_id']}
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending_tutor = pool.submit(client.post, root + '/tutor', json=topic_payload)
            try:
                assert topic_started.wait(3), 'previous_tutor_did_not_dispatch'
                with db.connect() as c:
                    assert c.execute('SELECT status FROM tutor_request_reservation WHERE action_id=?',
                                     (topic_payload['client_action_id'],)).fetchone()[0] == 'pending'
                route_time = int(datetime.now(timezone.utc).timestamp() * 1000)
                routed, receipt = native_voice(client, root, 'routed-continue', routed_words, route_reference,
                                              captured_ms=route_time, with_receipt=True)
                assert intents[-1]['previous_teaching'] == {'question': topic_words}
            finally:
                release_topic.set()
            assert pending_tutor.result(timeout=4).status_code == 409
        ticket = receipt['curriculum_tutor']
        assert ticket['version'] == 1 and ticket['subject'] == '化学' and ticket['action_id'].startswith('curriculum-tutor-')
        assert native_voice(client, root, 'routed-continue', routed_words, route_reference,
                            captured_ms=route_time, with_receipt=True)[1] == receipt
        assert sum(item['utterance'] == routed_words for item in intents) == 1
        tutor_payload = {'subject': ticket['subject'], 'question': routed_words, 'hint_level': 6, 'client_action_id': ticket['action_id']}
        wrong = client.post(root + '/tutor', json={**tutor_payload, 'question': '换一个问题。'})
        assert wrong.status_code == 409
        direct = client.post(root + '/tutor', json=tutor_payload)
        assert direct.status_code == 200, direct.text
        assert direct.json()['provider'] == 'deepseek' and direct.json()['output_envelope']['formative_only'] is True
        assert routed_words in requests[-1][-1]['content'] and excerpt in requests[-1][-1]['content']
        assert f'"previous_teaching": {{"question": "{topic_words}"}}' in requests[-1][-1]['content']
        assert client.post(root + '/tutor', json=tutor_payload).json()['turn_id'] == direct.json()['turn_id']
        with db.connect() as c:
            audit = json.loads(c.execute('SELECT summary_json FROM audit_event WHERE audit_id=?', (ticket['action_id'],)).fetchone()[0])
            assert audit['decision'] == 'continue_tutor' and audit['binding']['voice_input_id'] == routed['voice_input_id']
            assert audit['binding']['objective_id'] == original['objective_id']
            assert c.execute('SELECT count(*) FROM agent_turn WHERE client_action_id=?', (ticket['action_id'],)).fetchone()[0] == 0
        _, earlier_onset = native_voice(client, root, 'late-answer', '我先想一想。',
                                       captured_ms=route_time+1, with_receipt=True)
        assert 'curriculum_tutor' not in earlier_onset
        assert intents[-1]['previous_teaching'] == {'question': routed_words}
        _, uncertain_receipt = native_voice(client, root, 'invalid-route', uncertain_words, with_receipt=True)
        assert 'curriculum_tutor' not in uncertain_receipt
        assert sum(item['utterance'] == uncertain_words for item in intents) == 1
        assert intents[-1]['previous_teaching'] == {'question': routed_words, 'answer': direct.json()['answer']}
        assert client.post(root + '/tutor', json=tutor_payload).status_code == 409
        pending_voice = native_voice(client, root, 'withdrawn', '还想听一个例子。')
        pending_dispatch = claim_lesson(client, root, pending_voice)
        with db.connect() as c:
            c.execute("UPDATE curriculum_corpus SET review_status='quarantined' WHERE corpus_id='public-course'")
            c.commit()
        action = pending_dispatch['turn']['action']
        rejected = client.post(root + '/tutor', json={
            'subject': action['subject'], 'question': action['question'], 'hint_level': action['hint_level'],
            'client_action_id': action['tutor_request_action_id'],
        })
        assert rejected.status_code == 409 and rejected.json()['detail']['code'] == 'REALTIME_PREPARED_CURRICULUM_CHANGED'
        assert len(requests) == 5
        with db.connect() as c:
            ids = [request['agent_turn_id'], first_dispatch['turn']['agent_turn_id'], updated['agent_turn_id']]
            goals = c.execute('SELECT DISTINCT objective_id FROM agent_plan WHERE agent_turn_id IN (?,?,?)', ids).fetchall()
            assert len(goals) == 1
            assert c.execute('SELECT count(*) FROM tutor_request_reservation').fetchone()[0] == 5
            assert c.execute("SELECT count(*) FROM tutor_request_reservation WHERE status='completed'").fetchone()[0] == 4
            assert c.execute('SELECT count(*) FROM agent_outcome').fetchone()[0] == 0
        other = native_voice(client, root, 'standalone', '请独立解释同位素是什么。')
        independent = propose(client, root, other, 'learning.explain', {
            'subject': '化学', 'material_scope': 'standalone_concept', 'question_source': 'spoken_request',
        })
        assert independent['execution']['status'] == 'pending'
        with db.connect() as c:
            manifest = json.loads(c.execute('SELECT context_manifest_json FROM agent_plan WHERE agent_turn_id=?',
                                            (independent['agent_turn_id'],)).fetchone()[0])
            assert 'native_prepared_curriculum_v1' not in manifest
    assert not attempts
