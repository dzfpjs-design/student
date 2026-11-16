"""Same-user, offline code-pair update. No application imports or network access."""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import time
import uuid

PRODUCT = "home-study-companion"
MAIN_NAME = "家庭学习伙伴.exe"
GATE_NAME = MAIN_NAME + ".offline-update-held"
PAIR_PATHS = {
    "app_asar": ("resources", "app.asar"),
    "agent": ("resources", "local-agent", "home-study-local-agent.exe"),
}
PAYLOAD_NAMES = {"app_asar": "app.asar", "agent": "home-study-local-agent.exe"}
SHA = re.compile(r"[0-9a-f]{64}\Z")
RELEASE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")
PHASES = {"prepared", "gated", "backed_up", "committed"}
MESSAGES = {
    "CHECKING": "正在核对离线包、当前安装版本和同用户目录。",
    "CLOSING": "正在请求应用正常退出；请等待保存完成，最多两分钟。",
    "BACKUP": "正在本机备份数据库及加密配置；资料、媒体和草稿保留原位。",
    "REPLACING": "正在成对更新程序并校验版本，请勿关闭此更新器。",
    "RECOVERING": "发现上次未收口的更新，正在恢复程序；不会恢复旧数据库。",
    "INSTALLED": "更新成功。请通过原桌面图标启动家庭学习伙伴。",
    "ALREADY_INSTALLED": "此版本已经安装，无需重复更新。",
    "BAD_PACKAGE": "离线包不完整或校验失败，请重新取得完整更新包。",
    "UNSUPPORTED_BASE": "当前安装版本不在此更新包支持范围内，未更新程序。",
    "UNSAFE_PATH": "安装目录或备份目录不符合当前用户安全约定，更新已停止。",
    "APP_STILL_RUNNING": "应用尚未正常退出，更新已停止；请保存工作并正常退出后重试。",
    "OTHER_UPDATE_RUNNING": "另一个更新器正在运行，请等待它结束。",
    "BACKUP_FAILED": "本机安全备份未确认，更新已停止，未恢复旧数据库。",
    "UPDATE_ROLLED_BACK": "更新未完成，旧程序已恢复；资料和数据库保留当前状态。",
    "RECOVERY_HOLD": "程序恢复尚未确认，请勿启动应用；保留此更新包并联系维护者。",
    "UPDATE_FAILED": "更新已停止。请保留本机安装日志并联系维护者。",
    "FINALIZATION_PENDING": "新程序已完整写入，但安装收尾尚未确认。请重跑本更新包继续恢复收尾；不要手动改文件。资料和数据库保持当前状态。",
}


class UpdateError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def require(condition: bool, code: str = "BAD_PACKAGE") -> None:
    if not condition:
        raise UpdateError(code)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def json_bytes(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def exact(value: object, keys: set[str]) -> dict:
    require(type(value) is dict and set(value) == keys)
    return value


def safe_path(path: Path, *, file: bool | None = None, absent: bool = False) -> None:
    """Refuse links/reparse aliases before opening any installed or private file."""
    require(path.is_absolute(), "UNSAFE_PATH")
    for part in reversed((path, *path.parents)):
        if not part.exists() and not part.is_symlink():
            continue
        s = part.lstat()
        require(not stat.S_ISLNK(s.st_mode) and not getattr(s, "st_file_attributes", 0) & 0x400, "UNSAFE_PATH")
    if absent and not path.exists():
        return
    require(path.exists(), "UNSAFE_PATH")
    s = path.stat()
    if file is True:
        require(stat.S_ISREG(s.st_mode) and s.st_nlink == 1, "UNSAFE_PATH")
    elif file is False:
        require(stat.S_ISDIR(s.st_mode), "UNSAFE_PATH")


def fingerprint(path: Path) -> dict:
    safe_path(path, file=True)
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            size += len(block)
            h.update(block)
    return {"bytes": size, "sha256": h.hexdigest()}


def matches(path: Path, expected: dict) -> bool:
    return fingerprint(path) == expected


def atomic_bytes(path: Path, content: bytes) -> None:
    safe_path(path.parent, file=False)
    safe_path(path, file=True, absent=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("xb") as out:
            out.write(content)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def copy_checked(source: Path, target: Path, expected: dict | None = None) -> dict:
    before = fingerprint(source)
    require(expected is None or before == expected, "BAD_PACKAGE")
    safe_path(target.parent, file=False)
    safe_path(target, file=True, absent=True)
    with source.open("rb") as inp, target.open("xb") as out:
        for block in iter(lambda: inp.read(1024 * 1024), b""):
            out.write(block)
        out.flush()
        os.fsync(out.fileno())
    require(fingerprint(source) == before and matches(target, before), "BAD_PACKAGE")
    return before


def strict_json(path: Path, maximum: int = 65536) -> dict:
    safe_path(path, file=True)
    require(path.stat().st_size <= maximum)

    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result)
            result[key] = value
        return result

    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)
    except (ValueError, UnicodeError) as error:
        raise UpdateError("BAD_PACKAGE") from error
    require(type(value) is dict)
    return value


