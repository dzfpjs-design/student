"""Adversarial audit of whole speech, task constraints and actual evidence receipts."""
import json
from fractions import Fraction

import pytest
from fastapi.testclient import TestClient

from home_study_agent import spoken_numeric
from home_study_agent.app import create_app
from test_diagnostic_prompt_http import environment, start, endpoint, binding
import test_specialized_closed_contract as closed
from test_specialized_closed_http import generated_http, CHILD
from test_spoken_numeric import numeric_generated


@pytest.mark.parametrize('utterance', ['两三', '三五', '一二三', '答案是两三', '二三分之一', '6!'])
def test_ambiguous_digit_sequence_is_not_an_unstated_digit_reading_contract(utterance):
    assert spoken_numeric.parse(utterance, 'rational') is None


@pytest.mark.parametrize('prompt,kind,reference', [
    ('请把二分之一写成小数，只提交一个小数答案。', 'rational', '0.5'),
    ('请把零点五化为最简分数并写出最简分数。', 'rational', '1/2'),
    ('请算出半米等于多少厘米，答案必须带上厘米单位。', 'integer', '50'),
    ('请把三分之一按要求保留两位小数，提交近似值。', 'rational', '0.33'),
])
def test_representation_or_unit_required_task_is_not_a_plain_numeric_equivalence_task(tmp_path, prompt, kind, reference):
    _, service, context, _ = closed.context(tmp_path)
    candidate = numeric_generated()
    candidate['prompt'] = prompt
    candidate['scoring_contract']['numeric_kind'] = kind
    candidate['scoring_contract']['expected_answer']['exact_answers'] = [reference]
    assert service._validate(context, {'candidates': [candidate]}) == []


@pytest.mark.parametrize('prompt,reference', [
    ('请用二进制写出十进制六，提交二进制表示。', '110'),
    ('将数字六写成三位编号，前面补零。', '006'),
    ('请用大写中文数字表示六，提交规范中文写法。', '6'),
    ('写出第四个罗马数字作为答案。', '4'),
    ('请按从小到大的顺序连写一和二，提交两位序列。', '12'),
])
def test_representation_sensitive_integer_is_not_a_scalar_integer_contract(tmp_path, prompt, reference):
    _, service, context, _ = closed.context(tmp_path)
    candidate = numeric_generated()
    candidate['prompt'] = prompt
    candidate['scoring_contract']['expected_answer']['exact_answers'] = [reference]
    assert service._validate(context, {'candidates': [candidate]}) == []


@pytest.mark.parametrize('prompt,kind,reference,answer', [
    ('二十本书再放入三本，一共有多少本书？请说出整数答案。', 'integer', '23', '二十三'),
    ('比最大的两位数多一的数是多少？请说出整数答案。', 'integer', '100', '一百'),
    ('一与二相除的结果是多少？请说出一个数值。', 'rational', '1/2', '零点五'),
])
def test_scalar_neighbors_remain_typed_and_equivalent(tmp_path, prompt, kind, reference, answer):
    _, service, context, _ = closed.context(tmp_path)
    candidate = numeric_generated()
    candidate['prompt'] = prompt
    candidate['scoring_contract'].update(numeric_kind=kind)
    candidate['scoring_contract']['expected_answer']['exact_answers'] = [reference]
    rows = service._validate(context, {'candidates': [candidate]})
    assert len(rows) == 1
    assert spoken_numeric.score(rows[0]['expected_answer'], answer) == 1


def test_representation_text_contract_is_not_silently_numeric(tmp_path):
    from home_study_agent.local_supply import score_local_item
    _, service, context, _ = closed.context(tmp_path)
    candidate = closed.generated()
    candidate['prompt'] = '将数字六写成三位编号，前面补零。'
    candidate['scoring_contract']['expected_answer']['exact_answers'] = ['006']
    rows = service._validate(context, {'candidates': [candidate]})
    assert len(rows) == 1
    rubric = rows[0]['expected_answer']
    assert rubric['answer_format'] == 'reviewed_exact_text_v1'
    item = {'expected_answer_json': json.dumps(rubric), 'explanation': ''}
    assert score_local_item(item, '006')[0] == 1
    assert score_local_item(item, '六')[0] == 0


