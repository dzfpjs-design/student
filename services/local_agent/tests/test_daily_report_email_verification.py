"""Fixed-test identity and recovery; synthetic DB and in-process adapter only."""
from types import SimpleNamespace

from home_study_agent.daily_report_email_verification import EMAIL_VERIFICATION_SCHEMA, verified
from home_study_agent.notification_channels import ChannelDeliveryError
from home_study_agent.report_delivery import ReportDeliveryService
from test_daily_report_candidate import synthetic


class Fake:
    def __init__(self, callback=None, unknown=False): self.calls=[]; self.callback=callback; self.unknown=unknown
    def send(self, address, subject, text, html, config):
        self.calls.append(address)
        if self.callback: self.callback()
        if self.unknown: raise ChannelDeliveryError('UNKNOWN','unknown',delivery_unknown=True)
        return SimpleNamespace(provider_message_id=None)


def setup(synthetic):
    db,_=synthetic
    with db.connect() as c: c.execute(EMAIL_VERIFICATION_SCHEMA); c.commit()
    service=ReportDeliveryService(db,b's'*32); service.initialize()
    fake=Fake(); service.adapters['email']=fake
    service.save_channel_config('email',{'host':'127.0.0.1','port':25,'username':'synthetic','from_address':'x@example.test'})
    return db,service,fake


def check(db, child, recipient):
    with db.connect() as c:
        c.execute('BEGIN')
        try: return verified(c,child,recipient,b's'*32,'')
        finally: c.rollback()


def test_success_is_exact_recipient_child_and_credential(synthetic):
    db,service,fake=setup(synthetic)
    assert service.test_channel('a','email','test1')['status']=='passed'
    assert fake.calls==['parent@example.test']
    assert check(db,'a','recipient-a') and not check(db,'b','recipient-a') and not check(db,'b','recipient-b')
    assert next(item for item in service.channel_test_status('a') if item['channel']=='email')['verified_recipient_ids']==['recipient-a']
    with db.connect() as c:
        c.execute("UPDATE report_delivery_recipient SET address='other@example.test' WHERE recipient_id='recipient-a'"); c.commit()
    assert not check(db,'a','recipient-a')
    with db.connect() as c:
        c.execute("UPDATE report_delivery_recipient SET address='parent@example.test' WHERE recipient_id='recipient-a'"); c.commit()
    assert check(db,'a','recipient-a')
    service.save_channel_config('email',{'host':'127.0.0.1','port':25,'username':'new','from_address':'x@example.test'})
    assert not check(db,'a','recipient-a')


def test_resave_or_revoke_same_consent_invalidates(synthetic):
    db,service,_=setup(synthetic)
    assert service.test_channel('a','email','test1')['status']=='passed'
    service.add_recipient('a','email','parent@example.test','p',True)
    assert not check(db,'a','recipient-a')
    assert next(item for item in service.channel_test_status('a') if item['channel']=='email')['status']=='blocked'
    assert service.test_channel('a','email','test2')['status']=='passed'
    service.revoke_recipient('recipient-a')
    service.add_recipient('a','email','parent@example.test','p',True)
    assert not check(db,'a','recipient-a')


def test_unknown_and_late_success_never_record_passed(synthetic):
    db,service,_=setup(synthetic)
    service.adapters['email']=Fake(unknown=True)
    assert service.test_channel('a','email','unknown')['status']=='unknown'
    assert not check(db,'a','recipient-a')
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM daily_report_email_verification').fetchone()[0]==0
    service.keep_unknown_channel_test('a','email','unknown')
    def mark_unknown():
        with db.connect() as c:
            c.execute("UPDATE report_delivery_channel_test_action SET status='unknown' WHERE client_action_id='late'"); c.commit()
    service.adapters['email']=Fake(callback=mark_unknown)
    assert service.test_channel('a','email','late')['status']=='unknown'
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM daily_report_email_verification').fetchone()[0]==0


def test_restore_quarantine_erases_verification(synthetic):
    db,service,_=setup(synthetic)
    assert service.test_channel('a','email','test1')['status']=='passed'
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE'); service.quarantine_after_restore(c,'test-restore'); c.commit()
        assert c.execute('SELECT count(*) FROM daily_report_email_verification').fetchone()[0]==0
    assert not check(db,'a','recipient-a')


def test_password_and_ciphertext_change_without_version_are_rejected(synthetic):
    db,service,fake=setup(synthetic)
    service.save_channel_config('email',{'host':'127.0.0.1','port':25,'from_address':'x@example.test','password':'synthetic-old'})
    assert service.test_channel('a','email','secret-test')['status']=='passed'
    with db.connect() as c:
        c.execute('BEGIN')
        assert verified(c,'a','recipient-a',b's'*32,'synthetic-old')
        assert not verified(c,'a','recipient-a',b's'*32,'synthetic-new')
        c.rollback()
        row=c.execute('SELECT secret_fingerprint FROM daily_report_email_verification').fetchone()
        assert 'synthetic-old' not in row[0]
        c.execute("UPDATE notification_credential SET encrypted_json=encrypted_json || 'x' WHERE channel='email'")
        c.commit(); c.execute('BEGIN')
        assert not verified(c,'a','recipient-a',b's'*32,'synthetic-old')


def test_legacy_encrypted_password_is_bound_to_actual_config(synthetic):
    import base64, json, os
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    db,service,fake=setup(synthetic)
    secret='synthetic-legacy'
    config={'host':'127.0.0.1','port':25,'from_address':'x@example.test','password':secret}
    nonce=os.urandom(12)
    ciphertext=base64.b64encode(nonce+AESGCM(b's'*32).encrypt(nonce,json.dumps(config).encode(),b'hsc-notification-credential-v1')).decode()
    with db.connect() as c:
        c.execute("UPDATE notification_credential SET encrypted_json=? WHERE channel='email'", (ciphertext,)); c.commit()
    assert service.test_channel('a','email','legacy-test')['status']=='passed'
    with db.connect() as c:
        c.execute('BEGIN')
        assert verified(c,'a','recipient-a',b's'*32,secret)
        assert not verified(c,'a','recipient-a',b's'*32,'')


def test_explicit_reissue_success_resolves_only_original_unknown(synthetic):
    db,service,_=setup(synthetic)
    uncertain=Fake(unknown=True); service.adapters['email']=uncertain
    assert service.test_channel('a','email','u1')['status']=='unknown'
    service.keep_unknown_channel_test('a','email','u1')
    successful=Fake(); service.adapters['email']=successful
    assert service.reissue_unknown_channel_test('a','email','u1','replacement')['status']=='passed'
    assert successful.calls==['parent@example.test']
    assert check(db,'a','recipient-a')
    with db.connect() as c:
        original=c.execute("SELECT status,replacement_action_id FROM report_delivery_channel_test_action WHERE client_action_id='u1'").fetchone()
        assert tuple(original)==('superseded','replacement')
    assert service.reissue_unknown_channel_test('a','email','u1','replacement')['status']=='passed'
    assert len(successful.calls)==1


def test_explicit_reissue_does_not_ignore_other_unresolved_unknown(synthetic):
    db,service,_=setup(synthetic)
    service.adapters['email']=Fake(unknown=True)
    assert service.test_channel('a','email','u1')['status']=='unknown'
    assert service.test_channel('a','email','u2')['status']=='unknown'
    service.adapters['email']=Fake()
    assert service.reissue_unknown_channel_test('a','email','u1','replacement')['status']=='passed'
    assert not check(db,'a','recipient-a')
    service.keep_unknown_channel_test('a','email','u2')
    assert not check(db,'a','recipient-a')
