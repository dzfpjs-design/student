"""Practice inventory is not a simulated child answer or a scoring receipt."""
from datetime import datetime, timezone
import hashlib

import pytest

from home_study_agent.database import Database
from home_study_agent.goal_roadmap import _has_qualified_question
from home_study_agent.learning_obligations import qualified_exact_score
from test_pet_reviewed_source_publication import prepared as listening, publish, CHILD
from test_writing_source_publication import prepared as writing
from test_goal_roadmap_execution import execution, goal_db, exact_goal_item, CHILD as JUNIOR


def digest(c):
    return hashlib.sha256('\n'.join(c.iterdump()).encode()).hexdigest()


@pytest.mark.parametrize('prepare', [listening, writing], ids=['listening', 'writing'])
def test_reviewed_practice_available_without_fabricating_child_score(tmp_path, prepare, monkeypatch):
    db, service, spec, packet = prepare(tmp_path)
    published = publish(service, spec)
    item_id = published['diagnostic_item_id']
    with db.connect() as c:
        node = c.execute('SELECT knowledge_node_id FROM diagnostic_item WHERE diagnostic_item_id=?', (item_id,)).fetchone()[0]
        c.execute("UPDATE diagnostic_item SET status='retired' WHERE knowledge_node_id=? AND diagnostic_item_id NOT IN (?,?)",
                  (node, item_id, spec['seed_item_id']))
        c.commit()
    def no_scoring(*args, **kwargs):
        raise AssertionError('Inventory must not score a synthetic child answer')
    monkeypatch.setattr('home_study_agent.learning_obligations.qualified_exact_score', no_scoring)
    from home_study_agent.goal_question_inventory import practice_item
    for current in (db, Database(db.path)):
        with current.connect() as c:
            before = digest(c)
            assert practice_item(c, CHILD, item_id, datetime.now(timezone.utc))
            assert _has_qualified_question(c, CHILD, node, datetime.now(timezone.utc))
            assert not _has_qualified_question(c, 'child-junior-3', node, datetime.now(timezone.utc))
            assert digest(c) == before
            assert qualified_exact_score(c, CHILD, item_id, 'in front of the library') is None
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
    service.withdraw(CHILD, published['candidate_id'], actor='parent', action_id='inventory-withdraw',
                     expected_version=4, note='Original source withdrawn')
    with db.connect() as c:
        assert practice_item(c, CHILD, item_id, datetime.now(timezone.utc)) is None


def test_real_roadmap_and_execution_share_inventory_without_calling_scorer(execution, monkeypatch):
    from home_study_agent.goal_roadmap import GoalRoadmapService
    db, sid, pid, node, exact = execution
    def no_scoring(*args, **kwargs):
        raise AssertionError('Reading a plan must not submit a correct answer to a scorer')
    monkeypatch.setattr('home_study_agent.learning_obligations.qualified_exact_score', no_scoring)
    service = GoalRoadmapService(db)
    with db.connect() as c:
        before = digest(c)
        assert _has_qualified_question(c, JUNIOR, node, datetime.now(timezone.utc))
        assert digest(c) == before
    resolved = service.resolve_execution(sid, pid)
    assert resolved['status'] == 'ready'
    assert resolved['diagnostic_item_id'] == exact[0]
    with db.connect() as c:
        assert digest(c) == before
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0


@pytest.mark.parametrize('fault', ['withdrawn', 'changed_prompt', 'changed_stimulus', 'keyword_rubric'])
def test_inventory_cannot_weaken_original_source_or_scoring_contract(execution, fault):
    import json
    from home_study_agent.goal_question_inventory import practice_item
    db, _, _, _, exact = execution
    with db.connect() as c:
        if fault == 'withdrawn':
            c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?", (exact[0],))
        elif fault == 'changed_prompt':
            c.execute("UPDATE diagnostic_item SET prompt='Unreviewed replacement' WHERE diagnostic_item_id=?", (exact[0],))
        elif fault == 'changed_stimulus':
            c.execute("UPDATE diagnostic_item SET stimulus_text='Unreviewed context' WHERE diagnostic_item_id=?", (exact[0],))
        else:
            rubric = {'local_rule': 'local_exact_v1', 'exact_answers': ['1'], 'keyword_groups': [['one']]}
            c.execute('UPDATE diagnostic_item SET expected_answer_json=? WHERE diagnostic_item_id=?',
                      (json.dumps(rubric), exact[0]))
        c.commit()
        before = digest(c)
        assert practice_item(c, JUNIOR, exact[0], datetime.now(timezone.utc)) is None
        assert digest(c) == before
