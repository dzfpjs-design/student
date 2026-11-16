"""Synthetic native-harness seed through real local HTTP, never SQL learning claims."""
import argparse
import json
import os
from pathlib import Path

from fastapi.testclient import TestClient

def seed(database_path: Path) -> dict:
    if database_path.exists():
        raise ValueError("Seed requires a new isolated database; existing evidence is never overwritten")
    if (os.environ.get("HSC_DATABASE_PATH") != str(database_path)
            or os.environ.get("HSC_DATA_DIR") != str(database_path.parent)):
        raise ValueError("Seed requires the harness environment bound to its explicit database path")
    # app's module-level ASGI instance may initialize the configured path on import.
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from home_study_agent.database import Database
    from home_study_agent.parent_reports import ParentReportService
    from home_study_agent.repository import StudyRepository

    settings = Settings(data_dir=database_path.parent, database_path=database_path,
                        maintenance_enabled=False, model_api_key=None,
                        cloud_asr_provider="disabled", tts_provider="disabled")
    with TestClient(create_app(settings)) as client:
        def post(path, **payload):
            response = client.post(path, json=payload)
            if response.status_code not in (200, 201):
                raise AssertionError(f"Synthetic seed {path}: HTTP {response.status_code}: {response.text}")
            return response.json()

        session = post("/v1/sessions", child_id="child-junior-3", client_action_id="smtp-seed-session")
        sid = session["session_id"]

        def transition(before, after):
            post(f"/v1/sessions/{sid}/transition", expected_state=before, target_state=after,
                 reason="synthetic_native_fixture", client_action_id=f"smtp-seed-{after}")

        transition("check_in", "classroom_recall")
        post(f"/v1/sessions/{sid}/recalls/none", client_action_id="smtp-seed-no-recall")
        transition("classroom_recall", "homework_capture")
        post(f"/v1/sessions/{sid}/homework/none", client_action_id="smtp-seed-no-homework")
        post(f"/v1/sessions/{sid}/plan/generate", client_action_id="smtp-seed-plan")
        transition("plan_review", "study")
        # Untouched optional work is deferred by the production transition, never completed.
        transition("study", "reflection")
        post(f"/v1/sessions/{sid}/reflection/complete", learned_text="合成本机验证：今天没有开始学习任务。",
             difficult_text="暂时想不起课堂内容。", feeling="平静", next_action="下次先核对要做的任务。",
             skipped=False, client_action_id="smtp-seed-reflection")

        database = Database(database_path)
        repository = StudyRepository(database)
        report = repository.get_report_for_session(sid)
        snapshot = ParentReportService(database).create_from_daily(report)
        with database.connect() as connection:
            reflection_count = connection.execute("SELECT COUNT(*) FROM daily_reflection WHERE session_id=?", (sid,)).fetchone()[0]
            completed_tasks = connection.execute("""SELECT COUNT(*) FROM plan_item pi JOIN daily_plan dp USING(plan_id)
                WHERE dp.session_id=? AND pi.status='completed'""", (sid,)).fetchone()[0]
            job_status = connection.execute("SELECT status FROM report_job WHERE session_id=?", (sid,)).fetchone()[0]
        assert reflection_count == 1 and completed_tasks == 0 and job_status == "completed"
        assert repository.get_session(sid)["status"] == "completed"
        assert report["evidence"]["reflection_present"] is True
        assert snapshot["status"] == "draft"

    # Fresh service instances must read the durable records, not an in-memory response.
    assert StudyRepository(Database(database_path)).get_daily_report(report["report_id"])["report_id"] == report["report_id"]
    assert ParentReportService(Database(database_path)).get(snapshot["snapshot_id"])["status"] == "draft"
    return {"synthetic_only": True, "seed_via": "actual_local_http", "session_id": sid,
            "report_id": report["report_id"], "snapshot_id": snapshot["snapshot_id"],
            "reflection_count": reflection_count, "completed_learning_tasks": completed_tasks,
            "report_job_status": job_status, "snapshot_status": "draft", "external_delivery": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-path", type=Path, required=True)
    parser.add_argument("--receipt-path", type=Path, required=True)
    args = parser.parse_args()
    result = seed(args.database_path.resolve())
    with args.receipt_path.open("x", encoding="utf-8") as receipt:
        args.receipt_path.chmod(0o600)
        json.dump(result, receipt, ensure_ascii=False, indent=2)
        receipt.write("\n")
