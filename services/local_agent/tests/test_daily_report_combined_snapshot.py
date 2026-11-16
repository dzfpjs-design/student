"""Encrypted, local-only combined snapshot on a single synthetic real-source DB."""
import pytest
from home_study_agent.daily_report_combined_snapshot import CANDIDATE_SCHEMA, freeze, inspect
from home_study_agent.pilot_test_telemetry import JOINT_TEST_MEMBER_SCHEMA, PilotTestTelemetryService
from test_combined_report_real_sources_fixture import synthetic_combined_family, SYNTHETIC_ADDRESS, SYNTHETIC_KEY, OCR_TEXT, CANARY


@pytest.fixture
def candidate(synthetic_combined_family,tmp_path):
    f=synthetic_combined_family
    with f['db'].connect() as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='combined_diagnostic_member'").fetchone(): c.execute(JOINT_TEST_MEMBER_SCHEMA)
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='combined_daily_snapshot'").fetchone(): c.execute(CANDIDATE_SCHEMA)
        c.commit()
    batch=f['telemetry'].create_combined_batch(f['day'],SYNTHETIC_ADDRESS,[f['child']],SYNTHETIC_KEY)['batch_id']
    result=freeze(f['telemetry'],batch,f['child'],f['day'],f['recipient'],f['visual'],SYNTHETIC_KEY)
    return f,batch,result['snapshot_id'],tmp_path


def test_freeze_survives_restart_and_keeps_report_ocr_and_email_encrypted(candidate):
    f,batch,snapshot,tmp_path=candidate
    restarted=PilotTestTelemetryService(f['db'],tmp_path)
    result=inspect(restarted,snapshot,SYNTHETIC_KEY)
    assert result['current'] and result['external_delivery_permitted'] is False and result['source_withdrawal_checked'] is False
    with f['db'].connect() as c:
        row=c.execute('SELECT * FROM combined_daily_snapshot').fetchone()
        assert row['status']=='local_only'
        assert OCR_TEXT.encode() not in bytes(row['encrypted_payload'])
        assert SYNTHETIC_ADDRESS.encode() not in bytes(row['encrypted_payload'])
        assert CANARY.encode() not in bytes(row['encrypted_payload'])
        assert c.execute('SELECT count(*) FROM report_delivery_outbox').fetchone()[0]==0


def test_automatic_stop_pending_blocks_an_existing_combined_candidate(candidate):
    f,_,snapshot,_=candidate
    with f['db'].connect() as c:
        c.execute('''INSERT INTO habit_experiment(experiment_id,child_id,target_state,title,child_action,
            success_signal,duration_days,baseline_json,status,proposed_at,started_at)
            VALUES('queued-goal',?,'blocked_candidate','synthetic','synthetic','synthetic',7,'{}','active',?,?)''',
            (f['child'],f['day']+'T00:00:00+00:00',f['day']+'T00:00:00+00:00'))
        c.execute('''INSERT INTO habit_experiment_event(event_id,experiment_id,child_id,event_type,payload_json,occurred_at,client_action_id)
            VALUES('queued-event','queued-goal',?,'auto_stop_pending','{}',?,'queued-action')''',
            (f['child'],f['day']+'T01:00:00+00:00'))
        c.commit()
    with pytest.raises(ValueError,match='COMBINED_HABIT_STOP_PENDING'):
        inspect(f['telemetry'],snapshot,SYNTHETIC_KEY)


