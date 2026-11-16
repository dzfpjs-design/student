from __future__ import annotations

import sys
import os
from pathlib import Path
import subprocess
from types import ModuleType, SimpleNamespace


def test_real_desktop_entrypoint_constructs_application_in_an_isolated_process(tmp_path):
    # Importing FastAPI under pytest is not a substitute for starting the
    # desktop interpreter: type annotation compatibility must hold there too.
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith('HSC_') and not any(word in key.upper()
                       for word in ('API_KEY', 'TOKEN', 'SECRET', 'PASSWORD'))}
    environment.update(PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'),
                       HSC_DATA_DIR=str(tmp_path), HSC_DATABASE_PATH=str(tmp_path / 'isolated.db'),
                       HSC_MAINTENANCE_ENABLED='0')
    result = subprocess.run([sys.executable, '-m', 'home_study_agent', '--help'],
                            env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
    assert '--host' in result.stdout and '--port' in result.stdout


def test_desktop_agent_disables_identifier_bearing_access_logs(monkeypatch) -> None:
    import home_study_agent.__main__ as entrypoint

    captured: dict[str, object] = {}

    class FakeConfig:
        def __init__(self, app, **kwargs) -> None:
            captured["app"] = app
            captured.update(kwargs)

    class FakeServer:
        def __init__(self, _config) -> None:
            self.should_exit = False

        def run(self) -> None:
            captured["ran"] = True

    uvicorn = ModuleType("uvicorn")
    uvicorn.Config = FakeConfig  # type: ignore[attr-defined]
    uvicorn.Server = FakeServer  # type: ignore[attr-defined]
    app_module = ModuleType("home_study_agent.app")
    app_module.app = SimpleNamespace(state=SimpleNamespace())  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "uvicorn", uvicorn)
    monkeypatch.setitem(sys.modules, "home_study_agent.app", app_module)
    monkeypatch.setattr(entrypoint, "freeze_support", lambda: None)
    monkeypatch.setattr(sys, "argv", ["home-study-local-agent", "--port", "9876"])

    entrypoint.main()

    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 9876
    assert captured["log_level"] == "info"
    assert captured["access_log"] is False
    assert captured["ran"] is True
