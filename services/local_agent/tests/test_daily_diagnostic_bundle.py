from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import random
import stat
import sys
from types import SimpleNamespace
import zipfile

import pytest

from home_study_agent import daily_diagnostic_bundle as diagnostic


START = datetime(2026, 10, 27, 14, tzinfo=timezone.utc)
END = START + timedelta(days=1)
CORRELATION = "f210d041-a277-42a1-b117-033b8511af48"


def symlink_fixture(path, target, *, target_is_directory=False):
    try:
        path.symlink_to(target, target_is_directory=target_is_directory)
    except OSError as error:
        if os.name == "nt" and error.winerror == 1314:
            pytest.skip("Windows test account lacks symbolic-link creation privilege")
        raise


def event(**changes):
    result = {
        "privacy_policy_version": "diagnostic_metadata_v2",
        "timestamp": (START + timedelta(hours=1)).isoformat(),
        "source": "local_service", "level": "info", "event": "local_api_request",
        "correlation_id": CORRELATION,
        "details": {"method": "POST", "route_template": "/v1/agent-turns/{turn_id}/execute", "statusCode": 409, "durationMs": 23.5},
    }
    result.update(changes)
    return result


def logs(tmp_path, rows, name="runtime.jsonl"):
    directory = tmp_path / "diagnostics" / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(b"".join(json.dumps(row).encode() + b"\n" for row in rows))
    return path


def build(tmp_path, **kwargs):
    return diagnostic.build_daily_diagnostic_bundle(tmp_path, start_utc=START, end_utc=END, **kwargs)


def unpack(bundle):
    with zipfile.ZipFile(BytesIO(bundle.content)) as archive:
        assert archive.namelist() == ["manifest.json", "events.jsonl"]
        assert not any(info.is_dir() for info in archive.infolist())
        manifest_raw = archive.read("manifest.json")
        events_raw = archive.read("events.jsonl")
    manifest = json.loads(manifest_raw)
    rows = [json.loads(line) for line in events_raw.splitlines()]
    assert manifest == bundle.manifest
    assert manifest["events_bytes"] == len(events_raw)
    assert manifest["events_sha256"] == hashlib.sha256(events_raw).hexdigest()
    assert bundle.sha256 == hashlib.sha256(bundle.content).hexdigest()
    return manifest, rows, manifest_raw + events_raw


def test_policy_poll_export_aggregates_only_plain_success_and_preserves_failures(tmp_path):
    route = '/v1/private/money-egress'
    ok = lambda sec: event(timestamp=(START + timedelta(seconds=sec)).isoformat(), details={
        'method': 'GET', 'route_template': route, 'statusCode': 200, 'durationMs': 2})
    denied = event(level='warn', details={'method': 'GET', 'route_template': route, 'statusCode': 403})
    unusual = event(details={'method': 'GET', 'route_template': route, 'statusCode': 200,
                             'reason_code': 'QWEN_NATIVE_SOCKET_POLICY_UNCONFIRMED'})
    path = logs(tmp_path, [ok(3), ok(4), ok(3601), denied, unusual])
    original = path.read_bytes()
    manifest, rows, _ = unpack(build(tmp_path))
    summary = manifest['aggregated_successful_policy_polls']
    assert summary['individual_events_omitted'] == 3
    assert [bucket['count'] for bucket in summary['buckets']] == [2, 1]
    assert len(rows) == 2
    assert any(row['details']['statusCode'] == 403 for row in rows)
    assert any(row['details'].get('reason_code') == 'QWEN_NATIVE_SOCKET_POLICY_UNCONFIRMED' for row in rows)
    assert not manifest['coverage']['truncated']
    assert path.read_bytes() == original


def test_policy_poll_summary_bounds_bucket_count_for_long_windows():
    selection = diagnostic._Selection(diagnostic.LIMITS, diagnostic.Counter())
    selection.poll_summaries = {(i, str(i)): {'hour_utc': str(i), 'count': 2,
        'first_utc': f'{i:03}', 'last_utc': f'{i:03}'} for i in range(65)}
    summary = selection.poll_summary()
    assert summary['bucket_resolution'] == 'whole_window'
    assert summary['individual_events_omitted'] == 130
    assert len(summary['buckets']) == 1


