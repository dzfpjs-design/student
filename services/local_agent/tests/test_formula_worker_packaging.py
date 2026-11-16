from __future__ import annotations

import json
import multiprocessing as mp
import os
from pathlib import Path
import time

from home_study_agent.local_ocr.formula_recognition import provider_from_worker_spec
from home_study_agent.local_ocr.formula_worker import run_formula_provider_isolated


AGENT_ROOT = Path(__file__).parents[1]
PROJECT_ROOT = Path(__file__).parents[3]


def _outer_blocking_formula_worker(pid_file: str) -> None:
    run_formula_provider_isolated(
        {"kind": "local_factory", "factory_key": "test_support", "kwargs": {"mode": "block", "pid_file": pid_file}},
        b"not-an-image-needed-by-test-provider",
        timeout_seconds=30,
        max_rss_bytes=1_500_000_000,
        cancelled=lambda: False,
    )


def _import_frozen_entry_without_starting_app(data_dir: str, connection) -> None:
    os.environ["HSC_DATA_DIR"] = data_dir
    os.environ["HSC_DATABASE_PATH"] = str(Path(data_dir) / "agent.db")
    import home_study_agent.__main__  # noqa: F401
    connection.send({"database_exists": (Path(data_dir) / "agent.db").exists()})
    connection.close()


def _pid_is_running(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x00100000 | 0x1000, False, pid)
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == 0x102
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        state = os.popen(f"ps -o stat= -p {pid}").read().strip()
        return bool(state) and not state.startswith("Z")
    except OSError:
        return False


def test_pyinstaller_windows_entry_declares_freeze_support_and_formula_hidden_imports() -> None:
    entry = (AGENT_ROOT / "src" / "home_study_agent" / "__main__.py").read_text(encoding="utf-8")
    package = json.loads((PROJECT_ROOT / "package.json").read_text(encoding="utf-8"))
    command = package["scripts"]["build:agent:win"]

    assert "freeze_support()" in entry
    assert entry.index("freeze_support()") < entry.index("from home_study_agent.app import app")
    assert "home_study_agent.local_ocr.formula_worker" in command
    assert "home_study_agent.local_ocr.formula_recognition" in command


def test_spawn_import_of_frozen_entry_does_not_construct_database_before_freeze_support(tmp_path: Path) -> None:
    parent, child = mp.get_context("spawn").Pipe(duplex=False)
    process = mp.get_context("spawn").Process(target=_import_frozen_entry_without_starting_app, args=(str(tmp_path), child))
    process.start()
    child.close()
    assert parent.poll(5), "entry import child did not return"
    result = parent.recv()
    process.join(5)

    assert process.exitcode == 0
    assert result == {"database_exists": False}


def test_spawn_worker_smoke_covers_success_and_hard_timeout() -> None:
    success = run_formula_provider_isolated(
        {"kind": "local_factory", "factory_key": "test_support", "kwargs": {"mode": "success"}},
        b"test", timeout_seconds=2, max_rss_bytes=1_500_000_000, cancelled=lambda: False,
    )
    blocked = run_formula_provider_isolated(
        {"kind": "local_factory", "factory_key": "test_support", "kwargs": {"mode": "block"}},
        b"test", timeout_seconds=.1, max_rss_bytes=1_500_000_000, cancelled=lambda: False,
    )

    assert success["status"] == "completed"
    assert success["latex"] == "x=2"
    assert blocked["status"] == "timed_out"


def test_forcibly_ended_outer_worker_does_not_leave_formula_child_orphan(tmp_path: Path) -> None:
    pid_file = tmp_path / "formula-child.pid"
    outer = mp.get_context("spawn").Process(target=_outer_blocking_formula_worker, args=(str(pid_file),))
    outer.start()
    deadline = time.monotonic() + 5
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(.05)
    assert pid_file.exists(), "formula child did not start under isolated harness"
    child_pid = int(pid_file.read_text(encoding="utf-8"))
    assert _pid_is_running(child_pid)

    outer.terminate()
    outer.join(3)
    deadline = time.monotonic() + 5
    while _pid_is_running(child_pid) and time.monotonic() < deadline:
        time.sleep(.05)

    assert not _pid_is_running(child_pid)


def test_worker_factory_rejects_unregistered_module_factory() -> None:
    try:
        provider_from_worker_spec({"kind": "local_factory", "factory_key": "os.system", "kwargs": {}})
    except RuntimeError as error:
        assert "not_allowlisted" in str(error)
    else:
        raise AssertionError("unregistered formula worker factory was accepted")
