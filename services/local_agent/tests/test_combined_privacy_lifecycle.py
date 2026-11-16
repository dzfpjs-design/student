"""Synthetic combined ZIP erasure and restored-history tombstone checks."""
import pytest
from home_study_agent.database import Database
from home_study_agent.daily_report_combined_snapshot import freeze
from home_study_agent.daily_report_delivery_state import identity_key
from home_study_agent.learning_input_revision import LearningInputRevisionService
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from local_restore_operation_fixture import restore_local
from test_combined_report_real_sources_fixture import synthetic_combined_family,SYNTHETIC_ADDRESS,SYNTHETIC_KEY


def _batch(f,other='child-primary-4'):
    f['delivery'].add_recipient(other,'email',SYNTHETIC_ADDRESS,'synthetic',True)
    batch=f['telemetry'].create_combined_batch(f['day'],SYNTHETIC_ADDRESS,[other,f['child']],SYNTHETIC_KEY)['batch_id']
    snapshot=freeze(f['telemetry'],batch,f['child'],f['day'],f['recipient'],f['visual'],SYNTHETIC_KEY)['snapshot_id']
    return batch,snapshot


@pytest.mark.parametrize('status',['processing','unknown','sent'])
def test_sibling_delete_erases_joint_payload_and_retains_history(synthetic_combined_family,tmp_path,status):
    f=synthetic_combined_family; db=f['db']; batch,snapshot=_batch(f)
    token=identity_key(SYNTHETIC_KEY,f['child'],f['day'],SYNTHETIC_ADDRESS)
    with db.connect() as c:
        c.execute('INSERT INTO daily_observation_delivery_ledger VALUES(?,?,?, ?,?,?)',
                  (token,f['child'],f['visual'],status,'synthetic','synthetic'))
        c.execute('INSERT INTO combined_daily_delivery VALUES(?,?,?,?,?,?,?,?)',
                  (token,batch,f['child'],snapshot,status,'synthetic-claim','synthetic','synthetic'))
        c.commit()
    LearningInputRevisionService(db).initialize()
    privacy=PrivacyLifecycleService(db,tmp_path,SYNTHETIC_KEY);privacy.initialize()
    assert privacy.delete_child_scope('child-primary-4','media','确认删除','synthetic-delete')['status']=='completed'
    with db.connect() as c:
        assert not c.execute('SELECT 1 FROM combined_daily_delivery').fetchone()
        assert not c.execute('SELECT 1 FROM combined_daily_snapshot').fetchone()
        assert not c.execute('SELECT 1 FROM combined_diagnostic_member').fetchone()
        assert not c.execute('SELECT 1 FROM combined_diagnostic_batch').fetchone()
        tombstone=c.execute('SELECT * FROM pilot_test_digest_outbox').fetchone()
        assert tombstone['status']=='unknown' and tombstone['error_code']=='COMBINED_PRIVACY_TOMBSTONE'
        assert tombstone['combined_batch_id'] is None and tombstone['attachment_content'] is None and tombstone['body'] is None
        assert c.execute('SELECT status FROM daily_observation_delivery_ledger WHERE identity=?',(token,)).fetchone()[0]==('sent' if status=='sent' else 'unknown')
        assert c.execute('SELECT blocked FROM daily_observation_delivery_barrier').fetchone()[0]==1
        assert c.execute('SELECT 1 FROM daily_report WHERE child_id=?',(f['child'],)).fetchone()
        assert c.execute('SELECT 1 FROM capture_artifact WHERE artifact_id=?',('fixture-art',)).fetchone()


def test_restore_older_backup_preserves_live_combined_unknown_tombstone(synthetic_combined_family,tmp_path):
    f=synthetic_combined_family;db=f['db']
    privacy=PrivacyLifecycleService(db,tmp_path,SYNTHETIC_KEY);privacy.initialize()
    backup=privacy.create_backup(prune=False)
    _batch(f)
    assert restore_local(privacy,backup['backup_id'],'确认恢复')
    with db.connect() as c:
        row=c.execute('SELECT * FROM pilot_test_digest_outbox').fetchone()
        assert row['status']=='unknown' and row['error_code']=='COMBINED_PRIVACY_TOMBSTONE'
        assert row['combined_batch_id'] is None and row['attachment_content'] is None and row['body'] is None
        assert c.execute('SELECT blocked FROM daily_observation_delivery_barrier').fetchone()[0]==1
        assert not c.execute('SELECT 1 FROM combined_diagnostic_batch').fetchone()
        assert c.execute('SELECT 1 FROM daily_report WHERE child_id=?',(f['child'],)).fetchone()