def artifact(row: object, filename: str | None = None) -> dict:
    keys = {"bytes", "sha256"} | ({"filename"} if filename is not None else set())
    value = exact(row, keys)
    require(type(value["bytes"]) is int and 0 < value["bytes"] <= 8 * 1024**3)
    require(type(value["sha256"]) is str and SHA.fullmatch(value["sha256"]) is not None)
    if filename is not None:
        require(value["filename"] == filename)
    return {"bytes": value["bytes"], "sha256": value["sha256"]}


def load_manifest(path: Path) -> dict:
    m = exact(strict_json(path), {
        "schema", "product", "release_id", "data_schema_change",
        "electron", "files", "supported_bases",
    })
    require(type(m["schema"]) is int and m["schema"] == 1)
    require(m["product"] == PRODUCT and m["data_schema_change"] == "none")
    require(type(m["release_id"]) is str and RELEASE.fullmatch(m["release_id"]) is not None)
    artifact(m["electron"], MAIN_NAME)
    exact(m["files"], set(PAIR_PATHS))
    for key in PAIR_PATHS:
        artifact(m["files"][key], PAYLOAD_NAMES[key])
    require(type(m["supported_bases"]) is list and 1 <= len(m["supported_bases"]) <= 16)
    seen = set()
    for base in m["supported_bases"]:
        exact(base, {"app_asar_sha256", "agent_sha256", "release_info_sha256"})
        require(all(type(base[k]) is str and SHA.fullmatch(base[k]) for k in ("app_asar_sha256", "agent_sha256")))
        require(base["release_info_sha256"] is None or (
            type(base["release_info_sha256"]) is str and SHA.fullmatch(base["release_info_sha256"])))
        identity = tuple(base[k] for k in sorted(base))
        require(identity not in seen)
        seen.add(identity)
    return m


class Layout:
    def __init__(self, install: Path, profile: Path, state: Path):
        self.install, self.profile, self.state = install, profile, state
        self.main = install / MAIN_NAME
        self.gate = install / GATE_NAME
        self.pair = {key: install.joinpath(*parts) for key, parts in PAIR_PATHS.items()}
        self.release = install / "resources" / "release-info.json"
        self.journal = state / "pending.json"


