"""Bounded, metadata-only email attachment; never a family-data export.

The caller owns the reporting window and freezes the returned bytes in its outbox.
This module neither modifies local logs nor opens a database, network or mail client.
"""
from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import platform
import re
import stat
import sys
from typing import Any
import zipfile

from .daily_diagnostic_schema import (
    BOOLEANS, CONTAINERS, ENUMS, ERROR_CODES, EVENTS, LOGGER_SOURCE_SHA256,
    NUMBERS, REASON_CODES, ROUTES_SOURCE_SHA256, TOKEN_COUNTS,
)


@dataclass(frozen=True)
class DiagnosticLimits:
    max_input_bytes: int = 128 * 1024 * 1024
    max_raw_bytes: int = 16 * 1024 * 1024
    max_zip_bytes: int = 5 * 1024 * 1024
    max_line_bytes: int = 64 * 1024
    max_events: int = 100_000
    context_events: int = 3


# Source-level configuration, never supplied by a log row or public API.
LIMITS = DiagnosticLimits()
_MAX_ROTATIONS = 32
_MAX_CODE_BYTES = 1024 * 1024 * 1024
_WINDOWS = os.name == "nt"
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", re.I)
_VERSION = re.compile(r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}")
_SHA = re.compile(r"[0-9a-f]{64}")
_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})")
_ERROR_PATTERN = re.compile(r"(?:FOXMAIL_MAPI_(?:LOGON|SEND)_(?:0X[0-9A-F]{8}|[0-9]{1,10})|(?:PILOT_DIGEST_(?:CLAIM|RESULT)|SMTP_SUBMIT)_HTTP_[1-5][0-9]{2})")
_SOURCES = frozenset({"main", "renderer", "local_service", "device", "model", "ocr", "system"})
_LEVELS = frozenset({"debug", "info", "warn", "error"})
_FRAMES = frozenset({
    "main/index", "main/diagnostics", "main/local-service", "main/pilot-test-email",
    "renderer/api", "renderer/SessionWorkspace", "renderer/voiceCaptureContext",
    "home_study_agent.app", "home_study_agent.agent_runtime", "home_study_agent.agent_orchestrator",
    "home_study_agent.voice_capture_binding", "home_study_agent.report_delivery",
    "home_study_agent.notification_channels", "home_study_agent.pilot_test_telemetry",
    "home_study_agent.daily_diagnostic_bundle",
})


@dataclass(frozen=True)
class DiagnosticBundle:
    filename: str
    content: bytes = field(repr=False)
    sha256: str
    _manifest_json: bytes = field(repr=False)

    @property
    def manifest(self) -> dict:
        """A detached copy: consumers cannot mutate the frozen ZIP's manifest."""
        return json.loads(self._manifest_json)


def _encoded(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _json(raw: bytes) -> Any:
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("DUPLICATE_JSON_KEY")
            result[key] = value
        return result

    def invalid_constant(_):
        raise ValueError("NONFINITE_JSON")

    return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=invalid_constant)


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("DIAGNOSTIC_WINDOW_MUST_BE_AWARE")
    return value.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _identity(value: os.stat_result) -> tuple:
    return value.st_dev, value.st_ino, value.st_nlink, value.st_mode


def _directory_identity(value: os.stat_result) -> tuple:
    # A directory's link count changes when an unrelated child directory is
    # created or removed. It is not a stable identity for the held path.
    # Keep full mode and filesystem identity; _safe_node still rejects aliases.
    return value.st_dev, value.st_ino, value.st_mode


def _same_path_and_handle(path_node: os.stat_result, held_node: os.stat_result) -> bool:
    # Windows derives executable permission bits from a path extension, while
    # CRT fstat on the same handle need not include them. Never weaken same-
    # source before/after checks, file type, ownership identity or write bits.
    path_identity, held_identity = _identity(path_node), _identity(held_node)
    if not _WINDOWS:
        return path_identity == held_identity
    execute_bits = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    return (path_identity[:3] == held_identity[:3]
            and (path_identity[3] & ~execute_bits) == (held_identity[3] & ~execute_bits))


