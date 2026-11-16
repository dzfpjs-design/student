"""Legacy proof fixtures omit only fields introduced by migration 71.

The old proof and event journal remain immutable; this is not permission to
accept changed source text, a repaired edition, or an unrelated child.
"""
import json

import pytest

from home_study_agent.curriculum_supply import CurriculumSupplyService
from home_study_agent.database import Database
from home_study_agent.stage_source_review import stage_review_context
from test_curriculum_supply import CHILD, prepared, publish, draft


def legacy_proof(db, cid, partial=False):
    with db.connect() as c:
        proof = json.loads(c.execute('SELECT source_snapshot_json FROM curriculum_supply_package WHERE candidate_id=?', (cid,)).fetchone()[0])
        proof['source'].pop('current_version')
        if not partial:
            proof['source'].pop('predecessor_id')
        frozen = json.dumps(proof, ensure_ascii=False)
        c.execute('UPDATE curriculum_supply_package SET source_snapshot_json=? WHERE candidate_id=?', (frozen, cid))
        c.commit()
        return frozen


def test_legacy_published_proof_remains_eligible_and_readable_without_history_rewrite(tmp_path):
    db, service, payload = prepared(tmp_path)
    result = publish(service, payload)
    cid = result['candidate_id']
    frozen = legacy_proof(db, cid)
    with db.connect() as c:
        events = [dict(r) for r in c.execute('SELECT * FROM curriculum_supply_event ORDER BY rowid')]
    restarted = CurriculumSupplyService(Database(db.path))
    assert restarted.get(CHILD, cid)['eligible'] is True
    with db.connect() as c:
        assert stage_review_context(c, CHILD, cid)['candidate_id'] == cid
        assert c.execute('SELECT source_snapshot_json FROM curriculum_supply_package WHERE candidate_id=?', (cid,)).fetchone()[0] == frozen
        assert [dict(r) for r in c.execute('SELECT * FROM curriculum_supply_event ORDER BY rowid')] == events
    gone = restarted.withdraw(CHILD, cid, actor='parent', note='Withdraw original source', action_id='withdraw-legacy', expected_version=4)
    assert not gone['eligible']


def test_legacy_draft_can_finish_real_validation_review_and_publication(tmp_path):
    db, service, payload = prepared(tmp_path)
    cid = draft(service, payload)['candidate_id']
    frozen = legacy_proof(db, cid)
    assert service.validate(CHILD, cid, actor='checker', action_id='validate-old', expected_version=1)['state'] == 'validated'
    assert service.review(CHILD, cid, actor='parent', decision='approve', note='Checked exact original and requirement', action_id='review-old', expected_version=2)['state'] == 'reviewed'
    assert service.publish(CHILD, cid, actor='parent', action_id='publish-old', expected_version=3)['eligible']
    with db.connect() as c:
        assert c.execute('SELECT source_snapshot_json FROM curriculum_supply_package WHERE candidate_id=?', (cid,)).fetchone()[0] == frozen


@pytest.mark.parametrize('mutation', ['partial_metadata', 'historical', 'predecessor', 'title', 'child', 'standard'])
def test_legacy_compatibility_does_not_ignore_actual_source_or_scope_change(tmp_path, mutation):
    db, service, payload = prepared(tmp_path)
    cid = publish(service, payload)['candidate_id']
    legacy_proof(db, cid, partial=mutation == 'partial_metadata')
    with db.connect() as c:
        if mutation == 'historical':
            c.execute('UPDATE knowledge_source SET current_version=0')
        elif mutation == 'predecessor':
            c.execute("UPDATE knowledge_source SET predecessor_id='a-real-prior-version'")
        elif mutation == 'title':
            c.execute("UPDATE knowledge_source SET title='changed source metadata'")
        elif mutation == 'child':
            c.execute("UPDATE knowledge_source SET child_id='child-junior-3'")
        elif mutation == 'standard':
            c.execute("UPDATE curriculum_standard_source SET updated_at='2026-09-09'")
        c.commit()
    assert not service.get(CHILD, cid)['eligible']
