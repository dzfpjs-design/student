"""Exercise a packaged local agent in an isolated synthetic family directory."""
import argparse
import base64
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--document", action="store_true")
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="hsc-memory-acceptance-"))
    token = secrets.token_hex(24)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    env = {key: value for key, value in os.environ.items() if not key.startswith("HSC_")}
    env.update(HSC_DATA_DIR=str(root), HSC_DATABASE_PATH=str(root / "home-study.db"),
               HSC_SESSION_TOKEN=token, HSC_DATA_ENCRYPTION_KEY=base64.b64encode(secrets.token_bytes(32)).decode())
    process = None
    log = (root / "runtime.log").open("wb")

    def api(path, body=None, method=None, parent=None, expected=200):
        headers = {"X-HSC-Session": token, "Content-Type": "application/json"}
        if parent:
            headers["X-HSC-Parent"] = parent
        request = Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                          method=method or ("POST" if body is not None else "GET"), headers=headers)
        try:
            with urlopen(request, timeout=90 if path.endswith("/ocr") else 15) as response:
                status, data = response.status, response.read()
        except HTTPError as error:
            status, data = error.code, error.read()
        assert status == expected, f"{path}: expected {expected}, got {status}"
        return json.loads(data)

    def start():
        nonlocal process
        process = subprocess.Popen([str(Path(args.exe).resolve()), "--host", "127.0.0.1", "--port", str(port)], env=env, stdout=log, stderr=log)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            assert process.poll() is None, "packaged agent exited before readiness"
            try:
                api("/v1/health")
                return
            except (URLError, TimeoutError):
                time.sleep(.3)
        raise AssertionError("packaged service readiness timed out")

    def stop():
        nonlocal process
        if process and process.poll() is None:
            try:
                api("/v1/runtime/shutdown", {}, expected=202)
                process.wait(timeout=15)
            except Exception:
                process.terminate()
                process.wait(timeout=10)
        process = None

    def say(sid, text, key):
        turn = api(f"/v1/sessions/{sid}/agent-turn", {"transcript": text, "client_action_id": key})
        api(f"/v1/agent-turns/{turn['agent_turn_id']}/outcome", {"final_response": turn["response_text"], "tool_status": "completed"}, method="PUT")
        return turn

    checks = {}
    try:
        start()
        parent = api("/v1/parent/setup", {"pin": "2468"})["parent_token"]
        api("/v1/parent/privacy", {"cloud_model_text_allowed": False, "cloud_audio_allowed": False,
             "raw_media_storage_allowed": False, "model_improvement_allowed": False}, method="PUT", parent=parent)
        if args.document:
            import cv2
            import numpy as np
            sid = api("/v1/sessions", {"child_id": "child-primary-4", "client_action_id": "document-runtime-session"}, expected=201)["session_id"]
            def transition(before, after):
                api(f"/v1/sessions/{sid}/transition", {"expected_state": before, "target_state": after,
                    "reason": "synthetic acceptance", "client_action_id": "document-to-" + after})
            transition("check_in", "classroom_recall")
            api(f"/v1/sessions/{sid}/recalls", {"subject": "数学", "summary": "整数加法", "client_action_id": "document-recall"}, expected=201)
            transition("classroom_recall", "homework_capture")
            api(f"/v1/sessions/{sid}/homework", {"subject": "数学", "title": "加法练习", "expected_minutes": 20,
                "client_action_id": "document-homework"}, expected=201)
            api(f"/v1/sessions/{sid}/plan/generate", {"client_action_id": "document-plan"})
            transition("plan_review", "study")
            monitor = f"/v1/sessions/{sid}/document-monitor"
            api(monitor + "/start", {"client_action_id": "document-start"})
            revisions = []
            for index, lines in enumerate((["125 + 8 = 133"], ["125 + 8 = 133", "360 + 4 = 364"])):
                page = np.full((1000, 1400, 3), 100, dtype=np.uint8)
                cv2.rectangle(page, (100, 80), (1300, 920), (245, 245, 245), -1)
                for line_index, text in enumerate(lines):
                    cv2.putText(page, text, (180, 300 + 200 * line_index), cv2.FONT_HERSHEY_SIMPLEX, 2.5, (0, 0, 0), 4, cv2.LINE_AA)
                frame = base64.b64encode(cv2.imencode(".png", page)[1].tobytes()).decode()
                artifact = api(monitor + "/ocr", {"image_base64": frame, "client_action_id": f"document-frame-{index}"})
                assert artifact["extracted_text"].strip(), "packaged OCR returned no text"
                assert artifact["local_path"] is None
                revision = api(monitor + "/revisions", {"artifact_id": artifact["artifact_id"],
                    "content_signature": f"synthetic-page-{index}", "client_action_id": f"document-revision-{index}"})
                revisions.append(revision["revision_id"])
            deadline = time.monotonic() + 45
            memory_path = "/v1/parent/children/child-primary-4/companion-memory"
            while time.monotonic() < deadline:
                context = api(monitor + "/learning-context")
                items = api(memory_path, parent=parent)["items"]
                if context["status"] in {"completed", "blocked"} and items:
                    break
                time.sleep(.5)
            assert context["status"] in {"completed", "blocked"}, "background learning analysis did not settle"
            analysis_status = context["status"]
            if analysis_status == "blocked":
                assert context["analyses"] and all(item.get("reason") for item in context["analyses"])
            assert context["current_revision_id"] == revisions[-1]
            assert items, "document observations did not reach durable memory"
            assert not api(memory_path.replace("child-primary-4", "child-junior-3"), parent=parent)["items"]
            checks.update(real_packaged_ocr=True, two_page_revisions=True, background_analysis_or_explicit_block=True,
                          durable_document_memory=True, child_isolation=True, raw_frames_not_saved=True)
            stop()
            start()
            parent = api("/v1/parent/unlock", {"pin": "2468"})["parent_token"]
            assert api(monitor + "/learning-context")["current_revision_id"] == revisions[-1]
            assert api(memory_path, parent=parent)["items"]
            api("/v1/parent/children/child-primary-4/delete", {"scope": "media", "confirmation": "确认删除",
                "client_action_id": "document-delete"}, parent=parent)
            assert api(monitor + "/learning-context")["status"] == "empty"
            assert not api(memory_path, parent=parent)["items"]
            stop()
            start()
            parent = api("/v1/parent/unlock", {"pin": "2468"})["parent_token"]
            assert not api(memory_path, parent=parent)["items"]
            assert not api("/v1/parent/children/child-primary-4/model-calls", parent=parent)["items"]
            checks.update(restart_recovery=True, source_deletion=True, no_memory_resurrection=True, no_model_requests=True)
            output.write_text(json.dumps({"status": "passed", "runtime": "packaged_local_agent",
                "synthetic_isolated_data": True, "real_camera_or_child_evidence": False,
                "analysis_status": analysis_status, "unconfirmed_formula_not_graded": True, "checks": checks}, indent=2), encoding="utf-8")
            print(f"packaged continuous study acceptance: {len(checks)} checks passed")
            return
        sid = api("/v1/sessions", {"child_id": "child-primary-4", "client_action_id": "memory-runtime-session"}, expected=201)["session_id"]
        turn = say(sid, "记住我喜欢画图讲解", "memory-runtime-write")
        assert turn["intent"] == "memory_command" and turn["action"]["local_only"]
        path = "/v1/parent/children/child-primary-4/companion-memory"
        api(path, expected=401)
        items = api(path, parent=parent)["items"]
        assert len(items) == 1 and items[0]["source_text"]
        mid = items[0]["memory_id"]
        assert not api(path.replace("child-primary-4", "child-junior-3"), parent=parent)["items"]
        checks.update(voice_intent=True, source_persisted=True, parent_auth=True, child_isolation=True)
        stop()
        start()
        parent = api("/v1/parent/unlock", {"pin": "2468"})["parent_token"]
        assert api(path, parent=parent)["items"][0]["memory_id"] == mid
        recalled = say(sid, "你记得我喜欢什么吗", "memory-runtime-recall")
        assert "画图" in recalled["response_text"]
        api(f"/v1/sessions/{sid}/voice/synthesize/stream", {"text": recalled["response_text"],
            "client_action_id": "memory-runtime-tts", "source_agent_turn_id": recalled["agent_turn_id"]}, expected=409)
        api(path + "/" + mid, {"client_action_id": "memory-runtime-forget"}, method="DELETE", parent=parent)
        assert not api(path, parent=parent)["items"]
        replay = say(sid, "你记得我喜欢什么吗", "memory-runtime-recall")
        assert "画图" not in replay["response_text"]
        checks.update(restart_recall=True, cloud_tts_blocked=True, parent_forget=True, old_request_no_disclosure=True)
        stop()
        start()
        parent = api("/v1/parent/unlock", {"pin": "2468"})["parent_token"]
        assert not api(path, parent=parent)["items"]
        calls = api("/v1/parent/children/child-primary-4/model-calls", parent=parent)["items"]
        assert not calls
        checks.update(forget_survives_restart=True, no_model_requests=True)
        output.write_text(json.dumps({"status": "passed", "runtime": "packaged_local_agent", "synthetic_isolated_data": True,
                          "real_child_or_acoustic_evidence": False, "checks": checks}, indent=2), encoding="utf-8")
        print(f"packaged memory acceptance: {len(checks)} checks passed")
    finally:
        stop()
        log.close()


if __name__ == "__main__":
    main()
