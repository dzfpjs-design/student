import json

import pytest

from home_study_agent.local_supply import score_local_item
from home_study_agent import spoken_numeric as numeric
from test_specialized_closed_contract import context, generated


@pytest.mark.parametrize('answer', ['6', '６', '六', '答案是六', '我的答案是 六。', '正六'])
def test_integer_voice_equivalence(answer):
    assert numeric.parse(answer, 'integer') == 6


@pytest.mark.parametrize('answer', ['不是六', '六或七', '六加零', '六个', '可能是六', '六，七',
    '六十十', '百百', '二百三', '一万万', '1e6', '', '6.0', '6/1', '零点六', '六!!'])
def test_integer_ambiguity_and_unsupported_forms_need_clarification(answer):
    assert numeric.parse(answer, 'integer') is None


@pytest.mark.parametrize('answer,expected', [('二分之一', '1/2'), ('零点五', '1/2'), ('0.50', '1/2'),
    ('答案为负三分之二。', '-2/3'), ('-2/3', '-2/3'), ('两百零六', '206'), ('一千零一', '1001'),
    ('一万零六', '10006'), ('十二', '12'), ('一十二', '12'), ('一百一十', '110')])
def test_rational_whole_utterance(answer, expected):
    from fractions import Fraction
    assert numeric.parse(answer, 'rational') == Fraction(expected)


@pytest.mark.parametrize('answer', ['零分之一', '1/0', '1/2/3', '一分之二分之三', '1.2.3',
    '六或者七', '二点十', '50%', '一半', '6厘米', '999999999', '一亿', '负负六'])
def test_rational_invalid(answer):
    assert numeric.parse(answer, 'rational') is None


def numeric_generated(stage='parallel', index=0):
    item = generated(stage, index)
    item['prompt'] = item['prompt'].replace('只写整数。', '请说出整数答案。')
    item['scoring_contract'].update(schema=numeric.SCHEMA, numeric_kind='integer')
    return item


def test_typed_numeric_contract_scores_voice_without_mutating_answer(tmp_path):
    _, service, value, _ = context(tmp_path)
    candidate = service._validate(value, {'candidates': [numeric_generated()]})[0]
    item = {'expected_answer_json': json.dumps(candidate['expected_answer']), 'explanation': candidate['explanation']}
    for answer in ['6', '六', '答案是六']:
        assert score_local_item(item, answer)[0] == 1
    assert score_local_item(item, '七')[0] == 0
    assert score_local_item(item, '不是六') is None
    assert candidate['expected_answer']['answer_format'] == numeric.FORMAT


@pytest.mark.parametrize('kind,answers', [('unknown', ['6']), ('integer', ['0.5']), ('rational', ['1/0']),
    ('rational', ['0.5', '1/2']), ('rational', ['六']), ('integer', ['6个'])])
def test_bad_numeric_contract_is_not_reviewable(tmp_path, kind, answers):
    _, service, value, _ = context(tmp_path)
    item = numeric_generated()
    item['scoring_contract'].update(numeric_kind=kind)
    item['scoring_contract']['expected_answer']['exact_answers'] = answers
    assert service._validate(value, {'candidates': [item]}) == []


def test_legacy_text_contract_remains_representation_sensitive():
    item = {'expected_answer_json': json.dumps({'local_rule': 'local_exact_v1',
        'answer_format': 'reviewed_exact_text_v1', 'exact_answers': ['6']}), 'explanation': ''}
    assert score_local_item(item, '六')[0] == 0


def test_unclear_numeric_answer_never_falls_back_to_keyword_wrong_score():
    from home_study_agent.learning_state import LearningStateService
    item = {'answer_type': 'text', 'assessment_mode': 'objective', 'explanation': '',
        'expected_answer_json': json.dumps({'local_rule': 'local_exact_v1', 'answer_format': numeric.FORMAT,
                                           'numeric_kind': 'integer', 'exact_answers': ['6']})}
    assert LearningStateService._score_answer(item, '六或者七')[0] is None


