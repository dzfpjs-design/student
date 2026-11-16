"""Model-proposed question/recorded-answer corrections require joint readback."""
import json
import os

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.mistake_book import MistakeBookService
from home_study_agent.mistake_dialogue import MistakeDialogueService
from home_study_agent.mistake_source_revision import current
from test_cloud_first_agent import Planner
from test_mistake_source_revision_http import Gateway
from test_mistake_recapture_http import setup
from test_formula_voice_confirmation import turn
from test_paper_variant_line_projection_http import captured_question, QUESTION, ANSWER, post
from test_paper_variant_authorization_http import CHILD


@pytest.mark.parametrize('decision', ['joint', 'question_only', 'cancel', 'invented', 'changed_source'])
def test_joint_readback_preserves_original_until_confirmation(captured_question, decision):
    db, settings, sid, _, mid, _ = captured_question
    replacement = QUESTION.replace('三十七', '四十一')
    answer = '我原来写的是二百零八'
    utterance = '你把题干和我的原作答都看错了，题目完整内容：' + replacement + '；我的原作答：' + answer
    args = {'question_quote': replacement, 'original_answer_quote': answer if decision != 'invented' else '无依据的答案'}
    planner = Planner('mistake.repair_question', args, utterance)
    with TestClient(create_app(settings, model_gateway=planner)) as client:
        setup(client, captured_question)
        book = MistakeBookService(db)
        initial = book.propose_diagnosis_hypotheses(mid, CHILD, [{'error_type': 'unknown',
            'stance': 'unknown', 'explanation': 'synthetic original-answer uncertainty', 'confidence': .4}],
            0, 'before-joint-repair')
        requested = post(client, f'/v1/sessions/{sid}/agent-turn', {
            'transcript': utterance, 'client_action_id': 'joint-repair-offer', 'cloud_text_allowed': True})
        assert requested['runtime_capability'] == 'mistake.repair_question'
        if decision == 'changed_source':
            with db.connect() as c:
                c.execute("UPDATE mistake_case SET submitted_answer='intervening correction' WHERE mistake_id=?", (mid,))
                c.commit()
        offered = client.post(f"/v1/agent-turns/{requested['agent_turn_id']}/execute", json={})
        if decision in {'invented', 'changed_source'}:
            assert offered.status_code in {409, 422}, offered.text
            with db.connect() as c:
                assert c.execute('SELECT count(*) FROM mistake_source_revision').fetchone()[0] == 0
            return
        assert offered.status_code == 200, offered.text
        pending = offered.json()['execution']['result']['state']['question_confirmation']
        assert pending['question'] == replacement and pending['replacement_answer'] == answer
        readback = offered.json()['turn']['response_text']
        assert replacement in readback and answer in readback and '两项' in readback
        with db.connect() as c:
            assert tuple(c.execute('SELECT prompt_summary,submitted_answer FROM mistake_case WHERE mistake_id=?', (mid,)).fetchone()) == (QUESTION, ANSWER)
    gateway = Gateway(settings)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        text = {'joint': '题干和原答案两项都对，讲吧', 'question_only': '题干正确', 'cancel': '取消这次更正'}[decision]
        result = turn(client, sid, text, 'joint-repair-decision', gateway=gateway)
        with db.connect() as c:
            row = c.execute('SELECT prompt_summary,submitted_answer FROM mistake_case WHERE mistake_id=?', (mid,)).fetchone()
            if decision != 'joint':
                assert tuple(row) == (QUESTION, ANSWER)
                assert c.execute('SELECT count(*) FROM mistake_source_revision').fetchone()[0] == 0
                assert not result['execution'].get('tutor_turn')
            else:
                assert tuple(row) == (replacement, answer)
                revision = c.execute('SELECT * FROM mistake_source_revision').fetchone()
                assert json.loads(revision['before_json'])['case']['submitted_answer'] == ANSWER
                assert json.loads(revision['offer_json'])['replacement_answer'] == answer
                assert current(c, mid, CHILD)['score_authority'] is False
        if decision == 'joint':
            assert result['execution']['tutor_turn']['question'] == replacement
            assert answer not in '\n'.join(gateway.posts)
            diagnosis = MistakeBookService(db).diagnosis_state(mid, CHILD)
            assert diagnosis['history'] == initial['history']
            assert not any(h['original_current'] for h in diagnosis['hypotheses'])
            assert diagnosis['assessment']['support'] == []
            assert turn(client, sid, text, 'joint-repair-decision', gateway=gateway) == result
            assert MistakeDialogueService(db).state(sid)['state']['submitted_answer'] == answer