def books(stage='parallel', index=0):
    item = numeric_generated(stage, index)
    if stage == 'parallel':
        item['prompt'] = '书架原有二十本书，再放入三本书，共有多少本书？请说出整数答案。'
        item['scoring_contract']['expected_answer']['exact_answers'] = ['23']
        item['explanation'] = '二十加三等于二十三。'
    return item


def test_actual_http_ambiguous_original_never_becomes_strong_child_evidence(environment, monkeypatch):
    settings, db = environment
    monkeypatch.setattr(closed, 'generated', books)
    gateway = closed.ClosedGateway()
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        headers, rows = generated_http(client, db)
        candidate = next(r for r in rows if r['stage'] == 'parallel')
        published = client.post(f"/v1/parent/specialized-supply/candidates/{candidate['candidate_id']}/review", headers=headers,
            json={'decision': 'approve', 'note': 'Synthetic human verified 20+3 and single cardinal answer', 'client_action_id': 'audit30-parent-review'})
        assert published.status_code == 200, published.text
        item_id = published.json()['diagnostic_item_id']
        sid = start(client)
        prepared = client.post(endpoint(sid, 'prepare'), json={'track': 'competition_math', 'preferred_item_id': item_id,
            'client_action_id': 'audit30-prepare'})
        assert prepared.status_code == 200, prepared.text
        request = {**binding(prepared.json()['prompt']), 'answer': '答案是两三', 'client_action_id': 'audit30-original-voice'}
        response = client.post(endpoint(sid, 'answer'), json=request)
        assert response.status_code == 200, response.text
        result = response.json()['attempt']
        print({'http': response.status_code, 'original': result['submitted_answer'], 'correctness': result['correctness'],
               'score_status': result['score_status'], 'mastery_update_allowed': result['mastery_update_allowed']})
        assert result['submitted_answer'] == request['answer']
        assert result['correctness'] is None and result['mastery_update_allowed'] is False
        assert result['score_status'] == 'needs_review'
        replay = client.post(endpoint(sid, 'answer'), json=request)
        assert replay.status_code == 200 and replay.json()['attempt']['attempt_id'] == result['attempt_id']
        assert closed.counting(db)['current_stage'] == 'baseline'
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        replay = client.post(endpoint(sid, 'answer'), json=request)
        assert replay.status_code == 200 and replay.json()['attempt']['correctness'] is None
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt WHERE evidence_id=?', (result['evidence_id'],)).fetchone()[0] == 1
            from home_study_agent.learning_feedback import build_learning_feedback
            feedback = build_learning_feedback(c, CHILD)
            assert not any(result['evidence_id'] in json.dumps(node) for node in feedback['nodes'])
        from home_study_agent.child_model import ChildModelService
        model = ChildModelService(db).snapshot(CHILD)['payload']['learning']['reliable_feedback']
        assert not any(result['evidence_id'] in json.dumps(node) for node in model['nodes'])
    assert len(gateway.requests) == 1