@dataclass(frozen=True)
class _HeldPath:
    parents: tuple
    path_before: os.stat_result


def _safe_node(value: os.stat_result, directory: bool = False) -> bool:
    return (
        not stat.S_ISLNK(value.st_mode)
        and not (getattr(value, "st_file_attributes", 0) & 0x400)  # Windows reparse points/junctions.
        and (stat.S_ISDIR(value.st_mode) if directory else stat.S_ISREG(value.st_mode) and value.st_nlink == 1)
    )


def _chain(path: Path) -> tuple:
    """Reject aliases in every component, without resolve() following an alias."""
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("UNSAFE_DIAGNOSTIC_PATH")
    nodes = []
    for item in (*reversed(path.parents), path):
        node = item.lstat()
        if not _safe_node(node, directory=True):
            raise ValueError("UNSAFE_DIAGNOSTIC_DIRECTORY")
        nodes.append(_directory_identity(node))
    return tuple(nodes)


def _windows_read_fd(path: Path) -> int:
    """Allow the live logger's rename/delete, without following a final reparse.

    HANDLE ownership transfers exactly once to the CRT. Every failure before
    transfer closes the HANDLE; every failure after transfer closes the fd.
    The caller still performs its original held-fstat/path/parent identity checks.
    """
    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateFileW(str(path), 0x80000000, 0x7, None, 3,
                                0x00200000 | 0x08000000, None)
    if handle in (None, ctypes.c_void_p(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY | os.O_NOINHERIT)
        if descriptor < 0:
            raise OSError("DIAGNOSTIC_HANDLE_TRANSFER_FAILED")
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    try:
        os.set_inheritable(descriptor, False)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read_fd(path: Path, flags: int) -> int:
    return _windows_read_fd(path) if os.name == "nt" else os.open(path, flags)


def _open_held(path: Path):
    before_parent = _chain(path.parent)
    before = path.lstat()
    if not _safe_node(before):
        raise ValueError("UNSAFE_DIAGNOSTIC_FILE")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = _read_fd(path, flags)
    try:
        held = os.fstat(descriptor)
        if not _safe_node(held) or not _same_path_and_handle(before, held) or _chain(path.parent) != before_parent:
            raise ValueError("DIAGNOSTIC_FILE_CHANGED")
        return os.fdopen(descriptor, "rb", buffering=0), held, _HeldPath(before_parent, before)
    except BaseException:
        os.close(descriptor)
        raise


def _held_unchanged(path: Path, stream, before, parents, *, append_allowed: bool) -> bool:
    after = os.fstat(stream.fileno())
    pathname = path.lstat()
    if not _safe_node(after) or not _safe_node(pathname):
        return False
    if (_identity(before) != _identity(after) or _identity(parents.path_before) != _identity(pathname)
            or not _same_path_and_handle(pathname, after) or _chain(path.parent) != parents.parents):
        return False
    if append_allowed:
        return (after.st_size >= before.st_size and pathname.st_size >= before.st_size
                and (after.st_size > before.st_size or (after.st_mtime_ns, after.st_ctime_ns) == (before.st_mtime_ns, before.st_ctime_ns)))
    return (before.st_size, before.st_mtime_ns, before.st_ctime_ns) == (after.st_size, after.st_mtime_ns, after.st_ctime_ns) == (pathname.st_size, pathname.st_mtime_ns, pathname.st_ctime_ns)


def _hash_file(path: Path) -> tuple[str | None, str]:
    try:
        stream, before, parents = _open_held(path)
        with stream:
            if before.st_size > _MAX_CODE_BYTES:
                return None, "size_limit"
            digest = hashlib.sha256()
            remaining = before.st_size
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    return None, "changed"
                digest.update(chunk)
                remaining -= len(chunk)
            if not _held_unchanged(path, stream, before, parents, append_allowed=False):
                return None, "changed"
            return digest.hexdigest(), "verified"
    except FileNotFoundError:
        return None, "missing"
    except (OSError, ValueError):
        return None, "unavailable_or_unsafe"


def _read_release(path: Path) -> tuple[dict | None, str | None]:
    try:
        stream, before, parents = _open_held(path)
        with stream:
            if before.st_size > 64 * 1024:
                return None, None
            raw = stream.read(64 * 1024 + 1)
            if not _held_unchanged(path, stream, before, parents, append_allowed=False):
                return None, None
            value = _json(raw)
            return (value, hashlib.sha256(raw).hexdigest()) if type(value) is dict else (None, None)
    except (OSError, ValueError, RecursionError):
        return None, None


def _release_identity(release_info) -> dict:
    frozen = bool(getattr(sys, "frozen", False))
    result = {"mode": "frozen" if frozen else "source", "scope": "collector_current_code_not_historical_event_authority", "platform": sys.platform if sys.platform in {"win32", "darwin", "linux"} else "other"}
    result["architecture"] = {"x86_64": "x64", "amd64": "x64", "AMD64": "x64", "arm64": "arm64", "aarch64": "arm64", "i386": "ia32", "i686": "ia32"}.get(platform.machine(), "other")
    if not frozen:
        digest, status = _hash_file(Path(__file__).absolute())
        result.update(source_module_sha256=digest, source_module_status=status, release_id_status="not_an_installed_release")
        return result
    executable = Path(sys.executable)
    backend_sha, backend_status = _hash_file(executable)
    result.update(agent_sha256=backend_sha, backend_status=backend_status)
    if executable.name.lower() != "home-study-local-agent.exe" or executable.parent.name != "local-agent" or executable.parent.parent.name != "resources":
        result.update(frontend_status="unsupported_install_layout", release_id_status="unverified")
        return result
    resources = executable.parent.parent
    frontend_sha, frontend_status = _hash_file(resources / "app.asar")
    result.update(app_asar_sha256=frontend_sha, frontend_status=frontend_status, release_id_status="unverified")
    for candidate, release_file_sha in (_read_release(resources / "release-info.json"), (release_info, None)):
        if type(candidate) is not dict or not backend_sha or not frontend_sha:
            continue
        if type(candidate.get("schema")) is not int or candidate["schema"] != 1 or candidate.get("agent_sha256") != backend_sha or candidate.get("app_asar_sha256") != frontend_sha:
            continue
        identifier = candidate.get("release_id")
        if type(identifier) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", identifier):
            result.update(release_id=identifier, release_id_status="verified_actual_code_pair")
            if release_file_sha:
                result["release_info_sha256"] = release_file_sha
            source_sha = candidate.get("source_manifest_sha256")
            if type(source_sha) is str and _SHA.fullmatch(source_sha):
                result["declared_source_manifest_sha256"] = source_sha
            break
    return result


def _code(value) -> bool:
    return type(value) is str and (value in ERROR_CODES or _ERROR_PATTERN.fullmatch(value) is not None)


def _safe_number(key: str, value) -> bool:
    if type(value) not in {int, float} or not (-1_000_000_000_000 <= value <= 1_000_000_000_000):
        return False
    if key == "exitCode":
        return type(value) is int and -2**31 <= value <= 2**32 - 1
    if key == "statusCode":
        return type(value) is int and 100 <= value <= 599
    return value >= 0


def _details(value, counts: Counter, depth: int = 0, budget: list | None = None) -> dict:
    if type(value) is not dict or depth > 4:
        counts["fields_dropped"] += 1
        return {}
    budget = budget if budget is not None else [200]
    result = {}
    for index, (key, item) in enumerate(value.items()):
        budget[0] -= 1
        if index >= 100 or budget[0] < 0:
            counts["fields_dropped"] += len(value) - index
            break
        accepted = False
        if key in {'restored', 'confirmed'} and type(item) is int and 0 <= item <= 1000000:
            result[key] = item
            accepted = True
        elif key == 'recovery_windows' and type(item) is int and 0 <= item <= 2:
            result[key] = item
            accepted = True
        elif key == 'learning_tasks_replayed' and type(item) is int and item == 0:
            result[key] = 0
            accepted = True
        elif key in NUMBERS | TOKEN_COUNTS and _safe_number(key, item):
            result[key] = item
            accepted = True
        elif key in BOOLEANS | {"packaged"} and type(item) is bool:
            result[key] = item
            accepted = True
        elif key in {"physical_audibility_verified", "completion_verified", "acoustic_quality_verified", "contains_transcript"} and item is False:
            result[key] = False
            accepted = True
        elif key in {"code", "error_code"} and _code(item):
            result[key] = item
            accepted = True
        elif key == "reason_code" and type(item) is str and item in REASON_CODES:
            result[key] = item
            accepted = True
        elif key in ENUMS and type(item) is str and item in ENUMS[key]:
            result[key] = item
            accepted = True
        elif key == "version" and type(item) is str and _VERSION.fullmatch(item):
            result[key] = item
            accepted = True
        elif key == "connection_generation" and type(item) is int and 1 <= item <= 1_000_000_000:
            result[key] = item
            accepted = True
        elif key == "retry_delay_ms" and type(item) is int and item in {1000, 3000, 8000}:
            result[key] = item
            accepted = True
        elif key in {"exitCode", "exitSignal"} and item is None:
            result[key] = None
            accepted = True
        elif key in CONTAINERS and type(item) is dict:
            result[key] = _details(item, counts, depth + 1, budget)
            accepted = True
        elif key == "stack_frames" and type(item) is list:
            frames = []
            for frame in item[:10]:
                if type(frame) is dict and set(frame) == {"module_id", "line", "column"} and type(frame["module_id"]) is str and frame["module_id"] in _FRAMES and type(frame["line"]) is int and 1 <= frame["line"] <= 2_000_000 and type(frame["column"]) is int and 0 <= frame["column"] <= 100_000:
                    frames.append(frame.copy())
                else:
                    counts["fields_dropped"] += 1
            counts["fields_dropped"] += max(0, len(item) - 10)
            result[key] = frames
            accepted = True
        if not accepted:
            counts["fields_dropped"] += 1
    return result


def _event(raw, start, end, counts: Counter) -> dict | None:
    if type(raw) is not dict or raw.get("privacy_policy_version") != "diagnostic_metadata_v2":
        counts["unsupported_rows"] += 1
        return None
    timestamp = raw.get("timestamp")
    try:
        if type(timestamp) is not str or not _TIMESTAMP.fullmatch(timestamp):
            raise ValueError("TIMESTAMP")
        # Python 3.9 accepts only 3/6 fractional digits; ISO/JS readers may supply
        # 1..6. Normalize explicitly instead of silently dropping valid events.
        timestamp = re.sub(r"\.([0-9]{1,6})(?=Z|[+-][0-9]{2}:[0-9]{2}$)", lambda match: "." + match.group(1).ljust(6, "0"), timestamp)
        instant = _utc(datetime.fromisoformat(timestamp.replace("Z", "+00:00")))
    except (ValueError, OverflowError):
        counts["invalid_timestamps"] += 1
        return None
    if not start <= instant < end:
        counts["outside_window"] += 1
        return None
    if raw.get("source") not in _SOURCES or raw.get("level") not in _LEVELS:
        counts["unsupported_rows"] += 1
        return None
    name = raw.get("event")
    if type(name) is not str or name not in EVENTS:
        counts["unknown_events"] += 1
        name = "unregistered_event"
    safe = {"timestamp": _stamp(instant), "source": raw["source"], "level": raw["level"], "event": name}
    if "details" in raw:
        safe["details"] = _details(raw["details"], counts)
    if _code(raw.get("error_code")):
        safe["error_code"] = raw["error_code"]
    elif "error_code" in raw:
        counts["fields_dropped"] += 1
    correlation = raw.get("correlation_id")
    if type(correlation) is str and _UUID.fullmatch(correlation):
        safe["correlation_id"] = correlation.lower()  # Replaced with a package-only alias before encoding.
    elif "correlation_id" in raw:
        counts["fields_dropped"] += 1
    counts["fields_dropped"] += len(set(raw) - {"timestamp", "privacy_policy_version", "source", "level", "event", "details", "error_code", "correlation_id"})
    return safe


@dataclass
class _Item:
    serial: int
    priority: int
    origin: int
    row: dict
    size: int


class _Selection:
    def __init__(self, limits, counts):
        self.limits, self.counts = limits, counts
        self.rows = {}
        self.queues = [deque() for _ in range(4)]
        self.recent = deque(maxlen=limits.context_events)
        self.after_error = 0
        self.size = self.serial = 0
        # Aggregate only successful, unadorned policy polls in the export.
        # The original local JSONL remains unchanged for detailed investigation.
        self.poll_summaries = {}
        self.capacity = max(0, limits.max_raw_bytes - min(32 * 1024, limits.max_raw_bytes // 4))

    def _drop(self):
        for priority, queue in enumerate(self.queues):
            while queue:
                serial = queue.popleft()
                item = self.rows.get(serial)
                if item is not None and item.priority == priority:
                    del self.rows[serial]
                    self.size -= item.size
                    self.counts["events_dropped_budget"] += 1
                    return True
        return False

    def add(self, row: dict, origin: int, raw: dict | None = None):
        details = row.get('details', {})
        original = raw.get('details') if type(raw) is dict else None
        if (row['event'] == 'local_api_request' and row['source'] == 'local_service'
                and row['level'] == 'info' and 'error_code' not in row and 'error_code' not in raw
                and type(original) is dict and set(original) <= {'method', 'route_template', 'statusCode', 'durationMs'}
                and details.get('method') == 'GET' and details.get('route_template') == '/v1/private/money-egress'
                and details.get('statusCode') == 200):
            hour = row['timestamp'][:13]
            key = (origin, hour)
            summary = self.poll_summaries.get(key)
            if summary is None:
                self.poll_summaries[key] = {'hour_utc': hour, 'count': 1, 'first_utc': row['timestamp'], 'last_utc': row['timestamp']}
            else:
                summary['count'] += 1
                summary['first_utc'] = min(summary['first_utc'], row['timestamp'])
                summary['last_utc'] = max(summary['last_utc'], row['timestamp'])
            return
        severe = row["level"] in {"warn", "error"}
        if severe:
            for serial in self.recent:
                previous = self.rows.get(serial)
                if previous is not None and previous.priority < 2:
                    previous.priority = 2
                    self.queues[2].append(serial)
        priority = 3 if row["level"] == "error" else 2 if severe or self.after_error else 0 if row["level"] == "debug" else 1
        self.after_error = self.limits.context_events if severe else max(0, self.after_error - 1)
        self.serial += 1
        size = len(_encoded(row)) + 1
        item = _Item(self.serial, priority, origin, row, size)
        self.rows[item.serial] = item
        self.queues[priority].append(item.serial)
        self.recent.append(item.serial)
        self.size += size
        while self.size > self.capacity or len(self.rows) > self.limits.max_events:
            if not self._drop():
                break
        if sum(map(len, self.queues)) > max(4096, 4 * len(self.rows)):
            self.queues = [deque() for _ in range(4)]
            for serial, existing in sorted(self.rows.items()):
                self.queues[existing.priority].append(serial)

    def discard_origin(self, origin):
        for key in [key for key in self.poll_summaries if key[0] == origin]:
            del self.poll_summaries[key]
        for serial, item in list(self.rows.items()):
            if item.origin == origin:
                self.size -= item.size
                del self.rows[serial]
                self.counts["events_discarded_changed_file"] += 1

    def poll_summary(self):
        buckets = sorted(self.poll_summaries.values(), key=lambda item: (item['hour_utc'], item['first_utc']))
        total = sum(item['count'] for item in buckets)
        if len(buckets) > 64:
            buckets = [{'count': total, 'first_utc': min(b['first_utc'] for b in buckets),
                        'last_utc': max(b['last_utc'] for b in buckets)}]
            resolution = 'whole_window'
        else:
            resolution = 'per_source_file_and_hour'
        return {'route': '/v1/private/money-egress', 'method': 'GET', 'status': 200,
                'individual_events_omitted': total, 'bucket_resolution': resolution, 'buckets': buckets,
                'individual_timestamps_and_correlations': 'not_included_in_zip; original_local_logs_unchanged'}

    def trim(self):
        for _ in range(max(1, len(self.rows) // 8)):
            if not self._drop():
                break

    def encoded(self) -> bytes:
        aliases = {}
        output = []
        for item in sorted(self.rows.values(), key=lambda item: (datetime.fromisoformat(item.row["timestamp"].replace("Z", "+00:00")), item.origin, item.serial)):
            row = item.row.copy()
            if "correlation_id" in row:
                raw = row["correlation_id"]
                if raw not in aliases:
                    aliases[raw] = f"r{len(aliases) + 1:06d}"
                row["correlation_id"] = aliases[raw]
            output.append(_encoded(row) + b"\n")
        return b"".join(output)


def _scan(path: Path, origin: int, start, end, limits, counts, flags, selection, seen_files):
    opened = False
    try:
        stream, before, parents = _open_held(path)
        opened = True
        with stream:
            file_id = (before.st_dev, before.st_ino)
            if file_id in seen_files:
                flags.add("rotation_reobserved_file")
                counts["duplicate_files_skipped"] += 1
                return
            seen_files.add(file_id)
            counts["files_opened"] += 1
            remaining = before.st_size
            oversized = False
            pending = bytearray()
            while remaining:
                allowance = min(64 * 1024, remaining, limits.max_input_bytes - counts["input_bytes"])
                if allowance <= 0:
                    flags.add("input_limit")
                    break
                raw = stream.read(allowance)
                if not raw:
                    flags.add("file_shrank")
                    selection.discard_origin(origin)
                    break
                remaining -= len(raw)
                counts["input_bytes"] += len(raw)
                # Split on LF only. CR, Unicode line separators and invalid UTF-8
                # inside a record never start a second JSON event.
                pieces = raw.split(b"\n")
                for index, piece in enumerate(pieces):
                    terminated = index < len(pieces) - 1
                    if not oversized:
                        if len(pending) + len(piece) + int(terminated) > limits.max_line_bytes:
                            counts["oversized_lines"] += 1
                            pending.clear()
                            oversized = True
                        else:
                            pending.extend(piece)
                    if not terminated:
                        continue
                    if oversized:
                        oversized = False
                        continue
                    counts["lines_seen"] += 1
                    try:
                        raw_event = _json(bytes(pending))
                        row = _event(raw_event, start, end, counts)
                    except (ValueError, RecursionError, TypeError, OverflowError):
                        counts["invalid_lines"] += 1
                        row = None
                    pending.clear()
                    if row is not None:
                        counts["safe_events_seen"] += 1
                        selection.add(row, origin, raw_event)
            if pending or oversized:
                counts["partial_lines"] += 1
            if not _held_unchanged(path, stream, before, parents, append_allowed=True):
                flags.add("file_identity_changed")
                selection.discard_origin(origin)
            elif os.fstat(stream.fileno()).st_size != before.st_size:
                counts["live_append_files"] += 1
                flags.add("live_append_after_snapshot")
    except FileNotFoundError:
        if opened:
            flags.add("file_identity_changed")
            selection.discard_origin(origin)
        else:
            counts["missing_slots"] += 1
    except (OSError, ValueError):
        flags.add("unsafe_or_unreadable_file")
        counts["files_rejected"] += 1
        selection.discard_origin(origin)


def _zip(manifest: bytes, events: bytes) -> bytes:
    target = BytesIO()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, data in (("manifest.json", manifest), ("events.jsonl", events)):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100600 << 16
            archive.writestr(info, data)
    return target.getvalue()


def build_daily_diagnostic_bundle(data_dir: Path, *, start_utc: datetime, end_utc: datetime, release_info=None) -> DiagnosticBundle:
    """Freeze one safe attachment. The mail claim must persist these exact bytes.

    No event claims full device/day coverage: rotation, crashes before flush and
    the logger's own filtering can remove facts before this exporter sees them.
    """
    start, end = _utc(start_utc), _utc(end_utc)
    if start >= end:
        raise ValueError("DIAGNOSTIC_WINDOW_ORDER_INVALID")
    limits = LIMITS
    if min(limits.max_input_bytes, limits.max_raw_bytes, limits.max_zip_bytes, limits.max_line_bytes, limits.max_events) <= 0 or not 0 <= limits.context_events <= 10:
        raise ValueError("DIAGNOSTIC_LIMITS_INVALID")
    counts = Counter()
    flags = set()
    selection = _Selection(limits, counts)
    seen_files = set()
    log_dir = Path(data_dir) / "diagnostics" / "logs"
    slots = []
    try:
        _chain(log_dir)
        for index in range(_MAX_ROTATIONS + 1):
            name = "runtime.jsonl" + (f".{index}" if index else "")
            previous = counts["files_opened"]
            _scan(log_dir / name, index, start, end, limits, counts, flags, selection, seen_files)
            if counts["files_opened"] != previous:
                slots.append(index)
            if counts["input_bytes"] >= limits.max_input_bytes:
                flags.add("input_limit")
                break
        if slots and slots != list(range(max(slots) + 1)):
            flags.add("rotation_gap")
    except FileNotFoundError:
        flags.add("log_directory_missing")
    except (OSError, ValueError):
        flags.add("unsafe_or_unreadable_directory")
    release = _release_identity(release_info)
    while True:
        events = selection.encoded()
        manifest = {
            "schema": "runtime_diagnostic_bundle_v1", "privacy_policy": "closed_metadata_export_v1",
            "window": {"start_utc": _stamp(start), "end_utc": _stamp(end), "bounds": "inclusive_start_exclusive_end"},
            "release": release,
            "logger_schema_source_sha256": LOGGER_SOURCE_SHA256, "route_schema_source_sha256": ROUTES_SOURCE_SHA256,
            "coverage": {"scope": "available_runtime_logs_only", "full_day_completeness": "not_asserted", "retention_loss_possible": True,
                         "truncated": bool(counts["events_dropped_budget"] or "input_limit" in flags), "issues": sorted(flags)},
            "counts": dict(sorted(counts.items())), "events_included": len(selection.rows),
            "aggregated_successful_policy_polls": selection.poll_summary(),
            "events_bytes": len(events), "events_sha256": hashlib.sha256(events).hexdigest(),
            "selection": "errors_then_warnings_and_bounded_context_then_recent_info",
            "limits": {"input_bytes": limits.max_input_bytes, "raw_bytes": limits.max_raw_bytes, "zip_bytes": limits.max_zip_bytes, "line_bytes": limits.max_line_bytes, "events": limits.max_events},
            "excludes": ["database", "configuration", "keys", "raw_asr", "questions", "responses", "audio", "video", "crash_dumps", "raw_messages", "raw_stack", "absolute_paths"],
        }
        manifest_bytes = _encoded(manifest)
        content = _zip(manifest_bytes, events)
        if len(events) + len(manifest_bytes) <= limits.max_raw_bytes and len(content) <= limits.max_zip_bytes:
            break
        if not selection.rows:
            raise ValueError("DIAGNOSTIC_BUNDLE_LIMIT_TOO_SMALL")
        selection.trim()
    filename = f"hsc-diagnostics-{start.strftime('%Y%m%dT%H%M%SZ')}-{end.strftime('%Y%m%dT%H%M%SZ')}.zip"
    return DiagnosticBundle(filename, content, hashlib.sha256(content).hexdigest(), manifest_bytes)
