"""Recipient-scoped fixed-test receipts; never submits mail."""
import hashlib
import hmac
import sqlite3

EMAIL_VERIFICATION_SCHEMA = """CREATE TABLE IF NOT EXISTS daily_report_email_verification (
 child_id TEXT NOT NULL, recipient_id TEXT NOT NULL, address_identity TEXT NOT NULL,
 credential_id TEXT NOT NULL, credential_updated_at TEXT NOT NULL, credential_ciphertext_sha256 TEXT NOT NULL,
 recipient_consented_at TEXT NOT NULL, secret_fingerprint TEXT NOT NULL, action_id TEXT NOT NULL UNIQUE,
 tested_at TEXT NOT NULL, PRIMARY KEY(child_id,recipient_id)
)"""


def schema_valid(c):
    row = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='daily_report_email_verification'").fetchone()
    return bool(row and ' '.join(row[0].replace('IF NOT EXISTS ', '').split()) == ' '.join(EMAIL_VERIFICATION_SCHEMA.replace('IF NOT EXISTS ', '').split()))


def secret_fingerprint(key, secret):
    if not isinstance(key, bytes) or len(key) != 32 or not isinstance(secret, str):
        raise ValueError('EMAIL_VERIFICATION_SECRET_UNAVAILABLE')
    return hmac.new(key, b'hsc-email-test-secret-v1\0' + secret.encode('utf-8'), hashlib.sha256).hexdigest()


def verified(c, child_id, recipient_id, encryption_key, runtime_password):
    """Fail closed unless exact child, address, consent, credential and action match."""
    if not c.in_transaction:
        raise ValueError('VERIFICATION_TRANSACTION_REQUIRED')
    try:
        row = c.execute('''SELECT v.*,r.child_id AS actual_child,r.channel,r.address,r.consent_status,r.consented_at,r.revoked_at,
            n.credential_id AS actual_credential,n.updated_at AS actual_updated,n.encrypted_json AS actual_ciphertext,
            a.child_id AS action_child,a.channel AS action_channel,a.status AS action_status
        FROM daily_report_email_verification v
        JOIN report_delivery_recipient r ON r.recipient_id=v.recipient_id
        JOIN notification_credential n ON n.channel='email'
        JOIN report_delivery_channel_test_action a ON a.client_action_id=v.action_id
        WHERE v.child_id=? AND v.recipient_id=?''', (child_id,recipient_id)).fetchone()
    except sqlite3.Error:
        return False
    if not row:
        return False
    if (row['actual_child'] != child_id or row['action_child'] != child_id or row['channel'] != 'email'
            or row['action_channel'] != 'email' or row['action_status'] != 'passed'
            or row['consent_status'] != 'consented' or row['revoked_at'] is not None
            or not row['consented_at'] or row['recipient_consented_at'] != row['consented_at']
            or not isinstance(row['address'], str) or row['address'].strip().casefold() != row['address_identity']
            or row['credential_id'] != row['actual_credential'] or row['credential_updated_at'] != row['actual_updated']):
        return False
    if not isinstance(row['actual_ciphertext'], str) or not isinstance(row['credential_ciphertext_sha256'], str) or not hmac.compare_digest(
            row['credential_ciphertext_sha256'], hashlib.sha256(row['actual_ciphertext'].encode('utf-8')).hexdigest()):
        return False
    try:
        current_secret_fingerprint = secret_fingerprint(encryption_key, runtime_password)
    except (ValueError, UnicodeError):
        return False
    if not isinstance(row['secret_fingerprint'], str) or not hmac.compare_digest(row['secret_fingerprint'], current_secret_fingerprint):
        return False
    # A later in-flight or uncertain fixed test must not silently authorize mail.
    return not bool(c.execute('''SELECT 1 FROM report_delivery_channel_test_action
      WHERE child_id=? AND channel='email' AND status IN ('processing','unknown','unknown_acknowledged') LIMIT 1''', (child_id,)).fetchone())
