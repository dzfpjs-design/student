"""Schema constants for the not-yet-enabled combined report ownership."""

PILOT_BASE_SCHEMA = """CREATE TABLE IF NOT EXISTS pilot_test_digest_outbox (
  delivery_date TEXT NOT NULL, recipient TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','processing','sent','failed','unknown')),
  attempt_count INTEGER NOT NULL DEFAULT 0,
  subject TEXT, body TEXT, digest_sha256 TEXT, transport TEXT, error_code TEXT,
  accepted_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY(delivery_date, recipient))"""

PILOT_CONSENT_SCHEMA = """CREATE TABLE IF NOT EXISTS pilot_diagnostic_recipient_consent (
 recipient TEXT PRIMARY KEY, status TEXT NOT NULL CHECK(status IN ('consented','revoked')),
 updated_at TEXT NOT NULL)"""

BATCH_SCHEMA = """CREATE TABLE IF NOT EXISTS combined_diagnostic_batch (
 batch_id TEXT PRIMARY KEY, delivery_date TEXT NOT NULL, recipient_identity TEXT NOT NULL,
 pilot_delivery_date TEXT NOT NULL, pilot_digest_sha256 TEXT NOT NULL, attachment_sha256 TEXT NOT NULL,
 membership_sha256 TEXT NOT NULL, expected_count INTEGER NOT NULL CHECK(expected_count>0),
 sealed INTEGER NOT NULL CHECK(sealed IN (0,1)),
 status TEXT NOT NULL CHECK(status IN ('preparing','active','unknown','sent')),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(delivery_date,recipient_identity)
)"""

INDEX_SCHEMA = """CREATE UNIQUE INDEX IF NOT EXISTS idx_pilot_digest_combined_batch
 ON pilot_test_digest_outbox(combined_batch_id) WHERE combined_batch_id IS NOT NULL"""

DELIVERY_SCHEMA = """CREATE TABLE IF NOT EXISTS combined_daily_delivery (
 identity TEXT PRIMARY KEY, batch_id TEXT NOT NULL, child_id TEXT NOT NULL,
 snapshot_id TEXT NOT NULL REFERENCES combined_daily_snapshot(snapshot_id) ON DELETE RESTRICT,
 status TEXT NOT NULL CHECK(status IN ('processing','unknown','sent')),
 claim_token TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(batch_id,child_id),
 FOREIGN KEY(batch_id,child_id) REFERENCES combined_diagnostic_member(batch_id,child_id) ON DELETE RESTRICT,
 FOREIGN KEY(identity) REFERENCES daily_observation_delivery_ledger(identity) ON DELETE RESTRICT
)"""


def schema_valid(c):
    from .pilot_test_telemetry import JOINT_TEST_MEMBER_SCHEMA
    from .daily_report_combined_snapshot import CANDIDATE_SCHEMA
    def normalize(value):
        return ' '.join(value.replace('IF NOT EXISTS ', '').split())
    for name, expected in (('combined_diagnostic_batch', BATCH_SCHEMA),
                           ('idx_pilot_digest_combined_batch', INDEX_SCHEMA),
                           ('combined_diagnostic_member', JOINT_TEST_MEMBER_SCHEMA),
                           ('combined_daily_snapshot', CANDIDATE_SCHEMA),
                           ('combined_daily_delivery', DELIVERY_SCHEMA)):
        row = c.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()
        if not row or not row[0] or normalize(row[0]) != normalize(expected):
            return False
    columns = {row[1]: row for row in c.execute('PRAGMA table_info(pilot_test_digest_outbox)')}
    column = columns.get('combined_batch_id')
    if not column or column[2].upper() != 'TEXT' or column[3] != 0:
        return False
    consent_columns = {row[1]: row for row in c.execute('PRAGMA table_info(pilot_diagnostic_recipient_consent)')}
    generation = consent_columns.get('authority_generation')
    if not generation or generation[2].upper() != 'TEXT':
        return False
    foreign = {(row[2],row[3],row[4],row[5].upper(),row[6].upper())
               for row in c.execute('PRAGMA foreign_key_list(pilot_test_digest_outbox)')}
    return foreign == {('combined_diagnostic_batch','combined_batch_id','batch_id','NO ACTION','NO ACTION')}
