from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap

import pytest


@pytest.mark.parametrize("workers,inherited_paths", [(0, False), (2, True)])
def test_collection_import_isolates_app_stores(tmp_path: Path, workers: int, inherited_paths: bool) -> None:
    suite = tmp_path / "suite"
    suite.mkdir()
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    real_family = Path.home() / ".home-study-companion"
    fake_family = fake_home / ".home-study-companion"
    shutil.copy2(Path(__file__).with_name("conftest.py"), suite / "conftest.py")
    (suite / "test_probe.py").write_text(textwrap.dedent("""
        import json
        import os
        from pathlib import Path
        import sqlite3
        import sys

        root = Path(os.environ["HSC_DATA_DIR"]).resolve()
        forbidden = [Path(value).resolve() for value in json.loads(os.environ["PROBE_FORBIDDEN"])]
        assert not any(root == path or path in root.parents for path in forbidden)
        assert Path(os.environ["HSC_DATABASE_PATH"]) == root / "home-study.db"
        connected = set()
        violations = []

        def audit(event, args):
            if event in {"open", "os.mkdir", "os.remove", "os.rmdir", "sqlite3.connect"}:
                value = args[0]
                if isinstance(value, (str, bytes, os.PathLike)):
                    path = Path(os.fsdecode(value)).resolve()
                    blocked = any(path == blocked or blocked in path.parents for blocked in forbidden)
                    outside_database = event == "sqlite3.connect" and root not in path.parents
                    if blocked or outside_database:
                        violations.append((event, str(path)))
                        raise AssertionError(violations[-1])
                    if event == "sqlite3.connect":
                        connected.add(path.name)

        sys.addaudithook(audit)
        from home_study_agent.app import app
        assert not violations
        assert not connected
        assert not any(root.iterdir())
        # Deliberate startup stays inside the import-time pytest isolation root.
        assert app.state.settings.data_dir.resolve() == root

        stores = [("cpu-resource-queue.db", "cpu_task"),
                  ("home-study.db", "ocr_job"), ("home-study.db", "ocr_review_item")]
        for filename, table in stores:
            assert (root / filename).is_file(), filename
            with sqlite3.connect(root / filename) as connection:
                assert connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone()
        assert not (root / "ocr-review-queue.db").exists()
        assert not (root / "local-ocr.db").exists()
        worker = os.environ.get("PYTEST_XDIST_WORKER", "controller")
        (Path(os.environ["PROBE_EVIDENCE"]) / (worker + ".json")).write_text(
            json.dumps({"root": str(root), "stores": sorted(stores), "connected": sorted(connected)}), encoding="utf-8"
        )

        def test_import_completed_before_test_execution():
            assert app is not None
            assert not violations
    """), encoding="utf-8")
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("HSC_", "PYTEST_"))
    }
    env.update({
        "HOME": str(fake_home),
        "USERPROFILE": str(fake_home),
        "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PROBE_FORBIDDEN": json.dumps([str(real_family), str(fake_family)]),
        "PROBE_EVIDENCE": str(evidence),
    })
    if inherited_paths:
        env["HSC_DATA_DIR"] = str(fake_family)
        env["HSC_DATABASE_PATH"] = str(fake_family / "home-study.db")
    command = [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    if workers:
        command.extend(["-n", str(workers), "--dist=load"])
    result = subprocess.run(command, cwd=suite, env=env, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    records = [json.loads(path.read_text(encoding="utf-8")) for path in evidence.glob("*.json")]
    assert len(records) == (workers or 1)
    roots = {record["root"] for record in records}
    assert len(roots) == len(records), "Workers must not share import-time databases"
    assert os.environ["HSC_DATA_DIR"] not in roots
    assert not fake_family.exists()
    assert all(not Path(root).exists() for root in roots), "Temporary import stores must be cleaned up"
