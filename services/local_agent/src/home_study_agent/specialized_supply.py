from __future__ import annotations

from datetime import datetime, timezone
import asyncio
import hashlib
import json
import re
from typing import Any
from uuid import uuid4

from .database import Database
from .exam_blueprints import resolve_exam_blueprint
from .model_gateway import ModelGateway, ModelRequest
from .specialized_program import SpecializedProgramService
from .local_supply import LocalSupplyQueue, LocalSupplyError, ensure_job_columns, validate_item, validate_candidate_source, digest, item_snapshot, qualified_items, owns_model_attempt, _validate_source_snapshot
from .variant_supply import _cloud_supply_scope, CLOUD_SUPPLY_POLICY_VERSION


SUPPLY_PROMPT_VERSION = "specialized_diagnostic_closed_supply_v3"
SUPPLY_STAGES = ("parallel", "discrimination", "transfer")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize(value: str) -> str:
    return "".join(character.lower() for character in value if character.isalnum())


def _similarity(left: str, right: str) -> float:
    left_pairs = {_normalize(left)[index:index + 2] for index in range(max(0, len(_normalize(left)) - 1))}
    right_pairs = {_normalize(right)[index:index + 2] for index in range(max(0, len(_normalize(right)) - 1))}
    return len(left_pairs & right_pairs) / max(1, len(left_pairs | right_pairs))


def _closed_generation_seed(connection, candidates):
    """Return a seed whose full answer can be checked deterministically."""
    from .specialized_closed_contract import require_seed

    for seed in candidates:
        node = connection.execute(
            "SELECT * FROM knowledge_node WHERE knowledge_node_id=?",
            (seed["knowledge_node_id"],),
        ).fetchone()
        if node is None:
            continue
        try:
            require_seed(seed, node)
        except LocalSupplyError:
            continue
        return seed
    return None


class SpecializedSupplyError(ValueError):
    pass


def promote_candidate(connection, row):
    """One item publication primitive, called inside the caller's transaction."""
    from .pet_oral_source import candidate_source as oral_source, publish as publish_oral
    oral = oral_source(connection, row)
    if oral is not None:
        return publish_oral(connection, row, oral)
    from .open_response_source import candidate_source
    opened = candidate_source(connection,row)
    if opened is not None:
        from .curriculum_supply import validate_curriculum_candidate
        if not connection.in_transaction:
            raise SpecializedSupplyError('OPEN_RESPONSE_PUBLICATION_TRANSACTION_REQUIRED')
        validate_curriculum_candidate(connection,row)
        from .open_response_source import MATERIAL_SCHEMA, VISUAL_SCHEMA
        material = None
        if opened['schema'] in {MATERIAL_SCHEMA, VISUAL_SCHEMA}:
            from .open_response_materials import resolve_source_materials
            material = resolve_source_materials(connection, child_id=row['child_id'], source=opened,
                                                candidate_id=row['candidate_id'])
        diagnostic_item_id = f"specialized-{row['candidate_id']}"
        inserted = connection.execute('''INSERT INTO diagnostic_item(
            diagnostic_item_id,knowledge_node_id,prompt,answer_type,expected_answer_json,
            explanation,assessment_mode,difficulty,source_ref,status,stimulus_text)
            SELECT ?,di.knowledge_node_id,?,'open_response',?,?,'open_response_rubric',?,?,'active',?
            FROM diagnostic_item di JOIN knowledge_node kn USING(knowledge_node_id)
            WHERE di.diagnostic_item_id=? AND di.status='active' AND kn.status='active'
              AND kn.track='school' AND kn.grade_band=? AND kn.subject=? AND kn.knowledge_node_id=?''',
            (diagnostic_item_id,opened['prompt'],row['expected_answer_json'],row['explanation'],row['difficulty'],
             f"parent_approved_specialized:{row['candidate_id']}",material['stimulus_text'] if material else None,
             row['seed_item_id'],opened['grade'],
             opened['subject'],opened['knowledge_node_id']))
        if inserted.rowcount != 1:
            raise SpecializedSupplyError('OPEN_RESPONSE_SOURCE_NODE_UNAVAILABLE')
        return diagnostic_item_id
    from .pet_reviewed_source import candidate_listening_source
    listening = candidate_listening_source(connection, row)
    if listening is not None:
        from .curriculum_supply import validate_curriculum_candidate
        validate_curriculum_candidate(connection, row)
        diagnostic_item_id = f"specialized-{row['candidate_id']}"
        inserted = connection.execute("""INSERT INTO diagnostic_item(
            diagnostic_item_id,knowledge_node_id,prompt,answer_type,expected_answer_json,
            explanation,assessment_mode,difficulty,source_ref,status,stimulus_text)
            SELECT ?,di.knowledge_node_id,?,'listening_text',?,?,'objective',?,?,'active',?
            FROM diagnostic_item di JOIN knowledge_node kn USING(knowledge_node_id)
            WHERE di.diagnostic_item_id=? AND di.status='active' AND kn.status='active'
              AND ((kn.track='pet' AND kn.code LIKE 'PET.LISTENING.%') OR
                   (kn.track='school' AND kn.code='J3.ENGLISH.LISTENING'
                    AND kn.grade_band='初三' AND kn.subject='英语'))""",
            (diagnostic_item_id, listening["prompt"], row["expected_answer_json"], row["explanation"],
             row["difficulty"], f"parent_approved_specialized:{row['candidate_id']}",
             listening["stimulus_text"], row["seed_item_id"]))
        if inserted.rowcount != 1:
            raise SpecializedSupplyError("PET_LISTENING_SOURCE_NODE_UNAVAILABLE")
        return diagnostic_item_id
    diagnostic_item_id = f"specialized-{row['candidate_id']}"
    from .pet_writing_source import candidate_writing_source
    written = candidate_writing_source(connection, row)
    if written is not None:
        from .curriculum_supply import validate_curriculum_candidate
        validate_curriculum_candidate(connection, row)
        inserted = connection.execute('''INSERT INTO diagnostic_item(
            diagnostic_item_id,knowledge_node_id,prompt,answer_type,expected_answer_json,
            explanation,assessment_mode,difficulty,source_ref,status)
            SELECT ?,di.knowledge_node_id,?,'writing',?,?,'pet_reference_rubric',?,?,'active'
            FROM diagnostic_item di JOIN knowledge_node kn USING(knowledge_node_id)
            WHERE di.diagnostic_item_id=? AND di.status='active' AND kn.status='active'
            AND kn.track='pet' AND kn.code LIKE 'PET.WRITING.%' AND kn.knowledge_node_id=?''',
            (diagnostic_item_id,written['prompt'],row['expected_answer_json'],row['explanation'],row['difficulty'],
             f"parent_approved_specialized:{row['candidate_id']}",row['seed_item_id'],written['knowledge_node_id']))
        if inserted.rowcount != 1:
            raise SpecializedSupplyError('PET_WRITING_SOURCE_NODE_UNAVAILABLE')
        return diagnostic_item_id
    validate_candidate_source(connection, 'specialized_supply_candidate', row)
    inserted = connection.execute(
        """INSERT INTO diagnostic_item(
             diagnostic_item_id,knowledge_node_id,prompt,answer_type,expected_answer_json,
             explanation,assessment_mode,difficulty,source_ref,status
           ) SELECT ?,di.knowledge_node_id,?,'text',?,?, 'objective',?,?, 'active'
             FROM diagnostic_item di WHERE di.diagnostic_item_id=? AND di.status='active'""",
        (diagnostic_item_id, row["prompt"], row["expected_answer_json"], row["explanation"],
         row["difficulty"], f"parent_approved_specialized:{row['candidate_id']}", row["seed_item_id"]),
    )
    if inserted.rowcount != 1:
        raise SpecializedSupplyError("种子题已不可用，不能批准该候选题。")
    return diagnostic_item_id