def test_child_process_logs_never_export_synthetic_provider_secrets(tmp_path):
    # These are synthetic sentinels only, never credentials or household data.
    synthetic = ['SYNTHETIC_PRIMARY_ZIP', 'SYNTHETIC_FALLBACK_ZIP', 'SYNTHETIC_ASR_ZIP', 'SYNTHETIC_TTS_ZIP']
    logs(tmp_path, [event(source='local_service', event=kind, message=key,
                          details={'message': key, 'api_key': key, 'count': 1})
                    for kind in ('stdout', 'stderr') for key in synthetic])
    _, rows, raw = unpack(build(tmp_path))
    assert len(rows) == 8
    assert all(row['details'] == {'count': 1} for row in rows)
    for value in synthetic:
        assert value.encode() not in raw


def test_redaction_suppression_count_survives_zip_without_contents(tmp_path):
    logs(tmp_path, [event(source='local_service', event='local_service.output_redaction_suppressed',
                          level='warn', details={'count': 2, 'message': 'SYNTHETIC_PRIVATE'})])
    _, rows, raw = unpack(build(tmp_path))
    assert len(rows) == 1
    assert rows[0]['event'] == 'local_service.output_redaction_suppressed'
    assert rows[0]['details'] == {'count': 2}
    assert b'SYNTHETIC_PRIVATE' not in raw


def test_finite_asr_failure_reasons_survive_email_zip_without_private_details(tmp_path):
    reasons = ('ASR_CLOUD_DIALOGUE_UNAVAILABLE', 'ASR_MODEL_NOT_READY', 'ASR_CLIENT_TIMEOUT',
               'ASR_CLIENT_NETWORK_OR_PROTOCOL', 'ASR_CLIENT_UNKNOWN')
    kinds = ('api', 'timeout', 'network_or_protocol', 'client', 'api')
    logs(tmp_path, [event(event='voice.asr.failed', level='error', source='renderer', details={
        'mode': 'conversation', 'reason_code': reason, 'failure_kind': kind,
        'transcript': 'PRIVATE_SPEECH', 'message': 'PRIVATE_SECRET', 'audio': 'PRIVATE_AUDIO',
    }) for reason, kind in zip(reasons, kinds)] + [event(event='voice.asr.failed', details={
        'reason_code': 'ASR_CLIENT_PRIVATE_SECRET', 'failure_kind': 'PRIVATE_SPEECH',
    })])
    manifest, rows, raw = unpack(build(tmp_path))
    assert [r['details'] for r in rows[:5]] == [
        {'mode': 'conversation', 'reason_code': reason, 'failure_kind': kind}
        for reason, kind in zip(reasons, kinds)]
    assert rows[-1]['details'] == {}
    assert b'PRIVATE_' not in raw
    logger = Path(__file__).resolve().parents[3] / 'src/main/diagnostics.ts'
    assert manifest['logger_schema_source_sha256'] == hashlib.sha256(logger.read_bytes()).hexdigest()


def test_native_provider_reasons_survive_email_zip_without_private_details(tmp_path):
    reasons = ('QWEN_NATIVE_CREDENTIAL_REJECTED', 'QWEN_NATIVE_ACCESS_DENIED', 'QWEN_NATIVE_ACCOUNT_BALANCE')
    logs(tmp_path, [event(event='voice.asr.failed', details={
        'reason_code': reason, 'message': 'PRIVATE_PROVIDER_DETAIL', 'code': 'PRIVATE_PROVIDER_CODE',
    }) for reason in reasons] + [event(event='voice.asr.failed', details={
        'reason_code': 'PRIVATE_PROVIDER_CODE', 'message': 'PRIVATE_PROVIDER_DETAIL',
    })])
    _, rows, raw = unpack(build(tmp_path))
    assert [row['details'] for row in rows] == [{'reason_code': reason} for reason in reasons] + [{}]
    assert b'PRIVATE_' not in raw


def test_socket_field_types_and_fixed_provider_category_survive_zip_without_original_values(tmp_path):
    logs(tmp_path, [event(event='voice.realtime.socket_open_failed', details={
        'socket_message_code_type': 'string', 'socket_message_message_type': 'array',
        'socket_provider_category': 'rate_limit', 'code': 'PRIVATE_PROVIDER_CODE', 'message': 'PRIVATE_PROVIDER_MESSAGE',
    }), event(event='voice.realtime.socket_message_rejected', details={
        'socket_message_code_type': 'null', 'socket_message_message_type': 'boolean', 'socket_provider_category': 'unknown',
    }), event(event='voice.realtime.socket_message_rejected', details={
        'socket_message_code_type': 'PRIVATE_CODE', 'socket_message_message_type': 'PRIVATE_MESSAGE',
        'socket_provider_category': 'PRIVATE_UNKNOWN',
    })])
    _, rows, raw = unpack(build(tmp_path))
    assert [row['event'] for row in rows] == ['voice.realtime.socket_open_failed', 'voice.realtime.socket_message_rejected', 'voice.realtime.socket_message_rejected']
    assert [row['details'] for row in rows] == [{'socket_message_code_type': 'string',
        'socket_message_message_type': 'array', 'socket_provider_category': 'rate_limit'},
        {'socket_message_code_type': 'null', 'socket_message_message_type': 'boolean', 'socket_provider_category': 'unknown'}, {}]
    assert b'PRIVATE_' not in raw


