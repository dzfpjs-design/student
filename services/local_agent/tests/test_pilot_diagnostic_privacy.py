"""Synthetic records only; no SMTP connection or external submission."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Event
import json

import pytest

from test_pilot_test_telemetry import service_for, seed_activity
from home_study_agent.report_delivery import ReportDeliveryService

RECIPIENT = 'guardian@example.test'
NOW = datetime(2026, 9, 26, 5, tzinfo=timezone.utc)


def test_daily_body_omits_child_identity_and_both_sides_of_dialogue(tmp_path):
    db, service = service_for(tmp_path); seed_activity(db)
    claim = service.claim_due(RECIPIENT, now=NOW)
    assert claim is not None
    assert '初三试用孩子' not in claim.body
    assert '我想请你看这道题' not in claim.body
    assert '好，我先看你做到哪一步' not in claim.body
    assert '语音/Agent 回合' in claim.body and 'completed：1 条' in claim.body
    assert claim.window_policy['privacy_body_version'] == 2


def test_legacy_failed_frozen_body_is_held_for_review_without_resending(tmp_path):
    db, service = service_for(tmp_path); seed_activity(db)
    claim = service.claim_due(RECIPIENT, now=NOW)
    with db.connect() as c:
        row = dict(c.execute('SELECT * FROM pilot_test_digest_outbox WHERE delivery_date=?', (claim.delivery_date,)).fetchone())
        policy = json.loads(row['window_policy_json']); policy.pop('privacy_body_version')
        row['window_policy_json'] = service._canonical(policy)
        row['body'] = '历史孩子原话，测试用'
        digest = service._digest(row)
        c.execute("""UPDATE pilot_test_digest_outbox SET window_policy_json=?,body=?,digest_sha256=?,
            status='failed',submission_token=NULL WHERE delivery_date=? AND recipient=?""",
            (row['window_policy_json'], row['body'], digest, claim.delivery_date, claim.recipient))
        c.commit()
    assert service.claim_due(RECIPIENT, now=NOW) is None
    with db.connect() as c:
        row = c.execute('SELECT status,error_code,attempt_count FROM pilot_test_digest_outbox WHERE delivery_date=?', (claim.delivery_date,)).fetchone()
    assert row['status'] == 'unknown' and row['error_code'] == 'PILOT_LEGACY_BODY_REVIEW_REQUIRED'
    assert row['attempt_count'] == 1
    assert service.claim_due(RECIPIENT, now=NOW) is None


def test_old_processing_frozen_body_cannot_start_submission(tmp_path):
    db, service = service_for(tmp_path); seed_activity(db)
    claim = service.claim_due(RECIPIENT, now=NOW)
    with db.connect() as c:
        row = dict(c.execute('SELECT * FROM pilot_test_digest_outbox WHERE delivery_date=?', (claim.delivery_date,)).fetchone())
        policy = json.loads(row['window_policy_json']); policy.pop('privacy_body_version')
        row['window_policy_json'] = service._canonical(policy)
        digest = service._digest(row)
        c.execute('UPDATE pilot_test_digest_outbox SET window_policy_json=?,digest_sha256=? WHERE delivery_date=?', (row['window_policy_json'], digest, claim.delivery_date))
        c.commit()
    with pytest.raises(ValueError, match='PILOT_LEGACY_BODY_REVIEW_REQUIRED'):
        service.reserve_submission(claim.delivery_date, claim.recipient, digest, claim.claim_token)
    with db.connect() as c:
        assert c.execute('SELECT submission_token FROM pilot_test_digest_outbox WHERE delivery_date=?', (claim.delivery_date,)).fetchone()[0] is None


@pytest.mark.parametrize('operation', ['revoke', 'pending', 'minimal'])
def test_disclosure_changes_wait_for_owned_smtp_guard(tmp_path, operation):
    db, _ = service_for(tmp_path); seed_activity(db)
    delivery = ReportDeliveryService(db, b'0'*32); delivery.initialize()
    child = 'child-junior-3'
    recipient = delivery.list_recipients(child)[0]
    started = Event()
    def mutate():
        started.set()
        if operation == 'revoke':
            return delivery.revoke_recipient(recipient['recipient_id'])
        if operation == 'pending':
            return delivery.add_recipient(child, 'email', RECIPIENT, '合成家长', False)
        return delivery.update_preferences(child, '21:00', '07:00', 90, 'minimal')
    with ThreadPoolExecutor(max_workers=1) as pool:
        with db.external_effect_guard(timeout=2):
            future = pool.submit(mutate)
            assert started.wait(1)
            assert not future.done()
        result = future.result(timeout=3)
    if operation == 'minimal':
        assert result['redaction_level'] == 'minimal'
    else:
        assert result['consent_status'] == ('revoked' if operation == 'revoke' else 'pending')


def test_recipient_change_wait_is_bounded_and_does_not_modify_on_timeout(tmp_path, monkeypatch):
    from home_study_agent.report_delivery import DeliveryStateError
    db, _ = service_for(tmp_path); seed_activity(db)
    delivery = ReportDeliveryService(db, b'0'*32); delivery.initialize()
    recipient = delivery.list_recipients('child-junior-3')[0]
    original = db.external_effect_guard
    with ThreadPoolExecutor(max_workers=1) as pool:
        with original(timeout=2):
            monkeypatch.setattr(db, 'external_effect_guard', lambda timeout=120: original(timeout=.05))
            future = pool.submit(delivery.revoke_recipient, recipient['recipient_id'])
            with pytest.raises(DeliveryStateError, match='REPORT_DELIVERY_BUSY'):
                future.result(timeout=2)
    assert delivery.get_recipient(recipient['recipient_id'])['consent_status'] == 'consented'


def test_legacy_failed_attempt_without_frozen_body_is_not_reissued(tmp_path):
    db, service = service_for(tmp_path); seed_activity(db)
    claim = service.claim_due(RECIPIENT, now=NOW)
    with db.connect() as c:
        c.execute("""UPDATE pilot_test_digest_outbox SET status='failed',body=NULL,
            digest_sha256=NULL,attachment_content=NULL,submission_token=NULL WHERE delivery_date=?""", (claim.delivery_date,))
        c.commit()
    assert service.claim_due(RECIPIENT, now=NOW) is None
    with db.connect() as c:
        row = c.execute('SELECT status,error_code,attempt_count FROM pilot_test_digest_outbox WHERE delivery_date=?', (claim.delivery_date,)).fetchone()
    assert row['status'] == 'unknown' and row['error_code'] == 'PILOT_LEGACY_BODY_REVIEW_REQUIRED'
    assert row['attempt_count'] == 1


def test_all_diagnostic_category_fields_reject_free_form_secrets_and_speech(tmp_path):
    import io
    import zipfile
    db, service = service_for(tmp_path); seed_activity(db)
    private = 'sk-SYNTHETIC-孩子原话-PRIVATE-SENTINEL'
    with db.connect() as c:
        c.execute('PRAGMA ignore_check_constraints=ON')
        c.execute('UPDATE model_call SET provider=?,model=?,purpose=?,status=?,error_code=?', (private,)*5)
        c.execute('UPDATE agent_turn SET tool_status=?,intent=?,transcript=?,response_text=?', (private,)*4)
        c.execute("""INSERT INTO device_incident(incident_id,child_id,session_id,device_type,error_code,message,status,occurred_at,resolved_at,client_action_id)
            VALUES('private-incident','child-junior-3','session-seed-1',?,?,?,?, '2026-09-25T12:30:00+00:00',?,'private-incident-action')""", (private,private,private,private,private))
        c.execute("""INSERT INTO audit_event(audit_id,actor,action,child_id,summary_json,occurred_at)
            VALUES('private-audit','system',?,'child-junior-3','{}','2026-09-25T12:30:00+00:00')""", (private,))
        c.commit()
    log_dir = tmp_path / 'diagnostics' / 'logs'; log_dir.mkdir(parents=True)
    (log_dir/'runtime.jsonl').write_text(
        json.dumps({'timestamp':'2026-09-25T12:30:00Z','level':private,'source':private,'event':private},ensure_ascii=False)
        + '\n[]\n' + json.dumps({'timestamp':'2026-09-25T12:30:00','event':private},ensure_ascii=False) + '\n')
    body = service.build_digest(datetime(2026, 9, 25).date())
    assert private not in body and 'other' in body
    claim = service.claim_due(RECIPIENT, now=NOW)
    assert claim is not None and private not in claim.body
    with zipfile.ZipFile(io.BytesIO(claim.attachments[0].content)) as archive:
        assert all(private.encode('utf-8') not in archive.read(info) for info in archive.infolist())
