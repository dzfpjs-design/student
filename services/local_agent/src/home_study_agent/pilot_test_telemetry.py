from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
import hmac
import json
import re
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from .database import Database
from .daily_diagnostic_bundle import build_daily_diagnostic_bundle
from .notification_channels import EmailAttachment, validate_email_attachments
from .daily_diagnostic_schema import EVENTS as DIAGNOSTIC_EVENTS


PILOT_TIMEZONE = ZoneInfo("Asia/Shanghai")
JOINT_TEST_MEMBER_SCHEMA = """CREATE TABLE combined_diagnostic_member (
 batch_id TEXT NOT NULL REFERENCES combined_diagnostic_batch(batch_id) ON DELETE RESTRICT,
 child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE RESTRICT,
 status TEXT NOT NULL CHECK(status IN ('waiting','processing','sent','unknown')),
 claim_token TEXT, updated_at TEXT NOT NULL, PRIMARY KEY(batch_id,child_id))"""


@dataclass(frozen=True)
class PilotDigestClaim:
    delivery_date: str
    recipient: str
    subject: str
    body: str = field(repr=False)
    digest_sha256: str
    attachments: tuple[EmailAttachment, ...] = field(default=(), repr=False)
    attachment_manifest: dict | None = None
    window_start_utc: str | None = None
    window_end_utc: str | None = None
    window_policy: dict | None = None
    # Server-owned attempt capability. Never returned by the public /due DTO.
    submission_token: str | None = field(default=None, repr=False)
    purpose: str = "daily"
    client_action_id: str | None = None
    claim_token: str | None = field(default=None, repr=False)


class PilotSubmissionReplay(ValueError):
    """An already-owned/terminal verification action is a receipt, never new SMTP permission."""
    def __init__(self, result: dict):
        super().__init__("PILOT_VERIFICATION_ACTION_REPLAY")
        self.result = result


