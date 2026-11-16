"""Paid, synthetic-only cloud planning probes; no family database or media access."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--case", action="append")
    parser.add_argument("--include-synthetic-model-output", action="store_true")
    args = parser.parse_args()
    key = subprocess.run(["security", "find-generic-password", "-s", "hsc-deepseek-api-key", "-w"],
                         capture_output=True, text=True, check=True).stdout.strip()
    with tempfile.TemporaryDirectory(prefix="hsc-cloud-language-") as directory:
        root = Path(directory)
        os.environ["HSC_DATA_DIR"] = str(root / "import")
        os.environ["HSC_DATABASE_PATH"] = str(root / "import" / "test.db")
        from fastapi.testclient import TestClient
        from home_study_agent.app import create_app
        from home_study_agent.config import Settings
        from home_study_agent.document_monitoring import DocumentMonitoringService

        cases = [
            ("wake_word_change", "study", "把唤醒词改为小枝小枝", {"configure_wake_word"}),
            ("wake_word_natural", "check_in", "我想换个叫你的名字，以后喊你小葵行吗", {"configure_wake_word"}),
            ("camera_resume", "study", "你把摄像头也打开", {"resume_observation"}),
            ("camera_status", "study", "现在摄像头打开了吗，你能看到我吗", {"query_device_status"}),
            ("document_status", "study", "高拍仪打开了吗", {"query_device_status"}),
            ("occlusion_reminders_off", "study", "稍微有点遮挡是正常的，你不要提示", {"set_document_occlusion_reminders"}),
            ("occlusion_reminders_on", "study", "我挡住本子的时候你还是提醒我一下吧", {"set_document_occlusion_reminders"}),
            ("greeting", "check_in", "哈喽，你今天精神怎么样呀", {"none"}),
            ("topic_only", "check_in", "一元二次方程", {"none"}),
            ("capture_natural", "check_in", "我把不会的那一页摆好了，你瞅瞅呗", {"request_capture"}),
            ("capture_fixed", "check_in", "帮我看题", {"request_capture"}),
            ("question_natural", "check_in", "两个负数碰到一起咋就成正数了呀", {"ask_tutor"}),
            ("start_learning", "study", "开始学习", {"start_next_task"}),
            ("plan_status", "study", "今天还有哪些任务要做", {"describe_plan"}),
            ("story_natural", "check_in", "咱们编个故事呗，我来决定后面发生什么", {"start_companion_activity"}),
            ("homework_batch", "check_in", "数学的练习册写第八页，另外英语要抄五个单词，两个都帮我记好", {"add_homework_batch"}),
            ("monitor_natural", "study", "先别盯着我的本子了，等会儿我再喊你", {"control_document_monitor"}),
            ("negated_microphone_stop", "study", "不要关闭麦克风，我还想问一道题", {"none"}),
            ("question_about_camera", "study", "关闭摄像头会影响你给我讲数学吗", {"none"}),
            ("bare_acknowledgement", "study", "嗯", {"none"}),
            ("tutorial_resume", "check_in", "我忘了怎么叫你了，再带我练一次", {"resume_voice_tutorial"}),
            ("onboarding_name", "check_in", "我给你起个名字，以后就叫小知，好不好", {"onboarding:name_candidate"}),
            ("onboarding_confirm", "check_in", "嗯，就是这个，听着挺亲切的", {"onboarding:confirm_name"}),
            ("onboarding_reject", "check_in", "不对，我还没想好呢", {"onboarding:reject_name", "clarify:none"}),
            ("onboarding_task", "check_in", "这些先放一边吧，我现在想弄懂分数怎么约分", {"agent:none"}),
            ("onboarding_skip", "check_in", "先不练这个了，等有空再教我吧", {"onboarding:skip"}),
            ("onboarding_repeat", "check_in", "刚才漏听了，你慢点再讲一遍", {"onboarding:repeat"}),
        ]
        onboarding_contexts = {
            "onboarding_name": {"phase": "naming"},
            "onboarding_confirm": {"phase": "confirm_name", "pending_name": "小知"},
            "onboarding_reject": {"phase": "confirm_name", "pending_name": "小知"},
            "onboarding_task": {"phase": "tutorial", "step": "wake_practice", "wake_word": "四年级同学"},
            "onboarding_skip": {"phase": "tutorial", "step": "wake_practice", "wake_word": "四年级同学"},
            "onboarding_repeat": {"phase": "tutorial", "step": "wake_practice", "wake_word": "四年级同学"},
        }
        results = []
        for name, state, utterance, actions in cases:
            if args.case and name not in args.case:
                continue
            folder = root / name
            settings = Settings(data_dir=folder, database_path=folder / "test.db", maintenance_enabled=False,
                                model_provider="deepseek", model_api_key=key, model_base_url="https://api.deepseek.com",
                                model_name="deepseek-v4-pro", voice_intent_model_name="deepseek-v4-flash",
                                model_timeout_seconds=25, data_encryption_key=b"1" * 32)
            synthetic_outputs = []
            with TestClient(create_app(settings)) as client:
                gateway = client.app.state.model_gateway
                complete = gateway.complete
                async def record_synthetic(request):
                    result = await complete(request)
                    synthetic_outputs.append(result.content)
                    return result
                if args.include_synthetic_model_output:
                    gateway.complete = record_synthetic
                sid = client.post("/v1/sessions", json={"child_id": "child-primary-4", "client_action_id": name + "-session"}).json()["session_id"]
                if name == "start_learning":
                    def post_required(path, body):
                        response = client.post(path, json=body)
                        if response.status_code not in (200, 201):
                            raise RuntimeError(f"synthetic plan setup failed: {path}: {response.status_code}")
                    post_required(f"/v1/sessions/{sid}/transition", {"expected_state": "check_in", "target_state": "classroom_recall", "reason": "synthetic acceptance setup", "client_action_id": name + "-transition-1"})
                    post_required(f"/v1/sessions/{sid}/recalls/none", {"client_action_id": name + "-recall-none"})
                    post_required(f"/v1/sessions/{sid}/transition", {"expected_state": "classroom_recall", "target_state": "homework_capture", "reason": "synthetic acceptance setup", "client_action_id": name + "-transition-2"})
                    post_required(f"/v1/sessions/{sid}/homework", {"subject": "数学", "title": "合成验收练习", "expected_minutes": 20, "client_action_id": name + "-homework"})
                    post_required(f"/v1/sessions/{sid}/plan/generate", {"client_action_id": name + "-plan"})
                    post_required(f"/v1/sessions/{sid}/transition", {"expected_state": "plan_review", "target_state": "study", "reason": "synthetic acceptance setup", "client_action_id": name + "-transition-3"})
                with client.app.state.agent_orchestrator.database.connect() as db:
                    if name != "start_learning":
                        db.execute("UPDATE daily_session SET state=? WHERE session_id=?", (state, sid))
                        db.commit()
                if name == "monitor_natural":
                    DocumentMonitoringService(client.app.state.agent_orchestrator.database).start(sid, "synthetic-monitor")
                start = time.monotonic()
                body = {"transcript": utterance, "client_action_id": name + "-turn"}
                context = onboarding_contexts.get(name)
                if context:
                    body["context"] = context
                response = client.post(f"/v1/sessions/{sid}/" + ("voice-interaction" if context else "agent-turn"), json=body)
                value = response.json() if response.status_code == 200 else {}
                action = {"type": value.get("route", "") + ":" + value.get("action", ""), "source": value.get("source")} if context else value.get("action", {})
                result = {"case": name, "http": response.status_code, "action": action.get("type"),
                          "source": action.get("source"), "seconds": round(time.monotonic() - start, 2),
                          "reply": value.get("response_text"), "command": action.get("command"),
                          "passed": response.status_code == 200 and action.get("type") in actions and action.get("source") == ("cloud" if context else "verified_general_plan_control" if name == "start_learning" else "agent_runtime_planner")}
                if name == "homework_batch" and result["passed"]:
                    execution = client.post(f"/v1/agent-turns/{value['agent_turn_id']}/execute")
                    replay = client.post(f"/v1/agent-turns/{value['agent_turn_id']}/execute")
                    with client.app.state.agent_orchestrator.database.connect() as db:
                        count = db.execute("SELECT count(*) FROM homework_item WHERE session_id=?", (sid,)).fetchone()[0]
                    result.update(execute_http=execution.status_code, replay_http=replay.status_code, saved_items=count)
                    result["passed"] = execution.status_code == replay.status_code == 200 and count == 2
                if name == "start_learning" and result["passed"]:
                    execution = client.post(f"/v1/agent-turns/{value['agent_turn_id']}/execute")
                    with client.app.state.agent_orchestrator.database.connect() as db:
                        active_count = db.execute("SELECT COUNT(*) FROM plan_item WHERE plan_id IN (SELECT plan_id FROM daily_plan WHERE session_id=?) AND status='active'", (sid,)).fetchone()[0]
                    result.update(execute_http=execution.status_code, active_items=active_count)
                    result["passed"] = execution.status_code == 200 and active_count == 1
                if name == "story_natural" and result["passed"]:
                    execution = client.post(f"/v1/agent-turns/{value['agent_turn_id']}/execute")
                    result["execute_http"] = execution.status_code
                    result["opening_used"] = action.get("opening") in json.dumps(execution.json(), ensure_ascii=False) if action.get("opening") else False
                    result["passed"] = execution.status_code == 200 and result["opening_used"]
                with client.app.state.agent_orchestrator.database.connect() as db:
                    result["calls"] = [dict(row) for row in db.execute(
                        "SELECT purpose,provider,model,status,error_code,prompt_tokens,completion_tokens FROM model_call WHERE session_id=?", (sid,))]
                if name == "start_learning":
                    result["passed"] = result["passed"] and any(call["purpose"] == "agent_plan" and call["provider"] == "deepseek" and call["status"] == "completed" for call in result["calls"])
                results.append(result)
                if args.include_synthetic_model_output:
                    result["synthetic_model_outputs"] = synthetic_outputs
                Path(args.output).write_text(json.dumps({"synthetic_only": True, "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps({k: v for k, v in result.items() if k != "calls"}, ensure_ascii=False), flush=True)
        return 0 if all(item["passed"] for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