@pytest.mark.parametrize('answer_only', [False, True])
def test_joint_revision_rolls_back_both_fields_and_retries(captured_question, monkeypatch, answer_only):
    db, settings, sid, _, mid, _ = captured_question
    replacement, answer = QUESTION if answer_only else QUESTION.replace('三十七', '四十一'), '二百零八'
    utterance = '题目内容：' + replacement + '。我原来的作答：' + answer
    arguments = {'original_answer_quote': answer} if answer_only else {'question_quote': replacement, 'original_answer_quote': answer}
    planner = Planner('mistake.repair_question', arguments, utterance)
    with TestClient(create_app(settings, model_gateway=planner)) as client:
        setup(client, captured_question)
        request = post(client, f'/v1/sessions/{sid}/agent-turn', {
            'transcript': utterance, 'client_action_id': 'joint-rollback-offer', 'cloud_text_allowed': True})
        post(client, f"/v1/agent-turns/{request['agent_turn_id']}/execute", {})
    gateway = Gateway(settings)
    with TestClient(create_app(settings, model_gateway=gateway), raise_server_exceptions=False) as client:
        confirmation = turn(client, sid, '两项都对', 'joint-rollback-confirm', execute=False)
        endpoint = f"/v1/agent-turns/{confirmation['agent_turn_id']}/execute"
        underlying = MistakeDialogueService._save
        def fail(*args, **kwargs):
            underlying(*args, **kwargs)
            raise OSError('synthetic joint revision write failure')
        with monkeypatch.context() as patch:
            patch.setattr(MistakeDialogueService, '_save', fail)
            assert client.post(endpoint, json={}).status_code == 500
        with db.connect() as c:
            assert tuple(c.execute('SELECT prompt_summary,submitted_answer FROM mistake_case WHERE mistake_id=?', (mid,)).fetchone()) == (QUESTION, ANSWER)
            assert c.execute('SELECT count(*) FROM mistake_source_revision').fetchone()[0] == 0
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        gateway.configured = True
        result = post(client, endpoint, {})
        assert result['execution'].get('tutor_turn')
        assert post(client, endpoint, {}) == result
        with db.connect() as c:
            assert tuple(c.execute('SELECT prompt_summary,submitted_answer FROM mistake_case WHERE mistake_id=?', (mid,)).fetchone()) == (replacement, answer)
            assert c.execute('SELECT count(*) FROM mistake_source_revision').fetchone()[0] == 1


@pytest.mark.skipif(os.getenv('HSC_RUN_JOINT_REVISION_LIVE') != '1', reason='explicit paid synthetic model probe only')
def test_official_model_extracts_joint_correction_once(captured_question):
    from dataclasses import replace
    from pathlib import Path
    import subprocess
    import time
    import httpx
    from home_study_agent.model_gateway import ModelGateway
    db, settings, sid, _, mid, _ = captured_question
    credential = subprocess.run(['security', 'find-generic-password', '-s', 'hsc-deepseek-api-key', '-w'],
                                capture_output=True, text=True, timeout=15)
    assert credential.returncode == 0, 'authorized model credential unavailable'
    configured = replace(settings, model_provider='deepseek', model_api_key=credential.stdout.strip(),
        model_base_url='https://api.deepseek.com/v1', model_name='deepseek-v4-pro',
        voice_intent_model_name='deepseek-v4-pro', model_timeout_seconds=180,
        fallback_model_provider=None, fallback_model_api_key=None)
    metrics = []
    async def transport(request):
        assert request.url.host == 'api.deepseek.com'
        assert not metrics, 'one paid call maximum; no retry or secondary classifier'
        assert mid not in request.content.decode()
        entry = {'requested_model': json.loads(request.content)['model']}
        metrics.append(entry)
        start = time.monotonic()
        async with httpx.AsyncClient(timeout=180, trust_env=False) as client:
            response = await client.send(request)
        entry.update(http_status=response.status_code, elapsed_seconds=round(time.monotonic() - start, 3))
        data = response.json()
        entry.update(actual_model=data.get('model'), usage=data.get('usage'))
        return response
    gateway = ModelGateway(configured, transport=httpx.MockTransport(transport))
    replacement = QUESTION.replace('三十七', '四十一')
    answer = '我写的是二百零八'
    utterance = '别讲新题，刚才识别的题干和原作答都不对。完整题干是：' + replacement + '。我原来的作答是：' + answer
    with TestClient(create_app(settings, model_gateway=Gateway(settings))) as preparation:
        setup(preparation, captured_question)
    try:
        with TestClient(create_app(configured, model_gateway=gateway)) as client:
            planned = post(client, f'/v1/sessions/{sid}/agent-turn', {
                'transcript': utterance, 'client_action_id': 'live-joint-correction', 'cloud_text_allowed': True})
            assert planned['runtime_capability'] == 'mistake.repair_question', planned.get('runtime_capability')
            offered = post(client, f"/v1/agent-turns/{planned['agent_turn_id']}/execute", {})
            pending = offered['execution']['result']['state']['question_confirmation']
            assert pending['question'] == replacement
            assert pending['replacement_answer'] in {answer, '二百零八'}
            assert len(metrics) == 1
            with db.connect() as c:
                assert c.execute('SELECT submitted_answer FROM mistake_case WHERE mistake_id=?', (mid,)).fetchone()[0] == ANSWER
    finally:
        Path('/tmp/hsc-live-joint-revision-metrics.json').write_text(json.dumps(metrics, ensure_ascii=False, indent=2))
