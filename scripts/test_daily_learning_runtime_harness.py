"""Failure retention of the acceptance harness, not product-runtime proof."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_intentional_crash_kills_only_owned_process_and_waits_for_closed_port(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("daily_crash_harness", Path(__file__).with_name("verify-daily-learning-runtime.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    runtime = module.Runtime(tmp_path / "owned.exe", tmp_path)
    stopped, calls = False, []

    class OwnedProcess:
        pid = 43210

        def poll(self):
            return 1 if stopped else None

        def wait(self, *, timeout):
            assert stopped

    def kill(command, **kwargs):
        nonlocal stopped
        calls.append(command)
        stopped = True

    def closed_port(address, **kwargs):
        assert stopped and address == ("127.0.0.1", 43211)
        raise OSError("owned port is closed")

    runtime.process, runtime.port = OwnedProcess(), 43211
    monkeypatch.setattr(runtime, "api", lambda *args, **kwargs: pytest.fail("Crash must bypass graceful shutdown"))
    monkeypatch.setattr(module.subprocess, "run", kill)
    monkeypatch.setattr(module.socket, "create_connection", closed_port)
    try:
        runtime.stop(force=True)
        assert calls == [["taskkill", "/PID", "43210", "/T", "/F"]]
        assert runtime.process is None and runtime.forced_stops == 1
    finally:
        runtime.log.close()


@pytest.mark.parametrize("stop_fails", [False, True])
def test_failed_acceptance_keeps_owned_synthetic_profile(tmp_path, monkeypatch, stop_fails):
    spec = importlib.util.spec_from_file_location("daily_harness", Path(__file__).with_name("verify-daily-learning-runtime.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / "hsc-daily-learning-acceptance-owned"
    root.mkdir()
    marker = root / "synthetic-fixture.txt"
    marker.write_text("owned synthetic evidence", encoding="utf-8")
    executable = tmp_path / "fixture.exe"
    executable.write_bytes(b"not an executable; startup deliberately simulated to fail")
    output = tmp_path / "failure.json"

    class FailedRuntime:
        def __init__(self, exe, profile):
            assert exe == executable and profile == root
            self.log = (profile / "runtime.log").open("wb")
            self.starts, self.forced_stops = 1, 0

        def start(self):
            raise RuntimeError("synthetic startup failure")

        def stop(self):
            if stop_fails:
                raise RuntimeError("synthetic stop failure")

    monkeypatch.setattr(module, "Runtime", FailedRuntime)
    monkeypatch.setattr(module, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(module.tempfile, "mkdtemp", lambda **kwargs: str(root))
    monkeypatch.setattr("sys.argv", ["harness", "--exe", str(executable), "--output", str(output)])
    assert module.main() == 1
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert report["retained_synthetic_profile"] == str(root)
    assert marker.read_text(encoding="utf-8") == "owned synthetic evidence"
    assert (root / "runtime.log").is_file()
    assert "temporary_profile_removed" not in report["checks"]
    assert report["checks"].get("owned_process_and_port_stopped", False) is not stop_fails
