"""Shipped authored tasks, real installation/restart, no reviews or cloud grants."""
from datetime import datetime, timezone
import json

import pytest

from home_study_agent.database import Database
from home_study_agent.repository import StudyRepository
from home_study_agent.learning_state import LearningStateService
from home_study_agent.goal_question_inventory import practice_item
from home_study_agent.local_supply import validate_item, LocalSupplyError
from home_study_agent.open_response_profile import profile_for_item
from home_study_agent.school_oral_catalog import TASKS, items


def install(path):
    db = Database(path)
    db.initialize()
    StudyRepository(db).seed_family()
    LearningStateService(db).seed_catalog()
    return db


def without_authored_tasks(db, node=None):
    """Explicit missing/closed-source fixture; never used by product code."""
    with db.connect() as c:
        c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id LIKE 'diag-authored-oral-v1-%'"
                  + (' AND knowledge_node_id=?' if node else ''), (node,) if node else ())
        c.commit()


def test_fresh_install_has_twenty_owned_oral_tasks_without_fabricated_reviews(tmp_path):
    db = install(tmp_path / 'authored.db')
    expected = items()
    assert len(expected) == len({row[0] for row in expected}) == 20
    assert len({row[2] for row in expected}) == 20
    checked = {}
    with db.connect() as c:
        for task, row in zip(TASKS, expected):
            child = 'child-junior-3' if task[3] == '初三' else 'child-primary-4'
            other = 'child-primary-4' if child == 'child-junior-3' else 'child-junior-3'
            item = practice_item(c, child, row[0], datetime.now(timezone.utc))
            assert item is not None, row[0]
            assert item['prompt'] == row[2] and item['explanation'] == row[5]
            assert json.loads(item['expected_answer_json']) == row[4]
            node = dict(c.execute('SELECT * FROM knowledge_node WHERE knowledge_node_id=?', (row[1],)).fetchone())
            profile = profile_for_item(item, node)
            assert not profile['mastery_claim_allowed'] and not profile['automatic_certification']
            other_item = practice_item(c, other, row[0], datetime.now(timezone.utc))
            # Shared original tasks can support an older child's prerequisite work;
            # a younger child must not be silently assigned junior questions.
            assert (other_item is None) == (task[3] == '初三')
            checked[row[0]] = dict(c.execute('SELECT * FROM diagnostic_item WHERE diagnostic_item_id=?', (row[0],)).fetchone())
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='specialized_supply_candidate'").fetchone():
            assert c.execute('SELECT COUNT(*) FROM specialized_supply_candidate').fetchone()[0] == 0
    reopened = install(db.path)
    with reopened.connect() as c:
        for key, snapshot in checked.items():
            assert dict(c.execute('SELECT * FROM diagnostic_item WHERE diagnostic_item_id=?', (key,)).fetchone()) == snapshot


@pytest.mark.parametrize('field,value', [
    ('prompt','Changed prompt'),('explanation','Changed teaching'),
    ('expected_answer_json','{}'),('assessment_mode','objective'),('status','retired')])
def test_changed_or_withdrawn_pack_item_stays_unavailable_after_reseed(tmp_path, field, value):
    db = install(tmp_path / 'changed.db')
    item_id = items()[0][0]
    with db.connect() as c:
        c.execute(f'UPDATE diagnostic_item SET {field}=? WHERE diagnostic_item_id=?', (value,item_id))
        c.commit()
    reopened = install(db.path)
    with reopened.connect() as c:
        assert practice_item(c,'child-junior-3',item_id,datetime.now(timezone.utc)) is None
        with pytest.raises(LocalSupplyError):
            validate_item(c,'child-junior-3',item_id)
        assert c.execute(f'SELECT {field} FROM diagnostic_item WHERE diagnostic_item_id=?', (item_id,)).fetchone()[0] == value