def test_finite_speech_arbitration_metadata_survives_zip_without_private_content(tmp_path):
    dispositions = ('finished', 'unavailable', 'cancelled', 'expired', 'superseded', 'full')
    priorities = ('reminder', 'dialogue', 'lesson', 'urgent')
    expected = [{'disposition': disposition, 'priority': priority}
                for disposition in dispositions for priority in priorities]
    logs(tmp_path, [event(source='renderer', event='voice.tts.arbitration', details={
        **details, 'transcript': 'PRIVATE_SPEECH', 'message': 'PRIVATE_SECRET',
    }) for details in expected] + [event(event='voice.tts.arbitration', details={
        'disposition': value, 'priority': value,
    }) for value in ('cancelled_PRIVATE_SPEECH', 'urgent_PRIVATE_SECRET', 'PRIVATE_SPEECH', 1, True, None)]
         + [event(event='voice.tts.arbitration', details={'disposition': 'urgent', 'priority': 'cancelled'})])
    manifest, rows, raw = unpack(build(tmp_path))
    assert [row['details'] for row in rows[:24]] == expected
    assert [row['details'] for row in rows[24:]] == [{}] * 7
    assert b'PRIVATE_' not in raw
    logger = Path(__file__).resolve().parents[3] / 'src/main/diagnostics.ts'
    assert manifest['logger_schema_source_sha256'] == hashlib.sha256(logger.read_bytes()).hexdigest()


def test_finite_wake_control_events_survive_zip_without_names_or_speech(tmp_path):
    events = ('voice.wake.control_started', 'voice.wake.control_result', 'voice.wake.detected')
    details = {'mode': 'wake', 'local_only': True, 'woke': False, 'elapsed_ms': 42, 'audio_bytes': 16044}
    logs(tmp_path, [event(source='renderer', event=name, details={**details,
        'wake_word': 'PRIVATE_WAKE_NAME', 'name': 'PRIVATE_WAKE_NAME', 'transcript': 'PRIVATE_SPEECH',
        'text': 'PRIVATE_SPEECH', 'audio': 'PRIVATE_AUDIO', 'state': 'PRIVATE_STATE',
    }) for name in events] + [event(event='voice.wake.PRIVATE_WAKE_NAME', details=details)])
    manifest, rows, raw = unpack(build(tmp_path))
    assert [row['event'] for row in rows] == [*events, 'unregistered_event']
    assert [row['details'] for row in rows] == [details] * 4
    assert b'PRIVATE_' not in raw
    logger = Path(__file__).resolve().parents[3] / 'src/main/diagnostics.ts'
    assert manifest['logger_schema_source_sha256'] == hashlib.sha256(logger.read_bytes()).hexdigest()


def test_closed_metadata_no_content_and_safe_location_only(tmp_path):
    private = "PRIVATE_CHILD_ASR_TOKEN_PASSWORD_C_WINDOWS_family_sql"
    row = event(message=private, transcript=private, child_id=private, stack=private,
                details={"method": "POST", "route_template": "/v1/agent-turns/{turn_id}/execute",
                         "statusCode": 409, "error_code": "MODEL_TIMEOUT", "version": "1.24.3",
                         "platform": "win32", "architecture": "x64", "packaged": True,
                         "error": {"name": "TypeError", "message": private, "stack": private},
                         "raw_audio_or_video": False, "transcript": private, "path": private,
                         "runtime": private, "durationMs": private, "source_hash": private,
                         "stack_frames": [{"module_id": "home_study_agent.agent_runtime", "line": 150, "column": 4},
                                          {"module_id": private, "line": 5, "column": 8},
                                          {"module_id": "main/index", "line": 9, "column": 1, "message": private}]})
    path = logs(tmp_path, [row, event(level="warn")])
    before = path.read_bytes()
    bundle = build(tmp_path)
    manifest, rows, raw = unpack(bundle)
    assert private.encode() not in raw and CORRELATION.encode() not in raw
    assert path.read_bytes() == before
    assert rows[0]["correlation_id"] == rows[1]["correlation_id"] == "r000001"
    assert rows[0]["details"]["stack_frames"] == [{"module_id": "home_study_agent.agent_runtime", "line": 150, "column": 4}]
    assert rows[0]["details"]["version"] == "1.24.3"
    assert rows[0]["details"]["packaged"] is True
    assert "runtime" not in rows[0]["details"] and "durationMs" not in rows[0]["details"]
    assert manifest["counts"]["fields_dropped"] >= 10
    assert manifest["events_included"] == 2
    assert manifest["coverage"]["full_day_completeness"] == "not_asserted"


