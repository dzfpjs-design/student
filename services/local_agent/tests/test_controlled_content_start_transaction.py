"""Borrowed writes use the caller's real SQLite transaction, never a commit proxy."""
import pytest

from home_study_agent.controlled_content import ControlledContentError, ControlledContentService
from home_study_agent.database import Database
from test_controlled_content import seed


@pytest.fixture
def video(tmp_path):
    db = Database(tmp_path / 'media.db')
    db.initialize()
    sid, resource = seed(db, tmp_path / 'video')
    service = ControlledContentService(db)
    service.update_rule('child-media', enabled=True, max_daily_minutes=20, max_item_minutes=8)
    service.approve('child-media', resource, 6)
    return db, service, sid


def test_borrowed_start_is_uncommitted_and_outer_rollback_removes_outcome_and_audit(video):
    db, service, sid = video
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        result = service.start(sid, '分数', 'outer-rollback', connection=c)
        assert result['launch_allowed'] is True and c.in_transaction
        assert c.execute('SELECT COUNT(*) FROM companion_activity_outcome').fetchone()[0] == 1
        with db.connect() as reader:
            assert reader.execute('SELECT COUNT(*) FROM controlled_content_session').fetchone()[0] == 0
            assert reader.execute('SELECT COUNT(*) FROM companion_activity_outcome').fetchone()[0] == 0
        c.rollback()
        for table in ('controlled_content_session', 'companion_activity_outcome', 'companion_activity_outcome_event'):
            assert c.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM audit_event WHERE action='controlled_content_started'").fetchone()[0] == 0
    assert service.start(sid, '分数', 'outer-rollback')['launch_allowed'] is True


def test_start_rejects_nontransaction_and_other_database_connection(video, tmp_path):
    db, service, sid = video
    with db.connect() as c:
        with pytest.raises(ControlledContentError, match='TRANSACTION_REQUIRED'):
            service.start(sid, '分数', 'no-tx', connection=c)
    foreign = Database(tmp_path / 'foreign.db')
    foreign.initialize()
    with foreign.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        with pytest.raises(ControlledContentError, match='TRANSACTION_OWNER_MISMATCH'):
            service.start(sid, '分数', 'wrong-db', connection=c)
        assert c.in_transaction
        c.rollback()


def test_replay_does_not_recreate_removed_original_outcome(video):
    db, service, sid = video
    first = service.start(sid, '分数', 'no-backfill')
    with db.connect() as c:
        c.execute('DELETE FROM companion_activity_outcome_event')
        c.execute('DELETE FROM companion_activity_outcome')
        c.commit()
    with pytest.raises(ControlledContentError, match='IDEMPOTENCY_CONFLICT'):
        service.start(sid, '分数', 'no-backfill')
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM companion_activity_outcome').fetchone()[0] == 0
        assert c.execute('SELECT content_session_id FROM controlled_content_session').fetchone()[0] == first['content_session_id']
