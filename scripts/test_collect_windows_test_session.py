import importlib.util
import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path


SCRIPT = Path(__file__).with_name("collect_windows_test_session.py")
SPEC = importlib.util.spec_from_file_location("collect_windows_test_session", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class WindowsTestSessionCollectorTests(unittest.TestCase):
    def test_start_stop_captures_consistent_private_snapshot_and_safe_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / "profile"
            family_data = profile / "family-data"
            logs = profile / "family-data" / "diagnostics" / "logs"
            output = root / "sessions"
            install = root / "install"
            family_data.mkdir(parents=True)
            logs.mkdir(parents=True)
            install.mkdir()
            database = family_data / "home-study.db"
            with sqlite3.connect(database) as connection:
                connection.executescript(
                    "CREATE TABLE voice_input(id TEXT PRIMARY KEY, transcript TEXT);"
                    "CREATE TABLE mistake_case(id TEXT PRIMARY KEY, content TEXT);"
                    "CREATE TABLE model_call(id TEXT PRIMARY KEY, status TEXT);"
                )
                connection.execute("INSERT INTO voice_input VALUES('before', 'baseline')")
            (profile / "voice-settings.json").write_text('{"voice":"local"}', encoding="utf-8")
            onboarding = profile / "voice-onboarding"
            onboarding.mkdir()
            (onboarding / "child-primary-4.json").write_text('{"step":"name"}', encoding="utf-8")
            (profile / "qwen-realtime-settings.json").write_text('{"workspace":"local"}', encoding="utf-8")
            (install / "家庭学习伙伴.exe").write_bytes(b"candidate")
            (install / "Uninstall 家庭学习伙伴.exe").write_bytes(b"uninstaller")

            started = MODULE.session_start(profile, output, install)
            state = json.loads((Path(started["session_dir"]) / "session-state.json").read_text(encoding="utf-8"))
            self.assertEqual(
                ["家庭学习伙伴.exe"],
                [entry["path"] for entry in state["installed_artifacts"]],
            )
            self.assertTrue(
                (Path(started["session_dir"]) / "private" / "settings-before" / "voice-settings.json").is_file()
            )
            self.assertTrue(
                (
                    Path(started["session_dir"])
                    / "private"
                    / "settings-before"
                    / "voice-onboarding"
                    / "child-primary-4.json"
                ).is_file()
            )
            with sqlite3.connect(database) as connection:
                connection.execute("INSERT INTO voice_input VALUES('after', '孩子真实语音秘密')")
                connection.execute("INSERT INTO mistake_case VALUES('mistake', '家庭题目秘密')")
                connection.execute("INSERT INTO model_call VALUES('call', 'completed')")
            (logs / "runtime.jsonl").write_text(
                json.dumps(
                    {
                        "timestamp": MODULE.utc_now(),
                        "source": "renderer",
                        "event": "voice.asr.completed",
                        "level": "info",
                        "details": {"statusCode": 200, "mode": "wake"},
                        "transcript": "不得进入摘要的秘密",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            live = MODULE.session_status(profile, output, started["session_id"])
            self.assertEqual(["ok"], live["database_quick_check"])
            self.assertEqual(1, live["behavior_row_deltas"]["voice_input"])
            self.assertEqual(1, live["runtime_diagnostics"]["events_since_start"])
            self.assertEqual({"200": 1}, live["runtime_diagnostics"]["http_status_counts"])
            self.assertEqual({"wake": 1}, live["runtime_diagnostics"]["voice_mode_counts"])
            self.assertFalse(live["privacy"]["private_snapshot_created"])
            self.assertTrue((output / "active-test-session.json").is_file())

            result = MODULE.session_stop(profile, output, install, started["session_id"])
            session_dir = Path(result["session_dir"])
            manifest_text = (session_dir / "manifest.json").read_text(encoding="utf-8")
            manifest = json.loads(manifest_text)

            self.assertEqual(["ok"], result["database_quick_check"])
            self.assertEqual(1, manifest["behavior_row_deltas"]["voice_input"])
            self.assertEqual(1, manifest["behavior_row_deltas"]["mistake_case"])
            self.assertEqual(1, manifest["behavior_row_deltas"]["model_call"])
            self.assertEqual(1, manifest["runtime_diagnostics"]["events_since_start"])
            self.assertEqual(1, manifest["runtime_diagnostics"]["event_counts"]["voice.asr.completed"])
            self.assertNotIn("孩子真实语音秘密", manifest_text)
            self.assertNotIn("家庭题目秘密", manifest_text)
            self.assertNotIn("不得进入摘要的秘密", manifest_text)
            self.assertFalse(manifest["privacy"]["automatic_upload"])
            self.assertTrue((session_dir / "private" / "home-study.db").is_file())
            self.assertFalse((output / "active-test-session.json").exists())
            with zipfile.ZipFile(result["archive_path"]) as archive:
                self.assertIn("private/home-study.db", archive.namelist())
                self.assertIn("manifest.json", archive.namelist())

    def test_stop_rejects_a_different_session_id_without_destroying_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / "profile"
            data = profile / "family-data"
            data.mkdir(parents=True)
            with sqlite3.connect(data / "home-study.db") as connection:
                connection.execute("CREATE TABLE audit_event(id TEXT PRIMARY KEY)")
            output = root / "sessions"
            MODULE.session_start(profile, output, None)
            with self.assertRaisesRegex(RuntimeError, "does not match"):
                MODULE.session_stop(profile, output, None, "wrong-session")
            self.assertTrue((output / "active-test-session.json").is_file())

    def test_stop_retry_preserves_an_existing_private_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / "profile"
            data = profile / "family-data"
            data.mkdir(parents=True)
            with sqlite3.connect(data / "home-study.db") as connection:
                connection.execute("CREATE TABLE audit_event(id TEXT PRIMARY KEY)")
            output = root / "sessions"
            started = MODULE.session_start(profile, output, None)
            private = Path(started["session_dir"]) / "private"
            private.mkdir(parents=True, exist_ok=True)
            prior = private / "home-study.db"
            prior.write_bytes(b"prior-snapshot-must-remain")

            result = MODULE.session_stop(profile, output, None, started["session_id"])
            manifest = json.loads((Path(result["session_dir"]) / "manifest.json").read_text(encoding="utf-8"))

            self.assertEqual(b"prior-snapshot-must-remain", prior.read_bytes())
            self.assertNotEqual("private/home-study.db", manifest["database"]["snapshot_path"])
            self.assertTrue((Path(result["session_dir"]) / manifest["database"]["snapshot_path"]).is_file())


if __name__ == "__main__":
    unittest.main()