class SpecializedDiagnosticSupplyService:
    """Builds reviewed diagnostic depth without transmitting a child's profile."""

    def __init__(self, database: Database, gateway: ModelGateway | None = None):
        self.database = database
        self.gateway = gateway

    def ensure_schema(self) -> None:
        with self.database.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS specialized_supply_job (
                  job_id TEXT PRIMARY KEY,
                  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                  track TEXT NOT NULL,
                  subject TEXT NOT NULL,
                  domain_id TEXT NOT NULL,
                  seed_item_id TEXT NOT NULL REFERENCES diagnostic_item(diagnostic_item_id),
                  stages_json TEXT NOT NULL,
                  status TEXT NOT NULL CHECK(status IN ('pending','processing','completed','failed','blocked')),
                  attempt_count INTEGER NOT NULL DEFAULT 0,
                  error_code TEXT,
                  provider TEXT,
                  model TEXT,
                  prompt_tokens INTEGER,
                  completion_tokens INTEGER,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  client_action_id TEXT NOT NULL UNIQUE
                );
                CREATE INDEX IF NOT EXISTS idx_specialized_supply_job_status
                  ON specialized_supply_job(status, created_at);
                CREATE TABLE IF NOT EXISTS specialized_supply_candidate (
                  candidate_id TEXT PRIMARY KEY,
                  job_id TEXT NOT NULL REFERENCES specialized_supply_job(job_id) ON DELETE CASCADE,
                  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                  track TEXT NOT NULL,
                  subject TEXT NOT NULL,
                  domain_id TEXT NOT NULL,
                  seed_item_id TEXT NOT NULL REFERENCES diagnostic_item(diagnostic_item_id),
                  stage TEXT NOT NULL,
                  prompt TEXT NOT NULL,
                  expected_answer_json TEXT NOT NULL,
                  explanation TEXT NOT NULL,
                  difficulty INTEGER NOT NULL,
                  content_fingerprint TEXT NOT NULL,
                  validation_json TEXT NOT NULL,
                  status TEXT NOT NULL CHECK(status IN ('needs_review','promoted','rejected')),
                  diagnostic_item_id TEXT REFERENCES diagnostic_item(diagnostic_item_id),
                  reviewed_at TEXT,
                  review_note TEXT,
                  review_action_id TEXT UNIQUE,
                  created_at TEXT NOT NULL,
                  UNIQUE(track, domain_id, content_fingerprint)
                );
                CREATE INDEX IF NOT EXISTS idx_specialized_supply_review
                  ON specialized_supply_candidate(child_id, status, created_at);
                """
            )
            ensure_job_columns(connection, "specialized_supply_job")
            connection.commit()

    def enqueue_local(self, seed_item_id: str, child_id: str, action_id: str, count: int = 3) -> dict:
        with self.database.connect() as connection:
            seed = validate_item(connection, child_id, seed_item_id)
            node = dict(connection.execute("SELECT * FROM knowledge_node WHERE knowledge_node_id=?", (seed["knowledge_node_id"],)).fetchone())
            if node["track"] == "school" and node["grade_band"] != "初三":
                raise LocalSupplyError("LOCAL_USE_DIAGNOSTIC_VARIANT_QUEUE")
            track = "junior_exam" if node["track"] == "school" else node["track"]
            domain = SpecializedProgramService(self.database)._mastery_domain(track, node)
            if not domain:
                raise LocalSupplyError("LOCAL_DOMAIN_UNAVAILABLE")
        return LocalSupplyQueue(self).enqueue(child_id, seed_item_id, action_id, track=track, domain_id=domain, count=count)

    def enqueue_local_gaps(self, child_id: str, track: str, action_id: str, limit: int = 10, *, should_stop=None) -> dict:
        jobs, skipped = [], []

        def stopped():
            return should_stop is not None and should_stop()

        def stopped_result():
            return {"jobs": jobs, "skipped": skipped, "local_only": True, "cloud_transmission_allowed": False, "stopped": True}

        if stopped():
            return stopped_result()
        gaps = self.gaps(child_id, track)
        if stopped():
            return stopped_result()
        with self.database.connect() as connection:
            seeds = qualified_items(connection, child_id)
        for domain in gaps["domains"]:
            if stopped():
                return stopped_result()
            if not domain["missing_count"] or len(jobs) >= max(0, min(int(limit), 50)):
                continue
            candidates = [row for row in seeds if SpecializedProgramService(self.database)._mastery_domain(track, row) == domain["domain_id"]]
            # The local generator does not qualify open-response rubrics. Such
            # a new seed must not hide a supported existing closed-answer seed.
            seed = next((row for row in candidates if row['answer_type'] != 'open_response'),
                        candidates[0] if candidates else None)
            if not seed:
                skipped.append({"domain_id": domain["domain_id"], "reason": "seed_missing"})
                continue
            try:
                jobs.append(self.enqueue_local(seed["diagnostic_item_id"], child_id, action_id + ":" + domain["domain_id"]))
            except LocalSupplyError as error:
                skipped.append({"domain_id": domain["domain_id"], "reason": str(error)})
        return {"jobs": jobs, "skipped": skipped, "local_only": True, "cloud_transmission_allowed": False}

    def process_local_pending(self, limit: int = 10, *, should_stop=None) -> dict:
        return LocalSupplyQueue(self).process_pending(limit, should_stop=should_stop)

    def retry_job(self, child_id: str, job_id: str, *, allow_model_retry: bool = False) -> dict:
        return LocalSupplyQueue(self).retry(child_id, job_id, allow_model_retry=allow_model_retry)

    def bind_due_reviews(self, child_id: str, limit: int = 6) -> dict:
        return LocalSupplyQueue(self).bind_due_reviews(child_id, limit)

    def qualify_local(self, candidate_id: str, child_id: str, action_id: str) -> dict:
        return LocalSupplyQueue(self).qualify_local(child_id, candidate_id, action_id)

    def template_readiness(self, child_id: str) -> dict:
        from .local_supply_templates import readiness
        with self.database.connect() as connection:
            rows = readiness(connection, child_id, qualified_items(connection, child_id))
        return {"child_id": child_id, "templates": rows, "local_only": True, "cloud_transmission_allowed": False}

    def recover(self) -> int:
        self.ensure_schema()
        with self.database.connect() as connection:
            count = connection.execute(
                """UPDATE specialized_supply_job
                   SET status=CASE WHEN generation_mode='model' AND attempt_count>0 THEN 'blocked' ELSE 'pending' END,
                       error_code=CASE WHEN generation_mode='model' AND attempt_count>0 THEN 'GENERATION_OUTCOME_UNKNOWN' ELSE 'APP_RESTART' END, updated_at=?
                   WHERE status='processing'""",
                (_now(),),
            ).rowcount
            connection.commit()
        return count

    def gaps(self, child_id: str, track: str, target_per_domain: int = 4) -> dict[str, Any]:
        self.ensure_schema()
        program = SpecializedProgramService(self.database)
        roadmap = program.roadmap(child_id, track)
        target = max(4, min(int(target_per_domain), 12))
        with self.database.connect() as connection:
            supply_rows = connection.execute(
                """SELECT * FROM specialized_supply_candidate
                   WHERE child_id=? AND track=? AND status IN ('needs_review','promoted')""",
                (child_id, track),
            ).fetchall()
            valid_rows = []
            for row in supply_rows:
                try:
                    validate_candidate_source(connection, "specialized_supply_candidate", row)
                    if row["status"] == "promoted":
                        validate_item(connection, child_id, row["diagnostic_item_id"])
                except (ValueError, KeyError, TypeError):
                    continue
                valid_rows.append(row)
            supply_rows = valid_rows
            active = qualified_items(connection, child_id)
        rows = []
        for domain in roadmap["domains"]:
            domain_supply = [row for row in supply_rows if row["domain_id"] == domain["domain_id"]]
            pending = [row for row in domain_supply if row["status"] == "needs_review"]
            pending_stages = {row["stage"] for row in pending}
            promoted_stages = {row["stage"] for row in domain_supply if row["status"] == "promoted"}
            covered_stages = pending_stages | promoted_stages
            current = sum(program._mastery_domain(track, item) == domain["domain_id"] for item in active)
            missing_stages = [stage for stage in SUPPLY_STAGES if stage not in covered_stages]
            needed = max(target - current - len(pending), len(missing_stages), 0)
            stage_index = 0
            while len(missing_stages) < needed:
                missing_stages.append(SUPPLY_STAGES[stage_index % len(SUPPLY_STAGES)])
                stage_index += 1
            rows.append({
                "domain_id": domain["domain_id"], "subject": domain["subject"],
                "label": domain["label"], "active_count": current,
                "pending_review_count": len(pending), "target_count": target,
                "missing_count": len(missing_stages), "missing_stages": missing_stages,
                "promoted_stages": sorted(promoted_stages),
                "ready": current >= target and len(promoted_stages) == len(SUPPLY_STAGES) and not pending,
                "seed_available": current > 0,
            })
        return {
            "child_id": child_id, "track": track, "target_per_domain": target,
            "domains": rows,
            "ready_domain_count": sum(1 for row in rows if row["ready"]),
            "total_domain_count": len(rows),
        }

    def enqueue_gaps(
        self, child_id: str, track: str, client_action_id: str, target_per_domain: int = 4,
    ) -> dict[str, Any]:
        gap = self.gaps(child_id, track, target_per_domain)
        program = SpecializedProgramService(self.database)
        created = []
        skipped = []
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            candidates = qualified_items(connection, child_id)
            for domain in gap["domains"]:
                if domain["missing_count"] <= 0:
                    continue
                seed = _closed_generation_seed(connection, [
                    row for row in candidates
                    if program._mastery_domain(track, row) == domain["domain_id"]
                ])
                if not seed:
                    skipped.append({"domain_id": domain["domain_id"],
                                    "reason": "SPECIALIZED_TYPED_CLOSED_SEED_REQUIRED"})
                    continue
                replay_action = f"{client_action_id}:{domain['domain_id']}"
                replay = connection.execute(
                    "SELECT job_id,child_id,track FROM specialized_supply_job WHERE client_action_id=?", (replay_action,),
                ).fetchone()
                if replay:
                    if replay["child_id"] != child_id or replay["track"] != track:
                        raise SpecializedSupplyError("专项供应动作归属冲突。")
                    skipped.append({"domain_id": domain["domain_id"], "reason": "action_replayed", "job_id": replay["job_id"]})
                    continue
                active = connection.execute(
                    """SELECT job_id FROM specialized_supply_job
                       WHERE child_id=? AND track=? AND domain_id=? AND status IN ('pending','processing')
                       ORDER BY created_at DESC LIMIT 1""",
                    (child_id, track, domain["domain_id"]),
                ).fetchone()
                if active:
                    skipped.append({"domain_id": domain["domain_id"], "reason": "already_queued"})
                    continue
                stages = list(domain["missing_stages"])
                now, job_id = _now(), str(uuid4())
                connection.execute(
                    """INSERT INTO specialized_supply_job(
                         job_id,child_id,track,subject,domain_id,seed_item_id,stages_json,status,
                         created_at,updated_at,client_action_id
                       ) VALUES(?,?,?,?,?,?,?,'pending',?,?,?)""",
                    (job_id, child_id, track, domain["subject"], domain["domain_id"],
                     seed["diagnostic_item_id"], json.dumps(stages), now, now,
                     replay_action),
                )
                created.append(job_id)
            connection.commit()
        return {"created_job_ids": created, "created_count": len(created), "skipped": skipped, "gaps": gap}

    def blueprint_gaps(
        self,
        child_id: str,
        track: str,
        subject: str,
        blueprint_id: str | None = None,
    ) -> dict[str, Any]:
        self.ensure_schema()
        try:
            blueprint = resolve_exam_blueprint(track, subject, blueprint_id)
        except KeyError as error:
            raise SpecializedSupplyError("没有找到与专项和学科匹配的考试蓝图。") from error
        program = SpecializedProgramService(self.database)
        roadmap = self.gaps(child_id, track)
        domains = {
            row["domain_id"]: {
                "domain_id": row["domain_id"],
                "subject": row["subject"],
                "label": row["label"],
                "active_count": int(row["active_count"]),
            }
            for row in roadmap["domains"]
            if not subject or row["subject"] == subject
        }
        pending_by_domain = {row["domain_id"]: row["pending_review_count"] for row in roadmap["domains"]}
        sections = []
        recommended_targets: dict[str, int] = {}
        for section in blueprint["sections"]:
            prefixes = tuple(section["domain_prefixes"])
            section_domains = [
                {**row, "pending_review_count": pending_by_domain.get(domain_id, 0)}
                for domain_id, row in domains.items() if domain_id.startswith(prefixes)
            ]
            active_count = sum(row["active_count"] for row in section_domains)
            pending_count = sum(row["pending_review_count"] for row in section_domains)
            target = section.get("item_target")
            if target is None:
                target = len(section_domains)
            projected = active_count + pending_count
            deficit = max(0, int(target) - active_count)
            pending_deficit = max(0, int(target) - projected)
            allocation = {
                row["domain_id"]: row["active_count"] + row["pending_review_count"]
                for row in section_domains
            }
            while allocation and sum(allocation.values()) < int(target):
                domain_id = min(allocation, key=lambda key: (allocation[key], key))
                allocation[domain_id] += 1
            for domain_id, target_count in allocation.items():
                recommended_targets[domain_id] = max(recommended_targets.get(domain_id, 0), target_count)
            sections.append({
                "section_id": section["section_id"],
                "title": section["title"],
                "item_target": int(target),
                "active_count": active_count,
                "pending_review_count": pending_count,
                "deficit": deficit,
                "pending_generation_count": pending_deficit,
                "ready": deficit == 0,
                "domains": section_domains,
            })
        return {
            "child_id": child_id,
            "track": track,
            "subject": subject,
            "blueprint": blueprint,
            "sections": sections,
            "ready": all(section["ready"] for section in sections),
            "active_item_count": sum(section["active_count"] for section in sections),
            "required_item_count": sum(section["item_target"] for section in sections),
            "pending_review_count": sum(section["pending_review_count"] for section in sections),
            "recommended_domain_targets": recommended_targets,
        }

    def enqueue_blueprint_gaps(
        self,
        child_id: str,
        track: str,
        subject: str,
        client_action_id: str,
        blueprint_id: str | None = None,
    ) -> dict[str, Any]:
        readiness = self.blueprint_gaps(child_id, track, subject, blueprint_id)
        created: list[str] = []
        skipped: list[dict[str, str]] = []
        targets = readiness["recommended_domain_targets"]
        program = SpecializedProgramService(self.database)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            candidates = qualified_items(connection, child_id)
            supply_rows = connection.execute(
                """SELECT * FROM specialized_supply_candidate
                   WHERE child_id=? AND track=? AND status IN ('needs_review','promoted')""",
                (child_id, track),
            ).fetchall()
            current_supply = []
            for row in supply_rows:
                try:
                    validate_candidate_source(connection, 'specialized_supply_candidate', row)
                    if row['status'] == 'promoted':
                        validate_item(connection, child_id, row['diagnostic_item_id'])
                except (LocalSupplyError, ValueError, KeyError, TypeError):
                    continue
                current_supply.append(row)
            supply_rows = current_supply
            for domain_id, target in sorted(targets.items()):
                domain_candidates = [
                    row for row in supply_rows if row["domain_id"] == domain_id
                ]
                pending = [row for row in domain_candidates if row["status"] == "needs_review"]
                seed_items = [
                    row for row in candidates if program._mastery_domain(track, row) == domain_id
                ]
                active_count = len(seed_items)
                needed = max(0, int(target) - active_count - len(pending))
                if needed == 0:
                    continue
                covered_stages = {row["stage"] for row in domain_candidates}
                stages = [stage for stage in SUPPLY_STAGES if stage not in covered_stages]
                needed = max(needed, len(stages))
                stage_index = 0
                while len(stages) < needed:
                    stages.append(SUPPLY_STAGES[stage_index % len(SUPPLY_STAGES)])
                    stage_index += 1
                if not stages:
                    continue
                seed = _closed_generation_seed(connection, seed_items)
                if not seed:
                    skipped.append({"domain_id": domain_id,
                                    "reason": "SPECIALIZED_TYPED_CLOSED_SEED_REQUIRED"})
                    continue
                replay_action = f"{client_action_id}:{domain_id}"
                replay = connection.execute(
                    "SELECT job_id FROM specialized_supply_job WHERE client_action_id=?",
                    (replay_action,),
                ).fetchone()
                if replay:
                    skipped.append({"domain_id": domain_id, "reason": "action_replayed"})
                    continue
                active = connection.execute(
                    """SELECT job_id FROM specialized_supply_job
                       WHERE child_id=? AND track=? AND domain_id=?
                         AND status IN ('pending','processing') LIMIT 1""",
                    (child_id, track, domain_id),
                ).fetchone()
                if active:
                    skipped.append({"domain_id": domain_id, "reason": "already_queued"})
                    continue
                now, job_id = _now(), str(uuid4())
                connection.execute(
                    """INSERT INTO specialized_supply_job(
                         job_id,child_id,track,subject,domain_id,seed_item_id,stages_json,status,
                         created_at,updated_at,client_action_id
                       ) VALUES(?,?,?,?,?,?,?,'pending',?,?,?)""",
                    (
                        job_id, child_id, track, readiness["blueprint"]["subject"], domain_id,
                        seed["diagnostic_item_id"], json.dumps(stages), now, now,
                        replay_action,
                    ),
                )
                created.append(job_id)
            connection.commit()
        return {
            "created_job_ids": created,
            "created_count": len(created),
            "skipped": skipped,
            "readiness": readiness,
        }

    async def process_next(self) -> dict[str, Any] | None:
        self.ensure_schema()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = connection.execute(
                """SELECT * FROM specialized_supply_job
                   WHERE status='pending' AND generation_mode='model' AND attempt_count < 3 ORDER BY created_at LIMIT 1"""
            ).fetchone()
            if not job:
                connection.rollback()
                return None
            try:
                context = self._context(connection, job["job_id"])
                seed = validate_item(connection, job["child_id"], job["seed_item_id"])
                if job['source_snapshot_json'] is not None:
                    try:
                        snapshot = json.loads(job['source_snapshot_json'])
                    except (TypeError, ValueError) as error:
                        raise LocalSupplyError('SUPPLY_SOURCE_REVIEW_REQUIRED') from error
                    _validate_source_snapshot(snapshot, seed)
                else:
                    connection.execute("UPDATE specialized_supply_job SET source_snapshot_json=? WHERE job_id=?", (json.dumps(item_snapshot(seed)), job["job_id"]))
                scope = self._dispatch_scope(connection, job['job_id'])
            except LocalSupplyError as error:
                connection.execute("UPDATE specialized_supply_job SET status='blocked',error_code=?,updated_at=? WHERE job_id=?", (str(error), _now(), job["job_id"]))
                connection.commit()
                return self._job(connection, job["job_id"])
            if not self.gateway or not self.gateway.configured:
                inserted = connection.execute(
                    "UPDATE specialized_supply_job SET status='blocked',error_code='MODEL_NOT_CONFIGURED',updated_at=? WHERE job_id=?",
                    (_now(), job["job_id"]),
                )
                connection.commit()
                return self._job(connection, job["job_id"])
            connection.execute(
                """UPDATE specialized_supply_job SET status='processing',attempt_count=attempt_count+1,
                   error_code=NULL,updated_at=? WHERE job_id=?""",
                (_now(), job["job_id"]),
            )
            connection.commit()
        def authorize_dispatch():
            with self.database.connect() as connection:
                connection.execute('BEGIN')
                self._validate_dispatch(connection, job, scope)

        request = ModelRequest(
            request_id=f"specialized-supply:{job['job_id']}:{int(job['attempt_count']) + 1}",
            child_id=job["child_id"], session_id="local-specialized-supply",
            request_type="specialized_diagnostic_generation", risk_level="medium",
            context_manifest={"raw_media": False, "child_identity": False, "learning_history": False,
                              "domain_id": context["domain_id"], "stages": context["stages"],
                              "dispatch_scope_hash": scope},
            provider_preference="configured_primary", policy_version=CLOUD_SUPPLY_POLICY_VERSION,
            prompt_version=SUPPLY_PROMPT_VERSION, knowledge_version=context["knowledge_version"],
            output_type="knowledge_structuring", evidence_refs=(context["seed_item_id"],),
            pre_dispatch=authorize_dispatch,
            messages=(
                {"role": "system", "content": self._system_prompt()},
                {"role": "user", "content": json.dumps(context["cloud_context"], ensure_ascii=False)},
            ),
        )
        try:
            authorize_dispatch()
            result = await self.gateway.complete(request)
            generated = self._validate(context, self._parse(result.content))
            if len(generated) < len(context["stages"]):
                raise SpecializedSupplyError("模型未返回足量且通过校验的专项诊断题。")
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if not owns_model_attempt(connection, "specialized_supply_job", job):
                    return None
                self._validate_dispatch(connection, job, scope)
                for candidate in generated:
                    candidate['validation']['cloud_dispatch'] = {
                        'version': CLOUD_SUPPLY_POLICY_VERSION, 'scope_hash': scope,
                        'purpose': 'specialized_diagnostic_generation', 'local_only': True}
                    self._store(connection, job, context, candidate)
                if connection.execute("SELECT count(*) FROM specialized_supply_candidate WHERE job_id=?", (job["job_id"],)).fetchone()[0] < len(context["stages"]):
                    raise SpecializedSupplyError("重复候选没有补齐供应缺口。")
                connection.execute(
                    """UPDATE specialized_supply_job SET status='completed',provider=?,model=?,
                       prompt_tokens=?,completion_tokens=?,updated_at=? WHERE job_id=?""",
                    (result.provider, result.model, result.prompt_tokens, result.completion_tokens, _now(), job["job_id"]),
                )
                connection.commit()
                return self._job(connection, job["job_id"])
        except Exception as error:
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if not owns_model_attempt(connection, "specialized_supply_job", job):
                    return None
                attempts = connection.execute(
                    "SELECT attempt_count FROM specialized_supply_job WHERE job_id=?", (job["job_id"],)
                ).fetchone()[0]
                connection.execute(
                    "UPDATE specialized_supply_job SET status=?,error_code=?,updated_at=? WHERE job_id=?",
                    ('blocked' if isinstance(error, LocalSupplyError) else "failed" if attempts >= 3 else "pending",
                     str(error) if isinstance(error, LocalSupplyError) else type(error).__name__[:80], _now(), job["job_id"]),
                )
                connection.commit()
            raise

    def _dispatch_scope(self, connection, job_id):
        context = self._context(connection, job_id)
        seed = validate_item(connection, context['child_id'], context['seed_item_id'])
        try:
            snapshot = json.loads(context['source_snapshot_json'])
        except (TypeError, ValueError) as error:
            raise LocalSupplyError('SUPPLY_SOURCE_REVIEW_REQUIRED') from error
        _validate_source_snapshot(snapshot, seed)
        return _cloud_supply_scope(connection, seed=seed, context=context,
            purpose='specialized_diagnostic_generation', prompt_version=SUPPLY_PROMPT_VERSION,
            system_prompt=self._system_prompt())

    def _validate_dispatch(self, connection, job, expected_scope):
        if not owns_model_attempt(connection, 'specialized_supply_job', job):
            raise LocalSupplyError('SUPPLY_MODEL_ATTEMPT_CHANGED')
        if self._dispatch_scope(connection, job['job_id']) != expected_scope:
            raise LocalSupplyError('SUPPLY_CLOUD_DISPATCH_CHANGED')

    async def process_batch(self, limit: int = 3) -> list[dict[str, Any]]:
        """Drain several distinct jobs without making app startup wait for them."""
        workers = max(1, min(int(limit), 6))
        outcomes = await asyncio.gather(
            *(self.process_next() for _ in range(workers)), return_exceptions=True,
        )
        return [value for value in outcomes if isinstance(value, dict)]

    def stage_review_context(self, child_id: str, candidate_id: str) -> dict[str, Any]:
        from .stage_source_review import stage_review_context
        with self.database.connect() as connection:
            connection.execute('BEGIN')
            context = stage_review_context(connection, child_id, candidate_id)
            row = connection.execute('SELECT expected_answer_json FROM specialized_supply_candidate WHERE candidate_id=? AND child_id=?',
                                     (candidate_id, child_id)).fetchone()
            return {**context, 'candidate_expected_answer': json.loads(row['expected_answer_json'])}

    def review(self, candidate_id: str, decision: str, note: str, action_id: str, *, stage_review=None,
               expected_binding_version: str | None = None) -> dict[str, Any]:
        self.ensure_schema()
        if decision not in {"approve", "reject"} or not action_id.strip():
            raise SpecializedSupplyError("审核决定无效。")
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM specialized_supply_candidate WHERE candidate_id=?", (candidate_id,)
            ).fetchone()
            if not row:
                raise KeyError(candidate_id)
            job = connection.execute("SELECT generation_mode FROM specialized_supply_job WHERE job_id=?",
                                     (row["job_id"],)).fetchone()
            if job and job["generation_mode"] == "curriculum_review":
                raise SpecializedSupplyError("CURRICULUM_USE_REVIEW_AND_PUBLISH")
            if row["review_action_id"]:
                if (row["review_action_id"] != action_id or row["review_note"] != note[:500]
                        or (row["status"] == "promoted") != (decision == "approve")
                        or json.loads(row['validation_json']).get('stage_review') != stage_review
                        or json.loads(row['validation_json']).get('review_binding_version') != expected_binding_version):
                    raise SpecializedSupplyError("这道专项候选题已经审核。")
                if decision == 'approve':
                    validate_candidate_source(connection, 'specialized_supply_candidate', row)
                if stage_review is not None:
                    from .stage_source_review import validate_stage_review
                    validate_stage_review(connection, row['child_id'], candidate_id, stage_review)
                return self._candidate(row)
            if decision not in {"approve", "reject"}:
                raise SpecializedSupplyError("审核决定无效。")
            diagnostic_item_id, status = None, "rejected"
            if expected_binding_version is not None:
                from .stage_source_review import stage_review_context
                current = stage_review_context(connection, row['child_id'], candidate_id)
                if current['binding_version'] != expected_binding_version:
                    raise SpecializedSupplyError('STAGE_REVIEW_SOURCE_CONFLICT')
            if decision == "approve":
                validate_candidate_source(connection, "specialized_supply_candidate", row)
                if job and job['generation_mode'] == 'model' and not note.strip():
                    raise SpecializedSupplyError('SPECIALIZED_TYPED_CLOSED_REVIEW_NOTE_REQUIRED')
                validation = json.loads(row["validation_json"])
                if not validation.get("passed"):
                    raise SpecializedSupplyError("本地校验未通过，不能批准。")
                if expected_binding_version is not None:
                    validation['review_binding_version'] = expected_binding_version
                if stage_review is not None:
                    from .stage_source_review import validate_stage_review
                    validate_stage_review(connection, row['child_id'], candidate_id, stage_review)
                    if not note.strip():
                        raise SpecializedSupplyError('阶段审核须保留原题核对说明。')
                    validation['stage_review'] = stage_review
                if stage_review is not None or expected_binding_version is not None:
                    connection.execute('UPDATE specialized_supply_candidate SET validation_json=? WHERE candidate_id=?',
                                       (json.dumps(validation, ensure_ascii=False), candidate_id))
                diagnostic_item_id, status = promote_candidate(connection, row), "promoted"
            elif stage_review is not None:
                raise SpecializedSupplyError('拒绝候选不能登记通过的迁移阶段。')
            elif expected_binding_version is not None:
                validation = json.loads(row['validation_json'])
                validation['review_binding_version'] = expected_binding_version
                connection.execute('UPDATE specialized_supply_candidate SET validation_json=? WHERE candidate_id=?',
                                   (json.dumps(validation, ensure_ascii=False), candidate_id))
            now = _now()
            connection.execute(
                """UPDATE specialized_supply_candidate SET status=?,diagnostic_item_id=?,reviewed_at=?,
                   review_note=?,review_action_id=? WHERE candidate_id=?""",
                (status, diagnostic_item_id, now, note[:500], action_id, candidate_id),
            )
            connection.commit()
            return self._candidate(connection.execute(
                "SELECT * FROM specialized_supply_candidate WHERE candidate_id=?", (candidate_id,)
            ).fetchone())

    def list(self, child_id: str, status: str | None = None) -> dict[str, Any]:
        self.ensure_schema()
        with self.database.connect() as connection:
            sql, params = '''SELECT sc.*,sj.generation_mode AS review_workflow
                FROM specialized_supply_candidate sc JOIN specialized_supply_job sj USING(job_id)
                WHERE sc.child_id=?''', [child_id]
            if status:
                sql, params = sql + " AND sc.status=?", [child_id, status]
            items = [self._candidate(row) for row in connection.execute(sql + " ORDER BY sc.created_at", params)]
            jobs = [self._job(connection, row[0]) for row in connection.execute(
                "SELECT job_id FROM specialized_supply_job WHERE child_id=? ORDER BY created_at DESC", (child_id,)
            )]
        return {"items": items, "jobs": jobs}

    def _pending_count(self, child_id: str, track: str, domain_id: str) -> int:
        with self.database.connect() as connection:
            return int(connection.execute(
                """SELECT COUNT(*) FROM specialized_supply_candidate
                   WHERE child_id=? AND track=? AND domain_id=? AND status='needs_review'""",
                (child_id, track, domain_id),
            ).fetchone()[0])

    def _context(self, connection, job_id: str) -> dict[str, Any]:
        row = connection.execute(
            """SELECT ssj.*,di.prompt,di.expected_answer_json,di.explanation,di.difficulty,
                      di.answer_type,di.assessment_mode,di.knowledge_node_id,
                      kn.title AS knowledge_title,kn.description,kn.grade_band,kn.knowledge_version
               FROM specialized_supply_job ssj
               JOIN diagnostic_item di ON di.diagnostic_item_id=ssj.seed_item_id
               JOIN knowledge_node kn ON kn.knowledge_node_id=di.knowledge_node_id
               WHERE ssj.job_id=?""", (job_id,),
        ).fetchone()
        if not row:
            raise LocalSupplyError("SUPPLY_SOURCE_UNAVAILABLE")
        seed = validate_item(connection, row["child_id"], row["seed_item_id"])
        from .specialized_closed_contract import require_seed
        node = connection.execute('SELECT * FROM knowledge_node WHERE knowledge_node_id=?',
                                  (seed['knowledge_node_id'],)).fetchone()
        if row['generation_mode'] == 'model':
            require_seed(seed, node)
        stages = json.loads(row["stages_json"])
        value = dict(row)
        value['seed'] = seed
        value["stages"] = stages
        value["cloud_context"] = {
            "track": row["track"], "subject": row["subject"], "domain_id": row["domain_id"],
            "grade_band": row["grade_band"], "knowledge_title": row["knowledge_title"],
            "knowledge_description": row["description"], "seed_prompt": row["prompt"],
            "seed_expected_answer": json.loads(row["expected_answer_json"]),
            "seed_explanation": row["explanation"], "required_stages": stages,
            "scoring_contract_schemas": ["specialized_closed_exact_v1", "specialized_closed_numeric_v1"],
            "answer_type": "text", "assessment_mode": "objective",
        }
        return value

    @staticmethod
    def _system_prompt() -> str:
        return (
            "你是中小学专项能力诊断题设计器，只输出JSON对象{\"candidates\":[...]}. "
            "每项必须含stage,prompt,scoring_contract,explanation,difficulty；stage严格来自required_stages。"
            'scoring_contract严格为{"schema":"specialized_closed_exact_v1","answer_type":"text",'
            '"assessment_mode":"objective","expected_answer":{"local_rule":"local_exact_v1",'
            '"exact_answers":["完整参考答案"]}}。只生成有有限、完整且可人工核对答案的闭题；'
            '纯数值等价题必须改用schema="specialized_closed_numeric_v1"，另含numeric_kind="integer"或"rational"；'
            '参考答案用阿拉伯整数、分数或小数。题目支持口述答案，不要求点击或书写。'
            '数值合同不适用于单位、百分数、指定分数/小数表示、多个值或表达式；这些要求必须用文字合同另行明确审核。'
            "不得用关键词、部分要点、作文、口语、听力或开放论证冒充精确答案。不得自称已审核、已校准或已掌握。"
            "人工须核对题意、完整答案与学段适配后才可发布；模型阶段标签不授权迁移资格。parallel测同能力但换材料；"
            "discrimination诊断相邻概念混淆；transfer在新情境中测同能力。不得照抄种子题、泄漏答案、"
            "引入超纲术语、虚构标准或包含个人信息。"
        )

    @staticmethod
    def _parse(content: str) -> dict[str, Any]:
        value = content.strip()
        if value.startswith("```"):
            value = value.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        payload = json.loads(value)
        if not isinstance(payload, dict) or not isinstance(payload.get("candidates"), list):
            raise SpecializedSupplyError("专项题响应格式无效。")
        return payload

    def _validate(self, context: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
        required, accepted, seen = list(context["stages"]), [], [context["prompt"]]
        for item in payload["candidates"][: max(3, len(required) * 2)]:
            if not isinstance(item, dict) or not required:
                continue
            stage, prompt = str(item.get("stage") or ""), str(item.get("prompt") or "").strip()
            explanation = str(item.get("explanation") or "").strip()
            from .specialized_closed_contract import parse, VERSION, QUALIFICATION, fingerprint, exact_rubric
            try:
                contract = parse(context, prompt, item.get('scoring_contract'))
            except (LocalSupplyError, ValueError, TypeError, KeyError):
                continue
            errors = []
            if stage not in required: errors.append("stage")
            if not 8 <= len(prompt) <= 1200: errors.append("prompt_length")
            if not 4 <= len(explanation) <= 1200: errors.append("explanation_length")
            if type(item.get('difficulty')) is not int or not 1 <= item['difficulty'] <= 5: errors.append('difficulty')
            if any(_similarity(prompt, prior) >= .72 for prior in seen): errors.append("near_duplicate")
            if re.search(r"\b1[3-9]\d{9}\b|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", prompt): errors.append("personal_data")
            if errors:
                continue
            candidate = {
                "stage": stage, "prompt": prompt,
                "expected_answer": exact_rubric(contract),
                "explanation": explanation, "difficulty": item['difficulty'],
                "validation": {"passed": True, "errors": [], "ruleset": VERSION,
                    'qualification': QUALIFICATION, 'closed_contract': contract,
                    'pedagogical_review_required': True},
            }
            candidate['validation']['content_sha256'] = fingerprint(candidate, context['seed'])
            accepted.append(candidate)
            seen.append(prompt)
            required.remove(stage)
        return accepted

    def _store(self, connection, job, context, candidate) -> None:
        fingerprint = digest([job["child_id"], candidate["prompt"].strip()])
        connection.execute(
            """INSERT OR IGNORE INTO specialized_supply_candidate(
                 candidate_id,job_id,child_id,track,subject,domain_id,seed_item_id,stage,prompt,
                 expected_answer_json,explanation,difficulty,content_fingerprint,validation_json,status,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'needs_review',?)""",
            (str(uuid4()), job["job_id"], job["child_id"], job["track"], job["subject"], job["domain_id"],
             job["seed_item_id"], candidate["stage"], candidate["prompt"],
             json.dumps(candidate["expected_answer"], ensure_ascii=False), candidate["explanation"],
             candidate["difficulty"], fingerprint, json.dumps(candidate["validation"]), _now()),
        )

    def _job(self, connection, job_id: str) -> dict[str, Any]:
        value = dict(connection.execute("SELECT * FROM specialized_supply_job WHERE job_id=?", (job_id,)).fetchone())
        value["stages"] = json.loads(value.pop("stages_json"))
        value["candidate_count"] = connection.execute(
            "SELECT COUNT(*) FROM specialized_supply_candidate WHERE job_id=?", (job_id,)
        ).fetchone()[0]
        return value

    @staticmethod
    def _candidate(row) -> dict[str, Any]:
        value = dict(row)
        value["expected_answer"] = json.loads(value.pop("expected_answer_json"))
        value["validation"] = json.loads(value.pop("validation_json"))
        return value
