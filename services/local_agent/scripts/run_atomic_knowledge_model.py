"""Prepare or run durable evidence-bound atomic-knowledge extraction batches."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.knowledge_graph.atomic_model_pipeline import AtomicKnowledgeExtractionService
from home_study_agent.model_gateway import ModelGateway


def _items(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-db", type=Path, required=True)
    parser.add_argument("--idempotency-key", required=True)
    parser.add_argument("--grades", default="四年级,初三")
    parser.add_argument("--subjects", default="语文,数学,英语,物理,化学")
    parser.add_argument("--max-resources", type=int)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--stop-after", type=int)
    parser.add_argument("--concurrency", type=int, default=1)
    arguments = parser.parse_args()

    database = Database(arguments.app_db)
    database.initialize()
    gateway = ModelGateway(Settings.from_environment())
    service = AtomicKnowledgeExtractionService(database, gateway)
    try:
        job = service.create_job(
            idempotency_key=arguments.idempotency_key,
            grades=_items(arguments.grades), subjects=_items(arguments.subjects),
            max_resources=arguments.max_resources,
        )
        if not arguments.prepare_only:
            if not gateway.configured:
                raise SystemExit("HSC_MODEL_API_KEY is not configured; prepared job is durable and has not called a model")
            job = await service.run(
                job["job_id"], stop_after=arguments.stop_after, concurrency=arguments.concurrency,
            )
        print(json.dumps({
            "job_id": job["job_id"], "status": job["status"], "total_batches": job["total_batches"],
            "completed_batches": job["completed_batches"], "failed_batches": job["failed_batches"],
            "draft_count": job["draft_count"], "prompt_version": job["prompt_version"],
        }, ensure_ascii=False))
    finally:
        await gateway.close()


if __name__ == "__main__":
    asyncio.run(main())