def test_old_wrong_parser_receipt_is_retained_but_loses_current_authority(tmp_path, monkeypatch):
    from home_study_agent.diagnostic_prompt import DiagnosticPromptService
    from home_study_agent.repository import StudyRepository
    from home_study_agent.learning_state import LearningStateService
    from home_study_agent.child_model import ChildModelService
    monkeypatch.setattr(closed, 'generated', books)
    db, service, rows = closed.model_candidates(tmp_path)
    item = closed.publish(service, rows)['parallel']
    sid = StudyRepository(db).start_session(CHILD, 'legacy-audit30')['session_id']
    prompts = DiagnosticPromptService(db)
    prompt = prompts.prepare(sid, track='competition_math', preferred_item_id=item['diagnostic_item_id'], client_action_id='legacy-prepare30')['prompt']
    original = spoken_numeric.parse
    # Reproduce an immutable receipt made by the previous production parser.
    with monkeypatch.context() as old:
        old.setattr(spoken_numeric, 'parse', lambda answer, kind: Fraction(23) if answer == '两三' else original(answer, kind))
        result = prompts.answer(sid, prompt_id=prompt['prompt_id'], item_version=prompt['item_version'], answer='两三', client_action_id='legacy-answer30')['attempt']
        assert result['correctness'] == 1 and result['mastery_update_allowed']
    with db.connect() as c:
        current = LearningStateService._attempt(c, result['attempt_id'])
        assert current['correctness'] == 1  # Original journal is not rewritten.
        assert current['mastery_update_allowed'] is False and current['score_status'] == 'needs_review'
        assert c.execute('SELECT submitted_answer FROM diagnostic_attempt WHERE attempt_id=?', (result['attempt_id'],)).fetchone()[0] == '两三'
    model = ChildModelService(db).snapshot(CHILD)['payload']['learning']['reliable_feedback']
    assert not any(result['evidence_id'] in json.dumps(node) for node in model['nodes'])
    assert closed.counting(db)['qualified_stage'] is None


@pytest.mark.parametrize('change', ['published_reference', 'candidate_reference', 'kind', 'answer_type', 'mode', 'source', 'withdrawal'])
def test_old_review_never_qualifies_changed_numeric_sources(tmp_path, monkeypatch, change):
    from home_study_agent.local_supply import validate_item, LocalSupplyError
    monkeypatch.setattr(closed, 'generated', books)
    db, service, rows = closed.model_candidates(tmp_path)
    item = closed.publish(service, rows)['parallel']
    with db.connect() as c:
        before = validate_item(c, CHILD, item['diagnostic_item_id'])
        if change in {'published_reference', 'candidate_reference', 'kind'}:
            rubric = json.loads(before['expected_answer_json'])
            rubric.update(numeric_kind='rational') if change == 'kind' else rubric.update(exact_answers=['24'])
            c.execute('UPDATE diagnostic_item SET expected_answer_json=? WHERE diagnostic_item_id=?', (json.dumps(rubric), item['diagnostic_item_id']))
            if change != 'published_reference':
                c.execute('UPDATE specialized_supply_candidate SET expected_answer_json=? WHERE candidate_id=?', (json.dumps(rubric), item['candidate_id']))
        elif change == 'answer_type':
            c.execute("UPDATE diagnostic_item SET answer_type='open_response' WHERE diagnostic_item_id=?", (item['diagnostic_item_id'],))
        elif change == 'mode':
            c.execute("UPDATE diagnostic_item SET assessment_mode='pet_reference_rubric' WHERE diagnostic_item_id=?", (item['diagnostic_item_id'],))
        elif change == 'source':
            c.execute("UPDATE diagnostic_item SET prompt=prompt || ' Changed original.' WHERE diagnostic_item_id=?", (item['seed_item_id'],))
        else:
            c.execute("UPDATE specialized_supply_candidate SET status='rejected' WHERE candidate_id=?", (item['candidate_id'],))
        with pytest.raises(LocalSupplyError):
            validate_item(c, CHILD, item['diagnostic_item_id'])
        c.rollback()
        assert validate_item(c, CHILD, item['diagnostic_item_id']) == before
        with pytest.raises(LocalSupplyError):
            validate_item(c, 'child-junior-3', item['diagnostic_item_id'])