def test_window_exact_offsets_rotation_and_invalid_json(tmp_path):
    logs(tmp_path, [event(timestamp=START.isoformat()), event(timestamp=END.isoformat()),
                    event(timestamp=(START - timedelta(microseconds=1)).isoformat()), event(timestamp="2026-10-27T15:00:00")])
    path = logs(tmp_path, [event(timestamp=(START + timedelta(hours=1)).astimezone(timezone(timedelta(hours=8))).isoformat())], "runtime.jsonl.1")
    with path.open("ab") as stream:
        stream.write(b'{"timestamp":"x","timestamp":"y"}\n')
        stream.write(b'{"number":NaN}\n')
        stream.write(b'\xff\xfe\n')
        stream.write(b'{"incomplete":true}')
    manifest, rows, _ = unpack(build(tmp_path))
    assert len(rows) == 2
    assert rows[0]["timestamp"] == "2026-10-27T14:00:00Z"
    assert manifest["counts"]["invalid_lines"] == 3
    assert manifest["counts"]["partial_lines"] == 1
    assert manifest["counts"]["invalid_timestamps"] == 1
    assert manifest["counts"]["outside_window"] == 2


@pytest.mark.parametrize("window", [(START.replace(tzinfo=None), END), (START, END.replace(tzinfo=None)), (END, START), (START, START)])
def test_naive_or_unordered_window_rejected_before_io(tmp_path, monkeypatch, window):
    monkeypatch.setattr(diagnostic, "_scan", lambda *args: pytest.fail("must not touch logs"))
    with pytest.raises(ValueError):
        diagnostic.build_daily_diagnostic_bundle(tmp_path, start_utc=window[0], end_utc=window[1])


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_nonregular_or_aliased_file_is_not_read(tmp_path, kind):
    directory = tmp_path / "diagnostics" / "logs"
    directory.mkdir(parents=True)
    outside = tmp_path / "private.sqlite"
    outside.write_bytes(b"PRIVATE_NOT_A_RUNTIME_LOG")
    path = directory / "runtime.jsonl"
    if kind == "symlink":
        symlink_fixture(path, outside)
    elif kind == "hardlink":
        os.link(outside, path)
    else:
        if not hasattr(os, "mkfifo"):
            pytest.skip("FIFO files are not supported on this platform")
        os.mkfifo(path)
    manifest, rows, raw = unpack(build(tmp_path))
    assert rows == [] and b"PRIVATE_NOT_A_RUNTIME_LOG" not in raw
    assert manifest["counts"]["files_rejected"] == 1
    assert "unsafe_or_unreadable_file" in manifest["coverage"]["issues"]


def test_symlink_parent_and_traversal_fail_closed(tmp_path):
    outside = tmp_path / "outside"
    logs(outside, [event()])
    alias = tmp_path / "alias"
    symlink_fixture(alias, outside, target_is_directory=True)
    bundle = diagnostic.build_daily_diagnostic_bundle(alias, start_utc=START, end_utc=END)
    assert bundle.manifest["events_included"] == 0
    assert "unsafe_or_unreadable_directory" in bundle.manifest["coverage"]["issues"]
    bundle = diagnostic.build_daily_diagnostic_bundle(tmp_path / ".." / tmp_path.name, start_utc=START, end_utc=END)
    assert "unsafe_or_unreadable_directory" in bundle.manifest["coverage"]["issues"]


