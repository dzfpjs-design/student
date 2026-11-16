"""Packaged local-agent acceptance, stdlib only; never open a family profile.

Windows: python scripts/verify-daily-learning-runtime.py --exe <local-agent.exe>
         --output <new-evidence.json>

All writes use the real protected HTTP API and a freshly generated temporary
profile. SQLite is opened read-only ONLY inside that profile to detect duplicate
tasks hidden by summary aggregation. No clock changes, cloud, microphone, real
child, or delayed-retest success is claimed. Existing output files are refused.
"""
import argparse
import base64
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


CHINA = timezone(timedelta(hours=8))
FIXTURES = (
    ("child-junior-3", "数学", "合成A判别式作业", "diag-j3-discriminant-1"),
    ("child-primary-4", "数学", "合成B运算作业", "diag-p4-arithmetic-closed-1"),
)


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("Runtime redirects are forbidden")


def isolated_environment(root, token, encryption_key):
    env = {}
    for key, value in os.environ.items():
        if key.startswith("HSC_") or re.search(
            r"API_KEY|ACCESS_KEY|SECRET|TOKEN|CREDENTIAL|PASSWORD|KEYCHAIN", key, re.I
        ) or key.upper() in {
            "PYTHONPATH", "PYTHONHOME", "NODE_OPTIONS", "HTTP_PROXY", "HTTPS_PROXY",
            "ALL_PROXY", "NO_PROXY", "ELECTRON_RUN_AS_NODE", "ELECTRON_RENDERER_URL",
        }:
            continue
        env[key] = value
    for name in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA"):
        directory = root / name.lower()
        directory.mkdir(exist_ok=True)
        env[name] = str(directory)
    env.update(
        HSC_DATA_DIR=str(root / "data"),
        HSC_DATABASE_PATH=str(root / "data" / "home-study.db"),
        HSC_SESSION_TOKEN=token, HSC_DATA_ENCRYPTION_KEY=encryption_key,
        HTTP_PROXY="http://127.0.0.1:9", HTTPS_PROXY="http://127.0.0.1:9",
        ALL_PROXY="http://127.0.0.1:9", NO_PROXY="127.0.0.1,localhost,::1",
        PYTHONDONTWRITEBYTECODE="1",
    )
    return env