@pytest.mark.parametrize('feedback',[None,'accepted'])
def test_pending_intervention_withdrawal_blocks_only_the_owner_candidate(candidate,feedback):
    f,batch,snapshot,_=candidate
    with f['db'].connect() as c:
        c.execute('''INSERT INTO intervention(intervention_id,child_id,session_id,decided_at,candidate_state,message,
            intervention_type,status,withdrawal_reason,feedback) VALUES('synthetic-pending-intervention','child-primary-4',NULL,?,
            'blocked_candidate','synthetic','contextual_habit_ask_first','active','habit_support_source_changed_pending',?)''',
            (f['day']+'T00:00:00+00:00',feedback))
        c.commit()
    assert inspect(f['telemetry'],snapshot,SYNTHETIC_KEY)['current']
    with f['db'].connect() as c:
        c.execute("UPDATE intervention SET child_id=? WHERE intervention_id='synthetic-pending-intervention'",(f['child'],))
        c.commit()
    with pytest.raises(ValueError,match='COMBINED_INTERVENTION_WITHDRAWAL_PENDING'):
        inspect(f['telemetry'],snapshot,SYNTHETIC_KEY)
    with pytest.raises(ValueError,match='COMBINED_INTERVENTION_WITHDRAWAL_PENDING'):
        freeze(f['telemetry'],batch,f['child'],f['day'],f['recipient'],f['visual'],SYNTHETIC_KEY)
    with f['db'].connect() as c:
        c.execute("UPDATE intervention SET status='withdrawn' WHERE intervention_id='synthetic-pending-intervention'")
        c.commit()
    # This synthetic intervention was not a referenced report source; its
    # finalized withdrawal releases the guard without changing the snapshot.
    assert inspect(f['telemetry'],snapshot,SYNTHETIC_KEY)['current']


@pytest.mark.parametrize('change',['formal_revoke','formal_edit','visual','diagnostic_reconsent','zip','ciphertext','member'])
def test_changed_component_never_remains_current(candidate,change):
    f,batch,snapshot,_=candidate
    if change=='diagnostic_reconsent':
        f['telemetry'].set_diagnostic_consent(SYNTHETIC_ADDRESS,consented=False)
        f['telemetry'].set_diagnostic_consent(SYNTHETIC_ADDRESS,consented=True)
    else:
        with f['db'].connect() as c:
            if change=='formal_revoke': c.execute("UPDATE parent_report_snapshot SET status='revoked' WHERE snapshot_id=?",(f['formal']['snapshot_id'],))
            if change=='formal_edit': c.execute("UPDATE parent_report_snapshot SET parent_edit_json=? WHERE snapshot_id=?",('{"parent_note":"synthetic change"}',f['formal']['snapshot_id']))
            if change=='visual': c.execute("UPDATE capture_artifact SET extracted_text='synthetic changed' WHERE artifact_id='fixture-art'")
            if change=='zip': c.execute("UPDATE pilot_test_digest_outbox SET attachment_content=X'01' WHERE combined_batch_id=?",(batch,))
            if change=='ciphertext': c.execute("UPDATE combined_daily_snapshot SET encrypted_payload=X'00' WHERE snapshot_id=?",(snapshot,))
            if change=='member': c.execute("DELETE FROM combined_diagnostic_member WHERE batch_id=?",(batch,))
            c.commit()
    with pytest.raises(ValueError): inspect(f['telemetry'],snapshot,SYNTHETIC_KEY)


def test_wrong_child_key_and_missing_real_approval_are_rejected(synthetic_combined_family,tmp_path):
    f=synthetic_combined_family
    with f['db'].connect() as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='combined_diagnostic_member'").fetchone(): c.execute(JOINT_TEST_MEMBER_SCHEMA)
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='combined_daily_snapshot'").fetchone(): c.execute(CANDIDATE_SCHEMA)
        c.commit()
    batch=f['telemetry'].create_combined_batch(f['day'],SYNTHETIC_ADDRESS,[f['child']],SYNTHETIC_KEY)['batch_id']
    with pytest.raises(ValueError): freeze(f['telemetry'],batch,'not-the-child',f['day'],f['recipient'],f['visual'],SYNTHETIC_KEY)
    with pytest.raises(ValueError): freeze(f['telemetry'],batch,f['child'],f['day'],f['recipient'],f['visual'],b'x'*32)
    f['reports'].revoke(f['formal']['snapshot_id'],'synthetic revocation')
    result=freeze(f['telemetry'],batch,f['child'],f['day'],f['recipient'],f['visual'],SYNTHETIC_KEY)
    with f['db'].connect() as c:
        from home_study_agent.daily_report_combined_snapshot import _inspect_in_connection
        c.execute('BEGIN')
        _,body=_inspect_in_connection(c,f['telemetry'],result['snapshot_id'],SYNTHETIC_KEY)
        assert body['formal']==[] and body['formal_pending_count']==1


