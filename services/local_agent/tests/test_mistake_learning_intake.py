from uuid import uuid4

import pytest

from home_study_agent.database import Database
from home_study_agent.repository import StudyRepository
from home_study_agent.mistake_learning_intake import (
    MistakeIntakeError, MistakeLearningIntake, list_intake_cards, read_intake_card,
)


@pytest.fixture
def intake(tmp_path):
    db = Database(tmp_path / 'intake.db')
    db.initialize()
    repo = StudyRepository(db)
    repo.seed_family()
    sid = repo.start_session('child-primary-4', 'public-intake-session')['session_id']
    return db, sid, MistakeLearningIntake(db)


def test_question_without_graph_or_scoring_source_is_saved_and_resumes(intake):
    db, sid, service = intake
    with db.connect() as c:
        before = {t: c.execute('SELECT count(*) FROM '+t).fetchone()[0]
                  for t in ('knowledge_node', 'learning_evidence', 'mistake_case')}
    request = dict(child_id='child-primary-4', session_id=sid,
                   question='计算3/4+1/2，我把分子分母分别相加。', submitted_answer='4/6',
                   client_action_id=str(uuid4()), source_type='spoken')
    saved = service.create(**request)
    assert saved['prompt_summary'] == request['question']
    assert saved['submitted_answer'] == '4/6'
    assert service.create(**request)['mistake_id'] == saved['mistake_id']
    reloaded = MistakeLearningIntake(Database(db.path)).create(**request)
    assert reloaded == saved
    with db.connect() as c:
        assert len(list_intake_cards(c, 'child-primary-4')) == 1
        assert {t: c.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in before} == before
        assert c.execute('SELECT count(*) FROM mistake_learning_card_revision').fetchone()[0] == 1


def test_same_question_keeps_children_and_action_payloads_separate(intake):
    db, sid, service = intake
    key = str(uuid4())
    a = service.create(child_id='child-primary-4', question='地球昼夜交替的主要原因是什么？',
                       submitted_answer='地球公转', client_action_id=key)
    b = service.create(child_id='child-junior-3', question='地球昼夜交替的主要原因是什么？',
                       submitted_answer='地球公转', client_action_id=str(uuid4()))
    assert a['mistake_id'] != b['mistake_id']
    with db.connect() as c:
        assert read_intake_card(c, 'child-junior-3', a['mistake_id']) is None
    with pytest.raises(MistakeIntakeError, match='ACTION_CONFLICT'):
        service.create(child_id='child-primary-4', question='另一道题', client_action_id=key)
    with pytest.raises(MistakeIntakeError, match='SESSION_CHANGED'):
        service.create(child_id='child-junior-3', session_id=sid, question='另一道题', client_action_id=str(uuid4()))


def test_source_guard_revocation_rolls_back_whole_new_card(intake):
    db, sid, service = intake
    calls = []
    def guard(c):
        calls.append(True)
        return len(calls) == 1
    with pytest.raises(MistakeIntakeError, match='OWNER_CHANGED'):
        service.create(child_id='child-primary-4', session_id=sid, question='计算6×7。',
                       client_action_id=str(uuid4()), guard=guard)
    with db.connect() as c:
        for table in ('mistake_learning_card', 'mistake_learning_card_revision', 'mistake_learning_intake_action'):
            assert c.execute('SELECT count(*) FROM '+table).fetchone()[0] == 0


def test_duplicate_question_does_not_create_a_second_card_and_missing_answer_stays_unknown(intake):
    db, _, service = intake
    first = service.create(child_id='child-primary-4', question='请解释地球自转。', client_action_id=str(uuid4()))
    second = service.create(child_id='child-primary-4', question='请解释地球自转。', client_action_id=str(uuid4()))
    assert first['mistake_id'] == second['mistake_id']
    assert first['submitted_answer'] == ''
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM mistake_learning_card').fetchone()[0] == 1
