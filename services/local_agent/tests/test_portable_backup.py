from __future__ import annotations

import base64
import json
from pathlib import Path
import sqlite3

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import pytest

from home_study_agent.database import Database
from home_study_agent.privacy_lifecycle import InvalidBackupError, PrivacyLifecycleService
from home_study_agent.report_delivery import ReportDeliveryService


def _seed_source(data_dir: Path, key: bytes) -> tuple[Database, bytes]:
    database = Database(data_dir / "agent.db")
    database.initialize()
    ReportDeliveryService(database, key).initialize()
    media_path = data_dir / "media" / "voice" / "sample.bin"
    media_path.parent.mkdir(parents=True)
    plaintext = b"private-child-audio"
    nonce = b"n" * 12
    media_path.write_bytes(
        b"HSC1" + nonce + AESGCM(key).encrypt(nonce, plaintext, b"hsc-raw-media-v1")
    )
    credential_nonce = b"c" * 12
    credential = AESGCM(key).encrypt(
        credential_nonce,
        json.dumps({"smtp_password": "synthetic-dca6861589d640c028853cee"}).encode(),
        b"hsc-notification-credential-v1",
    )
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO child_profile VALUES (?, ?, ?, ?, 'active', datetime('now'), datetime('now'))",
            ("child-primary-4", "小学孩子", "4", "nanjing"),
        )
        connection.execute(
            """INSERT INTO daily_session(
              session_id, child_id, status, state, started_at, ended_at,
              last_checkpoint_at, observation_status, client_action_id
            ) VALUES ('portable-session', 'child-primary-4', 'completed', 'closed',
              datetime('now'), datetime('now'), datetime('now'), 'disabled', 'portable-session-action')"""
        )
        connection.execute(
            """INSERT INTO voice_input(
              voice_input_id, child_id, session_id, purpose, transcript, local_path,
              model_version, duration_ms, status, created_at, client_action_id
            ) VALUES ('portable-voice', 'child-primary-4', 'portable-session', 'check_in',
              '今天学了分数', ?, 'test', 900, 'completed', datetime('now'), 'portable-voice-action')""",
            (str(media_path),),
        )
        connection.execute(
            """INSERT INTO notification_credential(
              credential_id, channel, encrypted_json, created_at, updated_at
            ) VALUES ('credential-1', 'email', ?, datetime('now'), datetime('now'))""",
            (base64.b64encode(credential_nonce + credential).decode("ascii"),),
        )
        connection.commit()
    return database, plaintext


def test_portable_backup_restores_across_data_dirs_and_rotates_device_encryption(tmp_path: Path) -> None:
    source_dir, target_dir = tmp_path / "source", tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    old_key, new_key = b"o" * 32, b"n" * 32
    source_database, plaintext = _seed_source(source_dir, old_key)
    source = PrivacyLifecycleService(source_database, source_dir, old_key)

    portable = source.create_portable_backup("correct horse battery staple")
    portable_path = Path(portable["path"])
    assert portable_path.read_bytes().find("小学孩子".encode()) == -1

    target_database = Database(target_dir / "agent.db")
    target_database.initialize()
    ReportDeliveryService(target_database, new_key).initialize()
    target = PrivacyLifecycleService(target_database, target_dir, new_key)
    restored = target.restore_portable_backup(
        str(portable_path), "correct horse battery staple", "确认迁移恢复"
    )

    with target_database.connect() as connection:
        child = connection.execute(
            "SELECT nickname FROM child_profile WHERE child_id = 'child-primary-4'"
        ).fetchone()
        media_path = Path(connection.execute(
            "SELECT local_path FROM voice_input WHERE voice_input_id = 'portable-voice'"
        ).fetchone()[0])
        encrypted_credential = connection.execute(
            "SELECT encrypted_json FROM notification_credential WHERE credential_id = 'credential-1'"
        ).fetchone()[0]
        audit = connection.execute(
            "SELECT action FROM audit_event WHERE action = 'portable_backup_restored'"
        ).fetchone()
        recovery_manifest = connection.execute(
            "SELECT reason FROM backup_manifest WHERE backup_id = ?",
            (restored["recovery_point_backup_id"],),
        ).fetchone()
    media_payload = media_path.read_bytes()
    credential_payload = base64.b64decode(encrypted_credential)

    assert restored["status"] == "restored" and restored["integrity_status"] == "ok"
    assert child[0] == "小学孩子"
    assert media_path.is_relative_to(target_dir / "media")
    assert AESGCM(new_key).decrypt(
        media_payload[4:16], media_payload[16:], b"hsc-raw-media-v1"
    ) == plaintext
    restored_config = json.loads(AESGCM(new_key).decrypt(
        credential_payload[:12], credential_payload[12:], b"hsc-notification-credential-v1"
    ))
    assert restored_config == {}  # Legacy secrets are not portable send authority.
    assert not restored_config.get("allow_external_delivery")
    with pytest.raises(Exception):
        AESGCM(old_key).decrypt(
            credential_payload[:12], credential_payload[12:], b"hsc-notification-credential-v1"
        )
    with pytest.raises(Exception):
        AESGCM(old_key).decrypt(
            media_payload[4:16], media_payload[16:], b"hsc-raw-media-v1"
        )
    assert audit[0] == "portable_backup_restored"
    assert recovery_manifest[0] == "restore_recovery_point"


def test_portable_backup_wrong_passphrase_does_not_replace_current_database(tmp_path: Path) -> None:
    source_dir, target_dir = tmp_path / "source", tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    source_database, _ = _seed_source(source_dir, b"o" * 32)
    portable = PrivacyLifecycleService(source_database, source_dir, b"o" * 32).create_portable_backup(
        "correct horse battery staple"
    )
    target_database = Database(target_dir / "agent.db")
    target_database.initialize()
    target = PrivacyLifecycleService(target_database, target_dir, b"n" * 32)

    with pytest.raises(InvalidBackupError, match="口令错误"):
        target.restore_portable_backup(
            portable["path"], "this passphrase is wrong", "确认迁移恢复"
        )

    with target_database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM child_profile").fetchone()[0] == 0