class PilotTestTelemetryService:
    """Build and durably coordinate a daily family-test diagnostic email."""

    def __init__(self, database: Database, data_dir: Path):
        self.database = database
        self.data_dir = Path(data_dir)

    _DAILY_TABLE = "pilot_test_digest_outbox"
    _VERIFICATION_TABLE = "pilot_test_attachment_verification_outbox"
    _COMBINED_RESERVATION = 'COMBINED_DELIVERY_RESERVED'
    _ATTACHMENT_COLUMNS = {
        "attachment_filename": "TEXT", "attachment_content": "BLOB",
        "attachment_sha256": "TEXT", "attachment_manifest_json": "TEXT",
        "window_start_utc": "TEXT", "window_end_utc": "TEXT", "window_policy_json": "TEXT",
        "submission_token": "TEXT", "submission_started_at": "TEXT",
    }
    _PUBLIC_RESULT = ("delivery_date", "recipient", "status", "attempt_count", "digest_sha256",
                      "transport", "error_code", "accepted_at", "updated_at")

    @staticmethod
    def _require_combined_schema(connection) -> None:
        """Never interpret a missing or broken joint-delivery link as unbound."""
        from .daily_report_combined_schema import schema_valid
        if not schema_valid(connection):
            raise ValueError('PILOT_COMBINED_SCHEMA_UNAVAILABLE')
        if connection.execute('''SELECT 1 FROM combined_diagnostic_batch b
             LEFT JOIN pilot_test_digest_outbox p ON p.combined_batch_id=b.batch_id
             WHERE p.combined_batch_id IS NULL OR p.delivery_date!=b.pilot_delivery_date
                OR p.digest_sha256 IS NOT b.pilot_digest_sha256
                OR p.attachment_sha256 IS NOT b.attachment_sha256 LIMIT 1''').fetchone():
            raise ValueError('PILOT_COMBINED_LINK_UNCONFIRMED')
        if connection.execute('''SELECT 1 FROM pilot_test_digest_outbox p
             LEFT JOIN combined_diagnostic_batch b ON b.batch_id=p.combined_batch_id
             WHERE p.combined_batch_id IS NOT NULL AND b.batch_id IS NULL LIMIT 1''').fetchone():
            raise ValueError('PILOT_COMBINED_LINK_UNCONFIRMED')

    @classmethod
    def _require_independent_daily(cls, connection, row) -> None:
        cls._require_combined_schema(connection)
        if row is not None and row['combined_batch_id'] is not None:
            raise ValueError('PILOT_COMBINED_DELIVERY_OWNED')

    def initialize(self) -> None:
        now = self._utc_now()
        with self.database.connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS pilot_test_digest_outbox (
                  delivery_date TEXT NOT NULL, recipient TEXT NOT NULL,
                  status TEXT NOT NULL CHECK(status IN ('pending','processing','sent','failed','unknown')),
                  attempt_count INTEGER NOT NULL DEFAULT 0,
                  subject TEXT, body TEXT, digest_sha256 TEXT, transport TEXT, error_code TEXT,
                  accepted_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                  PRIMARY KEY(delivery_date, recipient));
                CREATE INDEX IF NOT EXISTS idx_pilot_test_digest_status
                  ON pilot_test_digest_outbox(status, delivery_date);
            """)
            connection.execute("""CREATE TABLE IF NOT EXISTS pilot_test_attachment_verification_outbox (
                client_action_id TEXT PRIMARY KEY,delivery_date TEXT NOT NULL,recipient TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending','processing','sent','failed','unknown')),
                attempt_count INTEGER NOT NULL DEFAULT 0,subject TEXT,body TEXT,digest_sha256 TEXT,
                transport TEXT,error_code TEXT,accepted_at TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)""")
            connection.execute("""CREATE TABLE IF NOT EXISTS pilot_diagnostic_recipient_consent (
                recipient TEXT PRIMARY KEY, status TEXT NOT NULL CHECK(status IN ('consented','revoked')),
                updated_at TEXT NOT NULL)""")
            connection.execute("""CREATE TABLE IF NOT EXISTS pilot_diagnostic_recipient_config (
                id INTEGER PRIMARY KEY CHECK(id=1), recipient TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            daily_columns = {row['name'] for row in connection.execute('PRAGMA table_info(pilot_test_digest_outbox)')}
            if 'claim_token' not in daily_columns:
                connection.execute('ALTER TABLE pilot_test_digest_outbox ADD COLUMN claim_token TEXT')
            for table in (self._DAILY_TABLE,self._VERIFICATION_TABLE):
                columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
                for name, sql_type in self._ATTACHMENT_COLUMNS.items():
                    if name not in columns:
                        connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")
                if table == self._DAILY_TABLE:
                    try:
                        self._require_combined_schema(connection)
                    except ValueError:
                        # Preserve all daily rows until migration/recovery can verify ownership.
                        continue
                    connection.execute("""UPDATE pilot_test_digest_outbox SET status='failed', error_code='CLAIM_ABANDONED_BEFORE_SUBMISSION',updated_at=?
                        WHERE status='processing' AND combined_batch_id IS NULL AND claim_token IS NOT NULL AND submission_token IS NULL AND attachment_content IS NOT NULL""", (now,))
                    connection.execute("""UPDATE pilot_test_digest_outbox SET status='unknown',error_code='DELIVERY_RESULT_UNKNOWN_AFTER_RESTART',updated_at=?
                        WHERE status='processing' AND combined_batch_id IS NULL""",(now,))
                else:
                    connection.execute(f"""UPDATE {table}
                        SET status='unknown',error_code='DELIVERY_RESULT_UNKNOWN_AFTER_RESTART',updated_at=?
                        WHERE status='processing'""",(now,))
            connection.commit()

    @staticmethod
    def _canonical(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)

    @staticmethod
    def _aware(value: str) -> datetime:
        if type(value) is not str or len(value) > 80:
            raise ValueError("PILOT_ATTACHMENT_WINDOW_INVALID")
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if result.tzinfo is None or result.utcoffset() is None:
                raise ValueError("AWARE_TIMESTAMP_REQUIRED")
            return result.astimezone(timezone.utc)
        except (ValueError, OverflowError):
            raise ValueError("PILOT_ATTACHMENT_WINDOW_INVALID") from None

    def _digest(self, row) -> str:
        return sha256(self._canonical({
            "schema": "pilot_digest_with_runtime_zip_v1",
            "purpose": "runtime_log_attachment_verification" if "client_action_id" in row.keys() else "daily",
            "client_action_id": row["client_action_id"] if "client_action_id" in row.keys() else None,
            "delivery_date": row["delivery_date"], "recipient": row["recipient"],
            "subject": row["subject"], "body": row["body"],
            "attachment_filename": row["attachment_filename"],
            "attachment_sha256": row["attachment_sha256"],
            "attachment_manifest": json.loads(row["attachment_manifest_json"]),
            "window_start_utc": row["window_start_utc"], "window_end_utc": row["window_end_utc"],
            "window_policy": json.loads(row["window_policy_json"]),
        }).encode("utf-8")).hexdigest()

    def _frozen_claim(self, row) -> PilotDigestClaim:
        if not row["attachment_content"] or not row["attachment_filename"]:
            raise ValueError("PILOT_LEGACY_ATTACHMENT_SNAPSHOT_MISSING")
        try:
            content = row["attachment_content"]
            manifest = json.loads(row["attachment_manifest_json"])
            window_policy = json.loads(row["window_policy_json"])
            if type(content) is not bytes or type(manifest) is not dict or type(window_policy) is not dict:
                raise ValueError("PILOT_ATTACHMENT_INVALID")
            attachment = EmailAttachment(row["attachment_filename"], content)
            validate_email_attachments((attachment,))
            if sha256(content).hexdigest() != row["attachment_sha256"]:
                raise ValueError("PILOT_ATTACHMENT_HASH_MISMATCH")
            start = self._aware(row["window_start_utc"]); end = self._aware(row["window_end_utc"])
            if end <= start or not row["subject"] or not row["body"]:
                raise ValueError("PILOT_ATTACHMENT_INVALID")
            if self._digest(row) != row["digest_sha256"]:
                raise ValueError("PILOT_DIGEST_CLAIM_MISMATCH")
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise ValueError("PILOT_ATTACHMENT_INVALID") from error
        return PilotDigestClaim(row["delivery_date"],row["recipient"],row["subject"],row["body"],
            row["digest_sha256"],(attachment,),manifest,row["window_start_utc"],row["window_end_utc"],
            window_policy,row["submission_token"],
            "runtime_log_attachment_verification" if "client_action_id" in row.keys() else "daily",
            row["client_action_id"] if "client_action_id" in row.keys() else None,
            row["claim_token"] if "claim_token" in row.keys() else None)

    def _require_recipient(self, connection, recipient: str) -> None:
        canonical=recipient.strip().casefold()
        matches=[]
        if self._table_exists(connection,"report_delivery_recipient"):
            matches=[row for row in connection.execute("SELECT address,consent_status,revoked_at FROM report_delivery_recipient WHERE channel='email'")
                     if type(row["address"])is str and row["address"].strip().casefold()==canonical]
        if any(row["consent_status"]=="revoked" or row["revoked_at"]is not None for row in matches):
            raise ValueError("PILOT_RECIPIENT_REVOKED")
        consent = connection.execute("SELECT status FROM pilot_diagnostic_recipient_consent WHERE recipient=?", (canonical,)).fetchone()
        if consent is not None:
            if consent["status"] == "consented":
                return
            raise ValueError("PILOT_RECIPIENT_REVOKED")
        # A legacy backup may not contain the consent table at quarantine time.
        # Historical sent rows from that backup must not silently reauthorize it.
        if self._table_exists(connection, 'audit_event') and connection.execute(
                "SELECT 1 FROM audit_event WHERE action='report_delivery_restore_quarantined' LIMIT 1").fetchone():
            raise ValueError("PILOT_RECIPIENT_NOT_AUTHORIZED")
        # Child-scoped learning-report consent is not authority to disclose the family-wide pilot digest.
        for row in connection.execute("SELECT recipient,delivery_date FROM pilot_test_digest_outbox WHERE status='sent'"):
            try:
                actual=date.fromisoformat(row["delivery_date"])
                valid_day=actual.isoformat()==row["delivery_date"]
            except (ValueError,TypeError):
                valid_day=False
            if valid_day and type(row["recipient"])is str and row["recipient"].strip().casefold()==canonical:
                return
        raise ValueError("PILOT_RECIPIENT_NOT_AUTHORIZED")

    @staticmethod
    def _persisted_identity(connection) -> str:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('pilot_diagnostic_recipient_config','pilot_diagnostic_recipient_consent','pilot_test_digest_outbox')")}
        # A restored old database may not yet have been initialized. Never
        # infer authority from it while the consent table is missing.
        if 'pilot_diagnostic_recipient_consent' not in tables:
            return ''
        selected = connection.execute('SELECT recipient FROM pilot_diagnostic_recipient_config WHERE id=1').fetchone() if 'pilot_diagnostic_recipient_config' in tables else None
        if selected is not None:
            candidate = selected['recipient']
            return candidate if isinstance(candidate, str) and candidate and candidate.count('@') == 1 and len(candidate) <= 320 and not any(c.isspace() for c in candidate) else ''
        candidates = set()
        for row in connection.execute("SELECT recipient FROM pilot_diagnostic_recipient_consent"):
            if isinstance(row['recipient'], str):
                candidates.add(row['recipient'].strip().casefold())
        if 'pilot_test_digest_outbox' in tables:
            for row in connection.execute("SELECT DISTINCT recipient FROM pilot_test_digest_outbox WHERE status='sent'"):
                if isinstance(row['recipient'], str):
                    candidates.add(row['recipient'].strip().casefold())
        if len(candidates) != 1:
            return ''
        candidate = next(iter(candidates))
        return candidate if candidate and candidate.count('@') == 1 and len(candidate) <= 320 and not any(c.isspace() for c in candidate) else ''

    def resolve_persisted_identity(self) -> str:
        """Return a single historical identity without granting it any authority."""
        with self.database.connect() as connection:
            return self._persisted_identity(connection)

    def resolve_persisted_recipient(self) -> str:
        """Recover only an unambiguous, currently authorized recipient.

        SMTP destinations and child-scoped consent do not authorize family logs.
        A revoked or ambiguous historical identity can never cause a new target
        to be selected silently.
        """
        with self.database.connect() as connection:
            candidate = self._persisted_identity(connection)
            if not candidate:
                return ''
            try:
                self._require_recipient(connection, candidate)
            except ValueError:
                return ''
        return candidate

    def _require_selected_if_explicit(self, connection, recipient: str) -> None:
        if not self._table_exists(connection, 'pilot_diagnostic_recipient_config'):
            return
        selected = connection.execute('SELECT recipient FROM pilot_diagnostic_recipient_config WHERE id=1').fetchone()
        if selected is not None and (not isinstance(selected['recipient'], str) or selected['recipient'].strip().casefold() != recipient.strip().casefold()):
            raise ValueError('PILOT_RECIPIENT_SELECTION_CHANGED')

    def explicit_selection_matches(self, recipient: str) -> bool:
        with self.database.connect() as connection:
            try:
                self._require_selected_if_explicit(connection, recipient)
            except ValueError:
                return False
            return True

    @staticmethod
    def _ensure_consent_authority_schema(connection) -> None:
        # Match schema 96 even if an older backup is restored while this
        # service is still running. Only existing explicit rows get epochs;
        # never infer consent from historical delivery receipts.
        connection.execute("""CREATE TABLE IF NOT EXISTS pilot_diagnostic_recipient_consent (
            recipient TEXT PRIMARY KEY, status TEXT NOT NULL CHECK(status IN ('consented','revoked')),
            updated_at TEXT NOT NULL)""")
        columns = {row['name']: row for row in connection.execute('PRAGMA table_info(pilot_diagnostic_recipient_consent)')}
        if not {'recipient', 'status', 'updated_at'}.issubset(columns):
            raise ValueError('PILOT_CONSENT_SCHEMA_UNAVAILABLE')
        generation = columns.get('authority_generation')
        if generation is None:
            connection.execute('ALTER TABLE pilot_diagnostic_recipient_consent ADD COLUMN authority_generation TEXT')
            connection.execute("UPDATE pilot_diagnostic_recipient_consent SET authority_generation=lower(hex(randomblob(16)))")
        elif str(generation['type']).upper() != 'TEXT':
            raise ValueError('PILOT_CONSENT_SCHEMA_UNAVAILABLE')

    @staticmethod
    def _validated_config_address(value: str) -> str:
        recipient = value.strip().casefold()
        if len(recipient) > 320 or not re.fullmatch(r'[^\s@]{1,64}@[^\s@.]+(?:\.[^\s@.]+)+', recipient):
            raise ValueError('PILOT_RECIPIENT_INVALID')
        return recipient

    def configure_diagnostic_recipient(self, recipient: str, confirmation: str) -> dict:
        recipient = self._validated_config_address(recipient)
        if recipient != self._validated_config_address(confirmation):
            raise ValueError('PILOT_RECIPIENT_CONFIRMATION_MISMATCH')
        with self.database.external_effect_guard(timeout=10), self.database.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            previous = self._persisted_identity(connection)
            # A restore from before this feature may not contain the selection
            # or consent table; create empty tables while the parent explicitly
            # configures the address, without inheriting any send authority.
            connection.execute("""CREATE TABLE IF NOT EXISTS pilot_diagnostic_recipient_config (
                id INTEGER PRIMARY KEY CHECK(id=1), recipient TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            self._ensure_consent_authority_schema(connection)
            if previous != recipient:
                # Selecting an address is not consent, even when old records
                # contain a grant or legacy sent receipt for that address.
                columns = {row['name'] for row in connection.execute('PRAGMA table_info(pilot_diagnostic_recipient_consent)')}
                prior = {recipient}
                if previous:
                    prior.add(previous)
                for row in connection.execute('SELECT recipient FROM pilot_diagnostic_recipient_consent'):
                    if isinstance(row['recipient'], str) and row['recipient'].strip():
                        prior.add(row['recipient'].strip().casefold())
                if self._table_exists(connection, 'pilot_test_digest_outbox'):
                    for row in connection.execute("SELECT DISTINCT recipient FROM pilot_test_digest_outbox WHERE status='sent'"):
                        if isinstance(row['recipient'], str) and row['recipient'].strip():
                            prior.add(row['recipient'].strip().casefold())
                for address in prior:
                    if 'authority_generation' in columns:
                        connection.execute("""INSERT INTO pilot_diagnostic_recipient_consent(recipient,status,updated_at,authority_generation)
                            VALUES(?,'revoked',?,?) ON CONFLICT(recipient) DO UPDATE SET status='revoked',updated_at=excluded.updated_at,
                            authority_generation=excluded.authority_generation""", (address, self._utc_now(), uuid4().hex))
                    else:
                        connection.execute("""INSERT INTO pilot_diagnostic_recipient_consent(recipient,status,updated_at)
                            VALUES(?,'revoked',?) ON CONFLICT(recipient) DO UPDATE SET status='revoked',updated_at=excluded.updated_at""",
                            (address, self._utc_now()))
            connection.execute("""INSERT INTO pilot_diagnostic_recipient_config(id,recipient,updated_at) VALUES(1,?,?)
                ON CONFLICT(id) DO UPDATE SET recipient=excluded.recipient,updated_at=excluded.updated_at""", (recipient, self._utc_now()))
            connection.commit()
        return {'recipient': recipient}

    def set_diagnostic_consent(self, recipient: str, *, consented: bool, require_selected: bool = False) -> dict:
        recipient = recipient.strip().casefold()
        if not recipient or recipient.count('@') != 1 or len(recipient) > 320 or any(c.isspace() for c in recipient):
            raise ValueError("PILOT_RECIPIENT_INVALID")
        status = 'consented' if consented else 'revoked'
        with self.database.external_effect_guard(timeout=10):
            with self.database.connect() as connection:
                connection.execute('BEGIN IMMEDIATE')
                self._ensure_consent_authority_schema(connection)
                if require_selected and self._persisted_identity(connection) != recipient:
                    raise ValueError('PILOT_RECIPIENT_NOT_CONFIGURED')
                columns={row['name'] for row in connection.execute('PRAGMA table_info(pilot_diagnostic_recipient_consent)')}
                if 'authority_generation' in columns:
                    connection.execute("""INSERT INTO pilot_diagnostic_recipient_consent(recipient,status,updated_at,authority_generation)
                        VALUES(?,?,?,?) ON CONFLICT(recipient) DO UPDATE SET status=excluded.status,updated_at=excluded.updated_at,
                        authority_generation=excluded.authority_generation""", (recipient,status,self._utc_now(),uuid4().hex))
                else:
                    connection.execute("""INSERT INTO pilot_diagnostic_recipient_consent(recipient,status,updated_at)
                        VALUES(?,?,?) ON CONFLICT(recipient) DO UPDATE SET status=excluded.status,updated_at=excluded.updated_at""",
                        (recipient, status, self._utc_now()))
                connection.commit()
        return {'recipient': recipient, 'status': status}

    def import_provisioned_consent(self, recipient: str) -> bool:
        recipient = recipient.strip().casefold()
        if not recipient or recipient.count('@') != 1 or len(recipient) > 320 or any(c.isspace() for c in recipient):
            raise ValueError('PILOT_RECIPIENT_INVALID')
        with self.database.external_effect_guard(timeout=10), self.database.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            # Restored data must never regain authority from an installer artifact.
            if connection.execute("SELECT 1 FROM audit_event WHERE action='report_delivery_restore_quarantined' LIMIT 1").fetchone():
                connection.rollback()
                return False
            self._ensure_consent_authority_schema(connection)
            columns={row['name'] for row in connection.execute('PRAGMA table_info(pilot_diagnostic_recipient_consent)')}
            if 'authority_generation' in columns:
                connection.execute("INSERT OR IGNORE INTO pilot_diagnostic_recipient_consent(recipient,status,updated_at,authority_generation) VALUES(?,'consented',?,?)", (recipient,self._utc_now(),uuid4().hex))
            else:
                connection.execute("INSERT OR IGNORE INTO pilot_diagnostic_recipient_consent(recipient,status,updated_at) VALUES(?,'consented',?)", (recipient,self._utc_now()))
            connection.commit()
            return True

    def diagnostic_status(self, recipient: str) -> dict:
        recipient = recipient.strip().casefold()
        with self.database.connect() as connection:
            blocked_children = []
            if self._table_exists(connection, 'report_delivery_recipient'):
                blocked_children = list(dict.fromkeys(row['nickname'] for row in connection.execute("""SELECT r.address,r.consent_status,r.revoked_at,c.nickname
                    FROM report_delivery_recipient r JOIN child_profile c ON c.child_id=r.child_id WHERE r.channel='email'""")
                    if type(row['address']) is str and row['address'].strip().casefold() == recipient
                    and (row['consent_status'] == 'revoked' or row['revoked_at'] is not None)))
            try:
                self._require_recipient(connection, recipient)
                consent = 'consented'
            except ValueError as error:
                if str(error) not in ('PILOT_RECIPIENT_REVOKED', 'PILOT_RECIPIENT_NOT_AUTHORIZED'):
                    raise
                consent = 'revoked' if str(error) == 'PILOT_RECIPIENT_REVOKED' else 'not_authorized'
            row = connection.execute("SELECT delivery_date,status FROM pilot_test_digest_outbox WHERE lower(trim(recipient))=? ORDER BY delivery_date DESC LIMIT 1", (recipient,)).fetchone()
            latest = {'date': row['delivery_date'], 'status': row['status']} if row else None
        return {'recipient': recipient, 'consent': consent, 'latest': latest, 'blocked_children': blocked_children}

    def _owned_scope(self, claim: PilotDigestClaim):
        if claim.purpose=="daily" and claim.client_action_id is None:
            return self._DAILY_TABLE,"delivery_date=? AND recipient=?",(claim.delivery_date,claim.recipient)
        if claim.purpose=="runtime_log_attachment_verification" and claim.client_action_id:
            return self._VERIFICATION_TABLE,"client_action_id=? AND delivery_date=? AND recipient=?",(claim.client_action_id,claim.delivery_date,claim.recipient)
        raise ValueError("PILOT_SUBMISSION_PURPOSE_INVALID")

    def _window(self, connection, recipient: str, target: date, send_hour: int):
        end = datetime.combine(target, time(send_hour), PILOT_TIMEZONE).astimezone(timezone.utc)
        default_start = end - timedelta(days=1)
        sent = [row for row in connection.execute("SELECT delivery_date,recipient,window_end_utc FROM pilot_test_digest_outbox WHERE status='sent'")
                if row["recipient"].strip().casefold()==recipient.strip().casefold()]
        ends = [self._aware(row["window_end_utc"]) for row in sent if row["window_end_utc"]]
        policy = {"kind": "daily_24h", "first_legacy_date": None, "offline_catchup": False}
        if ends:
            last = max(ends)
            if last >= end:
                return None
            start = min(default_start, last)
            policy.update(kind="successful_attachment_cursor",offline_catchup=last < default_start)
        else:
            legacy = []
            for row in sent:
                try:
                    d = date.fromisoformat(row["delivery_date"])
                    if d.isoformat() == row["delivery_date"] and d < target:
                        legacy.append(d)
                except (ValueError, TypeError):
                    continue
            if legacy:
                d = max(legacy)
                start = min(default_start,datetime.combine(d,time.min,PILOT_TIMEZONE).astimezone(timezone.utc))
                policy.update(kind="first_legacy_sent_catchup",first_legacy_date=d.isoformat(),offline_catchup=True)
            else:
                start = default_start
        return start,end,policy

    @staticmethod
    def _row_signature(row):
        if row is None:
            return None
        present=(bool(row["snapshot_present"])if "snapshot_present"in row.keys()
                 else any(row[name]is not None for name in ("body","digest_sha256","attachment_content")))
        return tuple(row[k]for k in ("recipient","status","attempt_count","digest_sha256","updated_at","submission_token"))+(present,)

    def _daily_selection(self,connection,day,recipient,*,payload=True,combined_reservation=False):
        self._require_combined_schema(connection)
        self._require_recipient(connection,recipient)
        columns=("*"if payload else "recipient,status,attempt_count,digest_sha256,updated_at,submission_token,combined_batch_id,error_code,(body IS NOT NULL OR attachment_content IS NOT NULL OR digest_sha256 IS NOT NULL)AS snapshot_present")
        rows=[r for r in connection.execute(f"SELECT {columns} FROM pilot_test_digest_outbox WHERE delivery_date=?",(day,))
              if r["recipient"].strip().casefold()==recipient.strip().casefold()]
        for row in rows:
            self._require_independent_daily(connection,row)
        if any(r["status"]not in {"pending","failed"}for r in rows):
            return None,recipient,False
        if len(rows)>1:
            raise ValueError("PILOT_RECIPIENT_ALIAS_AMBIGUOUS")
        row=rows[0]if rows else None
        if row is not None and row['error_code'] == self._COMBINED_RESERVATION and not combined_reservation:
            return None,recipient,False
        return row,row["recipient"]if row is not None else recipient,True

    def reserve_combined_day(self, delivery_date: str, recipient: str) -> None:
        """Reserve an untouched daily diagnostic identity for the combined sender.

        This only records ownership; it neither creates a ZIP nor sends mail.
        Previously attempted, claimed, ambiguous or alias identities stay blocked.
        """
        try:
            if date.fromisoformat(delivery_date).isoformat() != delivery_date:
                raise ValueError()
        except (TypeError, ValueError):
            raise ValueError('PILOT_DELIVERY_DATE_INVALID') from None
        if not isinstance(recipient,str): raise ValueError('PILOT_RECIPIENT_INVALID')
        recipient=recipient.strip().casefold()
        with self.database.external_effect_guard(timeout=10), self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            self._require_combined_schema(c)
            self._require_selected_if_explicit(c,recipient)
            self._require_recipient(c,recipient)
            rows=[r for r in c.execute('SELECT * FROM pilot_test_digest_outbox WHERE delivery_date=?',(delivery_date,))
                  if isinstance(r['recipient'],str) and r['recipient'].strip().casefold()==recipient]
            if len(rows)>1: raise ValueError('PILOT_RECIPIENT_ALIAS_AMBIGUOUS')
            if rows:
                r=rows[0]
                if self._is_combined_reservation(r,recipient):
                    return
                raise ValueError('PILOT_JOINT_HISTORY_UNRESOLVED')
            from .daily_report_formal_ownership import take_over_unsent
            take_over_unsent(c,delivery_date,recipient)
            now=self._utc_now()
            c.execute("INSERT INTO pilot_test_digest_outbox(delivery_date,recipient,status,attempt_count,error_code,created_at,updated_at) VALUES(?,?,'pending',0,?,?,?)",
                      (delivery_date,recipient,self._COMBINED_RESERVATION,now,now))
            c.commit()

    @staticmethod
    def _is_combined_reservation(row, recipient):
        return (row['recipient']==recipient and row['status']=='pending' and row['attempt_count']==0
                and row['error_code']==PilotTestTelemetryService._COMBINED_RESERVATION and row['combined_batch_id'] is None
                and all(row[x] is None for x in ('subject','body','digest_sha256','attachment_filename',
                    'attachment_content','attachment_sha256','attachment_manifest_json','window_start_utc',
                    'window_end_utc','window_policy_json','claim_token','submission_token',
                    'submission_started_at','accepted_at','transport')))

    def _bundle_fields(self,bundle,identity,*,subject,body,start,end,policy):
        validate_email_attachments((EmailAttachment(bundle.filename,bundle.content),))
        manifest=bundle.manifest
        if type(manifest)is not dict or sha256(bundle.content).hexdigest()!=bundle.sha256:
            raise ValueError("PILOT_ATTACHMENT_INVALID")
        frozen=dict(identity,subject=subject,body=body,attachment_filename=bundle.filename,
            attachment_content=bundle.content,attachment_sha256=bundle.sha256,
            attachment_manifest_json=self._canonical(manifest),window_start_utc=start.isoformat(),
            window_end_utc=end.isoformat(),window_policy_json=self._canonical(policy))
        frozen["digest_sha256"]=self._digest(frozen)
        return frozen

    @staticmethod
    def _joint_key(key):
        if not isinstance(key,bytes) or len(key) not in (16,24,32):
            raise ValueError('LOCAL_ENCRYPTION_KEY_UNAVAILABLE')
        return key

    @staticmethod
    def _joint_members_schema(c):
        row=c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='combined_diagnostic_member'").fetchone()
        if not row or ' '.join(row[0].split())!=' '.join(JOINT_TEST_MEMBER_SCHEMA.split()):
            raise ValueError('PILOT_COMBINED_MEMBER_SCHEMA_UNAVAILABLE')

    def _joint_authorities(self,c,recipient,children):
        from .daily_report_frozen_snapshot import _consent_generation
        self._require_selected_if_explicit(c,recipient)
        self._require_recipient(c,recipient)
        consent=c.execute('SELECT status,authority_generation FROM pilot_diagnostic_recipient_consent WHERE recipient=?',(recipient,)).fetchone()
        if not consent or consent['status']!='consented' or not isinstance(consent['authority_generation'],str) or not consent['authority_generation']:
            raise ValueError('PILOT_EXPLICIT_AUTHORITY_REQUIRED')
        members=[]
        for child in children:
            if c.execute("SELECT 1 FROM deletion_job WHERE child_id=? AND status!='completed' LIMIT 1", (child,)).fetchone():
                raise ValueError('PILOT_JOINT_DELETION_PENDING')
            # A ZIP is shared by every member. Even a completed deletion by a
            # different member invalidates that frozen ZIP for the whole batch.
            deleted=c.execute('''SELECT deletion_id,status,updated_at FROM deletion_job
                WHERE child_id=? ORDER BY rowid DESC LIMIT 1''', (child,)).fetchone()
            deletion_epoch=list(deleted) if deleted else None
            rows=[r for r in c.execute("SELECT recipient_id,address,consent_status,revoked_at FROM report_delivery_recipient WHERE child_id=? AND channel='email'",(child,))
                  if isinstance(r['address'],str) and r['address'].strip().casefold()==recipient]
            if len(rows)!=1 or rows[0]['consent_status']!='consented' or rows[0]['revoked_at'] is not None:
                raise ValueError('PILOT_JOINT_CHILD_AUTHORITY_REQUIRED')
            members.append([child,rows[0]['recipient_id'],_consent_generation(c,child,rows[0]['recipient_id']),deletion_epoch])
        return consent['authority_generation'],members

    @staticmethod
    def _joint_hmac(key,value):
        return hmac.new(key,json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode(),sha256).hexdigest()

    def create_combined_batch(self, delivery_date: str, recipient: str, child_ids: list[str], key: bytes, *, send_hour: int=22) -> dict:
        """Locally reserve one frozen ZIP for a sealed explicit member set; never send."""
        key=self._joint_key(key); recipient=recipient.strip().casefold()
        try: target=date.fromisoformat(delivery_date)
        except (ValueError,TypeError): raise ValueError('PILOT_DELIVERY_DATE_INVALID') from None
        if target.isoformat()!=delivery_date or type(send_hour) is not int or not 0<=send_hour<=23:
            raise ValueError('PILOT_DELIVERY_DATE_INVALID')
        if type(child_ids) is not list or not child_ids or len(child_ids)>32 or any(type(x) is not str or not x for x in child_ids) or len(set(child_ids))!=len(child_ids):
            raise ValueError('PILOT_JOINT_MEMBERS_INVALID')
        children=sorted(child_ids)
        identity=self._joint_hmac(key,['joint-log-recipient-v1',delivery_date,recipient])
        with self.database.external_effect_guard(timeout=10):
            with self.database.connect() as c:
                c.execute('BEGIN')
                self._require_combined_schema(c); self._joint_members_schema(c)
                generation,members=self._joint_authorities(c,recipient,children)
                if c.execute('SELECT 1 FROM combined_diagnostic_batch WHERE delivery_date=? AND recipient_identity=?',(delivery_date,identity)).fetchone():
                    raise ValueError('PILOT_JOINT_ALREADY_EXISTS')
                existing,_,eligible=self._daily_selection(c,delivery_date,recipient,combined_reservation=True)
                if not eligible or (existing is not None and not self._is_combined_reservation(existing,recipient)):
                    raise ValueError('PILOT_JOINT_HISTORY_UNRESOLVED')
                bounds=self._window(c,recipient,target,send_hour)
                if bounds is None: raise ValueError('PILOT_JOINT_HISTORY_UNRESOLVED')
                c.rollback()
            start,end,policy=bounds
            bundle=build_daily_diagnostic_bundle(self.data_dir,start_utc=start,end_utc=end)
            body=self.build_digest(target,start_utc=start.isoformat(),end_utc=end.isoformat(),runtime_bundle_manifest=bundle.manifest)
            frozen=self._bundle_fields(bundle,{'delivery_date':delivery_date,'recipient':recipient},subject=f'家庭学习伙伴试用日报 {delivery_date}',body=body,start=start,end=end,policy={**policy,'privacy_body_version':2})
            self._frozen_claim(dict(frozen,submission_token=None))
            batch=uuid4().hex; timestamp=self._utc_now()
            binding=self._joint_hmac(key,['joint-log-members-v1',batch,delivery_date,identity,bundle.sha256,generation,members])
            with self.database.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                self._require_combined_schema(c); self._joint_members_schema(c)
                if self._joint_authorities(c,recipient,children)!=(generation,members) or self._window(c,recipient,target,send_hour)!=bounds:
                    raise ValueError('PILOT_JOINT_AUTHORITY_CHANGED')
                old,_,eligible=self._daily_selection(c,delivery_date,recipient,combined_reservation=True)
                if not eligible or (old is not None and not self._is_combined_reservation(old,recipient)) or c.execute('SELECT 1 FROM combined_diagnostic_batch WHERE delivery_date=? AND recipient_identity=?',(delivery_date,identity)).fetchone():
                    raise ValueError('PILOT_JOINT_HISTORY_UNRESOLVED')
                c.execute("INSERT INTO combined_diagnostic_batch VALUES(?,?,?,?,?,?,?, ?,1,'active',?,?)",(batch,delivery_date,identity,delivery_date,frozen['digest_sha256'],bundle.sha256,binding,len(children),timestamp,timestamp))
                columns=tuple(frozen)
                if old is None:
                    c.execute('INSERT INTO pilot_test_digest_outbox('+','.join(columns)+',status,attempt_count,created_at,updated_at,combined_batch_id) VALUES('+','.join('?' for _ in columns)+",'processing',0,?,?,?)",tuple(frozen[k] for k in columns)+(timestamp,timestamp,batch))
                else:
                    changed=c.execute('UPDATE pilot_test_digest_outbox SET '+','.join(k+'=?' for k in columns)+",status='processing',error_code=NULL,updated_at=?,combined_batch_id=? WHERE delivery_date=? AND recipient=? AND status='pending' AND error_code=?",tuple(frozen[k] for k in columns)+(timestamp,batch,delivery_date,recipient,self._COMBINED_RESERVATION)).rowcount
                    if changed!=1: raise ValueError('PILOT_JOINT_HISTORY_UNRESOLVED')
                c.executemany("INSERT INTO combined_diagnostic_member(batch_id,child_id,status,claim_token,updated_at) VALUES(?,?,'waiting',NULL,?)",[(batch,child,timestamp) for child in children])
                c.commit()
        return {'batch_id':batch,'member_count':len(children),'external_delivery_permitted':False}

    def inspect_combined_batch(self, batch_id: str, key: bytes) -> dict:
        """Check local ownership and authorization, not permission to send."""
        key=self._joint_key(key)
        with self.database.external_effect_guard(timeout=10), self.database.connect() as c:
            c.execute('BEGIN'); self._require_combined_schema(c); self._joint_members_schema(c)
            batch=c.execute('SELECT * FROM combined_diagnostic_batch WHERE batch_id=?',(batch_id,)).fetchone()
            pilot=c.execute('SELECT * FROM pilot_test_digest_outbox WHERE combined_batch_id=?',(batch_id,)).fetchone()
            if not batch or not pilot or batch['sealed']!=1: raise ValueError('PILOT_JOINT_BATCH_NOT_FOUND')
            rows=c.execute('SELECT * FROM combined_diagnostic_member WHERE batch_id=? ORDER BY child_id',(batch_id,)).fetchall()
            if len(rows)!=batch['expected_count']: raise ValueError('PILOT_JOINT_MEMBERS_UNCONFIRMED')
            self._frozen_claim(pilot)
            recipient=pilot['recipient'].strip().casefold()
            identity=self._joint_hmac(key,['joint-log-recipient-v1',batch['delivery_date'],recipient])
            generation,members=self._joint_authorities(c,recipient,[r['child_id'] for r in rows])
            binding=self._joint_hmac(key,['joint-log-members-v1',batch_id,batch['delivery_date'],identity,pilot['attachment_sha256'],generation,members])
            if not hmac.compare_digest(identity,batch['recipient_identity']) or not hmac.compare_digest(binding,batch['membership_sha256']):
                raise ValueError('PILOT_JOINT_AUTHORITY_CHANGED')
            return {'batch_id':batch_id,'status':batch['status'],'member_count':len(rows),
                    'external_delivery_permitted':False,'source_withdrawal_checked':False}

    def recover_combined_batches(self) -> int:
        """Unknown in-flight members stay blocked; never retry or advance the ZIP cursor."""
        with self.database.external_effect_guard(timeout=10), self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE'); self._require_combined_schema(c); self._joint_members_schema(c)
            if c.execute('''SELECT 1 FROM combined_diagnostic_member m LEFT JOIN combined_diagnostic_batch b
                ON b.batch_id=m.batch_id WHERE b.batch_id IS NULL LIMIT 1''').fetchone():
                raise ValueError('PILOT_JOINT_MEMBERS_UNCONFIRMED')
            malformed=c.execute('''SELECT 1 FROM combined_diagnostic_batch b LEFT JOIN combined_diagnostic_member m ON m.batch_id=b.batch_id
                GROUP BY b.batch_id HAVING b.sealed!=1 OR COUNT(m.child_id)!=b.expected_count LIMIT 1''').fetchone()
            if malformed: raise ValueError('PILOT_JOINT_MEMBERS_UNCONFIRMED')
            rows=c.execute("SELECT DISTINCT batch_id FROM combined_diagnostic_member WHERE status='processing'").fetchall()
            for row in rows:
                c.execute("UPDATE combined_diagnostic_member SET status='unknown',updated_at=? WHERE batch_id=? AND status='processing'",(self._utc_now(),row['batch_id']))
                c.execute("UPDATE combined_diagnostic_batch SET status='unknown',updated_at=? WHERE batch_id=?",(self._utc_now(),row['batch_id']))
            c.commit(); return len(rows)

    def claim_due(self, recipient: str, *, now: datetime | None = None, send_hour: int = 22, require_unambiguous: bool = False) -> PilotDigestClaim | None:
        recipient=recipient.strip().casefold()
        if not recipient or "@"not in recipient or len(recipient)>320:
            raise ValueError("PILOT_RECIPIENT_INVALID")
        local_now=self._as_local(now or datetime.now(timezone.utc))
        if type(send_hour)is not int or not 0<=send_hour<=23:
            raise ValueError("PILOT_SEND_HOUR_INVALID")
        target=local_now.date()if local_now.hour>=send_hour else local_now.date()-timedelta(days=1)
        day=target.isoformat()
        # The shared external-effect lease prevents restore, but there is no SQLite writer during log/code IO.
        with self.database.external_effect_guard(timeout=10):
            with self.database.connect()as connection:
                connection.execute('BEGIN IMMEDIATE')
                self._require_selected_if_explicit(connection, recipient)
                if require_unambiguous:
                    self._require_recipient(connection, recipient)
                if require_unambiguous and self._persisted_identity(connection) != recipient:
                    raise ValueError('PILOT_RECIPIENT_ALIAS_AMBIGUOUS')
                self._require_combined_schema(connection)
                from .daily_report_combined_mode import blocks_legacy
                if blocks_legacy(connection):
                    return None
                cutoff = (datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat()
                connection.execute("""UPDATE pilot_test_digest_outbox SET status='failed',error_code='CLAIM_ABANDONED_BEFORE_SUBMISSION',updated_at=?
                    WHERE status='processing' AND combined_batch_id IS NULL AND claim_token IS NOT NULL AND submission_token IS NULL AND attachment_content IS NOT NULL AND updated_at<?""", (self._utc_now(),cutoff))
                connection.commit()
                row,recipient,eligible=self._daily_selection(connection,day,recipient)
                if not eligible:
                    return None
                signature=self._row_signature(row)
                if row is not None and not signature[-1] and (row['attempt_count'] > 0 or row['submission_token'] is not None):
                    # No frozen original means an older attempt cannot be
                    # distinguished from a lost receipt. Never invent a new send.
                    connection.execute("""UPDATE pilot_test_digest_outbox SET status='unknown',
                        error_code='PILOT_LEGACY_BODY_REVIEW_REQUIRED',updated_at=?
                        WHERE delivery_date=? AND recipient=? AND status IN ('pending','failed')""",
                        (self._utc_now(), day, recipient))
                    connection.commit()
                    return None
                if row is not None and signature[-1]:
                    retry=self._frozen_claim(row);bounds=None
                else:
                    retry=None;bounds=self._window(connection,recipient,target,send_hour)
            if retry is not None:
                if (retry.window_policy or {}).get('privacy_body_version') != 2:
                    # Historical frozen text could contain child speech. Do not
                    # reuse it or assume an older failure means no delivery.
                    with self.database.connect() as connection:
                        connection.execute('BEGIN IMMEDIATE')
                        current, _, eligible = self._daily_selection(connection,day,recipient,payload=False)
                        if eligible and self._row_signature(current) == signature:
                            connection.execute("""UPDATE pilot_test_digest_outbox SET status='unknown',
                                error_code='PILOT_LEGACY_BODY_REVIEW_REQUIRED',updated_at=?
                                WHERE delivery_date=? AND recipient=? AND status IN ('pending','failed')""",
                                (self._utc_now(), day, recipient))
                            connection.commit()
                        else:
                            connection.rollback()
                    return None
            if retry is None:
                if bounds is None:
                    return None
                start,end,policy=bounds
                bundle=build_daily_diagnostic_bundle(self.data_dir,start_utc=start,end_utc=end)
                manifest=bundle.manifest
                included=manifest.get("events_included",0);seen=(manifest.get("counts")or{}).get("safe_events_seen",0)
                if type(included)is not int or type(seen)is not int or included<0 or seen<0:
                    raise ValueError("PILOT_ATTACHMENT_INVALID")
                body=self.build_digest(target,start_utc=start.isoformat(),end_utc=end.isoformat(),runtime_bundle_manifest=manifest)
                if policy["first_legacy_date"]:
                    body+="日志首次补齐：含旧日报"+policy["first_legacy_date"]+"零点起仍可取得的运行日志；缺口以附件清单为准。\n"
                elif policy["offline_catchup"]:
                    body+="日志离线补齐：从上次成功附件截止时间继续；缺口以附件清单为准。\n"
                frozen_policy = {**policy, 'privacy_body_version': 2}
                frozen=self._bundle_fields(bundle,{"delivery_date":day,"recipient":recipient},subject=f"家庭学习伙伴试用日报 {day}",body=body,start=start,end=end,policy=frozen_policy)
                prepared=self._frozen_claim(dict(frozen,submission_token=None))
            else:
                prepared=replace(retry,submission_token=None)
            with self.database.connect()as connection:
                connection.execute("BEGIN IMMEDIATE")
                current,current_recipient,eligible=self._daily_selection(connection,day,recipient,payload=False)
                if not eligible or self._row_signature(current)!=signature or current_recipient!=recipient:
                    return None
                if retry is None and self._window(connection,recipient,target,send_hour)!=bounds:
                    return None  # A successful cursor changed during IO. Recompute next poll, never relabel this ZIP.
                timestamp=self._utc_now()
                if retry is None:
                    connection.execute("INSERT OR IGNORE INTO pilot_test_digest_outbox(delivery_date,recipient,status,attempt_count,created_at,updated_at)VALUES(?,?,'pending',0,?,?)",(day,recipient,timestamp,timestamp))
                    columns=tuple(frozen)
                    connection.execute("UPDATE pilot_test_digest_outbox SET "+",".join(k+"=?"for k in columns)+" WHERE delivery_date=?AND recipient=?",tuple(frozen[k]for k in columns)+(day,recipient))
                claim_token=uuid4().hex
                changed=connection.execute("""UPDATE pilot_test_digest_outbox SET status='processing',attempt_count=attempt_count+1,
                    error_code=NULL,submission_token=NULL,submission_started_at=NULL,updated_at=?,claim_token=?
                    WHERE delivery_date=?AND recipient=?AND status IN('pending','failed')""",(timestamp,claim_token,day,recipient))
                if changed.rowcount!=1:
                    raise ValueError("PILOT_DIGEST_CLAIM_CHANGED")
                connection.commit();return replace(prepared,claim_token=claim_token)

    def processing_claim(self, delivery_date: str, recipient: str, digest_sha256: str) -> PilotDigestClaim:
        with self.database.connect() as connection:
            self._require_recipient(connection,recipient)
            row=connection.execute("SELECT * FROM pilot_test_digest_outbox WHERE delivery_date=? AND recipient=?",(delivery_date,recipient)).fetchone()
            self._require_independent_daily(connection,row)
        if not row:
            raise KeyError("PILOT_DELIVERY_NOT_FOUND")
        if row["status"] != "processing":
            raise ValueError("PILOT_DELIVERY_NOT_PROCESSING")
        if row["digest_sha256"] != digest_sha256:
            raise ValueError("PILOT_DIGEST_CLAIM_MISMATCH")
        return self._frozen_claim(row)

    def reserve_submission(self, delivery_date: str, recipient: str, digest_sha256: str, claim_token: str, *, require_unambiguous: bool = False) -> PilotDigestClaim:
        with self.database.external_effect_guard(timeout=10), self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_selected_if_explicit(connection, recipient)
            if require_unambiguous and self._persisted_identity(connection) != recipient.strip().casefold():
                raise ValueError('PILOT_RECIPIENT_ALIAS_AMBIGUOUS')
            self._require_recipient(connection,recipient)
            row=connection.execute("SELECT * FROM pilot_test_digest_outbox WHERE delivery_date=? AND recipient=?",(delivery_date,recipient)).fetchone()
            self._require_independent_daily(connection,row)
            if not row:
                raise KeyError("PILOT_DELIVERY_NOT_FOUND")
            if row["status"] != "processing" or row["digest_sha256"] != digest_sha256 or not claim_token or row['claim_token'] != claim_token:
                raise ValueError("PILOT_DIGEST_CLAIM_MISMATCH")
            claim = self._frozen_claim(row)
            self._require_private_body(claim)
            if row["submission_token"] is not None:
                raise ValueError("PILOT_DIGEST_SUBMISSION_ALREADY_STARTED")
            token=uuid4().hex
            changed=connection.execute("""UPDATE pilot_test_digest_outbox SET submission_token=?,submission_started_at=?,updated_at=?
                WHERE delivery_date=? AND recipient=? AND status='processing' AND submission_token IS NULL""",
                (token,self._utc_now(),self._utc_now(),delivery_date,recipient))
            if changed.rowcount != 1:
                raise ValueError("PILOT_DIGEST_SUBMISSION_ALREADY_STARTED")
            row=connection.execute("SELECT * FROM pilot_test_digest_outbox WHERE delivery_date=? AND recipient=?",(delivery_date,recipient)).fetchone()
            claim=self._frozen_claim(row);connection.commit();return claim

    def submission_check(self, claim: PilotDigestClaim, *, require_unambiguous: bool = False) -> str | None:
        try:
            table,where,identity=self._owned_scope(claim)
            with self.database.connect()as connection:
                self._require_selected_if_explicit(connection, claim.recipient)
                if require_unambiguous and self._persisted_identity(connection) != claim.recipient.strip().casefold():
                    return 'PILOT_RECIPIENT_ALIAS_AMBIGUOUS'
                self._require_recipient(connection,claim.recipient)
                row=connection.execute(f"SELECT * FROM {table} WHERE {where}",identity).fetchone()
                if table == self._DAILY_TABLE:
                    self._require_independent_daily(connection,row)
            if row and row["status"]=="processing" and row["digest_sha256"]==claim.digest_sha256:
                current=self._frozen_claim(row)
                if current.submission_token and current.submission_token==claim.submission_token:
                    self._require_private_body(current)
                    return None
        except (KeyError,ValueError):
            pass
        return "PILOT_SUBMISSION_OWNERSHIP_CHANGED"

    def _require_private_body(self, claim: PilotDigestClaim) -> None:
        if claim.purpose != 'daily':
            return
        if (claim.window_policy or {}).get('privacy_body_version') != 2:
            raise ValueError('PILOT_LEGACY_BODY_REVIEW_REQUIRED')

    def _public_result(self,row) -> dict[str,Any]:
        result={key:row[key]for key in self._PUBLIC_RESULT}
        if "client_action_id"in row.keys():
            result.update(purpose="runtime_log_attachment_verification",client_action_id=row["client_action_id"])
        return result

    def _finish_owned(self, claim: PilotDigestClaim, *, status: str, transport: str | None = None,
                      error_code: str | None = None) -> dict[str,Any]:
        if status not in {"sent","failed","unknown"}:
            raise ValueError("PILOT_DELIVERY_STATUS_INVALID")
        table,where,identity=self._owned_scope(claim)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row=connection.execute(f"SELECT * FROM {table} WHERE {where}",identity).fetchone()
            if table == self._DAILY_TABLE:
                self._require_independent_daily(connection,row)
            if (not row or not claim.submission_token or row["submission_token"]!=claim.submission_token
                    or row["digest_sha256"]!=claim.digest_sha256):
                raise ValueError("PILOT_SUBMISSION_OWNERSHIP_CHANGED")
            if row["status"]=="processing":
                self._frozen_claim(row);timestamp=self._utc_now()
                connection.execute(f"""UPDATE {table} SET status=?,transport=?,error_code=?,accepted_at=?,updated_at=?
                    WHERE {where} AND submission_token=? AND status='processing'""",
                    (status,transport,error_code,timestamp if status=='sent'else None,timestamp)+identity+(claim.submission_token,))
            row=connection.execute(f"SELECT * FROM {table} WHERE {where}",identity).fetchone()
            connection.commit();return self._public_result(row)

    def finish_submission(self, delivery_date: str, recipient: str, digest_sha256: str, *, submission_token: str,
                          status: str, transport: str | None = None,error_code: str | None = None) -> dict[str,Any]:
        claim=PilotDigestClaim(delivery_date,recipient,"","",digest_sha256,submission_token=submission_token)
        return self._finish_owned(claim,status=status,transport=transport,error_code=error_code)

    def finish_owned_submission(self, claim: PilotDigestClaim, *, status: str,
                                transport: str | None = None,error_code: str | None = None) -> dict[str,Any]:
        return self._finish_owned(claim,status=status,transport=transport,error_code=error_code)

    def _verification_selection(self,connection,day,recipient,action_id,*,payload=True):
        self._require_selected_if_explicit(connection,recipient)
        self._require_recipient(connection,recipient)
        columns=("*"if payload else "delivery_date,recipient,status,attempt_count,digest_sha256,updated_at,submission_token,(body IS NOT NULL OR attachment_content IS NOT NULL OR digest_sha256 IS NOT NULL)AS snapshot_present,client_action_id,transport,error_code,accepted_at")
        row=connection.execute(f"SELECT {columns} FROM {self._VERIFICATION_TABLE} WHERE client_action_id=?",(action_id,)).fetchone()
        if row is not None and(row["delivery_date"]!=day or row["recipient"].strip().casefold()!=recipient):
            raise ValueError("PILOT_VERIFICATION_ACTION_CONFLICT")
        return row

    def _verification_replay(self,row):
        result=self._public_result(row);result.update(accepted=row["status"]=="sent",replayed=True)
        raise PilotSubmissionReplay(result)

    def reserve_verification(self, delivery_date: str, recipient: str, client_action_id: str,
                             *, now: datetime | None = None) -> PilotDigestClaim:
        import re
        recipient=recipient.strip().casefold()
        if not recipient or "@"not in recipient or len(recipient)>320:
            raise ValueError("PILOT_RECIPIENT_INVALID")
        if type(client_action_id)is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{7,119}",client_action_id):
            raise ValueError("PILOT_VERIFICATION_ACTION_INVALID")
        current=self._as_local(now or datetime.now(timezone.utc));actual_date=current.date().isoformat()
        if delivery_date!=actual_date:
            raise ValueError("PILOT_VERIFICATION_DATE_INVALID")
        table=self._VERIFICATION_TABLE
        with self.database.external_effect_guard(timeout=10):
            with self.database.connect()as connection:
                row=self._verification_selection(connection,delivery_date,recipient,client_action_id)
                if row is not None and row["status"]!="failed":
                    self._verification_replay(row)
                signature=self._row_signature(row)
                retry=self._frozen_claim(row)if row is not None else None
            token=uuid4().hex
            if retry is None:
                end=current.astimezone(timezone.utc);start=datetime.combine(current.date(),time.min,PILOT_TIMEZONE).astimezone(timezone.utc)
                if end<=start:
                    raise ValueError("PILOT_VERIFICATION_WINDOW_EMPTY")
                bundle=build_daily_diagnostic_bundle(self.data_dir,start_utc=start,end_utc=end)
                body=("运行日志附件验证\n这是一次显式附件通路验证，不是新学习日报，不改变原日报发送状态。\n"
                      f"实际日期：{actual_date}（中国标准时间）\n日志窗口：{start.isoformat()} -> {end.isoformat()}\n"
                      "仅含仍可取得且过滤后的运行事件；缺失、截断和代码版本核验结果以附件清单为准。\n")
                frozen=self._bundle_fields(bundle,{"client_action_id":client_action_id,"delivery_date":delivery_date,"recipient":recipient},
                    subject=f"家庭学习伙伴运行日志附件验证 {actual_date}",body=body,start=start,end=end,
                    policy={"kind":"explicit_verification_current_day","first_legacy_date":None,"offline_catchup":False})
                prepared=self._frozen_claim(dict(frozen,submission_token=token))
            else:
                prepared=replace(retry,submission_token=token)
            with self.database.connect()as connection:
                connection.execute("BEGIN IMMEDIATE")
                if now is None and self._as_local(datetime.now(timezone.utc)).date().isoformat()!=delivery_date:
                    raise ValueError("PILOT_VERIFICATION_DATE_INVALID")
                latest=self._verification_selection(connection,delivery_date,recipient,client_action_id,payload=False)
                if self._row_signature(latest)!=signature:
                    if latest is not None:
                        self._verification_replay(latest)
                    raise ValueError("PILOT_VERIFICATION_ACTION_CHANGED")
                timestamp=self._utc_now()
                if retry is None:
                    connection.execute(f"INSERT INTO {table}(client_action_id,delivery_date,recipient,status,attempt_count,created_at,updated_at)VALUES(?,?,?,'pending',0,?,?)",(client_action_id,delivery_date,recipient,timestamp,timestamp))
                    columns=tuple(frozen)
                    connection.execute(f"UPDATE {table} SET "+",".join(k+"=?"for k in columns)+" WHERE client_action_id=?",tuple(frozen[k]for k in columns)+(client_action_id,))
                changed=connection.execute(f"""UPDATE {table} SET status='processing',attempt_count=attempt_count+1,
                    submission_token=?,submission_started_at=?,updated_at=?,error_code=NULL
                    WHERE client_action_id=?AND status IN('pending','failed')""",(token,timestamp,timestamp,client_action_id))
                if changed.rowcount!=1:
                    raise ValueError("PILOT_VERIFICATION_ACTION_CHANGED")
                connection.commit();return prepared

    def record_result(self, delivery_date: str, recipient: str, *, status: str,
                      transport: str | None = None,error_code: str | None = None, claim_token: str | None = None) -> dict[str,Any]:
        if status not in {"sent","failed","unknown"}:
            raise ValueError("PILOT_DELIVERY_STATUS_INVALID")
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row=connection.execute("SELECT * FROM pilot_test_digest_outbox WHERE delivery_date=? AND recipient=?",(delivery_date,recipient)).fetchone()
            self._require_independent_daily(connection,row)
            if not row:
                raise KeyError("PILOT_DELIVERY_NOT_FOUND")
            if row['claim_token'] is not None and (not claim_token or row['claim_token'] != claim_token):
                raise ValueError('PILOT_DIGEST_CLAIM_MISMATCH')
            if row["status"] == "processing":
                if row['submission_token'] is not None:
                    # Once SMTP is reserved, only the server-owned submission
                    # can persist its outcome. A timed-out caller is not proof
                    # that the in-flight provider result is unknown.
                    connection.rollback()
                    return self._public_result(row)
                if row['claim_token'] is not None and row['attachment_content'] is not None:
                    # This generation has not reserved SMTP. Serializing this
                    # state transition with reserve makes a delayed request fail
                    # before external delivery, so the frozen claim can retry.
                    status='failed';error_code='CLAIM_ABANDONED_BEFORE_SUBMISSION'
                elif row["attachment_content"] is not None or row["submission_token"] is not None:
                    # Every new ZIP snapshot is server-SMTP-only, even before reservation.
                    status="unknown";error_code="SERVER_SUBMISSION_OUTCOME_NOT_PERSISTED"
                timestamp=self._utc_now()
                connection.execute("""UPDATE pilot_test_digest_outbox SET status=?,transport=?,error_code=?,accepted_at=?,updated_at=?
                    WHERE delivery_date=? AND recipient=? AND status='processing'""",
                    (status,transport,error_code,timestamp if status=='sent'else None,timestamp,delivery_date,recipient))
            row=connection.execute("SELECT * FROM pilot_test_digest_outbox WHERE delivery_date=? AND recipient=?",(delivery_date,recipient)).fetchone()
            connection.commit();return self._public_result(row)

    def build_digest(self, target_date: date, *, start_utc: str | None = None, end_utc: str | None = None,
                     runtime_bundle_manifest: dict | None = None) -> str:
        if start_utc is None or end_utc is None:
            start_utc, end_utc = self._utc_bounds(target_date)
        lines = [
            "家庭学习伙伴试用日报",
            f"日期：{target_date.isoformat()}（中国标准时间）",
            "范围：语音交互、Agent 调度、模型调用、设备、高拍仪 OCR、题目/错题和学习会话。",
            "隐私：本邮件不包含孩子原话、智能体回复、孩子身份、原始音频、原始视频、页面图像、本地文件路径或 API 密钥；事件按家庭汇总。",
            "",
        ]
        with self.database.connect() as connection:
            lines.extend(self._child_summary(connection, start_utc, end_utc))
            lines.extend(self._conversation_timeline(connection, start_utc, end_utc))
            lines.extend(self._model_summary(connection, start_utc, end_utc))
            lines.extend(self._device_summary(connection, start_utc, end_utc))
            lines.extend(self._document_summary(connection, start_utc, end_utc))
            lines.extend(self._audit_summary(connection, start_utc, end_utc))
        if runtime_bundle_manifest is None:
            lines.extend(self._runtime_log_summary(start_utc, end_utc))
        else:
            included=runtime_bundle_manifest.get("events_included",0)
            seen=(runtime_bundle_manifest.get("counts")or{}).get("safe_events_seen",0)
            coverage=runtime_bundle_manifest.get("coverage")or{}
            lines.extend(["【桌面运行日志附件】",f"窗口：{start_utc} -> {end_utc}",
                          f"附件记录：{included}；安全事件已见：{seen}。",
                          "仅含仍可取得的运行日志，不保证完整全日；截断与缺口见附件manifest。",""])
        return "\n".join(lines).strip() + "\n"

    def _child_summary(self, connection, start: str, end: str) -> list[str]:
        rows = connection.execute(
            """SELECT status,COUNT(*) AS n FROM daily_session
               WHERE started_at>=? AND started_at<? GROUP BY status""",
            (start, end),
        ).fetchall()
        lines = ["【孩子与学习会话】"]
        if not rows:
            return lines + ["无学习会话记录。", ""]
        for row in rows:
            status = row['status'] if row['status'] in {'active','completed','cancelled'} else 'other'
            lines.append(f"- {status}：{row['n']} 次")
        return lines + [""]

    def _conversation_timeline(self, connection, start: str, end: str) -> list[str]:
        rows = connection.execute(
            """SELECT tool_status,COUNT(*) AS n FROM agent_turn
               WHERE created_at>=? AND created_at<? GROUP BY tool_status""",
            (start, end),
        ).fetchall()
        lines = ["【语音/Agent 回合】", f"共 {sum(row['n'] for row in rows)} 条；不包含孩子原话或智能体回复。"]
        for row in rows:
            status = row['tool_status'] if row['tool_status'] in {'pending','processing','completed','failed','blocked','cancelled'} else 'other'
            lines.append(f"- 工具状态 {status}：{row['n']} 条")
        return lines + [""]

    def _model_summary(self, connection, start: str, end: str) -> list[str]:
        rows = connection.execute(
            """SELECT provider,model,purpose,status,COUNT(*) AS count,
                      ROUND(AVG(COALESCE(latency_ms,0))) AS avg_latency_ms,
                      SUM(COALESCE(fallback_used,0)) AS fallbacks,
                      SUM(COALESCE(prompt_tokens,0)+COALESCE(completion_tokens,0)) AS tokens,
                      GROUP_CONCAT(DISTINCT error_code) AS errors
               FROM model_call WHERE created_at>=? AND created_at<?
               GROUP BY provider,model,purpose,status ORDER BY count DESC""",
            (start, end),
        ).fetchall()
        lines = ["【云端/本地模型调用】"]
        if not rows:
            return lines + ["无模型调用记录。", ""]
        providers = {'qwen', 'deepseek', 'local', 'aliyun'}
        models = {'deepseek-v4-pro', 'qwen3.7-plus', 'qwen3.6-plus', 'qwen3.8-omni-flash-realtime', 'qwen3-asr-flash'}
        purposes = {'voice_turn', 'voice_interaction', 'agent_plan', 'generate_daily_report_and_parent_coaching'}
        statuses = {'succeeded', 'failed', 'cancelled', 'timeout', 'blocked'}
        errors = {'MODEL_TIMEOUT', 'MODEL_UNAVAILABLE', 'MODEL_RESPONSE_INVALID', 'MODEL_NOT_CONFIGURED', 'MODEL_NETWORK_ERROR'}
        for row in rows:
            provider = row['provider'] if row['provider'] in providers else 'other'
            model = row['model'] if row['model'] in models else 'other'
            purpose = row['purpose'] if row['purpose'] in purposes else 'other'
            status = row['status'] if row['status'] in statuses else 'other'
            codes = str(row['errors'] or '').split(',') if row['errors'] else []
            safe_codes = sorted({code if code in errors else 'other' for code in codes})
            lines.append(
                f"- {provider}/{model} {purpose} {status}："
                f"{row['count']} 次，平均 {row['avg_latency_ms']}ms，fallback={row['fallbacks']}，"
                f"tokens={row['tokens']}，错误={','.join(safe_codes) if safe_codes else '无'}"
            )
        return lines + [""]

    def _device_summary(self, connection, start: str, end: str) -> list[str]:
        rows = connection.execute(
            """SELECT occurred_at,device_type,error_code,status,resolved_at
               FROM device_incident WHERE occurred_at>=? AND occurred_at<? ORDER BY occurred_at""",
            (start, end),
        ).fetchall()
        lines = ["【设备与恢复】", f"共 {len(rows)} 条设备异常。"]
        device_types = {'camera', 'document_camera', 'microphone'}
        device_errors = {'CAMERA_NOT_FOUND', 'CAMERA_BUSY', 'CAMERA_PERMISSION_DENIED', 'DEVICE_NOT_BOUND',
                         'BOUND_DEVICE_NOT_FOUND', 'DEVICE_COMMAND_FAILED', 'DEVICE_COMMAND_TIMEOUT', 'DEVICE_PERMISSION_DENIED'}
        statuses = {'open', 'resolved', 'active', 'closed'}
        for row in rows:
            device = row['device_type'] if row['device_type'] in device_types else 'other'
            code = row['error_code'] if row['error_code'] in device_errors else 'other'
            status = row['status'] if row['status'] in statuses else 'other'
            lines.append(
                f"- {device} {code} 状态={status} 恢复={'是' if row['resolved_at'] else '否'}"
            )
        return lines + [""]

    def _document_summary(self, connection, start: str, end: str) -> list[str]:
        queries = [
            ("capture_artifact", "created_at", "拍摄/OCR 产物"),
            ("document_page_revision", "created_at", "去重后页面修订"),
            ("observed_question_occurrence", "observed_at", "题目观测次数"),
            ("mistake_capture_candidate", "created_at", "错题候选"),
            ("mistake_case", "created_at", "已入库错题"),
        ]
        lines = ["【高拍仪、OCR 与错题链路】"]
        for table, column, label in queries:
            if not self._table_exists(connection, table):
                lines.append(f"- {label}：表未建立")
                continue
            count = connection.execute(
                f"SELECT COUNT(*) AS count FROM {table} WHERE {column}>=? AND {column}<?", (start, end)
            ).fetchone()["count"]
            lines.append(f"- {label}：{count}")
        if self._table_exists(connection, "mistake_capture_candidate"):
            rows = connection.execute(
                """SELECT status,COUNT(*) AS count FROM mistake_capture_candidate
                   WHERE created_at>=? AND created_at<? GROUP BY status""",
                (start, end),
            ).fetchall()
            if rows:
                allowed = {'pending', 'processing', 'confirmed', 'rejected', 'skipped', 'failed', 'cancelled'}
                lines.append("- 错题候选状态：" + "，".join(f"{r['status'] if r['status'] in allowed else 'other'}={r['count']}" for r in rows))
        trial_rows = connection.execute('''SELECT artifact_type,COUNT(*) n FROM capture_artifact
            WHERE created_at>=? AND created_at<? AND artifact_type IN ('document_trial_frame','document_trial_review')
            GROUP BY artifact_type''', (start, end)).fetchall()
        for row in trial_rows:
            label = '本机诊断留图' if row['artifact_type'] == 'document_trial_frame' else '学习结束程序复盘'
            lines.append(f"- {label}：{row['n']}；图片仅在本机回放，不随邮件发送。")
        return lines + [""]

    def _audit_summary(self, connection, start: str, end: str) -> list[str]:
        rows = connection.execute(
            """SELECT action,COUNT(*) AS count FROM audit_event
               WHERE occurred_at>=? AND occurred_at<? GROUP BY action ORDER BY count DESC LIMIT 100""",
            (start, end),
        ).fetchall()
        lines = ["【持久化动作】"]
        lines.append(f"共 {sum(row['count'] for row in rows)} 次动作（最多统计 100 类）。")
        if not rows:
            lines.append("无审计动作。")
        return lines + [""]

    def _runtime_log_summary(self, start: str, end: str) -> list[str]:
        start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
        counts: dict[tuple[str, str, str], int] = {}
        log_dir = self.data_dir / "diagnostics" / "logs"
        for path in sorted(log_dir.glob("runtime.jsonl*")) if log_dir.exists() else []:
            try:
                with path.open("r", encoding="utf-8", errors="replace") as stream:
                    for raw in stream:
                        try:
                            item = json.loads(raw)
                            if not isinstance(item, dict):
                                continue
                            timestamp = datetime.fromisoformat(str(item.get("timestamp", "")).replace("Z", "+00:00"))
                        except (ValueError, TypeError, json.JSONDecodeError):
                            continue
                        if timestamp.tzinfo is None:
                            continue
                        if not start_dt <= timestamp < end_dt:
                            continue
                        level = item.get('level') if isinstance(item.get('level'), str) and item.get('level') in {'debug','info','warn','error'} else 'other'
                        source = item.get('source') if isinstance(item.get('source'), str) and item.get('source') in {'main','renderer','local_service','system','device'} else 'other'
                        event = item.get('event') if isinstance(item.get('event'), str) and item.get('event') in DIAGNOSTIC_EVENTS else 'other'
                        key = (level, source, event)
                        counts[key] = counts.get(key, 0) + 1
            except OSError:
                continue
        lines = ["【桌面运行日志元数据】"]
        for (level, source, event), count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))[:100]:
            lines.append(f"- {level}/{source}/{event}：{count}")
        if not counts:
            lines.append("无可用的桌面运行日志元数据。")
        return lines + [""]

    def _has_activity(self, start: str, end: str) -> bool:
        checks = (
            ("daily_session", "started_at"),
            ("agent_turn", "created_at"),
            ("model_call", "created_at"),
            ("device_incident", "occurred_at"),
            ("capture_artifact", "created_at"),
            ("audit_event", "occurred_at"),
        )
        with self.database.connect() as connection:
            for table, column in checks:
                if not self._table_exists(connection, table):
                    continue
                if connection.execute(
                    f"SELECT 1 FROM {table} WHERE {column}>=? AND {column}<? LIMIT 1", (start, end)
                ).fetchone():
                    return True
        return False

    @staticmethod
    def _table_exists(connection, table: str) -> bool:
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone() is not None

    @staticmethod
    def _single_line(value: Any, limit: int) -> str:
        text = " ".join(str(value or "").split())
        return text if len(text) <= limit else text[: limit - 1] + "…"

    @staticmethod
    def _as_local(value: datetime) -> datetime:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(PILOT_TIMEZONE)

    @staticmethod
    def _utc_bounds(day: date) -> tuple[str, str]:
        start = datetime.combine(day, time.min, PILOT_TIMEZONE).astimezone(timezone.utc)
        end = (datetime.combine(day, time.min, PILOT_TIMEZONE) + timedelta(days=1)).astimezone(timezone.utc)
        return start.isoformat(), end.isoformat()

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat()