@pytest.mark.parametrize('unclear', [False, True])
def test_numeric_voice_original_stage_recovery_and_revocation(tmp_path, monkeypatch, unclear):
    import test_specialized_closed_contract as closed
    from home_study_agent.database import Database
    from home_study_agent.diagnostic_prompt import DiagnosticPromptService
    from home_study_agent.repository import StudyRepository
    from home_study_agent.local_supply import LocalSupplyError, validate_item
    from home_study_agent.specialized_stages import revalidate_stage_action
    monkeypatch.setattr(closed, 'generated', numeric_generated)
    db, service, candidates = closed.model_candidates(tmp_path)
    items = closed.publish(service, candidates)
    sid = StudyRepository(db).start_session('child-primary-4', 'numeric-session')['session_id']
    prompts = DiagnosticPromptService(db)
    for index, label in enumerate(['parallel', 'discrimination', 'transfer']):
        action = closed.counting(db)['next_action']
        item_id = items[label]['diagnostic_item_id']
        assert action['diagnostic_item_id'] == item_id
        prepared = prompts.prepare(sid, track='competition_math', preferred_item_id=item_id,
                                   client_action_id='numeric-prepare-' + label)['prompt']
        answer = '六或者七' if unclear else ['六', '答案是六', '我的答案是六。'][index]
        kwargs = dict(prompt_id=prepared['prompt_id'], item_version=prepared['item_version'], answer=answer,
                      client_action_id='numeric-answer-' + label)
        receipt = prompts.answer(sid, **kwargs)['attempt']
        repeated = DiagnosticPromptService(Database(db.path)).answer(sid, **kwargs)['attempt']
        assert repeated['attempt_id'] == receipt['attempt_id']
        assert repeated['correctness'] == (None if unclear else 1)
        assert repeated['score_status'] == ('needs_review' if unclear else 'verified_local')
        with db.connect() as c:
            row = c.execute('SELECT * FROM diagnostic_attempt WHERE attempt_id=?', (receipt['attempt_id'],)).fetchone()
            assert answer in dict(row).values()
        if unclear:
            assert '只说一个数' in repeated['feedback']
            assert repeated['mastery_update_allowed'] is False
            assert closed.counting(db)['current_stage'] == 'baseline'
            break
    assert closed.counting(Database(db.path)) == closed.counting(db)
    with db.connect() as c:
        item = items['parallel']
        c.execute("UPDATE specialized_supply_candidate SET status='rejected' WHERE candidate_id=?", (item['candidate_id'],))
        c.commit()
    with Database(db.path).connect() as c:
        with pytest.raises(LocalSupplyError):
            validate_item(c, 'child-primary-4', item['diagnostic_item_id'])
        assert revalidate_stage_action(c, 'child-primary-4', 'competition_math', binding=action)['status'] == 'blocked'
        assert c.execute('SELECT count(*) FROM diagnostic_attempt WHERE session_id=?', (sid,)).fetchone()[0] == (1 if unclear else 3)
        if unclear:
            from home_study_agent.learning_state import LearningStateService
            assert '只说一个数' not in LearningStateService._attempt(c, receipt['attempt_id'])['feedback']


def test_numeric_scoring_kind_cannot_change_under_original_parent_review(tmp_path, monkeypatch):
    import test_specialized_closed_contract as closed
    from home_study_agent.local_supply import LocalSupplyError, validate_item
    monkeypatch.setattr(closed, 'generated', numeric_generated)
    db, service, candidates = closed.model_candidates(tmp_path)
    published = closed.publish(service, candidates)['parallel']
    with db.connect() as c:
        row = dict(c.execute('SELECT * FROM specialized_supply_candidate WHERE candidate_id=?',
                             (published['candidate_id'],)).fetchone())
        rubric, validation = json.loads(row['expected_answer_json']), json.loads(row['validation_json'])
        rubric['numeric_kind'] = 'rational'
        validation['closed_contract']['numeric_kind'] = 'rational'
        c.execute('UPDATE specialized_supply_candidate SET expected_answer_json=?,validation_json=? WHERE candidate_id=?',
                  (json.dumps(rubric), json.dumps(validation), row['candidate_id']))
        c.execute('UPDATE diagnostic_item SET expected_answer_json=? WHERE diagnostic_item_id=?',
                  (json.dumps(rubric), row['diagnostic_item_id']))
        with pytest.raises(LocalSupplyError, match='CONTENT_CHANGED'):
            validate_item(c, 'child-primary-4', row['diagnostic_item_id'])
        c.rollback()
        assert validate_item(c, 'child-primary-4', row['diagnostic_item_id'])