def test_only_fixed_log_names_and_no_database_media_config_crashes(tmp_path, monkeypatch):
    logs(tmp_path, [event()])
    for relative in ("diagnostics/logs/runtime.jsonl.100", "diagnostics/logs/runtime.jsonl.old", "diagnostics/crashes/memory.dmp", "family.db", "smtp.json", "voice.wav"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"SECRET_MUST_NEVER_BE_OPENED")
    opened = []
    original = diagnostic._open_held

    def witness(path):
        opened.append(path)
        return original(path)

    monkeypatch.setattr(diagnostic, "_open_held", witness)
    manifest, rows, raw = unpack(build(tmp_path))
    assert len(rows) == 1 and b"SECRET_MUST_NEVER_BE_OPENED" not in raw
    assert all(path.name not in {"runtime.jsonl.100", "runtime.jsonl.old", "memory.dmp", "family.db", "smtp.json", "voice.wav"} for path in opened)
    assert manifest["release"]["mode"] == "source"
    assert b"/Users/" not in raw


def test_oversized_line_discard_then_valid_event_and_input_budget(tmp_path, monkeypatch):
    path = logs(tmp_path, [])
    path.write_bytes(b"P" * (64 * 1024 + 20) + b"\n" + json.dumps(event(level="error")).encode() + b"\n")
    manifest, rows, raw = unpack(build(tmp_path))
    assert len(rows) == 1 and manifest["counts"]["oversized_lines"] == 1
    assert b"PPPPP" not in raw
    monkeypatch.setattr(diagnostic, "LIMITS", replace(diagnostic.LIMITS, max_input_bytes=32))
    manifest, rows, _ = unpack(build(tmp_path))
    assert not rows and manifest["counts"]["input_bytes"] <= 32
    assert manifest["coverage"]["truncated"] and "input_limit" in manifest["coverage"]["issues"]


def test_error_priority_and_bounded_context_under_raw_limit(tmp_path, monkeypatch):
    rows = [event(details={"count": index}) for index in range(300)]
    rows[50]["level"] = "error"
    logs(tmp_path, rows)
    monkeypatch.setattr(diagnostic, "LIMITS", replace(diagnostic.LIMITS, max_raw_bytes=8 * 1024, max_events=20))
    bundle = build(tmp_path)
    manifest, selected, _ = unpack(bundle)
    numbers = {row["details"]["count"] for row in selected}
    assert {47, 48, 49, 50, 51, 52, 53} <= numbers
    with zipfile.ZipFile(BytesIO(bundle.content)) as archive:
        assert sum(info.file_size for info in archive.infolist()) <= 8 * 1024
    assert manifest["coverage"]["truncated"]
    assert manifest["counts"]["events_dropped_budget"] > 0
    assert len(selected) <= 20


def test_zip_budget_reselects_without_exceeding_limit(tmp_path, monkeypatch):
    rng = random.Random(1)
    rows = [event(details={"durationMs": rng.randrange(10**11), "count": rng.randrange(10**11), "sample_rate": rng.randrange(10**11)}) for _ in range(900)]
    logs(tmp_path, rows)
    monkeypatch.setattr(diagnostic, "LIMITS", replace(diagnostic.LIMITS, max_zip_bytes=2200))
    bundle = build(tmp_path)
    manifest, selected, _ = unpack(bundle)
    assert len(bundle.content) <= 2200 and len(selected) < 900
    assert manifest["coverage"]["truncated"]


def test_file_replaced_after_scan_discards_its_events(tmp_path, monkeypatch):
    path = logs(tmp_path, [event()])
    original = diagnostic._held_unchanged

    def replace_on_check(candidate, *args, **kwargs):
        if candidate == path:
            path.rename(path.with_suffix(".moved"))
            path.write_bytes(b"PRIVATE_REPLACEMENT")
        return original(candidate, *args, **kwargs)

    monkeypatch.setattr(diagnostic, "_held_unchanged", replace_on_check)
    manifest, rows, raw = unpack(build(tmp_path))
    assert rows == [] and b"PRIVATE_REPLACEMENT" not in raw
    assert manifest["counts"]["events_discarded_changed_file"] == 1
    assert "file_identity_changed" in manifest["coverage"]["issues"]


def test_unrelated_ancestor_directory_change_does_not_discard_log(tmp_path, monkeypatch):
    path = logs(tmp_path, [event()])
    original = diagnostic._held_unchanged
    def add_unrelated_directory(candidate, *args, **kwargs):
        if candidate == path:
            (tmp_path / 'unrelated-synthetic-directory').mkdir()
        return original(candidate, *args, **kwargs)
    monkeypatch.setattr(diagnostic, '_held_unchanged', add_unrelated_directory)
    manifest, rows, _ = unpack(build(tmp_path))
    assert len(rows) == 1
    assert 'file_identity_changed' not in manifest['coverage']['issues']


