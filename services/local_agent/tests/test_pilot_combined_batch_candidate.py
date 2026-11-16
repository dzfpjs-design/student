"""Local joint-log reservation on synthetic data; no delivery is enabled."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date
import pytest
from home_study_agent.pilot_test_telemetry import JOINT_TEST_MEMBER_SCHEMA
from home_study_agent.report_delivery import ReportDeliveryService
from test_pilot_combined_ownership import ADDRESS, service_for

KEY=b's'*32


def setup_batch(tmp_path):
    db,service=service_for(tmp_path)
    with db.connect() as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='combined_diagnostic_member'").fetchone(): c.execute(JOINT_TEST_MEMBER_SCHEMA)
        for child in ('a','b'):
            c.execute("INSERT INTO child_profile(child_id,nickname,grade,curriculum_region,status,created_at,updated_at) VALUES(?,?,'初三','江苏','active','old','old')",(child,child))
        c.commit()
    delivery=ReportDeliveryService(db,KEY); delivery.initialize()
    for child in ('a','b'): delivery.add_recipient(child,'email',ADDRESS,'synthetic',True)
    service.set_diagnostic_consent(ADDRESS,consented=True)
    return db,service,delivery


def test_joint_claim_is_atomic_and_sealed_for_both_children(tmp_path):
    db,service,_=setup_batch(tmp_path)
    result=service.create_combined_batch(date.today().isoformat(),ADDRESS,['b','a'],KEY)
    assert result['member_count']==2 and result['external_delivery_permitted'] is False
    assert service.inspect_combined_batch(result['batch_id'],KEY)['source_withdrawal_checked'] is False
    with db.connect() as c:
        assert [tuple(r) for r in c.execute('SELECT child_id,status FROM combined_diagnostic_member ORDER BY child_id')]==[('a','waiting'),('b','waiting')]
        assert tuple(c.execute('SELECT status,combined_batch_id FROM pilot_test_digest_outbox').fetchone())==('processing',result['batch_id'])
        assert ADDRESS not in str(tuple(c.execute('SELECT * FROM combined_diagnostic_batch').fetchone()))
    with pytest.raises(ValueError): service.create_combined_batch(date.today().isoformat(),ADDRESS,['a','b'],KEY)


def test_reconsent_does_not_revive_frozen_zip(tmp_path):
    db,service,_=setup_batch(tmp_path)
    batch=service.create_combined_batch(date.today().isoformat(),ADDRESS,['a','b'],KEY)['batch_id']
    service.set_diagnostic_consent(ADDRESS,consented=False)
    service.set_diagnostic_consent(ADDRESS,consented=True)
    with pytest.raises(ValueError,match='PILOT_JOINT_AUTHORITY_CHANGED'):
        service.inspect_combined_batch(batch,KEY)
    with db.connect() as c:
        assert c.execute('SELECT status FROM pilot_test_digest_outbox').fetchone()[0]=='processing'


def test_one_child_reconsent_invalidates_the_entire_batch(tmp_path):
    db,service,delivery=setup_batch(tmp_path)
    batch=service.create_combined_batch(date.today().isoformat(),ADDRESS,['a','b'],KEY)['batch_id']
    with db.connect() as c: recipient=c.execute("SELECT recipient_id FROM report_delivery_recipient WHERE child_id='a'").fetchone()[0]
    delivery.revoke_recipient(recipient)
    delivery.add_recipient('a','email',ADDRESS,'synthetic',True)
    with pytest.raises(ValueError): service.inspect_combined_batch(batch,KEY)


def test_restart_converts_only_processing_members_to_unknown(tmp_path):
    db,service,_=setup_batch(tmp_path)
    batch=service.create_combined_batch(date.today().isoformat(),ADDRESS,['a','b'],KEY)['batch_id']
    with db.connect() as c:
        c.execute("UPDATE combined_diagnostic_member SET status='processing',claim_token='t' WHERE child_id='a'"); c.commit()
    assert service.recover_combined_batches()==1
    assert service.recover_combined_batches()==0
    with db.connect() as c:
        assert [tuple(r) for r in c.execute('SELECT child_id,status FROM combined_diagnostic_member ORDER BY child_id')]==[('a','unknown'),('b','waiting')]
        assert c.execute('SELECT status FROM combined_diagnostic_batch').fetchone()[0]=='unknown'
        assert c.execute('SELECT status FROM pilot_test_digest_outbox').fetchone()[0]=='processing'


def test_parallel_claims_never_create_duplicate_batch(tmp_path):
    db,service,_=setup_batch(tmp_path)
    def run(_):
        try: return service.create_combined_batch(date.today().isoformat(),ADDRESS,['a','b'],KEY)['batch_id']
        except ValueError: return None
    with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(run,range(2)))
    assert len([x for x in results if x])==1
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM combined_diagnostic_batch').fetchone()[0]==1
        assert c.execute('SELECT count(*) FROM combined_diagnostic_member').fetchone()[0]==2


def test_authority_changed_while_zip_is_built_rolls_back_joint_claim(tmp_path,monkeypatch):
    import home_study_agent.pilot_test_telemetry as module
    db,service,_=setup_batch(tmp_path)
    original=module.build_daily_diagnostic_bundle
    def withdraw(*args,**kwargs):
        result=original(*args,**kwargs)
        service.set_diagnostic_consent(ADDRESS,consented=False)
        service.set_diagnostic_consent(ADDRESS,consented=True)
        return result
    monkeypatch.setattr(module,'build_daily_diagnostic_bundle',withdraw)
    with pytest.raises(ValueError,match='PILOT_JOINT_AUTHORITY_CHANGED'):
        service.create_combined_batch(date.today().isoformat(),ADDRESS,['a','b'],KEY)
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM combined_diagnostic_batch').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM pilot_test_digest_outbox').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM combined_diagnostic_member').fetchone()[0]==0


def test_installer_consent_is_idempotent_and_never_reauthorizes_revocation(tmp_path):
    db,service,_=setup_batch(tmp_path)
    def current():
        with db.connect() as c:
            return tuple(c.execute('SELECT status,authority_generation FROM pilot_diagnostic_recipient_consent WHERE recipient=?',(ADDRESS,)).fetchone())
    first=current(); service.import_provisioned_consent(ADDRESS)
    assert current()==first
    service.set_diagnostic_consent(ADDRESS,consented=False); revoked=current()
    assert revoked[0]=='revoked' and revoked[1]!=first[1]
    service.import_provisioned_consent(ADDRESS)
    assert current()==revoked