class Runtime:
    def __init__(self, exe, root):
        self.exe, self.root = exe, root
        self.database = root / "data" / "home-study.db"
        self.encryption_key = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
        self.process = None
        self.log = (root / "runtime.log").open("wb")
        self.opener = build_opener(ProxyHandler({}), NoRedirect())
        self.starts = 0
        self.startup_seconds = []
        self.forced_stops = 0

    def api(self, path, body=None, method=None, parent=None, expected=200, timeout=30):
        require(path.startswith("/v1/"), "Only local API paths are allowed")
        headers = {"X-HSC-Session": self.token, "Content-Type": "application/json"}
        if parent:
            headers["X-HSC-Parent"] = parent
        request = Request(self.base + path, headers=headers,
                          data=json.dumps(body).encode("utf-8") if body is not None else None,
                          method=method or ("POST" if body is not None else "GET"))
        try:
            with self.opener.open(request, timeout=timeout) as response:
                status, data = response.status, response.read()
        except HTTPError as error:
            status, data = error.code, error.read()
        require(status == expected, "{} {}: expected HTTP {}, got {}".format(
            request.method, path, expected, status))
        return json.loads(data)

    def start(self):
        require(self.process is None, "Previous owned runtime must be stopped")
        started_at = time.monotonic()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.base = "http://127.0.0.1:{}".format(self.port)
        self.token = secrets.token_hex(24)
        self.process = subprocess.Popen(
            [str(self.exe), "--host", "127.0.0.1", "--port", str(self.port)],
            cwd=str(self.root), env=isolated_environment(self.root, self.token, self.encryption_key),
            stdin=subprocess.DEVNULL, stdout=self.log, stderr=self.log,
        )
        self.starts += 1
        # Match the desktop service manager's real startup budget.
        deadline = started_at + 60
        while time.monotonic() < deadline:
            require(self.process.poll() is None, "Packaged agent exited before readiness")
            try:
                health = self.api("/v1/health", timeout=2)
                require(health.get("status") == "ok", "Unexpected health response")
                bootstrap = self.api("/v1/bootstrap")
                require(bootstrap["model"]["configured"] is False, "Cloud model unexpectedly configured")
                require(bootstrap["model"]["fallback_configured"] is False, "Cloud fallback unexpectedly configured")
                require(bootstrap["voice"]["configured"] is False, "Cloud TTS unexpectedly configured")
                require(self.database.is_file(), "Runtime did not create the isolated database")
                self.startup_seconds.append(round(time.monotonic() - started_at, 3))
                return bootstrap
            except (URLError, TimeoutError):
                time.sleep(.3)
        raise RuntimeError("Packaged service readiness timed out")

    def stop(self, *, force=False):
        if self.process is None:
            return
        process = self.process
        if process.poll() is None:
            try:
                if force:
                    raise RuntimeError("Intentional owned-process crash acceptance")
                self.api("/v1/runtime/shutdown", {}, expected=202, timeout=5)
                process.wait(timeout=20)
            except Exception:
                self.forced_stops += 1
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=False)
                process.wait(timeout=10)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=.3):
                    pass
            except OSError:
                self.process = None
                return
            time.sleep(.1)
        raise RuntimeError("Owned runtime port remains open; temporary data retained")

    def snapshot(self):
        require(self.database.resolve().parent == (self.root / "data").resolve(), "Database left disposable profile")
        with closing(sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True, timeout=15)) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            return {
                "plans": db.execute("""SELECT plan_id,child_id,session_id,plan_date,status,
                    total_minutes,generator,version,client_action_id FROM daily_plan ORDER BY plan_id""").fetchall(),
                "reviews": db.execute("""SELECT review_task_id,child_id,source_evidence_id,
                    interval_label,due_at,status FROM review_task ORDER BY review_task_id""").fetchall(),
                "plan_items": db.execute("""SELECT pi.plan_item_id,dp.child_id,dp.session_id,
                    pi.source_type,pi.source_ref,pi.status FROM plan_item pi JOIN daily_plan dp USING(plan_id)
                    ORDER BY pi.plan_item_id""").fetchall(),
                "reflections": db.execute("SELECT reflection_id,child_id,session_id FROM daily_reflection ORDER BY reflection_id").fetchall(),
                "closures": db.execute("SELECT session_id,child_id,status FROM learning_session_closure ORDER BY session_id").fetchall(),
                "mistakes": db.execute("""SELECT mistake_id,child_id,source_evidence_id,status
                    FROM mistake_case ORDER BY mistake_id""").fetchall(),
            }


def transition(runtime, sid, before, after, key):
    return runtime.api("/v1/sessions/{}/transition".format(sid), {
        "expected_state": before, "target_state": after,
        "reason": "Synthetic packaged acceptance", "client_action_id": key + ":" + after,
    })


def verify_summary(summary, child, sid, title, homework_id, evidence_id, other_title):
    require(summary["child_id"] == child and summary["session_id"] == sid, "Summary ownership mismatch")
    require(summary["local_only"] is True and summary["cloud_transmission_allowed"] is False,
            "Learning summary must remain local")
    require(summary["mastery_update_allowed"] is False, "Summary cannot claim mastery")
    require(any(item["source"]["source_id"] == homework_id and item["title"] == title
                and item["status"] == "completed" for item in summary["homework"]),
            "Synthetic homework completion missing from summary")
    require(any(item["source"]["source_id"] == evidence_id for item in summary["weak_points"]),
            "Wrong-answer evidence missing from summary")
    for field in ("learning_content", "homework", "weak_points", "needs_verification"):
        require(all(item["source"]["session_id"] == sid for item in summary[field]), "Cross-session evidence in summary")
    require(title in summary["child_response"] and other_title not in json.dumps(summary, ensure_ascii=False),
            "Summary missing own homework or contains other child's synthetic marker")


