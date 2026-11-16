"""Real backup/restore/export/delete of versioned tasks; synthetic score boundary."""

from local_restore_operation_fixture import restore_local
from portable_restore_operation_fixture import restore_portable
import json
from pathlib import Path

import pytest

from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent.school_open_reviews import reconcile_school_reviews
from test_school_open_reviews import school_case, CHILD
from test_writing_calibrated_gate import authority


@pytest.mark.parametrize('portable', [False, True])
def test_successor_history_export_backup_restore_and_exact_child_delete(school_case, monkeypatch, portable):
    from home_study_agent.repository import StudyRepository
    db, client, headers, sid = school_case[:4]
    evidence, _, _, (path, body) = authority(monkeypatch, school_case, 3)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        old = reconcile_school_reviews(c, CHILD)['created'][0]
        c.commit()
    assert client.post(path,headers=headers,json=body).status_code == 200
    # Finish only the synthetic session, without claiming a learning result.
    repo = StudyRepository(db)
    repo.transition(sid,'study','reflection','isolated-backup','enter-reflection')
    repo.complete_with_reflection(sid,'','','','',True,'skip-for-isolated-backup')
    privacy = PrivacyLifecycleService(db,db.path.parent,data_encryption_key=b'v'*32)
    privacy.initialize()
    with db.connect() as c:
        before = [dict(r) for r in c.execute('SELECT * FROM review_task WHERE child_id=? ORDER BY review_task_id',(CHILD,))]
        other_before = [dict(r) for r in c.execute("SELECT * FROM child_profile WHERE child_id='child-primary-4'")]
        assert len(before)==2
        assert next(r for r in before if r['review_task_id']==old)['status']=='cancelled'
    exported = json.loads(Path(privacy.export_child(CHILD)['path']).read_text())
    assert sorted(exported['review_tasks'],key=lambda r:r['review_task_id']) == before
    backup = privacy.create_portable_backup('isolated-version-passphrase') if portable else privacy.create_backup()
    # Learning deletion intentionally purges backups containing that child's
    # data. Verify restoration BEFORE exercising that destructive boundary.
    restored = (restore_portable(privacy,backup['path'],'isolated-version-passphrase','确认迁移恢复') if portable
                else restore_local(privacy,backup['backup_id'],'确认恢复'))
    assert restored['status']=='restored',restored
    with db.connect() as c:
        after = [dict(r) for r in c.execute('SELECT * FROM review_task WHERE child_id=? ORDER BY review_task_id',(CHILD,))]
        assert after==before
        assert not c.execute('PRAGMA foreign_key_check').fetchall()
    deleted = privacy.delete_child_scope(CHILD,'learning','确认删除','delete-versioned-history')
    assert deleted['status']=='completed',deleted
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM review_task WHERE child_id=?',(CHILD,)).fetchone()[0]==0
        assert [dict(r) for r in c.execute("SELECT * FROM child_profile WHERE child_id='child-primary-4'")]==other_before