@pytest.mark.parametrize('approved',[False,True])
def test_new_same_day_formal_report_invalidates_frozen_snapshot(candidate,approved):
    f,_,snapshot,_=candidate
    with f['db'].connect() as c:
        c.execute("""INSERT INTO daily_session(session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id)
            SELECT 'later-session',child_id,status,state,started_at,last_checkpoint_at,'later-action'
            FROM daily_session WHERE session_id=?""",(f['session'],))
        c.execute("""INSERT INTO daily_report(report_id,session_id,child_id,report_date,source,evidence_json,summary_json,parent_coaching_json,created_at)
            SELECT 'later-report','later-session',child_id,report_date,source,evidence_json,summary_json,parent_coaching_json,created_at
            FROM daily_report WHERE session_id=?""",(f['session'],))
        if approved:
            c.execute("""INSERT INTO daily_reflection(reflection_id,session_id,child_id,learned_text,difficult_text,feeling,next_action,skipped,created_at,client_action_id)
                SELECT 'later-reflection','later-session',child_id,learned_text,difficult_text,feeling,next_action,skipped,created_at,'later-reflection-action'
                FROM daily_reflection WHERE session_id=?""",(f['session'],))
        c.commit()
    if approved:
        later=f['reports'].create_from_daily(f['repo'].get_daily_report('later-report'))
        f['reports'].approve(later['snapshot_id'])
    with pytest.raises(ValueError,match='COMBINED_FORMAL'):
        inspect(f['telemetry'],snapshot,SYNTHETIC_KEY)


def test_day_without_formal_report_can_freeze_observations(synthetic_combined_family):
    f=synthetic_combined_family
    with f['db'].connect() as c:
        c.execute('DELETE FROM parent_report_snapshot WHERE snapshot_id=?',(f['formal']['snapshot_id'],))
        c.execute('DELETE FROM daily_report WHERE session_id=?',(f['session'],))
        c.commit()
    batch=f['telemetry'].create_combined_batch(f['day'],SYNTHETIC_ADDRESS,[f['child']],SYNTHETIC_KEY)['batch_id']
    snapshot=freeze(f['telemetry'],batch,f['child'],f['day'],f['recipient'],f['visual'],SYNTHETIC_KEY)['snapshot_id']
    assert inspect(f['telemetry'],snapshot,SYNTHETIC_KEY)['current']


def test_task_time_change_invalidates_frozen_observation(candidate):
    f,_,snapshot,_=candidate
    with f['db'].connect() as c:
        c.execute('''UPDATE plan_item SET active_seconds=active_seconds+1
            WHERE plan_id IN (SELECT plan_id FROM daily_plan WHERE session_id=?)''',(f['session'],))
        c.commit()
    with pytest.raises(ValueError,match='COMBINED_VISUAL_STALE'):
        inspect(f['telemetry'],snapshot,SYNTHETIC_KEY)


