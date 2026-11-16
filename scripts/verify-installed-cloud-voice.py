"""Verify the installed Windows backend with cloud language and isolated synthetic data.

The credential is supplied on stdin through SSH, never in arguments or files.
No microphone, camera, family database, or stored desktop credentials are read.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--exe", help="Optional candidate backend; otherwise verify the installed backend")
    args = parser.parse_args()
    credential = json.load(sys.stdin)["deepseek_key"]
    if not isinstance(credential, str) or not credential.startswith("sk-"):
        raise RuntimeError("Missing cloud probe credential")
    path = Path(__file__).with_name("verify-daily-learning-runtime.py")
    spec = importlib.util.spec_from_file_location("runtime_probe", path)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    exe = Path(args.exe) if args.exe else Path(os.environ["LOCALAPPDATA"]) / "Programs/home-study-companion/resources/local-agent/home-study-local-agent.exe"

    class CloudRuntime(helper.Runtime):
        def start(self):
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                self.port = sock.getsockname()[1]
            self.base = "http://127.0.0.1:{}".format(self.port)
            self.token = secrets.token_hex(24)
            env = helper.isolated_environment(self.root, self.token, self.encryption_key)
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
                env.pop(key, None)
            env.update(HSC_MODEL_PROVIDER="deepseek", HSC_MODEL_API_KEY=credential,
                       HSC_MODEL_BASE_URL="https://api.deepseek.com", HSC_MODEL_NAME="deepseek-v4-pro",
                       HSC_VOICE_INTENT_MODEL_NAME="deepseek-v4-flash", HSC_MODEL_TIMEOUT_SECONDS="25",
                       HSC_AGENT_RUNTIME_ENABLED="1", HSC_MAINTENANCE_ENABLED="0")
            self.process = subprocess.Popen([str(self.exe), "--host", "127.0.0.1", "--port", str(self.port)],
                                            cwd=self.root, env=env, stdin=subprocess.DEVNULL, stdout=self.log, stderr=self.log)
            self.starts += 1
            start = time.monotonic()
            while time.monotonic() - start < 60:
                helper.require(self.process.poll() is None, "Installed backend exited")
                try:
                    self.api("/v1/health", timeout=2)
                    boot = self.api("/v1/bootstrap")
                    helper.require(boot["model"]["configured"] and boot["model"]["provider"] == "deepseek", "Wrong cloud provider")
                    self.startup_seconds.append(round(time.monotonic() - start, 2))
                    return
                except (URLError, TimeoutError):
                    time.sleep(.3)
            raise RuntimeError("Installed backend readiness timeout")

    digest = hashlib.sha256()
    with exe.open("rb") as binary:
        for part in iter(lambda: binary.read(1024 * 1024), b""):
            digest.update(part)
    report = {"installed_backend": not bool(args.exe), "backend_sha256": digest.hexdigest(), "synthetic_only": True, "family_data_touched": False, "checks": []}
    with tempfile.TemporaryDirectory(prefix="hsc-installed-cloud-") as directory:
        runtime = CloudRuntime(exe, Path(directory))
        try:
            runtime.start()
            sid = runtime.api("/v1/sessions", {"child_id": "child-primary-4", "client_action_id": "cloud-verify-session"}, expected=201)["session_id"]
            init = runtime.api(f"/v1/sessions/{sid}/voice-interaction", {
                "transcript": "先别练这些了，我现在想让你看看我不会的那页", "client_action_id": "cloud-verify-init",
                "context": {"phase": "tutorial", "step": "wake_practice", "wake_word": "四年级同学"},
            })
            helper.require(init["source"] == "cloud" and init["route"] == "agent", "Tutorial swallowed real task")
            report["checks"].append("onboarding_cloud_handoff")
            question = runtime.api(f"/v1/sessions/{sid}/agent-turn", {
                "transcript": "关闭摄像头会影响你给我讲数学吗", "client_action_id": "cloud-verify-device-question",
            }, timeout=40)
            helper.require(question["action"]["type"] == "none" and question["action"]["source"] == "agent_runtime_planner",
                           "Device discussion was rejected or executed as a command")
            report["checks"].append("device_question_answered_without_operation")
            capture = runtime.api(f"/v1/sessions/{sid}/agent-turn", {
                "transcript": "先别练这些了，我现在想让你看看我不会的那页", "client_action_id": "cloud-verify-capture",
            }, timeout=40)
            helper.require(capture["action"]["type"] == "request_capture" and capture["action"]["source"] == "agent_runtime_planner", "Natural capture request not dispatched")
            report["checks"].append("natural_capture_cloud_plan")
            homework = runtime.api(f"/v1/sessions/{sid}/agent-turn", {
                "transcript": "语文老师让读第三课，数学练习册做第十页，都记下来", "client_action_id": "cloud-verify-homework",
            }, timeout=40)
            helper.require(homework["action"]["type"] == "add_homework_batch", "Compound homework lost a subject")
            tid = homework["agent_turn_id"]
            runtime.api(f"/v1/agent-turns/{tid}/execute", {})
            runtime.api(f"/v1/agent-turns/{tid}/execute", {})
            with closing(sqlite3.connect(runtime.database)) as db:
                rows = db.execute("SELECT subject,title FROM homework_item WHERE session_id=? ORDER BY subject", (sid,)).fetchall()
                calls = db.execute("SELECT count(*) FROM model_call").fetchone()[0]
                helper.require(db.execute("SELECT state FROM daily_session WHERE session_id=?", (sid,)).fetchone()[0] == "check_in", "Intake silently changed the conversation stage")
            helper.require(len(rows) == 2 and {row[0] for row in rows} == {"语文", "数学"}, "Homework persistence failed")
            report["checks"].append("compound_homework_saved_once")
            runtime.stop()
            runtime.start()
            runtime.api(f"/v1/agent-turns/{tid}/execute", {})
            with closing(sqlite3.connect(runtime.database)) as db:
                helper.require(rows == db.execute("SELECT subject,title FROM homework_item WHERE session_id=? ORDER BY subject", (sid,)).fetchall(), "Homework changed after restart")
                helper.require(calls == db.execute("SELECT count(*) FROM model_call").fetchone()[0], "Replay called cloud again")
                report["cloud_calls"] = [dict(zip(("purpose", "model", "status", "latency_ms"), row)) for row in db.execute("SELECT purpose,model,status,latency_ms FROM model_call")]
            report["checks"].append("restart_replay_no_duplicates_or_cloud_calls")
            report.update(status="passed", startup_seconds=runtime.startup_seconds)
        finally:
            runtime.stop()
            runtime.log.close()
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
