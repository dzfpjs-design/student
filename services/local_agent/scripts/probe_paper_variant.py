"""One explicit live model call using invented paper content and a disposable DB.

No family data, images or credentials are written to the report. Approval/scoring
is deliberately not automated by this probe: a model draft is not a verified answer.
"""
import argparse
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import tempfile
import traceback

from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.learning_state import LearningStateService
from home_study_agent.mistake_book import MistakeBookService
from home_study_agent.mistake_capture import MistakeCaptureService
from home_study_agent.model_gateway import ModelGateway
from home_study_agent.repository import StudyRepository, utc_now
from home_study_agent.variant_supply import VariantSupplyService


async def probe(args):
    settings = Settings.from_environment()
    if args.keychain_service:
        secret = subprocess.run(["security", "find-generic-password", "-s", args.keychain_service, "-w"],
                                capture_output=True, text=True, check=False)
        if secret.returncode or not secret.stdout.strip():
            print(json.dumps({"status": "blocked", "reason": "credential_unavailable"}))
            return 2
        settings = replace(settings, model_api_key=secret.stdout.strip(), model_provider="deepseek",
                           model_base_url="https://api.deepseek.com", model_name="deepseek-v4-pro")
    if not settings.model_api_key or settings.model_base_url.rstrip("/") != "https://api.deepseek.com":
        print(json.dumps({"status": "blocked", "reason": "approved_model_not_configured"}))
        return 2
    with tempfile.TemporaryDirectory(prefix="hsc-paper-variant-probe-") as directory:
        root = Path(directory)
        settings = replace(settings, data_dir=root, database_path=root / "probe.db", model_timeout_seconds=180,
                           fallback_model_api_key=None, fallback_model_provider=None)
        database = Database(settings.database_path)
        database.initialize()
        repo = StudyRepository(database)
        repo.seed_family()
        LearningStateService(database).seed_catalog()
        sid = repo.start_session("child-primary-4", "probe-session")["session_id"]
        with database.connect() as connection:
            connection.execute("""INSERT INTO capture_artifact(artifact_id,child_id,session_id,artifact_type,
                extracted_text,confidence,details_json,status,created_at,client_action_id)
                VALUES('synthetic-paper','child-primary-4',?,'document_ocr',?,.95,'{}','completed',?,'probe-paper')""",
                (sid, "计算 (37+13)×4，只回答一个整数。", utc_now()))
            connection.commit()
        book = MistakeBookService(database)
        capture = MistakeCaptureService(database, book)
        candidate = capture.prepare("synthetic-paper", "child-primary-4", "probe-prepare")["items"][0]
        mistake = capture.capture(candidate["candidate_id"], "child-primary-4", "node-p4-math-arithmetic",
            "数学", "四则运算", "170", "probe-capture")["mistake"]
        mid = mistake["mistake_id"]
        book.confirm(mid, "child-primary-4", "confirm", "probe-confirm")
        book.diagnose(mid, "child-primary-4", "concept_gap", "合成探针：核对运算顺序", .6, "probe-diagnose")
        gateway = ModelGateway(settings)
        service = VariantSupplyService(database, gateway)
        job = service.enqueue(mid, "child-primary-4", "probe-generate")
        try:
            await service.process_next()
            current = service.list("child-primary-4")
            result = current["jobs"][0]
            print(json.dumps({"status": result["status"], "provider": result["provider"], "model": result["model"],
                "candidate_count": len(current["items"]),
                "exact_candidate_count": sum(item["expected_answer"].get("local_rule") == "local_exact_v1" for item in current["items"]),
                "prompt_tokens": result.get("prompt_tokens"), "completion_tokens": result.get("completion_tokens"),
                "all_require_review": all(item["status"] == "needs_review" for item in current["items"]),
                "mastery_updated": False, "source": "invented_paper_text_not_real_camera",
                "answer_quality_verified": False}))
            return 0 if result["status"] == "completed" else 1
        except Exception as error:
            with database.connect() as connection:
                state = connection.execute("SELECT status,error_code FROM variant_generation_job WHERE job_id=?", (job["job_id"],)).fetchone()
            print(json.dumps({"status": "failed", "failure_type": type(error).__name__,
                              "job_status": state["status"], "reason": state["error_code"],
                              "failure_locations": [f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
                                                    for frame in traceback.extract_tb(error.__traceback__)]}))
            return 1
        finally:
            await gateway.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--keychain-service")
    raise SystemExit(asyncio.run(probe(parser.parse_args())))