def test_changed_ancestor_identity_still_discards_log(tmp_path, monkeypatch):
    path = logs(tmp_path, [event()])
    original = diagnostic._chain
    changed = False
    def changed_ancestor(candidate):
        chain = original(candidate)
        if changed:
            first = chain[0]
            return ((first[0], first[1] + 1, first[2]), *chain[1:])
        return chain
    monkeypatch.setattr(diagnostic, '_chain', changed_ancestor)
    held, before, parents = diagnostic._open_held(path)
    with held:
        changed = True
        assert not diagnostic._held_unchanged(path, held, before, parents, append_allowed=True)


@pytest.mark.parametrize('replacement', ['directory', 'symlink'])
def test_replaced_ancestor_discards_log_even_when_file_is_original(tmp_path, monkeypatch, replacement):
    path = logs(tmp_path, [event()])
    original = diagnostic._held_unchanged
    def replace_parent(candidate, *args, **kwargs):
        if candidate == path:
            current = tmp_path / 'diagnostics'
            moved = tmp_path / 'moved-diagnostics'
            current.rename(moved)
            if replacement == 'symlink':
                try:
                    current.symlink_to(moved, target_is_directory=True)
                except OSError:
                    if os.name == 'nt': pytest.skip('Windows user cannot create directory symlinks')
                    raise
            else:
                (current / 'logs').mkdir(parents=True)
                (moved / 'logs' / 'runtime.jsonl').rename(path)
        return original(candidate, *args, **kwargs)
    monkeypatch.setattr(diagnostic, '_held_unchanged', replace_parent)
    manifest, rows, _ = unpack(build(tmp_path))
    assert rows == []
    assert manifest['counts']['events_discarded_changed_file'] == 1
    assert any(issue in manifest['coverage']['issues'] for issue in ('file_identity_changed', 'unsafe_or_unreadable_file'))


def test_live_append_is_not_read_beyond_held_snapshot(tmp_path, monkeypatch):
    path = logs(tmp_path, [event()])
    original = diagnostic._held_unchanged

    def append_on_check(candidate, *args, **kwargs):
        if candidate == path:
            with path.open("ab") as stream:
                stream.write(json.dumps(event(level="error")).encode() + b"\n")
        return original(candidate, *args, **kwargs)

    monkeypatch.setattr(diagnostic, "_held_unchanged", append_on_check)
    manifest, rows, _ = unpack(build(tmp_path))
    assert len(rows) == 1 and manifest["counts"]["live_append_files"] == 1
    assert manifest["coverage"]["full_day_completeness"] == "not_asserted"