@pytest.mark.parametrize('task,kind,reference', [
    ('请把二分之一写成小数，只提交一个小数答案。', 'rational', '0.5'),
    ('请用二进制写出十进制六，提交二进制表示。', 'integer', '110'),
    ('将数字六写成三位编号，前面补零。', 'integer', '006'),
    ('请用大写中文数字表示六，提交规范中文写法。', 'integer', '6'),
    ('写出第四个罗马数字作为答案。', 'integer', '4'),
    ('请按从小到大的顺序连写一和二，提交两位序列。', 'integer', '12'),
])
def test_preexisting_representation_contract_is_not_grandfathered_after_upgrade(tmp_path, monkeypatch, task, kind, reference):
    from home_study_agent.local_supply import validate_item, LocalSupplyError
    def constrained(stage='parallel', index=0):
        item = numeric_generated(stage, index)
        if stage == 'parallel':
            item['prompt'] = task
            item['scoring_contract'].update(numeric_kind=kind)
            item['scoring_contract']['expected_answer']['exact_answers'] = [reference]
        return item
    monkeypatch.setattr(closed, 'generated', constrained)
    with monkeypatch.context() as old:
        old.setattr(spoken_numeric, 'task_supported', lambda *_: True)
        db, service, rows = closed.model_candidates(tmp_path)
        item = closed.publish(service, rows)['parallel']
    with db.connect() as c:
        with pytest.raises(LocalSupplyError, match='NUMERIC_CONTRACT_INVALID'):
            validate_item(c, CHILD, item['diagnostic_item_id'])
        saved = c.execute('SELECT status,review_action_id FROM specialized_supply_candidate WHERE candidate_id=?', (item['candidate_id'],)).fetchone()
        assert saved['status'] == 'promoted' and saved['review_action_id']  # Retain human event.
    assert closed.counting(db)['qualified_stage'] is None


from test_mistake_gate_reliability import flow


def test_real_mistake_submission_keeps_ambiguous_original_out_of_child_model(flow, monkeypatch):
    from home_study_agent import learning_state
    from home_study_agent.child_model import ChildModelService
    from test_mistake_gate_reliability import prepare, NODE
    db, learning, _, _ = flow
    item_id = 'audit30-original-books'
    rubric = {'local_rule': 'local_exact_v1', 'answer_format': spoken_numeric.FORMAT, 'numeric_kind': 'integer', 'exact_answers': ['23']}
    # A reviewed exact original fixture in the normal catalog, not a source-gate bypass.
    monkeypatch.setattr(learning_state, 'ITEMS', (*learning_state.ITEMS,
        (item_id, NODE, '二十本书再放入三本，共有多少本？请说出整数答案。', 'text', rubric, '二十加三等于二十三。', 2)))
    learning.seed_catalog()
    book, case = prepare(flow)
    result = book.submit_attempt(case, CHILD, 'near_transfer', item_id, '两三', 0, 'mistake-original30')
    assert result['attempt_result']['correctness'] is None
    assert result['attempt_result']['score_status'] == 'needs_review' and not result['attempt_result']['independent']
    assert result['status'] != 'mastered'
    book.submit_attempt(case, CHILD, 'near_transfer', item_id, '两三', 0, 'mistake-original30')
    with db.connect() as c:
        rows = c.execute('SELECT * FROM mistake_attempt WHERE client_action_id=?', ('mistake-original30',)).fetchall()
        assert len(rows) == 1 and rows[0]['submitted_answer'] == '两三'
        evidence = rows[0]['evidence_id']
    model = ChildModelService(db).snapshot(CHILD)['payload']['learning']['reliable_feedback']
    assert not any(evidence in json.dumps(node) for node in model['nodes'])


