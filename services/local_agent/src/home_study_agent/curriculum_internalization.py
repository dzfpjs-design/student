from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import nullcontext
from typing import Any
from uuid import uuid4

from .database import Database
from .model_gateway import ModelGateway, ModelNotConfiguredError, ModelRequest, ModelResponseError
from .repository import UnknownChildError, utc_now


PROMPT_VERSION = "curriculum_internalization_v1"
AUTHORITY_VERSION = "curriculum_internalization_authority_v1"
CAPABILITY_TYPES = {
    "concept", "procedure", "reasoning", "representation", "language_function",
    "experiment", "reading_strategy", "writing_skill", "physical_skill", "value_judgement",
}
SEMESTERS = {"上册", "下册", "全册", "未知学期"}
MAX_TOPICS_PER_BATCH = 8


class InvalidInternalizationRequestError(Exception):
    pass


class InvalidInternalizationOutputError(Exception):
    pass


class InternalizationAuthorityError(Exception):
    def __init__(self, code="INTERNALIZATION_AUTHORITY_CHANGED_NEW_JOB_REQUIRED"):
        self.code = code
        super().__init__(code)


class UnknownInternalizationJobError(Exception):
    pass


class UnknownCurriculumCapabilityError(Exception):
    pass


class CurriculumInternalizationService:
    def __init__(self, database: Database, gateway: ModelGateway):
        self.database = database
        self.gateway = gateway

    def recover_interrupted(self) -> int:
        now = utc_now()
        with self.database.connect() as connection:
            jobs = connection.execute(
                "UPDATE curriculum_internalization_job SET status = 'interrupted', error_code = 'APP_RESTARTED', completed_at = ? WHERE status = 'processing'",
                (now,),
            ).rowcount
            connection.execute(
                "UPDATE curriculum_internalization_batch SET status = 'interrupted', error_code = 'APP_RESTARTED', completed_at = ? WHERE status = 'processing'",
                (now,),
            )
            connection.commit()
        return jobs

    def scopes(self, corpus_id: str, child_id: str) -> list[dict]:
        with self.database.connect() as connection:
            child = connection.execute(
                "SELECT grade FROM child_profile WHERE child_id = ?", (child_id,)
            ).fetchone()
            if not child:
                raise UnknownChildError(child_id)
            if not connection.execute(
                "SELECT 1 FROM curriculum_corpus WHERE corpus_id = ? AND status = 'ready'", (corpus_id,)
            ).fetchone():
                raise InvalidInternalizationRequestError("课程语料库尚未就绪。")
            rows = connection.execute(
                """
                SELECT grade_title, subject_title, COALESCE(version_title, '') AS version_title,
                       semester, unit_title, COUNT(*) AS topic_count
                FROM curriculum_topic
                WHERE corpus_id = ? AND grade_title = ?
                GROUP BY grade_title, subject_title, version_title, semester, unit_title
                ORDER BY subject_title, semester, unit_title
                """,
                (corpus_id, child["grade"]),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_job(self, corpus_id: str, values: dict) -> dict:
        subject = str(values.get("subject_title") or "").strip()
        semester = str(values.get("semester") or "").strip()
        version = str(values.get("version_title") or "").strip() or None
        requested_unit = str(values.get("unit_title") or "").strip() or None
        max_units = max(1, min(int(values.get("max_units") or 1), 12))
        if not subject or semester not in SEMESTERS:
            raise InvalidInternalizationRequestError("请选择有效的学科和学期。")

        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            child = connection.execute(
                "SELECT grade FROM child_profile WHERE child_id = ?", (values["child_id"],)
            ).fetchone()
            if not child:
                raise UnknownChildError(values["child_id"])
            existing = connection.execute(
                "SELECT job_id FROM curriculum_internalization_job WHERE client_action_id = ?",
                (values["client_action_id"],),
            ).fetchone()
            if existing:
                return self.get_job(existing["job_id"])
            corpus = connection.execute(
                "SELECT status FROM curriculum_corpus WHERE corpus_id = ?", (corpus_id,)
            ).fetchone()
            if not corpus or corpus["status"] != "ready":
                raise InvalidInternalizationRequestError("课程语料库尚未就绪。")
            clauses = [
                "corpus_id = ?", "grade_title = ?", "subject_title = ?", "semester = ?",
            ]
            parameters: list[Any] = [corpus_id, child["grade"], subject, semester]
            if version:
                clauses.append("COALESCE(version_title, '') = ?")
                parameters.append(version)
            if requested_unit:
                clauses.append("unit_title = ?")
                parameters.append(requested_unit)
            rows = connection.execute(
                f"""
                SELECT topic_id, unit_title FROM curriculum_topic
                WHERE {' AND '.join(clauses)}
                ORDER BY unit_title, title
                """,
                parameters,
            ).fetchall()
            units: dict[str, list[str]] = {}
            for row in rows:
                units.setdefault(row["unit_title"], []).append(row["topic_id"])
            selected_units = list(units.items())[:max_units]
            if not selected_units:
                raise InvalidInternalizationRequestError("该范围没有可内化的课程主题。")

            batches: list[tuple[str, list[str]]] = []
            for unit_title, topic_ids in selected_units:
                for offset in range(0, len(topic_ids), MAX_TOPICS_PER_BATCH):
                    batches.append((unit_title, topic_ids[offset:offset + MAX_TOPICS_PER_BATCH]))
            job_id = str(uuid4())
            now = utc_now()
            connection.execute(
                """
                INSERT INTO curriculum_internalization_job(
                  job_id, corpus_id, child_id, grade_title, subject_title, version_title,
                  semester, unit_title, status, total_batches, client_action_id, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'processing', ?, ?, ?)
                """,
                (
                    job_id, corpus_id, values["child_id"], child["grade"], subject, version,
                    semester, requested_unit, len(batches), values["client_action_id"], now,
                ),
            )
            for index, (unit_title, topic_ids) in enumerate(batches):
                batch_id = str(uuid4())
                connection.execute(
                    """
                    INSERT INTO curriculum_internalization_batch(
                      batch_id, job_id, batch_index, unit_title, source_topic_ids_json, status
                    ) VALUES (?, ?, ?, ?, ?, 'pending')
                    """,
                    (batch_id, job_id, index, unit_title, json.dumps(topic_ids)),
                )
                batch = connection.execute("SELECT * FROM curriculum_internalization_batch WHERE batch_id=?", (batch_id,)).fetchone()
                snapshot, _, _ = self._dispatch_snapshot(connection, batch)
                connection.execute("""INSERT INTO audit_event(audit_id,actor,action,child_id,summary_json,occurred_at)
                    VALUES(?,'system',?,?,?,?)""", (AUTHORITY_VERSION + ':' + batch_id, AUTHORITY_VERSION,
                    values['child_id'], json.dumps(snapshot, sort_keys=True), now))
            connection.commit()
        return self.get_job(job_id)

    async def run_job(self, job_id: str) -> None:
        try:
            batches = self._pending_batches(job_id)
            for batch in batches:
                batch = self._mark_batch(batch["batch_id"], "processing")
                if batch is None:
                    continue
                with self.database.connect() as connection:
                    connection.execute('BEGIN')
                    topics, standard_evidence = self._assert_dispatch_authority(connection, batch)
                    source_version = self._batch_source_version(connection, batch)

                def authorize_dispatch(original_batch=batch):
                    with self.database.connect() as connection:
                        connection.execute('BEGIN')
                        self._assert_dispatch_authority(connection, original_batch)

                request = self._model_request(job_id, batch, topics, standard_evidence, pre_dispatch=authorize_dispatch)
                authorize_dispatch()
                result = await self.gateway.complete(request)
                capabilities = self._parse_and_validate(result.content, topics, standard_evidence)
                self._store_batch_result(batch, capabilities, result, expected_source_version=source_version)
            self._complete_job(job_id)
        except InternalizationAuthorityError as error:
            self._fail_job(job_id, error.code)
        except ModelNotConfiguredError:
            self._fail_job(job_id, "MODEL_NOT_CONFIGURED")
        except ModelResponseError as error:
            self._fail_job(job_id, error.code)
        except InvalidInternalizationOutputError:
            self._fail_job(job_id, "INVALID_INTERNALIZATION_OUTPUT")
        except Exception:
            self._fail_job(job_id, "INTERNALIZATION_FAILED")

    def list_jobs(self, corpus_id: str, child_id: str) -> list[dict]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM curriculum_internalization_job
                WHERE corpus_id = ? AND child_id = ? ORDER BY started_at DESC LIMIT 30
                """,
                (corpus_id, child_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_job(self, job_id: str) -> dict:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM curriculum_internalization_job WHERE job_id = ?", (job_id,)
            ).fetchone()
        if not row:
            raise UnknownInternalizationJobError(job_id)
        return dict(row)

    def list_capabilities(
        self, child_id: str, subject: str | None = None, maturity: str | None = None, limit: int = 100
    ) -> list[dict]:
        with self.database.connect() as connection:
            child = connection.execute(
                "SELECT grade FROM child_profile WHERE child_id = ?", (child_id,)
            ).fetchone()
            if not child:
                raise UnknownChildError(child_id)
            clauses = ["cc.grade_title = ?"]
            parameters: list[Any] = [child["grade"]]
            from .curriculum_selection import current_curriculum_predicate
            current, current_params = current_curriculum_predicate(connection, child_id, "cc")
            clauses.append(current)
            parameters.extend(current_params)
            if subject:
                clauses.append("cc.subject_title = ?")
                parameters.append(subject)
            if maturity:
                clauses.append("cc.maturity = ?")
                parameters.append(maturity)
            parameters.append(max(1, min(limit, 200)))
            rows = connection.execute(
                f"""
                SELECT cc.*, GROUP_CONCAT(DISTINCT ct.title) AS source_titles,
                       (SELECT GROUP_CONCAT(css.subject_title || '课标第' || csr.page_start || '页', '｜')
                        FROM curriculum_capability_standard ccs2
                        JOIN curriculum_standard_requirement csr ON csr.requirement_id = ccs2.requirement_id
                        JOIN curriculum_standard_source css ON css.source_id = csr.source_id
                        WHERE ccs2.capability_id = cc.capability_id) AS standard_refs
                FROM curriculum_capability cc
                JOIN curriculum_capability_source ccs ON ccs.capability_id = cc.capability_id
                JOIN curriculum_topic ct ON ct.topic_id = ccs.topic_id
                WHERE {' AND '.join(clauses)}
                GROUP BY cc.capability_id
                ORDER BY cc.updated_at DESC LIMIT ?
                """,
                parameters,
            ).fetchall()
            items = [self._project_capability(connection, row) for row in rows]
        return [item for item in items if not maturity or item["maturity"] == maturity]

    def review_capability(self, capability_id: str, decision: str, note: str | None) -> dict:
        if decision not in {"verify", "reject"}:
            raise InvalidInternalizationRequestError("审核决定必须是通过或退回。")
        normalized_note = (note or '').strip() or None
        # Replaying the same still-current review is read-only. A changed
        # source, missing receipt or different note must be revalidated.
        with self.database.connect() as connection:
            connection.execute('BEGIN')
            if self._review_is_current_replay(connection,capability_id,decision,normalized_note):
                return next(item for item in self.list_capabilities_for_ids([capability_id]))
        # The review changes already-qualified requirement rows; own the same
        # effect guard as a report sender before opening the write transaction.
        with self.database.external_effect_guard(timeout=2), self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if self._review_is_current_replay(connection,capability_id,decision,normalized_note):
                connection.rollback()
                return next(item for item in self.list_capabilities_for_ids([capability_id]))
            row = connection.execute(
                "SELECT maturity FROM curriculum_capability WHERE capability_id = ?", (capability_id,)
            ).fetchone()
            if not row:
                raise UnknownCurriculumCapabilityError(capability_id)
            if row["maturity"] not in {"model_drafted", "verified"}:
                raise InvalidInternalizationRequestError("该能力当前不能审核。")
            if decision == "verify":
                source_state = connection.execute(
                    """
                    SELECT
                      EXISTS(SELECT 1 FROM curriculum_standard_source css
                             WHERE css.subject_title = REPLACE(REPLACE(cc.subject_title, '小学', ''), '初中', '')
                               AND css.status IN ('ocr_ready', 'structured')) AS has_standard,
                      EXISTS(SELECT 1 FROM curriculum_capability_standard ccs
                             WHERE ccs.capability_id = cc.capability_id) AS has_alignment
                    FROM curriculum_capability cc WHERE cc.capability_id = ?
                    """,
                    (capability_id,),
                ).fetchone()
                if source_state["has_standard"] and not source_state["has_alignment"]:
                    raise InvalidInternalizationRequestError("已有官方课标，但该能力尚未形成可追溯对齐，不能确认。")
            maturity = "verified" if decision == "verify" else "retired"
            now = utc_now()
            connection.execute(
                "UPDATE curriculum_capability SET maturity = ?, review_note = ?, updated_at = ? WHERE capability_id = ?",
                (maturity, normalized_note, now, capability_id),
            )
            connection.execute(
                """
                UPDATE curriculum_standard_requirement SET maturity = ?, updated_at = ?
                WHERE requirement_id IN (
                  SELECT requirement_id FROM curriculum_capability_standard WHERE capability_id = ?
                )
                """,
                (maturity, now, capability_id),
            )
            connection.execute(
                """
                UPDATE curriculum_standard_topic_alignment SET maturity = ?
                WHERE requirement_id IN (
                  SELECT requirement_id FROM curriculum_capability_standard WHERE capability_id = ?
                )
                """,
                (maturity, capability_id),
            )
            connection.execute(
                """
                INSERT INTO audit_event(audit_id, actor, action, child_id, summary_json, occurred_at)
                VALUES (?, 'parent', 'curriculum_capability_reviewed', NULL, ?, ?)
                """,
                (str(uuid4()), json.dumps({"capability_id": capability_id, "decision": decision,
                    "review_fingerprint": self._review_fingerprint(connection, capability_id)}), now),
            )
            connection.commit()
        return next(item for item in self.list_capabilities_for_ids([capability_id]))

    @classmethod
    def _review_is_current_replay(cls, connection, capability_id, decision, note):
        current=connection.execute('SELECT maturity,review_note FROM curriculum_capability WHERE capability_id=?', (capability_id,)).fetchone()
        receipt=connection.execute('''SELECT summary_json FROM audit_event
            WHERE action='curriculum_capability_reviewed' AND json_valid(summary_json)
              AND json_extract(summary_json,'$.capability_id')=? ORDER BY rowid DESC LIMIT 1''', (capability_id,)).fetchone()
        target='verified' if decision=='verify' else 'retired'
        if not current or not receipt or current['maturity']!=target or current['review_note']!=note:
            return False
        saved=json.loads(receipt['summary_json'])
        return saved.get('decision')==decision and saved.get('review_fingerprint')==cls._review_fingerprint(connection,capability_id)

    def list_capabilities_for_ids(self, capability_ids: list[str]) -> list[dict]:
        if not capability_ids:
            return []
        placeholders = ",".join("?" for _ in capability_ids)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT cc.*, GROUP_CONCAT(DISTINCT ct.title) AS source_titles,
                       (SELECT GROUP_CONCAT(css.subject_title || '课标第' || csr.page_start || '页', '｜')
                        FROM curriculum_capability_standard ccs2
                        JOIN curriculum_standard_requirement csr ON csr.requirement_id = ccs2.requirement_id
                        JOIN curriculum_standard_source css ON css.source_id = csr.source_id
                        WHERE ccs2.capability_id = cc.capability_id) AS standard_refs
                FROM curriculum_capability cc
                JOIN curriculum_capability_source ccs ON ccs.capability_id = cc.capability_id
                JOIN curriculum_topic ct ON ct.topic_id = ccs.topic_id
                WHERE cc.capability_id IN ({placeholders}) GROUP BY cc.capability_id
                """,
                capability_ids,
            ).fetchall()
            return [self._project_capability(connection, row) for row in rows]

    @staticmethod
    def _review_fingerprint(connection, capability_id):
        capability = connection.execute("SELECT * FROM curriculum_capability WHERE capability_id=?", (capability_id,)).fetchone()
        material = {key: capability[key] for key in ("corpus_id", "grade_title", "subject_title", "version_title",
            "semester", "unit_title", "capability_type", "title", "learning_objective", "prerequisites_json",
            "misconceptions_json", "mastery_criteria_json", "diagnostic_blueprint_json",
            "explanation_strategies_json", "transfer_goals_json")}
        topics = [dict(row) for row in connection.execute("""SELECT ct.topic_id,ct.title,ct.learning_objective,
            ct.grade_title,ct.subject_title,ct.version_title,ct.semester,ct.knowledge_json,corpus.status
            FROM curriculum_capability_source link JOIN curriculum_topic ct USING(topic_id)
            JOIN curriculum_corpus corpus ON corpus.corpus_id=ct.corpus_id
            WHERE link.capability_id=? ORDER BY ct.topic_id""", (capability_id,))]
        resources = [dict(row) for row in connection.execute("""SELECT DISTINCT cr.row_id,cr.title,cr.transcript_text,
            cr.transcript_hash,cr.version_title,cr.semester FROM curriculum_capability_source link
            JOIN curriculum_resource_topic rt USING(topic_id) JOIN curriculum_resource cr USING(row_id)
            WHERE link.capability_id=? ORDER BY cr.row_id""", (capability_id,))]
        requirements = [dict(row) for row in connection.execute("""SELECT req.requirement_id,req.requirement_text,
            req.observable_outcome,req.stage_scope,req.maturity,css.source_id,css.status,css.pdf_sha256,
            page.page_number,page.content_text,page.content_hash
            FROM curriculum_capability_standard link JOIN curriculum_standard_requirement req USING(requirement_id)
            JOIN curriculum_standard_source css ON css.source_id=req.source_id
            LEFT JOIN curriculum_standard_page page ON page.source_id=req.source_id
                AND page.page_number BETWEEN req.page_start AND req.page_end
            WHERE link.capability_id=? ORDER BY req.requirement_id,page.page_number""", (capability_id,))]
        return hashlib.sha256(json.dumps([material, topics, resources, requirements], sort_keys=True,
            ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()

    def _project_capability(self, connection, row):
        value = self._serialize_capability(row)
        value["stored_maturity"] = value["maturity"]
        value["review_provenance"] = "not_verified"
        if value["maturity"] == "verified":
            review = connection.execute("""SELECT summary_json FROM audit_event
                WHERE action='curriculum_capability_reviewed'
                  AND json_extract(summary_json,'$.capability_id')=?
                ORDER BY occurred_at DESC,rowid DESC LIMIT 1""", (row["capability_id"],)).fetchone()
            proof = json.loads(review[0]) if review else {}
            if proof.get("decision") == "verify" and proof.get("review_fingerprint") == self._review_fingerprint(connection, row["capability_id"]):
                value["review_provenance"] = "current_sources_match"
            else:
                value["maturity"] = "conflicted"
                value["review_provenance"] = "source_changed_or_legacy_review_requires_reconfirmation"
        return value

    def _pending_batches(self, job_id: str) -> list[sqlite3.Row]:
        with self.database.connect() as connection:
            job = connection.execute(
                "SELECT status FROM curriculum_internalization_job WHERE job_id = ?", (job_id,)
            ).fetchone()
            if not job:
                raise UnknownInternalizationJobError(job_id)
            if job["status"] != "processing":
                return []
            return connection.execute(
                "SELECT * FROM curriculum_internalization_batch WHERE job_id = ? AND status = 'pending' ORDER BY batch_index",
                (job_id,),
            ).fetchall()

    def _batch_topics(self, batch: sqlite3.Row, *, connection=None) -> list[dict]:
        topic_ids = json.loads(batch["source_topic_ids_json"])
        placeholders = ",".join("?" for _ in topic_ids)
        with (nullcontext(connection) if connection is not None else self.database.connect()) as connection:
            rows = connection.execute(
                f"""
                SELECT ct.topic_id, ct.title, ct.lesson_kind, ct.learning_objective,
                       ct.unit_title, ct.grade_title, ct.subject_title, ct.version_title, ct.semester,
                       COALESCE((
                         SELECT substr(cr.transcript_text, 1, 600)
                         FROM curriculum_resource_topic crt
                         JOIN curriculum_resource cr ON cr.row_id = crt.row_id
                         WHERE crt.topic_id = ct.topic_id AND cr.transcript_text != ''
                         ORDER BY cr.row_id LIMIT 1
                       ), '') AS evidence_excerpt
                FROM curriculum_topic ct WHERE ct.topic_id IN ({placeholders})
                ORDER BY ct.title
                """,
                topic_ids,
            ).fetchall()
        if len(rows) != len(topic_ids):
            raise InvalidInternalizationRequestError("内化批次的来源主题已经变化，请重新创建任务。")
        return [dict(row) for row in rows]

    @staticmethod
    def _model_request(
        job_id: str, batch: sqlite3.Row, topics: list[dict], standard_evidence: list[dict], *, pre_dispatch=None
    ) -> ModelRequest:
        compact_topics = [
            {
                "topic_id": item["topic_id"], "title": item["title"],
                "lesson_kind": item["lesson_kind"], "learning_objective": item["learning_objective"][:300],
                "evidence_excerpt": item["evidence_excerpt"][:600],
            }
            for item in topics
        ]
        scope = topics[0]
        system = (
            "你是中国义务教育课程知识工程师。只根据给定课程证据拆解可独立诊断的原子知识或能力。"
            "不得补写具体教材原文或题目答案，不得声称模型草案已经通过教研验证。只输出JSON对象。"
        )
        user = json.dumps({
            "scope": {key: scope[key] for key in ("grade_title", "subject_title", "version_title", "semester", "unit_title")},
            "topics": compact_topics,
            "official_standard_evidence": standard_evidence,
            "output_schema": {
                "capabilities": [{
                    "title": "2-60字", "capability_type": sorted(CAPABILITY_TYPES),
                    "learning_objective": "可观察目标，5-300字",
                    "prerequisites": ["前置能力名称"], "misconceptions": ["具体常见错误"],
                    "mastery_criteria": ["可观察、可判定标准"],
                    "diagnostic_blueprint": ["基础识别", "独立应用", "迁移或延迟复测"],
                    "explanation_strategies": ["适龄讲解或提示策略"],
                    "transfer_goals": ["迁移目标"], "source_topic_ids": ["仅限输入topic_id"],
                    "standard_alignments": [{
                        "source_ref": "仅限输入的source_id:页码", "source_quote": "该页可逐字找到的8-160字短依据",
                        "requirement_type": "course_goal|content_requirement|academic_requirement|academic_quality|implementation_requirement|local_supplement",
                        "stage_scope": "例如第二学段（3~4年级）", "requirement_text": "课标要求的准确概括",
                        "observable_outcome": "可观察、可评估的学生表现"
                    }],
                    "confidence": "0到1"
                }]
            },
            "hard_rules": [
                "每个输入topic_id至少被一个能力引用", "每个能力必须有掌握标准和三档诊断蓝图",
                "有official_standard_evidence时每个能力至少引用一条，source_quote必须逐字来自对应页；没有时输出空数组",
                "能力数不得超过输入主题数的3倍", "只输出JSON，不使用Markdown代码块",
            ],
        }, ensure_ascii=False, separators=(",", ":"))
        return ModelRequest(
            request_id=str(uuid4()), child_id="curriculum-system", session_id=f"curriculum:{job_id}",
            request_type="curriculum_internalization", risk_level="medium",
            context_manifest={"purpose": "curriculum_internalization", "raw_media": False, "topic_count": len(topics)},
            provider_preference="configured_primary", policy_version="curriculum_source_boundary_v1",
            prompt_version=PROMPT_VERSION, knowledge_version=None, output_type="knowledge_structuring",
            evidence_refs=tuple(item["topic_id"] for item in topics),
            messages=({"role": "system", "content": system}, {"role": "user", "content": user}),
            pre_dispatch=pre_dispatch,
        )

    @staticmethod
    def _parse_and_validate(
        content: str, topics: list[dict], standard_evidence: list[dict]
    ) -> list[dict]:
        value = content.strip()
        if value.startswith("```"):
            value = value.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        try:
            payload = json.loads(value)
        except json.JSONDecodeError as error:
            raise InvalidInternalizationOutputError("模型没有返回有效JSON。") from error
        if not isinstance(payload, dict) or set(payload) != {"capabilities"}:
            raise InvalidInternalizationOutputError("模型输出顶层结构不符合约定。")
        capabilities = payload["capabilities"]
        if not isinstance(capabilities, list) or not 1 <= len(capabilities) <= len(topics) * 3:
            raise InvalidInternalizationOutputError("模型输出的能力数量异常。")
        allowed_ids = {item["topic_id"] for item in topics}
        covered_ids: set[str] = set()
        required = {
            "title", "capability_type", "learning_objective", "prerequisites", "misconceptions",
            "mastery_criteria", "diagnostic_blueprint", "explanation_strategies", "transfer_goals",
            "source_topic_ids", "confidence",
            "standard_alignments",
        }
        normalized: list[dict] = []
        for item in capabilities:
            if not isinstance(item, dict) or set(item) != required:
                raise InvalidInternalizationOutputError("能力字段不完整或包含未允许字段。")
            title = CurriculumInternalizationService._bounded_text(item["title"], 2, 60)
            objective = CurriculumInternalizationService._bounded_text(item["learning_objective"], 5, 300)
            if item["capability_type"] not in CAPABILITY_TYPES:
                raise InvalidInternalizationOutputError("能力类型不受支持。")
            source_ids = CurriculumInternalizationService._string_list(item["source_topic_ids"], 1, len(topics), 80)
            if not set(source_ids).issubset(allowed_ids):
                raise InvalidInternalizationOutputError("模型引用了批次之外的来源主题。")
            confidence = item["confidence"]
            if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= float(confidence) <= 1:
                raise InvalidInternalizationOutputError("能力置信度无效。")
            alignments = CurriculumInternalizationService._validate_standard_alignments(
                item["standard_alignments"], standard_evidence
            )
            entry = {
                "title": title, "capability_type": item["capability_type"],
                "learning_objective": objective,
                "prerequisites": CurriculumInternalizationService._string_list(item["prerequisites"], 0, 12, 120),
                "misconceptions": CurriculumInternalizationService._string_list(item["misconceptions"], 1, 12, 240),
                "mastery_criteria": CurriculumInternalizationService._string_list(item["mastery_criteria"], 1, 8, 240),
                "diagnostic_blueprint": CurriculumInternalizationService._string_list(item["diagnostic_blueprint"], 3, 8, 240),
                "explanation_strategies": CurriculumInternalizationService._string_list(item["explanation_strategies"], 1, 8, 240),
                "transfer_goals": CurriculumInternalizationService._string_list(item["transfer_goals"], 0, 8, 240),
                "source_topic_ids": source_ids, "confidence": round(float(confidence), 3),
                "standard_alignments": alignments,
            }
            covered_ids.update(source_ids)
            normalized.append(entry)
        if covered_ids != allowed_ids:
            raise InvalidInternalizationOutputError("模型没有覆盖本批次全部来源主题。")
        return normalized

    @staticmethod
    def _validate_standard_alignments(value: Any, evidence: list[dict]) -> list[dict]:
        if not isinstance(value, list) or len(value) > 6:
            raise InvalidInternalizationOutputError("课标对齐字段无效。")
        if evidence and not value:
            raise InvalidInternalizationOutputError("存在官方课标证据但模型没有建立对齐。")
        if not evidence and value:
            raise InvalidInternalizationOutputError("模型引用了未提供的课标证据。")
        by_ref = {item["source_ref"]: item["content_excerpt"] for item in evidence}
        allowed_types = {
            "course_goal", "content_requirement", "academic_requirement", "academic_quality",
            "implementation_requirement", "local_supplement",
        }
        required = {
            "source_ref", "source_quote", "requirement_type", "stage_scope",
            "requirement_text", "observable_outcome",
        }
        result = []
        for item in value:
            if not isinstance(item, dict) or set(item) != required or item["source_ref"] not in by_ref:
                raise InvalidInternalizationOutputError("课标对齐引用或字段无效。")
            if item["requirement_type"] not in allowed_types:
                raise InvalidInternalizationOutputError("课标要求类型无效。")
            quote = CurriculumInternalizationService._bounded_text(item["source_quote"], 8, 160)
            normalized_quote = "".join(quote.split())
            normalized_evidence = "".join(by_ref[item["source_ref"]].split())
            if normalized_quote not in normalized_evidence:
                raise InvalidInternalizationOutputError("课标短依据无法在官方页中逐字核对。")
            result.append({
                "source_ref": item["source_ref"], "source_quote": quote,
                "requirement_type": item["requirement_type"],
                "stage_scope": CurriculumInternalizationService._bounded_text(item["stage_scope"], 2, 80),
                "requirement_text": CurriculumInternalizationService._bounded_text(item["requirement_text"], 8, 300),
                "observable_outcome": CurriculumInternalizationService._bounded_text(item["observable_outcome"], 8, 300),
            })
        return result

    @staticmethod
    def _bounded_text(value: Any, minimum: int, maximum: int) -> str:
        if not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum:
            raise InvalidInternalizationOutputError("模型文本字段长度异常。")
        return value.strip()

    @staticmethod
    def _string_list(value: Any, minimum: int, maximum: int, item_maximum: int) -> list[str]:
        if not isinstance(value, list) or not minimum <= len(value) <= maximum:
            raise InvalidInternalizationOutputError("模型列表字段长度异常。")
        result: list[str] = []
        for item in value:
            if not isinstance(item, str) or not item.strip() or len(item.strip()) > item_maximum:
                raise InvalidInternalizationOutputError("模型列表字段包含无效文本。")
            if item.strip() not in result:
                result.append(item.strip())
        if len(result) < minimum:
            raise InvalidInternalizationOutputError("模型列表字段去重后数量不足。")
        return result

    @staticmethod
    def _authority_hash(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
            separators=(',', ':')).encode('utf-8')).hexdigest()

    def _dispatch_snapshot(self, c, batch):
        job = c.execute('SELECT * FROM curriculum_internalization_job WHERE job_id=?', (batch['job_id'],)).fetchone()
        if not job:
            raise InternalizationAuthorityError()
        child = c.execute('SELECT child_id,grade,status FROM child_profile WHERE child_id=?', (job['child_id'],)).fetchone()
        corpus = c.execute('SELECT * FROM curriculum_corpus WHERE corpus_id=?', (job['corpus_id'],)).fetchone()
        ids = json.loads(batch['source_topic_ids_json'])
        if not isinstance(ids, list) or not ids or len(set(ids)) != len(ids):
            raise InternalizationAuthorityError()
        marks = ','.join('?' for _ in ids)
        topics = [dict(r) for r in c.execute(f'SELECT * FROM curriculum_topic WHERE topic_id IN ({marks}) ORDER BY topic_id', ids)]
        links = [dict(r) for r in c.execute(f'SELECT * FROM curriculum_resource_topic WHERE topic_id IN ({marks}) ORDER BY topic_id,row_id', ids)]
        resources = [dict(r) for r in c.execute(f'''SELECT DISTINCT r.* FROM curriculum_resource r
            JOIN curriculum_resource_topic l USING(row_id) WHERE l.topic_id IN ({marks}) ORDER BY r.row_id''', ids)]
        subject = str(job['subject_title']).replace('小学', '').replace('初中', '')
        subject = '生物学' if subject == '生物' else subject
        standards = [dict(r) for r in c.execute('SELECT * FROM curriculum_standard_source WHERE subject_title=? ORDER BY source_id', (subject,))]
        pages = [dict(r) for r in c.execute('''SELECT p.* FROM curriculum_standard_page p
            JOIN curriculum_standard_source s USING(source_id) WHERE s.subject_title=? ORDER BY p.source_id,p.page_number''', (subject,))]
        alignments = [dict(r) for r in c.execute(f'''SELECT * FROM curriculum_topic_standard_candidate
            WHERE topic_id IN ({marks}) ORDER BY topic_id,source_id,page_number''', ids)]
        source_allowed = bool(child and child['status'] == 'active' and child['grade'] == job['grade_title']
            and corpus and corpus['status'] == 'ready' and corpus['corpus_type'] == 'seyk'
            and corpus['license_status'] == 'public_personal_use' and corpus['review_status'] == 'approved_baseline'
            and len(topics) == len(ids) and all(t['corpus_id'] == job['corpus_id']
                and t['grade_title'] == job['grade_title'] and t['subject_title'] == job['subject_title']
                and t['semester'] == job['semester'] and t['unit_title'] == batch['unit_title']
                and (job['version_title'] is None or t['version_title'] == job['version_title']) for t in topics)
            and all(r['corpus_id'] == job['corpus_id'] for r in resources))
        privacy = c.execute('SELECT cloud_model_text_allowed FROM privacy_preference WHERE singleton_id=1').fetchone()
        consent = c.execute("SELECT * FROM consent_record WHERE scope='cloud_model_text_allowed' ORDER BY rowid DESC LIMIT 1").fetchone()
        consent_allowed = bool(privacy and privacy[0] == 1 and consent
            and consent['allowed'] == 1 and consent['source'] == 'parent_ui'
            and consent['policy_version'] and consent['occurred_at'])
        model_topics = self._batch_topics(batch, connection=c)
        model_standards = self._standard_evidence(model_topics, connection=c)
        standard_status = {r['source_id']: r['status'] for r in standards}
        source_allowed = source_allowed and all(standard_status.get(r['source_ref'].rsplit(':', 1)[0])
            in {'ocr_ready', 'structured'} for r in model_standards)
        request = self._model_request(job['job_id'], batch, model_topics, model_standards)
        snapshot = {
            'version': AUTHORITY_VERSION, 'purpose': 'curriculum_internalization',
            'job_id': job['job_id'], 'batch_id': batch['batch_id'],
            'job_binding_hash': self._authority_hash({k: job[k] for k in (
                'job_id', 'corpus_id', 'child_id', 'grade_title', 'subject_title', 'version_title',
                'semester', 'unit_title', 'total_batches', 'client_action_id', 'started_at')}),
            'batch_binding_hash': self._authority_hash({k: batch[k] for k in (
                'batch_id', 'job_id', 'batch_index', 'unit_title', 'source_topic_ids_json')}),
            'source_hash': self._authority_hash([dict(corpus) if corpus else None, dict(child) if child else None,
                topics, links, resources, standards, pages, alignments]),
            'request_hash': self._authority_hash([request.messages, request.request_type,
                request.prompt_version, request.policy_version, request.output_type]),
            'consent_hash': self._authority_hash(dict(consent) if consent else None),
            'consent_allowed': consent_allowed, 'source_allowed': source_allowed,
        }
        return snapshot, model_topics, model_standards

    def _assert_dispatch_authority(self, c, batch):
        live = c.execute('''SELECT b.*,j.status AS job_status,j.child_id FROM curriculum_internalization_batch b
            JOIN curriculum_internalization_job j USING(job_id) WHERE batch_id=?''', (batch['batch_id'],)).fetchone()
        if (not live or live['job_status'] != 'processing' or live['status'] != 'processing'
                or live['job_id'] != batch['job_id'] or live['started_at'] != batch['started_at']):
            raise InternalizationAuthorityError()
        stored = c.execute("SELECT summary_json FROM audit_event WHERE audit_id=? AND action=? AND actor='system' AND child_id=?",
            (AUTHORITY_VERSION + ':' + batch['batch_id'], AUTHORITY_VERSION, live['child_id'])).fetchone()
        if not stored:
            raise InternalizationAuthorityError('INTERNALIZATION_AUTHORITY_MISSING_NEW_JOB_REQUIRED')
        quarantined = c.execute("""SELECT 1 FROM audit_event WHERE child_id=?
            AND action='curriculum_internalization_authority_quarantined_v1'
            AND json_extract(summary_json,'$.authority_audit_id')=? LIMIT 1""",
            (live['child_id'], AUTHORITY_VERSION + ':' + batch['batch_id'])).fetchone()
        if quarantined:
            raise InternalizationAuthorityError('INTERNALIZATION_RESTORE_REAUTHORIZATION_REQUIRED')
        frozen = json.loads(stored[0])
        if not frozen.get('consent_allowed'):
            raise InternalizationAuthorityError('INTERNALIZATION_TEXT_CONSENT_REQUIRED_NEW_JOB_REQUIRED')
        if not frozen.get('source_allowed'):
            raise InternalizationAuthorityError('INTERNALIZATION_SOURCE_NOT_ELIGIBLE_NEW_JOB_REQUIRED')
        pending = c.execute("""SELECT 1 FROM deletion_job WHERE child_id=? AND target_type='child_scope'
            AND scope IN ('learning','knowledge','media','all_history') AND status!='completed' LIMIT 1""", (live['child_id'],)).fetchone()
        if pending:
            raise InternalizationAuthorityError()
        current, topics, standards = self._dispatch_snapshot(c, live)
        if current != frozen:
            raise InternalizationAuthorityError()
        return topics, standards

    @staticmethod
    def _batch_source_version(connection, batch):
        topic_ids = json.loads(batch["source_topic_ids_json"])
        placeholders = ",".join("?" for _ in topic_ids)
        topics = [dict(row) for row in connection.execute(f"""SELECT topic_id,corpus_id,grade_title,
            subject_title,version_title,semester,unit_title,title,learning_objective,knowledge_json
            FROM curriculum_topic WHERE topic_id IN ({placeholders}) ORDER BY topic_id""", topic_ids)]
        resources = [dict(row) for row in connection.execute(f"""SELECT DISTINCT cr.row_id,cr.title,
            cr.grade_title,cr.subject_title,cr.version_title,cr.semester,cr.transcript_text,cr.transcript_hash
            FROM curriculum_resource cr JOIN curriculum_resource_topic link USING(row_id)
            WHERE link.topic_id IN ({placeholders}) ORDER BY cr.row_id""", topic_ids)]
        subject = str(topics[0]["subject_title"]).replace("小学", "").replace("初中", "") if topics else ""
        standards = [dict(row) for row in connection.execute("""SELECT css.source_id,css.status,
            css.pdf_sha256,css.text_sha256,csp.page_number,csp.content_text,csp.content_hash
            FROM curriculum_standard_source css JOIN curriculum_standard_page csp USING(source_id)
            WHERE css.subject_title=? ORDER BY css.source_id,csp.page_number""",
            ("生物学" if subject == "生物" else subject,))]
        return hashlib.sha256(json.dumps([topics, resources, standards], sort_keys=True,
            ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()

    def _store_batch_result(self, batch: sqlite3.Row, capabilities: list[dict], result: Any,
                            *, expected_source_version=None) -> None:
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_dispatch_authority(connection, batch)
            source_version = self._batch_source_version(connection, batch)
            if expected_source_version is not None and source_version != expected_source_version:
                raise InvalidInternalizationOutputError("内化期间题源发生修订，请基于新版本重新创建任务。")
            job = connection.execute(
                "SELECT * FROM curriculum_internalization_job WHERE job_id = ?", (batch["job_id"],)
            ).fetchone()
            if not job:
                raise UnknownInternalizationJobError(batch["job_id"])
            for item in capabilities:
                # Approval belongs to this material version, not to a title. A new
                # rubric or source binding creates a separate unreviewed capability.
                fingerprint = hashlib.sha256(
                    json.dumps({"version": "curriculum_material_v2", "scope": [
                        job["corpus_id"], job["grade_title"], job["subject_title"],
                        job["version_title"], job["semester"], batch["unit_title"],
                    ], "source_version": source_version,
                       "material": {key: value for key, value in item.items() if key != "confidence"}},
                        sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                ).hexdigest()
                capability_id = f"cap-{fingerprint[:24]}"
                values = (
                    capability_id, job["corpus_id"], job["job_id"], job["grade_title"],
                    job["subject_title"], job["version_title"], job["semester"], batch["unit_title"],
                    item["capability_type"], item["title"], item["learning_objective"],
                    json.dumps(item["prerequisites"], ensure_ascii=False),
                    json.dumps(item["misconceptions"], ensure_ascii=False),
                    json.dumps(item["mastery_criteria"], ensure_ascii=False),
                    json.dumps(item["diagnostic_blueprint"], ensure_ascii=False),
                    json.dumps(item["explanation_strategies"], ensure_ascii=False),
                    json.dumps(item["transfer_goals"], ensure_ascii=False), item["confidence"],
                    result.provider, result.model, PROMPT_VERSION, fingerprint, now, now,
                )
                connection.execute(
                    """
                    INSERT INTO curriculum_capability(
                      capability_id, corpus_id, job_id, grade_title, subject_title, version_title,
                      semester, unit_title, capability_type, title, learning_objective,
                      prerequisites_json, misconceptions_json, mastery_criteria_json,
                      diagnostic_blueprint_json, explanation_strategies_json, transfer_goals_json,
                      confidence, maturity, model_provider, model_name, prompt_version,
                      fingerprint, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'model_drafted', ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(corpus_id, grade_title, subject_title, version_title, semester, unit_title, fingerprint)
                    DO UPDATE SET job_id = excluded.job_id, confidence = excluded.confidence,
                      maturity = curriculum_capability.maturity,
                      model_provider = excluded.model_provider, model_name = excluded.model_name,
                      prompt_version = excluded.prompt_version, updated_at = excluded.updated_at
                    """,
                    values,
                )
                stored = connection.execute(
                    """
                    SELECT capability_id FROM curriculum_capability
                    WHERE corpus_id = ? AND grade_title = ? AND subject_title = ?
                      AND version_title IS ? AND semester = ? AND unit_title = ? AND fingerprint = ?
                    """,
                    (job["corpus_id"], job["grade_title"], job["subject_title"], job["version_title"], job["semester"], batch["unit_title"], fingerprint),
                ).fetchone()["capability_id"]
                connection.execute("DELETE FROM curriculum_capability_source WHERE capability_id = ?", (stored,))
                for topic_id in item["source_topic_ids"]:
                    connection.execute(
                        "INSERT INTO curriculum_capability_source(capability_id, topic_id) VALUES (?, ?)",
                        (stored, topic_id),
                    )
                connection.execute("DELETE FROM curriculum_capability_standard WHERE capability_id = ?", (stored,))
                for alignment in item["standard_alignments"]:
                    source_id, page_value = alignment["source_ref"].rsplit(":", 1)
                    requirement_key = "|".join((
                        source_id, page_value, alignment["requirement_type"], alignment["stage_scope"],
                        alignment["source_quote"], alignment["observable_outcome"], alignment["requirement_text"],
                        source_version,
                    ))
                    requirement_id = f"requirement-{hashlib.sha256(requirement_key.encode('utf-8')).hexdigest()[:24]}"
                    connection.execute(
                        """
                        INSERT INTO curriculum_standard_requirement(
                          requirement_id, source_id, subject_title, stage_scope, requirement_type,
                          title, requirement_text, observable_outcome, page_start, page_end,
                          confidence, maturity, model_provider, model_name, prompt_version,
                          created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'model_drafted', ?, ?, ?, ?, ?)
                        ON CONFLICT(requirement_id) DO NOTHING
                        """,
                        (
                            requirement_id, source_id, job["subject_title"], alignment["stage_scope"],
                            alignment["requirement_type"], alignment["source_quote"],
                            alignment["requirement_text"], alignment["observable_outcome"],
                            int(page_value), int(page_value), item["confidence"], result.provider,
                            result.model, PROMPT_VERSION, now, now,
                        ),
                    )
                    connection.execute(
                        "INSERT OR IGNORE INTO curriculum_capability_standard(capability_id, requirement_id) VALUES (?, ?)",
                        (stored, requirement_id),
                    )
                    for topic_id in item["source_topic_ids"]:
                        connection.execute(
                            """
                            INSERT INTO curriculum_standard_topic_alignment(
                              requirement_id, topic_id, coverage_type, rationale, confidence, maturity
                            ) VALUES (?, ?, 'partial', ?, ?, 'model_drafted')
                            ON CONFLICT(requirement_id, topic_id) DO UPDATE SET
                              rationale = excluded.rationale, confidence = excluded.confidence,
                              maturity = CASE WHEN curriculum_standard_topic_alignment.maturity = 'verified'
                                THEN 'verified' ELSE 'model_drafted' END
                            """,
                            (
                                requirement_id, topic_id,
                                f"能力“{item['title']}”由该课标要求和课程主题共同支撑。",
                                item["confidence"],
                            ),
                        )
            output_hash = hashlib.sha256(
                json.dumps(capabilities, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()
            connection.execute(
                "UPDATE curriculum_internalization_batch SET status = 'completed', output_hash = ?, completed_at = ? WHERE batch_id = ?",
                (output_hash, now, batch["batch_id"]),
            )
            connection.execute(
                """
                UPDATE curriculum_internalization_job
                SET processed_batches = processed_batches + 1,
                    capability_count = (SELECT COUNT(*) FROM curriculum_capability WHERE job_id = ?),
                    model_provider = ?, model_name = ?
                WHERE job_id = ?
                """,
                (job["job_id"], result.provider, result.model, job["job_id"]),
            )
            connection.commit()

    def _standard_evidence(self, topics: list[dict], *, connection=None) -> list[dict]:
        if not topics:
            return []
        subject = str(topics[0]["subject_title"]).replace("小学", "").replace("初中", "")
        if subject == "生物":
            subject = "生物学"
        candidates: list[dict] = []
        stage_label = {
            "四年级": "第二学段", "五年级": "第三学段", "六年级": "第三学段",
            "初一": "第四学段", "初二": "第四学段", "初三": "第四学段",
        }.get(str(topics[0]["grade_title"]), "")
        with (nullcontext(connection) if connection is not None else self.database.connect()) as connection:
            topic_ids = [item["topic_id"] for item in topics]
            placeholders = ",".join("?" for _ in topic_ids)
            aligned_rows = connection.execute(
                f"""
                SELECT ctsc.topic_id, ctsc.matched_term, css.source_id, css.subject_title,
                       css.title, csp.page_number, csp.content_text
                FROM curriculum_topic_standard_candidate ctsc
                JOIN curriculum_standard_source css ON css.source_id = ctsc.source_id
                JOIN curriculum_standard_page csp
                  ON csp.source_id = ctsc.source_id AND csp.page_number = ctsc.page_number
                WHERE ctsc.topic_id IN ({placeholders})
                  AND ctsc.alignment_status = 'candidate'
                ORDER BY ctsc.confidence DESC
                """,
                topic_ids,
            ).fetchall()
            for row in aligned_rows:
                compact = row["matched_term"] or ""
                text = row["content_text"]
                index = text.find(compact) if compact else -1
                excerpt = text[max(0, index - 350):index + len(compact) + 650] if index >= 0 else text[:1000]
                candidates.append({
                    "source_ref": f"{row['source_id']}:{row['page_number']}",
                    "subject_title": row["subject_title"], "source_title": row["title"],
                    "page_number": row["page_number"], "content_excerpt": excerpt,
                })
                if len(candidates) >= 4:
                    return candidates
            for topic in topics:
                terms = self._standard_search_terms(topic)
                for term in terms:
                    compact = str(term or "").strip()
                    if len(compact) < 2:
                        continue
                    rows = connection.execute(
                        """
                        SELECT css.source_id, css.subject_title, css.title, csp.page_number, csp.content_text
                        FROM curriculum_standard_page csp
                        JOIN curriculum_standard_source css ON css.source_id = csp.source_id
                        WHERE css.subject_title = ? AND css.status IN ('ocr_ready', 'structured')
                          AND csp.content_text LIKE ?
                        ORDER BY csp.page_number LIMIT 12
                        """,
                        (subject, f"%{compact}%"),
                    ).fetchall()
                    ordered_rows = sorted(
                        rows,
                        key=lambda row: (
                            0 if stage_label and stage_label in row["content_text"] else 1,
                            row["page_number"],
                        ),
                    )
                    for row in ordered_rows[:2]:
                        source_ref = f"{row['source_id']}:{row['page_number']}"
                        if any(item["source_ref"] == source_ref for item in candidates):
                            continue
                        text = row["content_text"]
                        index = text.find(compact)
                        excerpt = text[max(0, index - 350):index + len(compact) + 650] if index >= 0 else text[:1000]
                        candidates.append({
                            "source_ref": source_ref, "subject_title": row["subject_title"],
                            "source_title": row["title"], "page_number": row["page_number"],
                            "content_excerpt": excerpt,
                        })
                    if len(candidates) >= 4:
                        break
                if len(candidates) >= 4:
                    break
        return candidates[:4]

    @staticmethod
    def _standard_search_terms(topic: dict) -> list[str]:
        title = str(topic.get("title") or "").strip()
        objective = str(topic.get("learning_objective") or "").strip()
        terms = [title]
        replacements = {
            "加法": "加减", "减法": "加减", "乘法": "乘除", "除法": "乘除",
            "图像": "图象", "生物": "生物学",
        }
        for old, new in replacements.items():
            if old in title:
                terms.append(title.replace(old, new))
        compact = "".join(character for character in title if character.isalnum())
        ignored = {"练习", "复习", "整理", "活动", "实验", "测试", "入门", "应用"}
        for size in (4, 3, 2):
            for index in range(max(0, len(compact) - size + 1)):
                term = compact[index:index + size]
                if term not in ignored:
                    terms.append(term)
        for phrase in objective.replace("，", "。").split("。"):
            value = phrase.strip()
            if 3 <= len(value) <= 18:
                terms.append(value)
        return list(dict.fromkeys(term for term in terms if len(term) >= 2))[:12]

    def _mark_batch(self, batch_id: str, status: str):
        with self.database.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            changed = connection.execute(
                """UPDATE curriculum_internalization_batch SET status = ?, started_at = ?
                   WHERE batch_id = ? AND status='pending' AND EXISTS(
                     SELECT 1 FROM curriculum_internalization_job j
                     WHERE j.job_id=curriculum_internalization_batch.job_id AND j.status='processing')""",
                (status, utc_now(), batch_id),
            ).rowcount
            row = connection.execute('SELECT * FROM curriculum_internalization_batch WHERE batch_id=?', (batch_id,)).fetchone() if changed else None
            connection.commit()
            return row

    def _complete_job(self, job_id: str) -> None:
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE curriculum_internalization_job SET status = 'completed', error_code = NULL, completed_at = ?
                   WHERE job_id = ? AND status='processing' AND total_batches>0
                   AND total_batches=(SELECT COUNT(*) FROM curriculum_internalization_batch b
                       WHERE b.job_id=curriculum_internalization_job.job_id AND b.status='completed')""",
                (now, job_id),
            )
            connection.commit()

    def _fail_job(self, job_id: str, code: str) -> None:
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute(
                """UPDATE curriculum_internalization_batch SET status = 'failed', error_code = ?, completed_at = ?
                   WHERE job_id = ? AND status IN ('pending', 'processing') AND EXISTS(
                     SELECT 1 FROM curriculum_internalization_job j
                     WHERE j.job_id=curriculum_internalization_batch.job_id AND j.status='processing')""",
                (code, now, job_id),
            )
            connection.execute(
                "UPDATE curriculum_internalization_job SET status = 'failed', error_code = ?, completed_at = ? WHERE job_id = ? AND status='processing'",
                (code, now, job_id),
            )
            connection.commit()

    @staticmethod
    def _serialize_capability(row: sqlite3.Row) -> dict:
        item = dict(row)
        for column in (
            "prerequisites_json", "misconceptions_json", "mastery_criteria_json",
            "diagnostic_blueprint_json", "explanation_strategies_json", "transfer_goals_json",
        ):
            item[column.removesuffix("_json")] = json.loads(item.pop(column))
        item["source_titles"] = [value for value in (item.get("source_titles") or "").replace("｜", ",").split(",") if value]
        item["standard_refs"] = [value for value in (item.get("standard_refs") or "").split("｜") if value]
        return item