def test_windows_identity_normalizes_only_cross_source_execute_bits(tmp_path, monkeypatch):
    path = tmp_path / "synthetic.exe"
    path.write_bytes(b"PUBLIC_TEST_ONLY")
    path.chmod(0o777)
    real_fstat = os.fstat

    class HandleStat:
        def __init__(self, value, mode):
            self._value, self.st_mode = value, mode

        def __getattr__(self, name):
            return getattr(self._value, name)

    monkeypatch.setattr(diagnostic, "_WINDOWS", True)
    def windows_fstat(fd):
        value = real_fstat(fd)
        return HandleStat(value, value.st_mode & ~(stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
    monkeypatch.setattr(diagnostic.os, "fstat", windows_fstat)
    digest, status = diagnostic._hash_file(path)
    assert (digest, status) == (hashlib.sha256(b"PUBLIC_TEST_ONLY").hexdigest(), "verified")
    original = path.lstat()
    with path.open("rb") as stream:
        held = windows_fstat(stream.fileno())
    assert diagnostic._same_path_and_handle(original, held)
    for changed in ("st_dev", "st_ino", "st_nlink", "st_mode"):
        altered = HandleStat(held, held.st_mode)
        setattr(altered, changed, getattr(held, changed) ^ (stat.S_IWUSR if changed == "st_mode" else 1))
        assert not diagnostic._same_path_and_handle(original, altered)
    monkeypatch.setattr(diagnostic, "_WINDOWS", False)
    assert not diagnostic._same_path_and_handle(original, held)


@pytest.mark.parametrize("changed_source", ["path_execute", "handle_execute", "reparse"])
def test_windows_held_recheck_rejects_same_source_changes(monkeypatch, changed_source):
    def node(mode, attributes=0):
        return SimpleNamespace(st_dev=1, st_ino=2, st_nlink=1, st_mode=mode, st_file_attributes=attributes,
                               st_size=5, st_mtime_ns=6, st_ctime_ns=7)
    path_before = node(stat.S_IFREG | 0o777)
    handle_before = node(stat.S_IFREG | 0o666)
    path_after = node(path_before.st_mode ^ (stat.S_IXUSR if changed_source == "path_execute" else 0),
                      0x400 if changed_source == "reparse" else 0)
    handle_after = node(handle_before.st_mode ^ (stat.S_IXUSR if changed_source == "handle_execute" else 0))
    path = SimpleNamespace(parent=object(), lstat=lambda: path_after)
    stream = SimpleNamespace(fileno=lambda: 123)
    monkeypatch.setattr(diagnostic, "_WINDOWS", True)
    monkeypatch.setattr(diagnostic.os, "fstat", lambda _: handle_after)
    monkeypatch.setattr(diagnostic, "_chain", lambda _: ("same-parent",))
    assert not diagnostic._held_unchanged(path, stream, handle_before,
        diagnostic._HeldPath(("same-parent",), path_before), append_allowed=False)


def test_frozen_release_accepts_windows_executable_handle_mode_difference(tmp_path, monkeypatch):
    resources = tmp_path / "install" / "resources"
    backend = resources / "local-agent" / "home-study-local-agent.exe"
    backend.parent.mkdir(parents=True)
    backend.write_bytes(b"SYNTHETIC_BACKEND")
    backend.chmod(0o777)
    frontend = resources / "app.asar"
    frontend.write_bytes(b"SYNTHETIC_FRONTEND")
    metadata = {"schema": 1, "release_id": "synthetic-release", "agent_sha256": hashlib.sha256(backend.read_bytes()).hexdigest(),
                "app_asar_sha256": hashlib.sha256(frontend.read_bytes()).hexdigest()}
    (resources / "release-info.json").write_text(json.dumps(metadata))
    real_fstat = os.fstat

    class HandleStat:
        def __init__(self, value):
            self._value = value
            self.st_mode = value.st_mode & ~(stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        def __getattr__(self, name):
            return getattr(self._value, name)

    monkeypatch.setattr(diagnostic, "_WINDOWS", True)
    monkeypatch.setattr(diagnostic.os, "fstat", lambda fd: HandleStat(real_fstat(fd)))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(backend))
    manifest, _, _ = unpack(build(tmp_path))
    assert manifest["release"]["backend_status"] == "verified"
    assert manifest["release"]["release_id"] == "synthetic-release"
    assert manifest["release"]["release_id_status"] == "verified_actual_code_pair"


def test_frozen_release_requires_actual_dual_sha_and_fixed_layout(tmp_path, monkeypatch):
    resources = tmp_path / "install" / "resources"
    backend = resources / "local-agent" / "home-study-local-agent.exe"
    backend.parent.mkdir(parents=True)
    backend.write_bytes(b"PUBLIC_TEST_ONLY_BACKEND")
    frontend = resources / "app.asar"
    frontend.write_bytes(b"PUBLIC_TEST_ONLY_FRONTEND")
    metadata = {"schema": 1, "release_id": "pilot-20261028-r1", "agent_sha256": hashlib.sha256(backend.read_bytes()).hexdigest(), "app_asar_sha256": hashlib.sha256(frontend.read_bytes()).hexdigest(), "installed_at_utc": "2026-10-28T00:00:00Z", "secret": "PRIVATE_VALUE"}
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(backend))
    (resources / "release-info.json").write_text(json.dumps(metadata))
    manifest, _, raw = unpack(build(tmp_path))
    assert manifest["release"]["release_id"] == "pilot-20261028-r1"
    assert manifest["release"]["agent_sha256"] == metadata["agent_sha256"]
    assert manifest["release"]["release_id_status"] == "verified_actual_code_pair"
    assert manifest["release"]["release_info_sha256"] == hashlib.sha256((resources / "release-info.json").read_bytes()).hexdigest()
    assert b"PRIVATE_VALUE" not in raw and str(resources).encode() not in raw
    metadata["agent_sha256"] = "0" * 64
    (resources / "release-info.json").write_text(json.dumps(metadata))
    manifest, _, _ = unpack(build(tmp_path, release_info=metadata))
    assert "release_id" not in manifest["release"]
    assert manifest["release"]["release_id_status"] == "unverified"
    assert "release_info_sha256" not in manifest["release"]
    (resources / "release-info.json").unlink()
    metadata["agent_sha256"] = hashlib.sha256(backend.read_bytes()).hexdigest()
    metadata["release_info_sha256"] = "f" * 64
    manifest, _, _ = unpack(build(tmp_path, release_info=metadata))
    assert manifest["release"]["release_id_status"] == "verified_actual_code_pair"
    assert "release_info_sha256" not in manifest["release"]


def test_frozen_release_symlink_and_untrusted_paths_cannot_authorize(tmp_path, monkeypatch):
    resources = tmp_path / "resources"
    backend = resources / "local-agent" / "home-study-local-agent.exe"
    backend.parent.mkdir(parents=True)
    backend.write_bytes(b"BACKEND")
    outside = tmp_path / "private"
    outside.write_bytes(b"FRONTEND_OR_SECRET")
    symlink_fixture(resources / "app.asar", outside)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(backend))
    manifest, _, raw = unpack(build(tmp_path, release_info={"release_id": "forged", "app_asar_path": str(outside)}))
    assert manifest["release"]["frontend_status"] == "unavailable_or_unsafe"
    assert "release_id" not in manifest["release"] and b"FRONTEND_OR_SECRET" not in raw