def create_learning_day(runtime, fixture, parent):
    child, subject, title, diagnostic_id = fixture
    # A keyword/reference rubric must not be used as reliable mastery evidence.
    # Populate qualified local items through the real parent API, never SQL writes.
    for _ in range(2):
        runtime.api('/v1/parent/local-supply/refresh', {}, parent=parent, timeout=90)
    with closing(sqlite3.connect(runtime.database.as_uri() + '?mode=ro', uri=True)) as connection:
        candidates = connection.execute('''SELECT di.diagnostic_item_id, di.expected_answer_json
            FROM diagnostic_item di JOIN knowledge_node kn USING (knowledge_node_id)
            JOIN child_profile cp ON cp.child_id=?
            WHERE di.status='active' AND kn.grade_band=cp.grade
              AND di.knowledge_node_id=(SELECT knowledge_node_id FROM diagnostic_item WHERE diagnostic_item_id=?)
            ORDER BY (di.diagnostic_item_id=?) DESC, di.diagnostic_item_id''',
            (child, diagnostic_id, diagnostic_id)).fetchall()
    qualified = [row[0] for row in candidates if json.loads(row[1]).get('local_rule') == 'local_exact_v1']
    require(len(qualified) >= 2, 'No independently scored original and transfer question available for this node')
    diagnostic_id = qualified[0]
    key = "packaged-daily:" + child
    runtime.api("/v1/parent/children/{}/family-rule".format(child), {
        "latest_end_time": None, "max_learning_minutes": 90, "task_segment_minutes": 30,
        "break_minutes": 5, "pet_min_minutes": 0, "competition_minutes": 0,
        "observation_mode": "shadow", "reminder_budget_per_10m": 0,
    }, method="PUT", parent=parent)
    sid = runtime.api("/v1/sessions", {"child_id": child, "client_action_id": key + ":session"}, expected=201)["session_id"]
    path = "/v1/sessions/" + sid
    transition(runtime, sid, "check_in", "classroom_recall", key)
    runtime.api(path + "/recalls", {"subject": subject, "summary": title + "课堂回想",
                                   "client_action_id": key + ":recall"}, expected=201)
    transition(runtime, sid, "classroom_recall", "homework_capture", key)
    homework = runtime.api(path + "/homework", {"subject": subject, "title": title,
        "expected_minutes": 10, "client_action_id": key + ":homework"}, expected=201)
    plan = runtime.api(path + "/plan/generate", {"client_action_id": key + ":plan"})
    require(plan["session"]["state"] == "plan_review", "Plan did not enter review")
    transition(runtime, sid, "plan_review", "study", key)
    school = [item for item in plan["plan_items"] if item["source_type"] == "school_homework" and item["status"] == "pending"]
    require(bool(school), "Synthetic homework not present in real plan")
    for index, item in enumerate(plan["plan_items"]):
        if item["status"] not in ("pending", "active", "paused"):
            continue
        item_path = "/v1/plan-items/" + item["plan_item_id"]
        if item["source_type"] == "school_homework":
            runtime.api(item_path + "/start", {"client_action_id": key + ":start:" + str(index)})
            runtime.api(item_path + "/complete", {"actual_minutes": 1,
                "completion_evidence_type": "child_confirmed",
                "completion_note": "Synthetic self-report only, not real child or mastery evidence.",
                "client_action_id": key + ":complete:" + str(index)})
        else:
            runtime.api(item_path + "/reschedule", {"reason": "Synthetic acceptance explicitly defers optional work",
                "client_action_id": key + ":defer:" + str(index)})
    item = runtime.api(path + "/diagnostics/next?" + urlencode({"track": "school", "preferred_item_id": diagnostic_id}))["item"]
    require(item and item["diagnostic_item_id"] == diagnostic_id, "Expected real seeded diagnostic unavailable")
    attempt = runtime.api(path + "/diagnostics/answer", {"diagnostic_item_id": diagnostic_id,
        "answer": "我暂时不知道", "client_action_id": key + ":wrong"})
    require(attempt["child_id"] == child and attempt["session_id"] == sid and attempt["correctness"] == 0,
            "Synthetic answer was not recorded as incorrect for this child")
    mistake = attempt.get("mistake_created") or attempt.get("mistake")
    require(mistake and mistake["status"] == "diagnosing", "Reliable wrong answer did not open its mistake diagnosis")
    mistake_path = "/v1/mistakes/" + mistake["mistake_id"] + "/next?" + urlencode({"child_id": child})
    require(runtime.api(mistake_path)["kind"] == "diagnosis", "Mistake diagnosis is not available through real HTTP")
    immediate = runtime.api(path + "/diagnostics/next?track=school")["item"]
    require(not immediate or not immediate.get("review_task_id"), "Parallel generic review survived mistake handoff")
    transition(runtime, sid, "study", "reflection", key)
    finish = {"skipped": True, "client_action_id": key + ":finish"}
    ended = runtime.api(path + "/reflection/complete", finish)
    require(ended["session"]["status"] == "completed" and ended["session"]["state"] == "completed", "Session did not complete")
    summary = runtime.api(path + "/learning-summary")
    other_title = next(other[2] for other in FIXTURES if other[0] != child)
    verify_summary(summary, child, sid, title, homework["homework_id"], attempt["evidence_id"], other_title)
    reviews = [row for row in summary["remediation"] if row["source_type"] == "mistake_review"]
    require(len(reviews) == 1 and reviews[0]["source_ref"] == mistake["mistake_id"] and reviews[0]["status"] == "pending",
            "Closure lost the single pending mistake remediation source")
    require(not any(row["source_type"] == "due_review" for row in summary["remediation"]),
            "Closure recreated a parallel generic review")
    before = runtime.snapshot()
    runtime.api(path + "/reflection/complete", finish)
    runtime.api(path + "/reflection/complete", {**finish, "client_action_id": key + ":second-finish"}, expected=409)
    require(runtime.snapshot() == before, "Repeated completion created or changed durable tasks")
    require(runtime.api(path + "/learning-summary") == summary, "Repeated completion changed summary")
    return {"child": child, "sid": sid, "summary": summary, "finish": finish,
            "day": summary["learning_date"], "evidence_id": attempt["evidence_id"],
            "diagnostic_item_id": diagnostic_id,
            "mistake_id": mistake["mistake_id"], "mistake_path": mistake_path}


