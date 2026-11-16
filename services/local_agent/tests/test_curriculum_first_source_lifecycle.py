import sqlite3

import pytest

from home_study_agent.curriculum_first_source import (
    CurriculumFirstSourceService, VALIDATOR, purge_sources, quarantine_after_restore,
)
from home_study_agent.database import Database
from home_study_agent.learning_obligations import qualified_exact_score
from test_curriculum_first_source_http import CHILD, first_source_fixture, publish
from test_curriculum_first_source_reliability import client_for


def test_restore_requires_new_actions_same_identity_and_borrows_transaction(tmp_path):
    db, _, spec, _ = first_source_fixture(tmp_path)
    packet = publish(client_for(db), spec)
    cid, iid = packet['candidate_id'], packet['diagnostic_item_id']
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        assert quarantine_after_restore(c, 'restore-a') == 1
        assert c.in_transaction
        assert qualified_exact_score(c, CHILD, iid, '2,3') is None
        c.rollback()
    service = CurriculumFirstSourceService(Database(db.path))
    assert service.get(CHILD, cid)['eligible']
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        assert quarantine_after_restore(c, 'restore-a') == 1
        c.commit()
    restored = service.get(CHILD, cid)
    assert restored['state'] == 'blocked' and not restored['eligible']
    assert restored['blockers'] == ['CURRICULUM_FIRST_REVIEW_AFTER_RESTORE']
    old = service.transition(CHILD,cid,'publish',actor='parent',action_id='first-publish',expected_version=3)
    assert old['state'] == 'blocked'
    service.transition(CHILD,cid,'validate',actor=VALIDATOR,action_id='new-v',expected_version=5)
    service.transition(CHILD,cid,'review',actor='parent',action_id='new-r',expected_version=6,
                       values=dict(decision='approve',note='恢复后重新核对原来源与课标'))
    current = service.transition(CHILD,cid,'publish',actor='parent',action_id='new-p',expected_version=7)
    assert current['eligible'] and current['diagnostic_item_id'] == iid
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM diagnostic_item WHERE diagnostic_item_id=?',(iid,)).fetchone()[0] == 1


def test_precise_source_purge_rolls_back_and_retains_other_child(tmp_path):
    db, source, spec, _ = first_source_fixture(tmp_path)
    packet = publish(client_for(db), spec)
    with db.connect() as c:
        other = [tuple(r) for r in c.execute("SELECT * FROM knowledge_source WHERE child_id != ?",(CHILD,))]
        item_before = tuple(c.execute('SELECT * FROM diagnostic_item WHERE diagnostic_item_id=?',(packet['diagnostic_item_id'],)).fetchone())
        c.execute("CREATE TRIGGER fail_first_erasure BEFORE DELETE ON curriculum_first_source BEGIN SELECT RAISE(ABORT,'injected erasure failure'); END")
        c.commit()
    with pytest.raises(sqlite3.IntegrityError,match='injected erasure'):
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            purge_sources(c,CHILD,source_ids={source['source_id']},affected_scopes={})
            c.commit()
    with db.connect() as c:
        assert tuple(c.execute('SELECT * FROM diagnostic_item WHERE diagnostic_item_id=?',(packet['diagnostic_item_id'],)).fetchone()) == item_before
        assert c.execute('SELECT count(*) FROM curriculum_first_source').fetchone()[0] == 1
        c.execute('DROP TRIGGER fail_first_erasure')
        c.commit()
        c.execute('BEGIN IMMEDIATE')
        assert purge_sources(c,'child-primary-4',source_ids={source['source_id']},affected_scopes={})['curriculum_first_source'] == 0
        counts = purge_sources(c,CHILD,source_ids={source['source_id']},affected_scopes={})
        assert counts['curriculum_first_source'] == 1 and counts['diagnostic_item_redacted'] == 1
        c.commit()
    with Database(db.path).connect() as c:
        assert [tuple(r) for r in c.execute('SELECT * FROM knowledge_source WHERE child_id != ?',(CHILD,))] == other
        item = c.execute('SELECT * FROM diagnostic_item WHERE diagnostic_item_id=?',(packet['diagnostic_item_id'],)).fetchone()
        assert item['prompt'] == '题源已删除' and item['expected_answer_json'] == '{}'
        assert c.execute('SELECT count(*) FROM curriculum_first_source_event').fetchone()[0] == 0
        assert qualified_exact_score(c, CHILD, packet['diagnostic_item_id'], '2,3') is None