class UpdateEngine:
    """Injectable platform only for synthetic tests; the CLI has no path override."""
    def __init__(self, layout: Layout, platform, fault=None, progress=None):
        self.l, self.p = layout, platform
        self.fault = fault or (lambda _: None)
        self.progress = progress or (lambda _: None)

    def point(self, name: str) -> None:
        self.fault(name)

    def secure_dir(self, path: Path) -> None:
        safe_path(path, absent=True)
        if path.exists():
            self.p.check_private(path)
        else:
            path.mkdir()
            self.p.protect(path)
        safe_path(path, file=False)

    def emit(self, code: str) -> None:
        self.progress(code)
        # Only finite codes and software metadata; never error text or family paths.
        event = json_bytes({"utc": utc_now(), "event": code})
        path = self.l.state / "install.jsonl"
        safe_path(path, file=True, absent=True)
        with path.open("ab") as log:
            log.write(event)
            log.flush()
            os.fsync(log.fileno())

    def read_old(self) -> dict:
        return {
            "app_asar": fingerprint(self.l.pair["app_asar"]),
            "agent": fingerprint(self.l.pair["agent"]),
            "release_info": fingerprint(self.l.release) if self.l.release.exists() else None,
        }

    def runtime(self, m: dict, *, gated: bool = False) -> None:
        require(matches(self.l.gate if gated else self.l.main, artifact(m["electron"], MAIN_NAME)), "UNSUPPORTED_BASE")

    def validate_base(self, m: dict, old: dict) -> None:
        current = {
            "app_asar_sha256": old["app_asar"]["sha256"],
            "agent_sha256": old["agent"]["sha256"],
            "release_info_sha256": old["release_info"]["sha256"] if old["release_info"] else None,
        }
        require(current in m["supported_bases"], "UNSUPPORTED_BASE")

    def already_installed(self, m: dict, manifest_sha: str) -> bool:
        if not all(matches(self.l.pair[k], artifact(m["files"][k], PAYLOAD_NAMES[k])) for k in PAIR_PATHS):
            return False
        if not self.l.release.exists():
            return False
        info = exact(strict_json(self.l.release), {
            "schema", "release_id", "app_asar_sha256", "agent_sha256",
            "release_manifest_sha256", "installed_at_utc",
        })
        return (
            type(info["schema"]) is int and info["schema"] == 1
            and info["release_id"] == m["release_id"]
            and info["release_manifest_sha256"] == manifest_sha
            and info["app_asar_sha256"] == m["files"]["app_asar"]["sha256"]
            and info["agent_sha256"] == m["files"]["agent"]["sha256"]
            and type(info["installed_at_utc"]) is str
            and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", info["installed_at_utc"]) is not None
        )

    def write_journal(self, j: dict) -> None:
        atomic_bytes(self.l.journal, json_bytes(j))

    def txdir(self, j: dict) -> Path:
        return self.l.state / "transactions" / j["transaction"]

    def gate(self, m: dict) -> None:
        require(not self.l.gate.exists(), "RECOVERY_HOLD")
        self.runtime(m)
        os.replace(self.l.main, self.l.gate)
        self.point("after_gate_rename")
        self.runtime(m, gated=True)
        require(self.p.closed(self.l.install), "APP_STILL_RUNNING")

    def restore_main(self, m: dict) -> None:
        if self.l.gate.exists():
            require(not self.l.main.exists(), "RECOVERY_HOLD")
            self.runtime(m, gated=True)
            os.replace(self.l.gate, self.l.main)
        self.runtime(m)

    def private_backup(self, tx: Path) -> dict:
        result = {}
        for name, required in (("data-encryption-key.bin", True), ("report-delivery-secrets.json", False)):
            source = self.l.profile / name
            if not source.exists():
                require(not required, "BACKUP_FAILED")
                result[name] = None
            else:
                result[name] = copy_checked(source, tx / name)
        database = self.l.profile / "family-data" / "home-study.db"
        safe_path(database, file=True)
        backup = tx / "home-study.db"
        require(not backup.exists(), "BACKUP_FAILED")
        # Never initialize/import the application, replay schema, or restore this DB.
        started = time.monotonic()
        def bounded_backup(_status, _remaining, _total):
            require(time.monotonic() - started < 120, "BACKUP_FAILED")
        try:
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=5)) as source:
                source.execute("PRAGMA query_only=ON")
                with closing(sqlite3.connect(backup, timeout=5)) as target:
                    source.backup(target, pages=256, progress=bounded_backup, sleep=0.05)
                    require(target.execute("PRAGMA quick_check").fetchall() == [("ok",)], "BACKUP_FAILED")
            with backup.open("r+b") as saved:
                os.fsync(saved.fileno())
            result["home-study.db"] = fingerprint(backup)
            for name, before in result.items():
                if name != "home-study.db":
                    source = self.l.profile / name
                    require((not source.exists()) if before is None else matches(source, before), "BACKUP_FAILED")
            atomic_bytes(tx / "backup-receipt.json", json_bytes({
                "schema": 1, "files": result, "database_quick_check": "ok",
                "database_restore_authorized": False, "media_and_drafts": "preserved_in_place",
            }))
        except (OSError, sqlite3.Error) as error:
            raise UpdateError("BACKUP_FAILED") from error
        return result

    def replace_from(self, source: Path, target: Path, expected: dict) -> None:
        safe_path(target, file=True, absent=True)
        require(matches(source, expected), "BAD_PACKAGE")
        os.replace(source, target)
        require(matches(target, expected), "BAD_PACKAGE")

    def restore_code(self, j: dict, m: dict) -> None:
        tx = self.txdir(j)
        if not self.l.gate.exists():
            self.p.close(self.l.install)
            self.gate(m)
        else:
            require(not self.l.main.exists(), "RECOVERY_HOLD")
            self.runtime(m, gated=True)
            require(self.p.closed(self.l.install), "APP_STILL_RUNNING")
        for key in PAIR_PATHS:
            if matches(self.l.pair[key], j["old"][key]):
                continue
            restore = tx / (key + ".rollback")
            if restore.exists():
                safe_path(restore, file=True)
                restore.unlink()
            copy_checked(tx / (key + ".old"), restore, j["old"][key])
            self.replace_from(restore, self.l.pair[key], j["old"][key])
        if j["old"]["release_info"] is None:
            if self.l.release.exists():
                safe_path(self.l.release, file=True)
                self.l.release.unlink()
        else:
            restore = tx / "release-info.rollback"
            if restore.exists():
                safe_path(restore, file=True)
                restore.unlink()
            copy_checked(tx / "release-info.old", restore, j["old"]["release_info"])
            self.replace_from(restore, self.l.release, j["old"]["release_info"])
        require(self.read_old() == j["old"], "RECOVERY_HOLD")
        self.restore_main(m)
        atomic_bytes(tx / "result.json", json_bytes({
            "schema": 1, "status": "rolled_back", "release_id": m["release_id"],
            "code_pair_verified": True, "database_restored": False, "app_started": False,
            "old": j["old"], "utc": utc_now(),
        }))
        self.l.journal.unlink()

    def read_journal(self) -> tuple[dict, dict]:
        j = exact(strict_json(self.l.journal), {
            "schema", "transaction", "phase", "manifest_sha256", "old", "new_release_info",
        })
        require(type(j["schema"]) is int and j["schema"] == 1, "RECOVERY_HOLD")
        require(type(j["transaction"]) is str and re.fullmatch("[0-9a-f]{32}", j["transaction"]), "RECOVERY_HOLD")
        require(j["phase"] in PHASES, "RECOVERY_HOLD")
        require(type(j["manifest_sha256"]) is str and SHA.fullmatch(j["manifest_sha256"]), "RECOVERY_HOLD")
        exact(j["old"], {"app_asar", "agent", "release_info"})
        for key in PAIR_PATHS:
            artifact(j["old"][key])
        if j["old"]["release_info"] is not None:
            artifact(j["old"]["release_info"])
        artifact(j["new_release_info"])
        tx = self.txdir(j)
        safe_path(tx, file=False)
        self.p.check_private(tx)
        require(fingerprint(tx / "manifest.json")["sha256"] == j["manifest_sha256"], "RECOVERY_HOLD")
        m = load_manifest(tx / "manifest.json")
        self.validate_base(m, j["old"])
        for key in PAIR_PATHS:
            require(matches(tx / (key + ".old"), j["old"][key]), "RECOVERY_HOLD")
        if j["old"]["release_info"] is not None:
            require(matches(tx / "release-info.old", j["old"]["release_info"]), "RECOVERY_HOLD")
        return j, m

    def finish(self, j: dict, m: dict) -> None:
        require(all(matches(self.l.pair[k], artifact(m["files"][k], PAYLOAD_NAMES[k])) for k in PAIR_PATHS), "RECOVERY_HOLD")
        require(matches(self.l.release, j["new_release_info"]), "RECOVERY_HOLD")
        self.restore_main(m)
        self.point("after_main_restored")
        atomic_bytes(self.txdir(j) / "result.json", json_bytes({
            "schema": 1, "status": "installed", "release_id": m["release_id"],
            "code_pair_verified": True, "database_restored": False, "app_started": False,
            "old": j["old"], "new": self.read_old(), "utc": utc_now(),
        }))
        self.l.journal.unlink()

    def recover(self) -> bool:
        if not self.l.journal.exists():
            require(not self.l.gate.exists(), "RECOVERY_HOLD")
            return False
        self.emit("RECOVERING")
        try:
            j, m = self.read_journal()
        except Exception as error:
            raise UpdateError("RECOVERY_HOLD") from error
        if j["phase"] == "committed":
            # Only the parsed disk marker authorizes this path. A repeated
            # finish IO error must never reverse an already published pair.
            try:
                self.finish(j, m)
            except UpdateError:
                raise
            except Exception as error:
                raise UpdateError("FINALIZATION_PENDING") from error
        else:
            try:
                self.restore_code(j, m)
            except Exception as error:
                raise UpdateError("RECOVERY_HOLD") from error
        return True

    def run(self, manifest_path: Path, payload: Path) -> str:
        self.p.check_context(self.l)
        self.secure_dir(self.l.state)
        self.p.check_private(self.l.state)
        with self.p.mutex():
            self.recover()  # Recovery uses its own held manifest, not the new download.
            self.emit("CHECKING")
            safe_path(payload, file=False)
            require(manifest_path == payload / "manifest.json")
            require(set(p.name for p in payload.iterdir()) == {"manifest.json", *PAYLOAD_NAMES.values()})
            m = load_manifest(manifest_path)
            manifest_pin = fingerprint(manifest_path)
            self.runtime(m)
            for key in PAIR_PATHS:
                require(matches(payload / PAYLOAD_NAMES[key], artifact(m["files"][key], PAYLOAD_NAMES[key])))
            if self.already_installed(m, manifest_pin["sha256"]):
                self.emit("ALREADY_INSTALLED")
                return "already_installed"
            old = self.read_old()
            self.validate_base(m, old)
            self.emit("CLOSING")
            self.p.close(self.l.install)
            require(self.p.closed(self.l.install), "APP_STILL_RUNNING")
            self.runtime(m)
            require(self.read_old() == old, "UNSUPPORTED_BASE")
            self.secure_dir(self.l.state / "transactions")
            txid = uuid.uuid4().hex
            tx = self.l.state / "transactions" / txid
            self.secure_dir(tx)
            copy_checked(manifest_path, tx / "manifest.json", manifest_pin)
            for key in PAIR_PATHS:
                copy_checked(self.l.pair[key], tx / (key + ".old"), old[key])
                copy_checked(payload / PAYLOAD_NAMES[key], tx / (key + ".new"), artifact(m["files"][key], PAYLOAD_NAMES[key]))
            if old["release_info"] is not None:
                copy_checked(self.l.release, tx / "release-info.old", old["release_info"])
            info = {
                "schema": 1, "release_id": m["release_id"],
                "app_asar_sha256": m["files"]["app_asar"]["sha256"],
                "agent_sha256": m["files"]["agent"]["sha256"],
                "release_manifest_sha256": manifest_pin["sha256"], "installed_at_utc": utc_now(),
            }
            atomic_bytes(tx / "release-info.new", json_bytes(info))
            j = {
                "schema": 1, "transaction": txid, "phase": "prepared",
                "manifest_sha256": manifest_pin["sha256"], "old": old,
                "new_release_info": fingerprint(tx / "release-info.new"),
            }
            self.write_journal(j)
            commit_boundary_entered = False
            try:
                self.point("after_prepared")
                self.gate(m)
                j["phase"] = "gated"
                self.write_journal(j)
                self.emit("BACKUP")
                self.private_backup(tx)
                self.point("after_backup")
                j["phase"] = "backed_up"
                self.write_journal(j)
                require(self.p.closed(self.l.install), "APP_STILL_RUNNING")
                require(self.read_old() == old, "UNSUPPORTED_BASE")
                self.emit("REPLACING")
                self.replace_from(tx / "app_asar.new", self.l.pair["app_asar"], artifact(m["files"]["app_asar"], "app.asar"))
                self.point("after_first_replace")
                self.replace_from(tx / "agent.new", self.l.pair["agent"], artifact(m["files"]["agent"], "home-study-local-agent.exe"))
                self.point("after_second_replace")
                self.replace_from(tx / "release-info.new", self.l.release, j["new_release_info"])
                self.point("after_release_info")
                require(all(matches(self.l.pair[k], artifact(m["files"][k], PAYLOAD_NAMES[k])) for k in PAIR_PATHS), "RECOVERY_HOLD")
                require(matches(self.l.release, j["new_release_info"]), "RECOVERY_HOLD")
                # This means commit entry, not a claim that its marker is on
                # disk. If writing it fails, the held precommit journal will
                # safely roll back on a later run while the main EXE is gated.
                commit_boundary_entered = True
                self.point("before_commit_marker")
                j["phase"] = "committed"
                self.write_journal(j)
                self.point("after_committed")
                self.finish(j, m)
            except Exception as primary:
                if commit_boundary_entered:
                    # Never turn a complete new pair into a partially reversed
                    # pair under an on-disk committed marker. Do not clear or
                    # rewrite the actual journal on this error path.
                    if isinstance(primary, UpdateError):
                        raise
                    raise UpdateError("FINALIZATION_PENDING") from primary
                if (isinstance(primary, UpdateError) and type(primary.code) is str
                        and primary.code in (
                            "BAD_PACKAGE", "UNSUPPORTED_BASE", "UNSAFE_PATH",
                            "APP_STILL_RUNNING", "BACKUP_FAILED", "RECOVERY_HOLD",
                        )):
                    try:
                        self.emit(primary.code)
                    except Exception:
                        pass  # A diagnostic failure must never prevent rollback.
                try:
                    self.restore_code(j, m)
                except Exception as rollback_error:
                    # A verified gate is kept closed when rollback cannot be proved.
                    raise UpdateError("RECOVERY_HOLD") from rollback_error
                raise UpdateError("UPDATE_ROLLED_BACK") from primary
            self.emit("INSTALLED")
            return "installed"