def daily_summary(runtime, record):
    result = runtime.api("/v1/children/{}/daily-learning-summary?{}".format(
        record["child"], urlencode({"learning_date": record["day"]})))
    require(result["status"] == "ready" and len(result["sessions"]) == 1
            and result["sessions"][0]["session_id"] == record["sid"]
            and result["sessions"][0]["child_id"] == record["child"]
            and result["sessions"][0]["remediation"] == record["summary"]["remediation"]
            and result["sessions"][0]["child_response"] == record["summary"]["child_response"],
            "Daily summary lost or mixed session evidence; rerun if China midnight was crossed")
    require(result["cloud_transmission_allowed"] is False, "Daily summary permits cloud transmission")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", required=True, help="Packaged local-agent .exe, NOT the Electron installer")
    parser.add_argument("--output", required=True, help="New JSON evidence file; existing files are never overwritten")
    args = parser.parse_args()
    exe, output = Path(args.exe).resolve(), Path(args.output).resolve()
    require(os.name == "nt", "Actual acceptance requires Windows; --help and syntax checks work elsewhere")
    require(exe.is_file() and exe.suffix.lower() == ".exe", "--exe must be the real packaged local agent")
    require(output.suffix.lower() == ".json" and not output.exists(), "--output must be a new .json file")
    output.parent.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="hsc-daily-learning-acceptance-")).resolve()
    runtime = Runtime(exe, root)
    report = {"status": "running", "runtime": "packaged_local_agent", "synthetic_isolated_data": True,
              "all_fixture_writes": "real_http", "database_access": "own_temporary_profile_read_only",
              "real_child_or_acoustic_evidence": False, "delayed_retest_answered": False,
              "system_clock_changed": False, "checks": {}}
    failure = None
    try:
        with exe.open("rb") as binary:
            digest = hashlib.sha256()
            for chunk in iter(lambda: binary.read(1024 * 1024), b""):
                digest.update(chunk)
        report["exe_sha256"] = digest.hexdigest()
        bootstrap = runtime.start()
        require({child["child_id"] for child in bootstrap["children"]} == {row[0] for row in FIXTURES}
                and bootstrap["active_session"] is None, "New synthetic profile was not empty")
        parent = runtime.api("/v1/parent/setup", {"pin": "2468"})["parent_token"]
        privacy = {"cloud_model_text_allowed": False, "cloud_audio_allowed": False,
                   "raw_media_storage_allowed": False, "model_improvement_allowed": False}
        runtime.api("/v1/parent/privacy", privacy, method="PUT", parent=parent)
        for child, _, _, _ in FIXTURES:
            require(runtime.api("/v1/children/{}/daily-learning-summary".format(child))["sessions"] == [],
                    "Fresh profile contains learning history")
        report["checks"]["isolated_empty_profile_and_no_cloud_configuration"] = True
        records = []
        for fixture in FIXTURES:
            record = create_learning_day(runtime, fixture, parent)
            record["daily"] = daily_summary(runtime, record)
            records.append(record)
            for existing in records:
                require(daily_summary(runtime, existing) == existing["daily"], "Other child changed existing daily summary")
            if len(records) == 1:
                require(runtime.api("/v1/children/{}/daily-learning-summary".format(FIXTURES[1][0]))["sessions"] == [],
                        "First child's learning leaked into untouched child")
        baseline = runtime.snapshot()
        require(len(baseline["closures"]) == 2 and all(row[2] == "completed" for row in baseline["closures"]),
                "Both completed closure receipts must exist")
        for record in records:
            reviews = [row for row in baseline["reviews"] if row[1] == record["child"]]
            require(len(reviews) == 3 and {row[3] for row in reviews} == {"immediate", "48h", "7d"}
                    and all(row[2] == record["evidence_id"] and row[5] == "cancelled" for row in reviews),
                    "Parallel review history was deleted, revived, duplicated, or cross-child")
            cases = [row for row in baseline["mistakes"] if row[1] == record["child"]]
            require(len(cases) == 1 and cases[0][0] == record["mistake_id"]
                    and cases[0][2] == record["evidence_id"] and cases[0][3] == "diagnosing",
                    "Mistake source was not durably transferred exactly once")
        report["checks"].update(http_homework_wrong_answer_and_completion=True,
                                local_summary_and_mistake_handoff=True, two_child_isolation=True,
                                repeated_completion_does_not_duplicate_tasks=True)
        runtime.stop()
        bootstrap = runtime.start()
        require(bootstrap["active_session"] is None, "Completed session resurrected after executable restart")
        parent = runtime.api("/v1/parent/unlock", {"pin": "2468"})["parent_token"]
        require(runtime.snapshot() == baseline, "Restart changed durable task identities or schedule")
        for record in records:
            path = "/v1/sessions/" + record["sid"]
            require(runtime.api(path + "/learning-summary") == record["summary"], "Session summary not restored")
            require(daily_summary(runtime, record) == record["daily"], "Daily summary not restored")
            require(runtime.api(record["mistake_path"])["kind"] == "diagnosis", "Restart lost the pending diagnosis")
            runtime.api(path + "/reflection/complete", record["finish"])
            require(runtime.snapshot() == baseline, "Replayed completion after restart duplicated tasks")
            require(not runtime.api("/v1/parent/children/{}/model-calls".format(record["child"]), parent=parent)["items"],
                    "Unexpected cloud/model call record")
        report["checks"].update(executable_restart_summary_and_mistake_source_persist=True,
                                replay_after_restart_is_idempotent=True, no_model_call_records=True)
        following = []
        for record in records:
            key = "packaged-following-plan:" + record["child"]
            sid = runtime.api("/v1/sessions", {"child_id": record["child"], "client_action_id": key}, expected=201)["session_id"]
            transition(runtime, sid, "check_in", "classroom_recall", key)
            runtime.api("/v1/sessions/" + sid + "/recalls", {"subject": "数学", "summary": "合成后续学习",
                "client_action_id": key + ":recall"}, expected=201)
            transition(runtime, sid, "classroom_recall", "homework_capture", key)
            runtime.api("/v1/sessions/" + sid + "/homework", {"subject": "数学", "title": "合成后续作业",
                "expected_minutes": 5, "client_action_id": key + ":homework"}, expected=201)
            plan = runtime.api("/v1/sessions/" + sid + "/plan/generate", {"client_action_id": key + ":plan"})
            tasks = [row for row in plan["plan_items"] if row["source_type"] == "mistake_review"
                and row["source_ref"] == record["mistake_id"]]
            require(len(tasks) == 1 and tasks[0]["status"] == "pending", "Following plan lost pending mistake remediation")
            transition(runtime, sid, "plan_review", "study", key)
            for index, item in enumerate(plan["plan_items"]):
                if item["status"] in ("pending", "active", "paused"):
                    runtime.api("/v1/plan-items/" + item["plan_item_id"] + "/reschedule", {
                        "reason": "Synthetic acceptance preserves unfinished work, not mastery",
                        "client_action_id": key + ":defer:" + str(index)})
            transition(runtime, sid, "study", "reflection", key)
            runtime.api("/v1/sessions/" + sid + "/reflection/complete", {
                "skipped": True, "client_action_id": key + ":finish"})
            require(runtime.api(record["mistake_path"])["kind"] == "diagnosis", "Deferring work falsely advanced mastery")
            following.append({"child": record["child"], "sid": sid,
                              "workspace": runtime.api("/v1/sessions/" + sid),
                              "mistake_task_id": tasks[0]["plan_item_id"],
                              "mistake_id": record["mistake_id"]})
        report["checks"]["following_plan_resumes_owned_mistake"] = True
        following_baseline = runtime.snapshot()
        for record in following:
            rows = [row for row in following_baseline["plan_items"] if row[0] == record["mistake_task_id"]]
            require(len(rows) == 1 and rows[0][1:6] == (
                record["child"], record["sid"], "mistake_review", record["mistake_id"], "rescheduled"),
                "Deferred remediation was completed, duplicated, or assigned to another child")
        require(len(following_baseline["closures"]) == 4, "Following sessions lack durable closure receipts")
        # Kill only this harness-owned process tree, without a graceful WAL checkpoint.
        runtime.stop(force=True)
        bootstrap = runtime.start()
        require(bootstrap["active_session"] is None, "Crash resurrected a completed following session")
        parent = runtime.api("/v1/parent/unlock", {"pin": "2468"})["parent_token"]
        require(runtime.snapshot() == following_baseline, "Crash recovery changed following plans or task evidence")
        for record in following:
            require(runtime.api("/v1/sessions/" + record["sid"]) == record["workspace"],
                    "Following workspace was not restored exactly after a crash")
            require(not runtime.api("/v1/parent/children/{}/model-calls".format(record["child"]), parent=parent)["items"],
                    "Crash recovery unexpectedly invoked a model")
        for record in records:
            require(runtime.api(record["mistake_path"])["kind"] == "diagnosis", "Crash recovery falsely advanced mastery")
        report["checks"].update(following_plan_survives_owned_process_crash=True,
                                deferred_work_remains_unfinished_after_crash=True)
        report["evidence"] = {"sessions": len(records) + len(following), "initial_sessions": len(records),
                              "following_sessions": len(following), "reviews": len(baseline["reviews"]),
                              "startup_seconds": runtime.startup_seconds,
                              "diagnostic_items": [r["diagnostic_item_id"] for r in records],
                              "scoring_protocol": "local_exact_v1_not_keyword_reference",
                              "cancelled_parallel_review_intervals": ["immediate", "48h", "7d"],
                              "active_remediation": "mistake_diagnosis_not_mastery",
                              "following_plan_is_real_http_not_simulated_next_day": True,
                              "summary_sha256": [hashlib.sha256(json.dumps(r["summary"], sort_keys=True).encode()).hexdigest() for r in records]}
        report["status"] = "passed"
    except Exception as error:
        failure = error
        report.update(status="failed", error_type=type(error).__name__, error=str(error))
    finally:
        try:
            runtime.stop()
            report["checks"]["owned_process_and_port_stopped"] = True
        except Exception as error:
            failure = failure or error
            report.update(status="failed", cleanup_error=str(error), retained_synthetic_profile=str(root))
        runtime.log.close()
        report["runtime_starts"] = runtime.starts
        report["forced_stops"] = runtime.forced_stops
        if failure:
            report["retained_synthetic_profile"] = str(root)
        elif report["checks"].get("owned_process_and_port_stopped"):
            try:
                shutil.rmtree(root)
                report["checks"]["temporary_profile_removed"] = True
            except OSError as error:
                failure = failure or error
                report.update(status="failed", cleanup_error=str(error), retained_synthetic_profile=str(root))
        with output.open("x", encoding="utf-8") as destination:
            json.dump(report, destination, ensure_ascii=False, indent=2)
            destination.write("\n")
        print("packaged daily-learning acceptance: {}; evidence: {}".format(report["status"], output))
    return 1 if failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
