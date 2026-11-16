"""Real configured model on a synthetic study page in a disposable family DB."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import tempfile

from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.repository import StudyRepository, utc_now
from home_study_agent.document_monitoring import DocumentMonitoringService
from home_study_agent.document_learning import DocumentLearningService
from home_study_agent.companion_memory import CompanionMemory
from home_study_agent.model_gateway import ModelGateway
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService


async def main():
    with tempfile.TemporaryDirectory(prefix="hsc-document-learning-probe-") as directory:
        path = Path(directory)
        settings = replace(Settings.from_environment(), data_dir=path, database_path=path / "probe.db")
        database = Database(settings.database_path)
        database.initialize()
        PrivacyLifecycleService(database, path).initialize()
        repository = StudyRepository(database)
        repository.seed_family()
        sid = repository.start_session("child-primary-4", "synthetic-learning-session")["session_id"]
        with database.connect() as connection:
            connection.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
            connection.execute("UPDATE privacy_preference SET cloud_model_text_allowed=1")
            connection.execute("""INSERT INTO capture_artifact(artifact_id,child_id,session_id,artifact_type,
                local_path,extracted_text,confidence,details_json,status,created_at,client_action_id)
                VALUES('synthetic-page','child-primary-4',?,'document_ocr',NULL,?,.93,'{}','completed',?,'synthetic-ocr')""",
                (sid, "一个长方形长12厘米，宽8厘米，求周长。\n解题记录：12+8=20厘米。\n需要核对周长与相邻两边长度和的关系。", utc_now()))
            connection.commit()
        monitor = DocumentMonitoringService(database)
        monitor.start(sid, "synthetic-monitor")
        revision = monitor.record_revision(sid, "synthetic-page", "synthetic-signature", "synthetic-revision")
        gateway = ModelGateway(settings)
        try:
            service = DocumentLearningService(database, gateway, settings=settings, repository=repository, timeout_seconds=45)
            result = await service.analyze_revision(sid, revision["revision_id"])
            memory = CompanionMemory(database)
            memory.observe_document_revision(revision["revision_id"])
            with database.connect() as connection:
                call = connection.execute("SELECT model,latency_ms,prompt_tokens,completion_tokens FROM model_call ORDER BY created_at DESC LIMIT 1").fetchone()
            print(json.dumps({"status": result["status"], "source": result["source"], "reason": result.get("reason"),
                "observations": len(result["observations"]), "hypotheses": len(result["hypotheses"]),
                "specific_focus_count": sum(bool(item.get("learning_focus")) for item in result["observations"]),
                "memory_count": len(memory.recall("child-primary-4", "高拍仪")["memories"]),
                "model": call["model"] if call else None, "latency_ms": call["latency_ms"] if call else None,
                "mastery_updated": result.get("mastery_update", False)}))
        finally:
            await gateway.close()


if __name__ == "__main__":
    asyncio.run(main())