class WindowsPlatform:
    """Only Win32 metadata, owner/ACL, WM_CLOSE, and a same-user mutex."""
    def __init__(self):
        require(os.name == "nt", "UNSAFE_PATH")
        self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.a = ctypes.WinDLL("advapi32", use_last_error=True)
        self.u = ctypes.WinDLL("user32", use_last_error=True)
        self._declare()
        self.sid = self.process_sid(self.k.GetCurrentProcess())

    def _declare(self):
        H, D, P = wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p
        self.k.GetCurrentProcess.restype = H
        self.k.OpenProcess.argtypes = (D, wintypes.BOOL, D)
        self.k.OpenProcess.restype = H
        self.k.CloseHandle.argtypes = (H,)
        self.k.QueryFullProcessImageNameW.argtypes = (H, D, wintypes.LPWSTR, ctypes.POINTER(D))
        self.k.CreateToolhelp32Snapshot.argtypes = (D, D)
        self.k.CreateToolhelp32Snapshot.restype = H
        self.k.CreateMutexW.argtypes = (P, wintypes.BOOL, wintypes.LPCWSTR)
        self.k.CreateMutexW.restype = H
        self.k.WaitForSingleObject.argtypes = (H, D)
        self.k.ReleaseMutex.argtypes = (H,)
        self.k.LocalFree.argtypes = (P,)
        self.a.OpenProcessToken.argtypes = (H, D, ctypes.POINTER(H))
        self.a.GetTokenInformation.argtypes = (H, ctypes.c_int, P, D, ctypes.POINTER(D))
        self.a.ConvertSidToStringSidW.argtypes = (P, ctypes.POINTER(wintypes.LPWSTR))
        self.a.ConvertStringSidToSidW.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(P))
        self.a.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (wintypes.LPCWSTR, D, ctypes.POINTER(P), ctypes.POINTER(D))
        self.a.GetSecurityDescriptorDacl.argtypes = (P, ctypes.POINTER(wintypes.BOOL), ctypes.POINTER(P), ctypes.POINTER(wintypes.BOOL))
        self.a.SetNamedSecurityInfoW.argtypes = (wintypes.LPWSTR, ctypes.c_int, D, P, P, P, P)
        self.a.GetNamedSecurityInfoW.argtypes = (wintypes.LPWSTR, ctypes.c_int, D, ctypes.POINTER(P), P, ctypes.POINTER(P), P, ctypes.POINTER(P))
        self.a.GetSecurityDescriptorControl.argtypes = (P, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(D))
        self.a.GetAclInformation.argtypes = (P, P, D, ctypes.c_int)
        self.a.GetAce.argtypes = (P, D, ctypes.POINTER(P))
        self.u.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(D))
        self.u.IsWindowVisible.argtypes = (wintypes.HWND,)
        self.u.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        self.u.MessageBoxW.argtypes = (wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT)

    def sid_text(self, pointer) -> str:
        text = wintypes.LPWSTR()
        require(bool(self.a.ConvertSidToStringSidW(pointer, ctypes.byref(text))), "UNSAFE_PATH")
        try:
            return text.value
        finally:
            self.k.LocalFree(ctypes.cast(text, ctypes.c_void_p))

    def process_sid(self, process) -> str:
        token = wintypes.HANDLE()
        require(bool(self.a.OpenProcessToken(process, 0x0008, ctypes.byref(token))), "UNSAFE_PATH")
        try:
            needed = wintypes.DWORD()
            self.a.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
            data = ctypes.create_string_buffer(needed.value)
            require(bool(self.a.GetTokenInformation(token, 1, data, needed, ctypes.byref(needed))), "UNSAFE_PATH")
            return self.sid_text(ctypes.cast(data, ctypes.POINTER(ctypes.c_void_p))[0])
        finally:
            self.k.CloseHandle(token)

    def owner_acl(self, path: Path):
        owner, acl, sd = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        error = self.a.GetNamedSecurityInfoW(str(path), 1, 0x5, ctypes.byref(owner), None, ctypes.byref(acl), None, ctypes.byref(sd))
        require(error == 0, "UNSAFE_PATH")
        return owner, acl, sd

    def check_owner(self, path: Path) -> None:
        owner, _acl, sd = self.owner_acl(path)
        try:
            require(self.sid_text(owner) == self.sid, "UNSAFE_PATH")
        finally:
            self.k.LocalFree(sd)

    def check_context(self, layout: Layout) -> None:
        current = self.current_layout()
        require((layout.install, layout.profile, layout.state) == (current.install, current.profile, current.state), "UNSAFE_PATH")
        for path in (layout.install, layout.profile):
            safe_path(path, file=False)
        for path in (*layout.pair.values(), layout.release):
            safe_path(path, file=True, absent=(path == layout.release))
        for path in (layout.main, layout.gate):
            safe_path(path, file=True, absent=True)

    def protect(self, path: Path) -> None:
        # Called only for a newly created, empty private directory. Elevated
        # same-user Windows tokens can default its owner to Administrators.
        previous_owner, _previous_acl, previous_sd = self.owner_acl(path)
        try:
            already_owned = self.sid_text(previous_owner) == self.sid
        finally:
            self.k.LocalFree(previous_sd)
        sd = ctypes.c_void_p()
        owner = ctypes.c_void_p()
        try:
            require(bool(self.a.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                "D:P(A;OICI;FA;;;" + self.sid + ")", 1, ctypes.byref(sd), None)), "UNSAFE_PATH")
            present, defaulted, acl = wintypes.BOOL(), wintypes.BOOL(), ctypes.c_void_p()
            require(bool(self.a.GetSecurityDescriptorDacl(sd, ctypes.byref(present), ctypes.byref(acl), ctypes.byref(defaulted))), "UNSAFE_PATH")
            require(bool(present.value) and acl.value is not None, "UNSAFE_PATH")
            information = 0x80000004  # PROTECTED_DACL | DACL; no WRITE_OWNER.
            if not already_owned:
                require(bool(self.a.ConvertStringSidToSidW(self.sid, ctypes.byref(owner))), "UNSAFE_PATH")
                information |= 0x1  # OWNER, only when changing a different owner.
            require(self.a.SetNamedSecurityInfoW(
                str(path), 1, information, None if already_owned else owner, None, acl, None
            ) == 0, "UNSAFE_PATH")
        finally:
            if owner.value is not None:
                self.k.LocalFree(owner)
            if sd.value is not None:
                self.k.LocalFree(sd)
        self.check_private(path)

    def current_layout(self) -> Layout:
        class Guid(ctypes.Structure):
            _fields_ = [("a", ctypes.c_uint32), ("b", ctypes.c_uint16),
                        ("c", ctypes.c_uint16), ("d", ctypes.c_ubyte * 8)]
        shell = ctypes.WinDLL("shell32", use_last_error=True)
        ole = ctypes.WinDLL("ole32", use_last_error=True)
        shell.SHGetKnownFolderPath.argtypes = (ctypes.POINTER(Guid), wintypes.DWORD,
                                               wintypes.HANDLE, ctypes.POINTER(wintypes.LPWSTR))
        shell.SHGetKnownFolderPath.restype = ctypes.c_long
        ole.CoTaskMemFree.argtypes = (ctypes.c_void_p,)
        def folder(guid_text):
            identifier = Guid.from_buffer_copy(uuid.UUID(guid_text).bytes_le)
            text = wintypes.LPWSTR()
            require(shell.SHGetKnownFolderPath(ctypes.byref(identifier), 0, None, ctypes.byref(text)) == 0, "UNSAFE_PATH")
            try:
                path = Path(text.value)
                safe_path(path, file=False)
                return path
            finally:
                ole.CoTaskMemFree(ctypes.cast(text, ctypes.c_void_p))
        local = folder("F1B32785-6FBA-4FCF-9D55-7B8E7F157091")
        roaming = folder("3EB685DB-65F9-4CF6-A03A-E3EF65729F3D")
        return Layout(local / "Programs" / PRODUCT, roaming / PRODUCT, local / (PRODUCT + "-updates"))

    def check_private(self, path: Path) -> None:
        owner, acl, sd = self.owner_acl(path)
        class AclSize(ctypes.Structure):
            _fields_ = [("count", wintypes.DWORD), ("used", wintypes.DWORD), ("free", wintypes.DWORD)]
        try:
            require(self.sid_text(owner) == self.sid and acl.value is not None, "UNSAFE_PATH")
            control, revision = wintypes.WORD(), wintypes.DWORD()
            require(bool(self.a.GetSecurityDescriptorControl(sd, ctypes.byref(control), ctypes.byref(revision))) and control.value & 0x1000, "UNSAFE_PATH")
            size = AclSize()
            require(bool(self.a.GetAclInformation(acl, ctypes.byref(size), ctypes.sizeof(size), 2)) and size.count == 1, "UNSAFE_PATH")
            ace = ctypes.c_void_p()
            require(bool(self.a.GetAce(acl, 0, ctypes.byref(ace))), "UNSAFE_PATH")
            require(ctypes.c_ubyte.from_address(ace.value).value == 0, "UNSAFE_PATH")
            require(ctypes.c_uint32.from_address(ace.value + 4).value == 0x1F01FF, "UNSAFE_PATH")
            require(self.sid_text(ace.value + 8) == self.sid, "UNSAFE_PATH")
        finally:
            self.k.LocalFree(sd)

    @contextmanager
    def mutex(self):
        # Global mutexes (unlike file mappings) require no SeCreateGlobalPrivilege.
        # Same SID in an SSH/interactive/RDP session must share one transaction.
        name = "Global\\HomeStudyCompanionOfflineUpdate-" + hashlib.sha256(self.sid.encode("ascii")).hexdigest()[:24]
        handle = self.k.CreateMutexW(None, False, name)
        require(bool(handle), "OTHER_UPDATE_RUNNING")
        acquired = False
        try:
            result = self.k.WaitForSingleObject(handle, 0)
            require(result in (0, 0x80), "OTHER_UPDATE_RUNNING")
            acquired = True
            yield
        finally:
            if acquired:
                self.k.ReleaseMutex(handle)
            self.k.CloseHandle(handle)

    def processes(self, install: Path) -> list[int]:
        class Entry(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
            ]
        self.k.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(Entry))
        self.k.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(Entry))
        snapshot = self.k.CreateToolhelp32Snapshot(2, 0)
        require(snapshot not in (None, ctypes.c_void_p(-1).value), "APP_STILL_RUNNING")
        found = []
        prefix = os.path.normcase(str(install)) + os.sep
        try:
            entry = Entry()
            entry.dwSize = ctypes.sizeof(entry)
            more = self.k.Process32FirstW(snapshot, ctypes.byref(entry))
            require(bool(more), "APP_STILL_RUNNING")
            while more:
                process = self.k.OpenProcess(0x101000, False, entry.th32ProcessID)
                if process:
                    try:
                        length = wintypes.DWORD(32768)
                        name = ctypes.create_unicode_buffer(length.value)
                        queried = self.k.QueryFullProcessImageNameW(process, 0, name, ctypes.byref(length))
                        if queried and os.path.normcase(name.value).startswith(prefix):
                            require(self.process_sid(process) == self.sid, "APP_STILL_RUNNING")
                            found.append(entry.th32ProcessID)
                        elif not queried and self.k.WaitForSingleObject(process, 0) != 0 and entry.szExeFile.lower() in (MAIN_NAME.lower(), "home-study-local-agent.exe", "crashpad_handler.exe"):
                            raise UpdateError("APP_STILL_RUNNING")
                    finally:
                        self.k.CloseHandle(process)
                elif ctypes.get_last_error() != 87 and entry.szExeFile.lower() in (MAIN_NAME.lower(), "home-study-local-agent.exe", "crashpad_handler.exe"):
                    raise UpdateError("APP_STILL_RUNNING")
                more = self.k.Process32NextW(snapshot, ctypes.byref(entry))
            require(ctypes.get_last_error() == 18, "APP_STILL_RUNNING")
        finally:
            self.k.CloseHandle(snapshot)
        return found

    def closed(self, install: Path) -> bool:
        return not self.processes(install)

    def close(self, install: Path) -> None:
        pids = set(self.processes(install))
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        errors = []
        @callback_type
        def callback(window, _):
            pid = wintypes.DWORD()
            self.u.GetWindowThreadProcessId(window, ctypes.byref(pid))
            if pid.value in pids and self.u.IsWindowVisible(window):
                if not self.u.PostMessageW(window, 0x0010, 0, 0):
                    errors.append(True)
            return True
        self.u.EnumWindows.argtypes = (callback_type, wintypes.LPARAM)
        require(bool(self.u.EnumWindows(callback, 0)) and not errors, "APP_STILL_RUNNING")
        deadline = time.monotonic() + 120
        while not self.closed(install):
            require(time.monotonic() < deadline, "APP_STILL_RUNNING")
            time.sleep(0.25)

    def failure_message(self, code: str) -> None:
        self.u.MessageBoxW(None, MESSAGES.get(code, MESSAGES["UPDATE_FAILED"]) + "\n错误码：" + code,
                           "家庭学习伙伴离线更新", 0x10)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="家庭学习伙伴离线更新")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--payload-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    platform = None
    engine = None
    try:
        platform = WindowsPlatform()
        layout = platform.current_layout()
        engine = UpdateEngine(layout, platform)
        engine.run(args.manifest.absolute(), args.payload_dir.absolute())
        return 0
    except Exception as error:
        code = error.code if isinstance(error, UpdateError) else "UPDATE_FAILED"
        if engine is not None and engine.l.state.is_dir():
            try:
                engine.emit(code)
            except Exception:
                pass
        if platform is not None:
            platform.failure_message(code)
        return 20 if code == "RECOVERY_HOLD" else 10


if __name__ == "__main__":
    sys.exit(main())