@pytest.mark.parametrize('other_status',['sent','unknown'])
def test_waiting_child_inspection_survives_other_child_terminal_state(synthetic_combined_family,other_status):
    f=synthetic_combined_family; other='child-primary-4'
    f['delivery'].add_recipient(other,'email',SYNTHETIC_ADDRESS,'synthetic sibling',True)
    with f['db'].connect() as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='combined_diagnostic_member'").fetchone(): c.execute(JOINT_TEST_MEMBER_SCHEMA)
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='combined_daily_snapshot'").fetchone(): c.execute(CANDIDATE_SCHEMA)
        c.commit()
    batch=f['telemetry'].create_combined_batch(f['day'],SYNTHETIC_ADDRESS,[other,f['child']],SYNTHETIC_KEY)['batch_id']
    snapshot=freeze(f['telemetry'],batch,f['child'],f['day'],f['recipient'],f['visual'],SYNTHETIC_KEY)['snapshot_id']
    with f['db'].connect() as c:
        c.execute('UPDATE combined_diagnostic_member SET status=? WHERE batch_id=? AND child_id=?',(other_status,batch,other))
        if other_status=='unknown': c.execute("UPDATE combined_diagnostic_batch SET status='unknown' WHERE batch_id=?",(batch,))
        c.commit()
    assert inspect(f['telemetry'],snapshot,SYNTHETIC_KEY)['current']
    with pytest.raises(ValueError):
        freeze(f['telemetry'],batch,f['child'],f['day'],f['recipient'],f['visual'],SYNTHETIC_KEY)
    with f['db'].connect() as c:
        c.execute("UPDATE combined_diagnostic_member SET status='unknown' WHERE batch_id=? AND child_id=?",(batch,f['child'])); c.commit()
    with pytest.raises(ValueError,match='COMBINED_BATCH_NOT_FRESH'):
        inspect(f['telemetry'],snapshot,SYNTHETIC_KEY)


def test_candidate_plaintext_size_limit_fails_before_persistence(synthetic_combined_family,monkeypatch):
    import home_study_agent.daily_report_combined_snapshot as combined
    f=synthetic_combined_family
    with f['db'].connect() as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='combined_diagnostic_member'").fetchone(): c.execute(JOINT_TEST_MEMBER_SCHEMA)
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='combined_daily_snapshot'").fetchone(): c.execute(CANDIDATE_SCHEMA)
        c.commit()
    batch=f['telemetry'].create_combined_batch(f['day'],SYNTHETIC_ADDRESS,[f['child']],SYNTHETIC_KEY)['batch_id']
    monkeypatch.setattr(combined,'MAX_CANDIDATE_BYTES',200)
    with pytest.raises(ValueError,match='COMBINED_CANDIDATE_TOO_LARGE'):
        freeze(f['telemetry'],batch,f['child'],f['day'],f['recipient'],f['visual'],SYNTHETIC_KEY)
    with f['db'].connect() as c:
        assert c.execute('SELECT count(*) FROM combined_daily_snapshot').fetchone()[0]==0


def test_processing_validation_rejects_another_processing_member(synthetic_combined_family):
    from home_study_agent.daily_report_combined_snapshot import _inspect_in_connection
    f=synthetic_combined_family; other='child-primary-4'
    f['delivery'].add_recipient(other,'email',SYNTHETIC_ADDRESS,'synthetic sibling',True)
    with f['db'].connect() as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='combined_diagnostic_member'").fetchone(): c.execute(JOINT_TEST_MEMBER_SCHEMA)
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='combined_daily_snapshot'").fetchone(): c.execute(CANDIDATE_SCHEMA)
        c.commit()
    batch=f['telemetry'].create_combined_batch(f['day'],SYNTHETIC_ADDRESS,[other,f['child']],SYNTHETIC_KEY)['batch_id']
    snapshot=freeze(f['telemetry'],batch,f['child'],f['day'],f['recipient'],f['visual'],SYNTHETIC_KEY)['snapshot_id']
    # Populate the exact visual-ledger claim without sending anything.
    with f['db'].connect() as c:
        from home_study_agent.daily_report_delivery_state import identity_key
        identity=identity_key(SYNTHETIC_KEY,f['child'],f['day'],SYNTHETIC_ADDRESS)
        c.execute("INSERT INTO daily_observation_delivery_ledger VALUES(?,?,?,'processing','old','old')",(identity,f['child'],f['visual']))
        c.execute("UPDATE combined_diagnostic_member SET status='processing' WHERE batch_id=?",(batch,)); c.commit()
    with f['db'].connect() as c:
        c.execute('BEGIN')
        with pytest.raises(ValueError,match='COMBINED_BATCH_NOT_FRESH'):
            _inspect_in_connection(c,f['telemetry'],snapshot,SYNTHETIC_KEY,processing_identity=identity)