def test_delete_one_joint_batch_preserves_other_email_batch(synthetic_combined_family,tmp_path):
    f=synthetic_combined_family; db=f['db']; first,_=_batch(f)
    alternate='other-parent@example.test'
    f['delivery'].add_recipient(f['child'],'email',alternate,'other',True)
    f['telemetry'].set_diagnostic_consent(alternate,consented=True)
    second=f['telemetry'].create_combined_batch(f['day'],alternate,[f['child']],SYNTHETIC_KEY)['batch_id']
    LearningInputRevisionService(db).initialize()
    privacy=PrivacyLifecycleService(db,tmp_path,SYNTHETIC_KEY);privacy.initialize()
    assert privacy.delete_child_scope('child-primary-4','media','确认删除','synthetic-delete')['status']=='completed'
    with db.connect() as c:
        assert not c.execute('SELECT 1 FROM combined_diagnostic_batch WHERE batch_id=?',(first,)).fetchone()
        assert c.execute('SELECT 1 FROM combined_diagnostic_batch WHERE batch_id=?',(second,)).fetchone()
        remaining=c.execute('SELECT attachment_content FROM pilot_test_digest_outbox WHERE combined_batch_id=?',(second,)).fetchone()
        assert remaining and remaining['attachment_content']
        assert c.execute('SELECT 1 FROM daily_report WHERE child_id=?',(f['child'],)).fetchone()


def test_corrupt_member_set_prevents_any_deletion(synthetic_combined_family,tmp_path):
    f=synthetic_combined_family;db=f['db']; batch,_=_batch(f)
    with db.connect() as c:
        c.execute("DELETE FROM combined_diagnostic_member WHERE batch_id=? AND child_id='child-primary-4'",(batch,));c.commit()
    privacy=PrivacyLifecycleService(db,tmp_path,SYNTHETIC_KEY);privacy.initialize()
    with pytest.raises(ValueError,match='COMBINED_PRIVACY_MEMBERS_UNCONFIRMED'):
        privacy.delete_child_scope('child-primary-4','media','确认删除','synthetic-delete')
    with db.connect() as c:
        assert not c.execute("SELECT 1 FROM deletion_job WHERE client_action_id='synthetic-delete'").fetchone()
        assert c.execute('SELECT 1 FROM combined_diagnostic_batch WHERE batch_id=?',(batch,)).fetchone()


def test_restore_ambiguous_prior_pilot_identity_rolls_back(synthetic_combined_family,tmp_path):
    f=synthetic_combined_family;db=f['db']
    privacy=PrivacyLifecycleService(db,tmp_path,SYNTHETIC_KEY);privacy.initialize()
    with db.connect() as c:
        c.execute("INSERT INTO pilot_test_digest_outbox(delivery_date,recipient,status,created_at,updated_at) VALUES(?,?,'pending','synthetic','synthetic')",(f['day'],SYNTHETIC_ADDRESS));c.commit()
    backup=privacy.create_backup(prune=False)
    with db.connect() as c: c.execute('DELETE FROM pilot_test_digest_outbox');c.commit()
    batch,snapshot=_batch(f)
    tables=('combined_diagnostic_batch','combined_diagnostic_member','combined_daily_snapshot',
            'combined_daily_delivery','pilot_test_digest_outbox','daily_report',
            'daily_observation_report_snapshot','capture_artifact')
    def contents(connection):
        return {table: sorted((tuple(row) for row in connection.execute('SELECT * FROM '+table)), key=repr)
                for table in tables}
    with db.connect() as c:
        before=contents(c)
        assert c.execute('SELECT 1 FROM combined_daily_snapshot WHERE snapshot_id=?',(snapshot,)).fetchone()
    original_operations=[]
    with pytest.raises(ValueError,match='COMBINED_PRIVACY_IDENTITY_UNCONFIRMED'):
        restore_local(privacy,backup['backup_id'],'确认恢复',original_operations=original_operations)
    from home_study_agent.restore_publication import RestorePublicationError
    with pytest.raises(RestorePublicationError,match='RESTORE_PUBLICATION_RECOVERY_REQUIRED'):
        with db.connect():
            pass
    # Resolve the original, unapplied operation using its actual metadata;
    # never reset the gate or synthesize a publication receipt.
    from home_study_agent.parent_restore_workflow import recover_original
    assert len(original_operations)==1
    assert recover_original(db,original_operations[0])['status']=='failed'
    with db.connect() as c:
        assert contents(c)==before
        assert c.execute('SELECT 1 FROM combined_diagnostic_batch WHERE batch_id=?',(batch,)).fetchone()
        assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert not c.execute('PRAGMA foreign_key_check').fetchall()
    reopened=Database(db.path); reopened.initialize()
    with reopened.connect() as c:
        assert contents(c)==before