def test_bundle_frozen_bytes_detached_manifest_and_deterministic(tmp_path):
    logs(tmp_path, [event()])
    first, second = build(tmp_path), build(tmp_path)
    assert first.content == second.content and first.sha256 == second.sha256
    with pytest.raises(FrozenInstanceError):
        first.sha256 = "0" * 64
    first.manifest["events_included"] = 999
    assert first.manifest["events_included"] == 1
    assert "content=" not in repr(first)


def test_missing_rotation_and_bad_schema_are_explicit(tmp_path):
    logs(tmp_path, [event(privacy_policy_version="old_raw_schema"), event(event="PRIVATE_UNKNOWN_EVENT")], "runtime.jsonl.2")
    manifest, rows, raw = unpack(build(tmp_path))
    assert "rotation_gap" in manifest["coverage"]["issues"]
    assert manifest["counts"]["unsupported_rows"] == 1
    assert manifest["counts"]["unknown_events"] == 1
    assert rows[0]["event"] == "unregistered_event"
    assert b"PRIVATE_UNKNOWN_EVENT" not in raw


def test_empty_logs_still_return_truthful_safe_package(tmp_path):
    manifest, rows, _ = unpack(build(tmp_path))
    assert rows == [] and manifest["events_included"] == 0
    assert "log_directory_missing" in manifest["coverage"]["issues"]
    assert manifest["release"]["mode"] == "source"


@pytest.mark.parametrize("value", [-1, True, 10**30, "PRIVATE", float("inf")])
def test_disallowed_numeric_and_free_text_values_are_filtered(tmp_path, value):
    logs(tmp_path, [event(details={"count": value, "statusCode": 999, "version": "1.2.3-PRIVATE"})])
    manifest, rows, raw = unpack(build(tmp_path))
    if value == float("inf"):
        assert not rows and manifest["counts"].get("invalid_lines") == 1
    else:
        assert rows[0]["details"] == {}
    assert b"PRIVATE" not in raw
    assert manifest["counts"].get("fields_dropped", 0) >= 3 or manifest["counts"].get("invalid_lines", 0) == 1


def test_fractional_timestamps_are_chronological(tmp_path):
    logs(tmp_path, [event(timestamp="2026-10-27T15:00:00.1Z"), event(timestamp="2026-10-27T15:00:00Z")])
    _, rows, _ = unpack(build(tmp_path))
    assert [row["timestamp"] for row in rows] == ["2026-10-27T15:00:00Z", "2026-10-27T15:00:00.100000Z"]


def test_rotation_during_slot_scan_never_counts_same_inode_twice(tmp_path, monkeypatch):
    path = logs(tmp_path, [event()])
    original = diagnostic._scan

    def rotate_after_first(candidate, *args):
        original(candidate, *args)
        if candidate == path:
            path.rename(path.parent / "runtime.jsonl.1")

    monkeypatch.setattr(diagnostic, "_scan", rotate_after_first)
    manifest, rows, _ = unpack(build(tmp_path))
    assert len(rows) == 1
    assert manifest["counts"]["duplicate_files_skipped"] == 1
    assert "rotation_reobserved_file" in manifest["coverage"]["issues"]