@pytest.mark.parametrize('transcript', ['答案是两三', '答案是6!'])
def test_original_agent_speech_cannot_gain_a_numeric_score_by_lossy_extraction(environment, monkeypatch, transcript):
    settings, db = environment
    monkeypatch.setattr(closed, 'generated', books if transcript.endswith('两三') else numeric_generated)
    gateway = closed.ClosedGateway()
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        headers, rows = generated_http(client, db)
        row = next(r for r in rows if r['stage'] == 'parallel')
        approved = client.post(f"/v1/parent/specialized-supply/candidates/{row['candidate_id']}/review", headers=headers,
            json={'decision': 'approve', 'note': 'Synthetic reviewed scalar task', 'client_action_id': 'agent-parent30'}).json()
        sid = start(client)
        prompt = client.post(endpoint(sid, 'prepare'), json={'track': 'competition_math',
            'preferred_item_id': approved['diagnostic_item_id'], 'client_action_id': 'agent-prepare30'}).json()['prompt']
        turn = client.post(f'/v1/sessions/{sid}/agent-turn', json={'transcript': transcript,
            'client_action_id': 'agent-original30', 'cloud_text_allowed': True})
        assert turn.status_code == 200, turn.text
        action = turn.json()['action']
        with db.connect() as c:
            original = dict(c.execute('SELECT * FROM agent_turn WHERE agent_turn_id=?', (turn.json()['agent_turn_id'],)).fetchone())
            assert transcript in original.values()
        if action['type'] == 'submit_diagnostic_answer':
            assert action['diagnostic_prompt_id'] == prompt['prompt_id']
            response = client.post(endpoint(sid, 'answer'), json={**binding(prompt), 'answer': action['answer'],
                'client_action_id': 'agent-submit30', 'authorized_turn_id': turn.json()['agent_turn_id']})
            assert response.status_code == 200, response.text
            receipt = response.json()['attempt']
            print({'original': transcript, 'extracted': action['answer'], 'score': receipt['correctness'],
                   'strong': receipt['mastery_update_allowed']})
            assert receipt['correctness'] is None and not receipt['mastery_update_allowed']
    assert len(gateway.requests) == 1


def test_legacy_lossy_agent_receipt_loses_current_authority_without_erasure(environment, monkeypatch):
    from home_study_agent.agent_runtime import AgentRuntimeStore
    from home_study_agent.learning_state import LearningStateService
    from home_study_agent.child_model import ChildModelService
    settings, db = environment
    monkeypatch.setattr(closed, 'generated', numeric_generated)
    gateway = closed.ClosedGateway()
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        headers, rows = generated_http(client, db)
        row = next(r for r in rows if r['stage'] == 'parallel')
        approved = client.post(f"/v1/parent/specialized-supply/candidates/{row['candidate_id']}/review", headers=headers,
            json={'decision': 'approve', 'note': 'Synthetic reviewed scalar task', 'client_action_id': 'legacy-agent-review30'}).json()
        sid = start(client)
        prompt = client.post(endpoint(sid, 'prepare'), json={'track': 'competition_math',
            'preferred_item_id': approved['diagnostic_item_id'], 'client_action_id': 'legacy-agent-prepare30'}).json()['prompt']
        # The old local normalizer stripped factorial punctuation. Retain its real receipt.
        with monkeypatch.context() as old:
            original = AgentRuntimeStore.local_answer_forms
            old.setattr(AgentRuntimeStore, 'local_answer_forms', staticmethod(
                lambda text, capability: (text, '6') if text == '答案是6!' else original(text, capability)))
            turn = client.post(f'/v1/sessions/{sid}/agent-turn', json={'transcript': '答案是6!',
                'client_action_id': 'legacy-agent-turn30', 'cloud_text_allowed': True}).json()
            assert turn['action']['answer'] == '6'
            response = client.post(endpoint(sid, 'answer'), json={**binding(prompt), 'answer': '6',
                'client_action_id': 'legacy-agent-answer30', 'authorized_turn_id': turn['agent_turn_id']})
            assert response.status_code == 200, response.text
            receipt = response.json()['attempt']
            assert receipt['correctness'] == 1 and receipt['mastery_update_allowed']
    with TestClient(create_app(settings, model_gateway=gateway)):
        with db.connect() as c:
            current = LearningStateService._attempt(c, receipt['attempt_id'])
            assert current['correctness'] == 1 and current['submitted_answer'] == '6'
            assert current['score_status'] == 'needs_review' and not current['mastery_update_allowed']
            assert '答案是6!' in dict(c.execute('SELECT * FROM agent_turn WHERE agent_turn_id=?', (turn['agent_turn_id'],)).fetchone()).values()
        model = ChildModelService(db).snapshot(CHILD)['payload']['learning']['reliable_feedback']
        assert not any(receipt['evidence_id'] in json.dumps(node) for node in model['nodes'])
        assert closed.counting(db)['qualified_stage'] is None
