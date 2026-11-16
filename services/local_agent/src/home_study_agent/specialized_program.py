from __future__ import annotations

from contextlib import nullcontext
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
import sqlite3
from typing import Any
from uuid import uuid4

from .database import Database
from .family_clock import family_today
from .specialized_catalog import SPECIALIZED_DOMAIN_HINTS
from .standard_relocation_provenance import snapshot_read


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# A scheduling policy, not a scientific expiry date for a child's knowledge.
REFERENCE_PLANNING_DAYS = 30


def _domain_priority(row):
    return (0 if (row.get("priority_status") == "recheck_needed"
                  or (row.get('exam_reference_score') is not None and row['exam_reference_score'] < 60)) else
            1 if row.get("priority_status") == "supported_only" else 2,
            row["reference_score"] is not None, row["reference_score"] or 0,
            row["learning_evidence_count"])


PET_DOMAINS = {
    "reading": ("阅读", (("main_idea", "主旨与目的"), ("detail", "细节定位"), ("vocabulary", "语境词义"), ("inference", "简单推断"), ("text_function", "文本功能"))),
    "listening": ("听力", (("gist", "整体理解"), ("detail", "细节提取"), ("speaker_intent", "说话者意图"), ("note_completion", "信息记录"))),
    "writing": ("写作", (("task_coverage", "任务点覆盖"), ("organization", "组织衔接"), ("grammar", "句子控制"), ("vocabulary", "词汇表达"))),
    "speaking": ("口语", (("interaction", "互动回应"), ("extended_turn", "连续表达"), ("picture", "图片描述"), ("intelligibility", "表达可理解度"))),
}

COMPETITION_DOMAINS = {
    "number_sense": "数感与估算",
    "arithmetic_structure": "运算结构与巧算",
    "number_theory": "整除、因数与余数",
    "pattern": "规律发现与归纳",
    "counting": "有序计数与组合",
    "logic": "条件分析与逻辑推理",
    "geometry": "几何分割、重组与度量",
    "modeling": "应用建模",
    "expression": "数学表达与论证",
    "transfer": "陌生情境迁移",
}

JUNIOR_DOMAINS = {
    "语文": ("现代文信息提取", "文学文本鉴赏", "古诗文理解", "语言运用", "写作立意与结构"),
    "数学": ("数与式", "方程与不等式", "函数", "几何证明", "统计与概率", "综合建模"),
    "英语": ("听力", "词汇语法", "阅读理解", "任务型阅读", "书面表达"),
    "物理": ("力与运动", "密度压强浮力", "功和机械能", "电学", "实验探究"),
    "化学": ("物质构成", "化学用语", "化学方程式", "常见物质", "实验与计算"),
    "道德与法治": ("核心概念", "材料分析", "观点论证", "实践应用"),
    "历史": ("时空线索", "史实理解", "材料解释", "因果与影响", "比较与评价"),
}

NODE_DOMAIN_HINTS = {
    "node-j3-math-quadratic-formula": "junior_exam.数学.方程与不等式",
    "node-j3-math-discriminant": "junior_exam.数学.方程与不等式",
    "node-j3-chinese-reading-evidence": "junior_exam.语文.现代文信息提取",
    "node-j3-english-reading-purpose": "junior_exam.英语.阅读理解",
    "node-j3-physics-experiment-reasoning": "junior_exam.物理.实验探究",
    "node-j3-chem-equation": "junior_exam.化学.化学方程式",
    "node-j3-civics-material": "junior_exam.道德与法治.材料分析",
    "node-j3-history-causality": "junior_exam.历史.因果与影响",
    "node-pet-reading-main-idea": "pet.reading.main_idea",
    "node-pet-writing-email": "pet.writing.task_coverage",
    "node-pet-listening-detail": "pet.listening.detail",
    "node-pet-speaking-picture": "pet.speaking.picture",
    "node-pet-speaking-read-aloud": "pet.speaking.intelligibility",
    "node-oly-number-sense": "competition_math.number_sense",
    "node-oly-arithmetic-structure": "competition_math.arithmetic_structure",
    "node-oly-number-theory": "competition_math.number_theory",
    "node-oly-pattern": "competition_math.pattern",
    "node-oly-counting": "competition_math.counting",
    "node-oly-logic": "competition_math.logic",
    "node-oly-area": "competition_math.geometry",
    "node-oly-modeling": "competition_math.modeling",
    "node-oly-expression": "competition_math.expression",
    **SPECIALIZED_DOMAIN_HINTS,
}


class SpecializedProgramError(ValueError):
    pass


@snapshot_read
def report_reference_fingerprint(connection: sqlite3.Connection, child_id: str) -> str:
    """Conservative report invalidation token, never a score or mastery certificate.

    The caller owns schema setup and transaction lifetime. No projection cache or
    generated roadmap is an input, so report rebuilds cannot invalidate themselves.
    """
    from .learning_obligations import project_child_mastery_evidence
    from .local_supply import LocalSupplyError, validate_item

    now = _now()
    child = connection.execute("SELECT grade,status FROM child_profile WHERE child_id=?", (child_id,)).fetchone()
    if not child:
        raise KeyError(child_id)
    tracks = ("pet", "competition_math") if child["grade"] == "四年级" else ("school",)
    projections = project_child_mastery_evidence(connection, child_id, as_of=now)
    cutoff = datetime.fromisoformat(now) - timedelta(days=90)
    learning = {node: {
        "fingerprint": projection["evidence_fingerprint"],
        "current_refs": sorted(r["evidence_id"] for r in projection["effective_evidence"]
                               if datetime.fromisoformat(r["occurred_at"]) >= cutoff),
    } for node, projection in projections.items()}
    catalog = []
    for row in connection.execute("""SELECT di.diagnostic_item_id,kn.knowledge_node_id,kn.subject,kn.title,kn.track
        FROM diagnostic_item di JOIN knowledge_node kn USING(knowledge_node_id)
        WHERE di.status='active' ORDER BY di.diagnostic_item_id"""):
        if row["track"] not in tracks:
            continue
        try:
            validate_item(connection, child_id, row["diagnostic_item_id"])
        except (LocalSupplyError, ValueError, TypeError):
            continue
        catalog.append(dict(row))

    def optional_rows(table, query, parameters):
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            return []
        return [dict(row) for row in connection.execute(query, parameters)]

    from .specialized_stages import project_specialized_stages
    stage_tracks = ("pet", "competition_math") if child["grade"] == "四年级" else ("junior_exam",)
    payload = {
        "version": "specialized_report_reference_v1", "child_id": child_id,
        "qualified_stage_fingerprints": {track: project_specialized_stages(connection, child_id, track, as_of=now)["evidence_fingerprint"]
            for track in stage_tracks} if child["status"] == "active" else {},
        "child": dict(child), "learning": learning, "catalog": catalog,
        # Raw bindings detect tampering; only the shared projection qualifies scores.
        "runs": optional_rows("specialized_assessment_run", """SELECT run_id,session_id,track,catalog_track,
            subject,phase,item_queue_json,status,started_at,completed_at FROM specialized_assessment_run
            WHERE child_id=? ORDER BY run_id""", (child_id,)),
        "attachments": optional_rows("specialized_assessment_run_attempt", """SELECT a.*
            FROM specialized_assessment_run_attempt a JOIN specialized_assessment_run r USING(run_id)
            WHERE r.child_id=? ORDER BY a.run_id,a.attempt_id""", (child_id,)),
        "attached_receipts": optional_rows("specialized_assessment_run_attempt", """SELECT DISTINCT da.*
            FROM specialized_assessment_run_attempt a JOIN specialized_assessment_run r USING(run_id)
            JOIN diagnostic_attempt da ON da.attempt_id=a.attempt_id
            WHERE r.child_id=? ORDER BY da.attempt_id""", (child_id,)),
        "run_sessions": optional_rows("specialized_assessment_run", """SELECT DISTINCT ds.session_id,ds.child_id
            FROM daily_session ds JOIN specialized_assessment_run r ON r.session_id=ds.session_id
            WHERE r.child_id=? ORDER BY ds.session_id""", (child_id,)),
        "assessments": optional_rows("specialized_assessment_event", """SELECT assessment_id,track,subject,
            assessment_type,occurred_at,score,max_score,domain_results_json,source_ref
            FROM specialized_assessment_event WHERE child_id=? AND julianday(occurred_at)<=julianday(?)
            ORDER BY assessment_id""", (child_id, now)),
        "goals": optional_rows("goal_program", """SELECT program_id,track,target_statement,target_horizon_json,
            default_weekly_minutes,default_weekly_sessions FROM goal_program
            WHERE child_id=? AND status='active' ORDER BY program_id""", (child_id,)),
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True,
                                     separators=(",", ":")).encode()).hexdigest()
    return payload["version"] + ":" + digest


class SpecializedProgramService:
    """Evidence-led roadmaps for the two family-specific long-term programs."""

    def __init__(self, database: Database):
        self.database = database

    def ensure_schema(self) -> None:
        with self.database.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS specialized_assessment_event (
                  assessment_id TEXT PRIMARY KEY,
                  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                  track TEXT NOT NULL,
                  subject TEXT NOT NULL,
                  assessment_type TEXT NOT NULL,
                  occurred_at TEXT NOT NULL,
                  score REAL,
                  max_score REAL,
                  domain_results_json TEXT NOT NULL,
                  source_ref TEXT,
                  notes TEXT,
                  client_action_id TEXT NOT NULL UNIQUE,
                  created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_specialized_assessment_child_track
                  ON specialized_assessment_event(child_id, track, occurred_at DESC);
                CREATE TABLE IF NOT EXISTS specialized_program_snapshot (
                  snapshot_id TEXT PRIMARY KEY,
                  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                  track TEXT NOT NULL,
                  snapshot_date TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  payload_hash TEXT NOT NULL,
                  generated_at TEXT NOT NULL,
                  UNIQUE(child_id, track, snapshot_date)
                );
                CREATE TABLE IF NOT EXISTS specialized_assessment_run (
                  run_id TEXT PRIMARY KEY,
                  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                  session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
                  plan_item_id TEXT REFERENCES plan_item(plan_item_id) ON DELETE SET NULL,
                  track TEXT NOT NULL,
                  catalog_track TEXT NOT NULL,
                  subject TEXT,
                  phase TEXT NOT NULL,
                  item_queue_json TEXT NOT NULL,
                  status TEXT NOT NULL CHECK(status IN ('active','completed','cancelled')),
                  started_at TEXT NOT NULL,
                  completed_at TEXT,
                  client_action_id TEXT NOT NULL UNIQUE,
                  UNIQUE(plan_item_id)
                );
                CREATE INDEX IF NOT EXISTS idx_specialized_run_session
                  ON specialized_assessment_run(session_id, status, started_at DESC);
                CREATE TABLE IF NOT EXISTS specialized_assessment_run_attempt (
                  run_id TEXT NOT NULL REFERENCES specialized_assessment_run(run_id) ON DELETE CASCADE,
                  attempt_id TEXT NOT NULL REFERENCES diagnostic_attempt(attempt_id) ON DELETE CASCADE,
                  diagnostic_item_id TEXT NOT NULL REFERENCES diagnostic_item(diagnostic_item_id),
                  domain_id TEXT NOT NULL,
                  correctness REAL NOT NULL,
                  created_at TEXT NOT NULL,
                  PRIMARY KEY(run_id, attempt_id),
                  UNIQUE(run_id, diagnostic_item_id)
                );
                """
            )
            connection.commit()

    def tracks_for_child(self, child_id: str) -> tuple[str, ...]:
        with self.database.connect() as connection:
            child = connection.execute(
                "SELECT grade FROM child_profile WHERE child_id=? AND status='active'", (child_id,)
            ).fetchone()
        if not child:
            raise KeyError(child_id)
        return ("pet", "competition_math") if child["grade"] == "四年级" else ("junior_exam",)

    def record_assessment(
        self,
        child_id: str,
        track: str,
        subject: str,
        assessment_type: str,
        domain_results: dict[str, float],
        client_action_id: str,
        score: float | None = None,
        max_score: float | None = None,
        occurred_at: str | None = None,
        source_ref: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        self.ensure_schema()
        allowed = self.tracks_for_child(child_id)
        if track not in allowed:
            raise SpecializedProgramError("该专项不适用于当前孩子。")
        valid_domains = self._domain_ids(track, subject)
        if not domain_results or any(key not in valid_domains for key in domain_results):
            raise SpecializedProgramError("测评能力域与专项定义不匹配。")
        normalized = {key: float(value) for key, value in domain_results.items()}
        if any(not math.isfinite(value) or value < 0 or value > 1 for value in normalized.values()):
            raise SpecializedProgramError("能力结果必须在0到1之间。")
        if (score is None) != (max_score is None) or (max_score is not None and max_score <= 0):
            raise SpecializedProgramError("总分与满分必须同时提供，且满分大于0。")
        if score is not None and (not math.isfinite(score) or not math.isfinite(max_score)
                                  or not 0 <= score <= max_score):
            raise SpecializedProgramError("参考总分必须是满分范围内的有限数值。")
        if assessment_type.startswith("guided_"):
            if score is not None:
                raise SpecializedProgramError("连续测评不能附加未经作答重算的总分。")
            try:
                run = self.guided_status(source_ref, _reconcile=False)
            except (KeyError, TypeError) as error:
                raise SpecializedProgramError("连续测评必须绑定真实执行记录。") from error
            if (run["child_id"] != child_id or run["track"] != track or run["subject"] != subject
                    or assessment_type != f"guided_{run['phase']}"
                    or run["status"] != "completed" or not run["domain_results"]
                    or run["domain_results"] != normalized):
                raise SpecializedProgramError("连续测评汇总与当前有效作答不匹配。")
        now = _now()
        assessment_id = str(uuid4())
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = connection.execute(
                "SELECT * FROM specialized_assessment_event WHERE client_action_id=?",
                (client_action_id,),
            ).fetchone()
            if replay:
                if (replay["child_id"], replay["track"], replay["subject"], replay["assessment_type"]) != (
                        child_id, track, subject, assessment_type):
                    raise SpecializedProgramError("测评操作标识不属于当前孩子与测评范围。")
                return self._assessment_dict(replay)
            connection.execute(
                """
                INSERT INTO specialized_assessment_event(
                  assessment_id, child_id, track, subject, assessment_type, occurred_at,
                  score, max_score, domain_results_json, source_ref, notes,
                  client_action_id, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    assessment_id, child_id, track, subject, assessment_type,
                    occurred_at or now, score, max_score,
                    json.dumps(normalized, ensure_ascii=False, sort_keys=True),
                    source_ref, notes, client_action_id, now,
                ),
            )
            connection.execute(
                """
                INSERT INTO audit_event(audit_id, actor, action, child_id, summary_json, occurred_at)
                VALUES (?, 'parent', 'specialized_assessment_recorded', ?, ?, ?)
                """,
                (
                    str(uuid4()), child_id,
                    json.dumps({"assessment_id": assessment_id, "track": track, "subject": subject}, ensure_ascii=False),
                    now,
                ),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM specialized_assessment_event WHERE assessment_id=?", (assessment_id,)
            ).fetchone()
        return self._assessment_dict(row)

    def roadmaps(self, child_id: str) -> list[dict[str, Any]]:
        self.ensure_schema()
        return [self.roadmap(child_id, track) for track in self.tracks_for_child(child_id)]

    def start_for_plan_item(self, plan_item_id: str, client_action_id: str) -> dict[str, Any] | None:
        """Start a restart-safe, voice-driven multi-item assessment for a specialized plan item."""
        self.ensure_schema()
        with self.database.connect() as connection:
            item = connection.execute(
                """SELECT pi.plan_item_id,pi.source_type,pi.source_ref,pi.subject,pi.status,dp.session_id,dp.child_id
                   FROM plan_item pi JOIN daily_plan dp ON dp.plan_id=pi.plan_id
                   WHERE pi.plan_item_id=?""", (plan_item_id,),
            ).fetchone()
            if not item or item["status"] != "active":
                return None
            if item["source_type"] == "competition_math":
                track, catalog_track, subject = "competition_math", "competition_math", "数学"
            elif item["source_type"] == "junior_exam_goal":
                track, catalog_track, subject = "junior_exam", "school", item["subject"]
            else:
                return None
            existing = connection.execute(
                "SELECT run_id FROM specialized_assessment_run WHERE plan_item_id=?", (plan_item_id,)
            ).fetchone()
            if existing:
                return self.guided_status(existing["run_id"])
            roadmap = self.roadmap(item["child_id"], track)
            ranked_domains = [row["domain_id"] for row in sorted(
                roadmap["domains"],
                key=_domain_priority,
            ) if not subject or row["subject"] == subject]
            # Goal-program items carry the intended ability domain in source_ref.
            # Honor it before broad baseline rotation so a child receives the
            # task that today's plan promised.
            preferred_domain = str(item["source_ref"] or "")
            if preferred_domain in ranked_domains:
                ranked_domains = [preferred_domain] + [value for value in ranked_domains if value != preferred_domain]
            candidates = connection.execute(
                """SELECT di.diagnostic_item_id,di.difficulty,kn.knowledge_node_id,kn.subject,kn.title,
                          COUNT(da.attempt_id) AS child_attempt_count, MAX(da.created_at) AS last_attempt_at
                   FROM diagnostic_item di JOIN knowledge_node kn ON kn.knowledge_node_id=di.knowledge_node_id
                   LEFT JOIN diagnostic_attempt da ON da.diagnostic_item_id=di.diagnostic_item_id AND da.child_id=?
                   WHERE di.status='active' AND kn.track=? AND (? IS NULL OR kn.subject=?)
                     AND (kn.track!='school' OR kn.grade_band='初三')
                   GROUP BY di.diagnostic_item_id,di.difficulty,kn.knowledge_node_id,kn.subject,kn.title
                   ORDER BY child_attempt_count, last_attempt_at, di.difficulty,di.diagnostic_item_id""",
                (item["child_id"], catalog_track, subject, subject),
            ).fetchall()
            by_domain: dict[str, list[dict[str, Any]]] = {}
            for candidate in candidates:
                from .local_supply import LocalSupplyError, validate_item
                try:
                    validate_item(connection, item["child_id"], candidate["diagnostic_item_id"])
                except (LocalSupplyError, ValueError, TypeError):
                    continue
                domain_id = self._mastery_domain(track, candidate)
                if domain_id:
                    by_domain.setdefault(domain_id, []).append(dict(candidate))
            queue: list[dict[str, Any]] = []
            for domain_id in ranked_domains:
                for candidate in by_domain.get(domain_id, []):
                    queue.append({"diagnostic_item_id": candidate["diagnostic_item_id"], "domain_id": domain_id})
                    if len(queue) >= 3:
                        break
                if len(queue) >= 3:
                    break
            if not queue:
                return None
            run_id, now = str(uuid4()), _now()
            connection.execute(
                """INSERT INTO specialized_assessment_run(
                     run_id,child_id,session_id,plan_item_id,track,catalog_track,subject,phase,
                     item_queue_json,status,started_at,client_action_id
                   ) VALUES(?,?,?,?,?,?,?,?,?,'active',?,?)""",
                (run_id,item["child_id"],item["session_id"],plan_item_id,track,catalog_track,subject,
                 roadmap["phase"],json.dumps(queue,ensure_ascii=False),now,client_action_id),
            )
            connection.commit()
        return self.guided_status(run_id)

    def active_guided_for_session(self, session_id: str) -> dict[str, Any] | None:
        self.ensure_schema()
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT run_id FROM specialized_assessment_run
                   WHERE session_id=? AND status='active' ORDER BY started_at DESC LIMIT 1""", (session_id,)
            ).fetchone()
            if row and not self._run_owner_active(connection, row["run_id"]):
                return None
        return self.guided_status(row["run_id"]) if row else None

    @staticmethod
    def _run_owner_active(connection, run_id):
        # Rescheduling and the legacy cancel journal are separate commits. The
        # original plan owner, not just the run flag, must still permit capture.
        return connection.execute("""SELECT 1 FROM specialized_assessment_run r
            LEFT JOIN plan_item pi ON pi.plan_item_id=r.plan_item_id
            LEFT JOIN daily_plan dp ON dp.plan_id=pi.plan_id AND dp.child_id=r.child_id AND dp.session_id=r.session_id
            JOIN daily_session ds ON ds.session_id=r.session_id AND ds.child_id=r.child_id
            JOIN child_profile cp ON cp.child_id=r.child_id
            WHERE r.run_id=? AND r.status='active' AND ds.status='active' AND cp.status='active'
              AND (r.plan_item_id IS NULL OR (pi.status IN ('active','paused') AND dp.status='active'))
              AND ds.state NOT IN ('evolving','completed')""", (run_id,)).fetchone() is not None

    def cancel_for_plan_item(self, plan_item_id: str, reason: str = "plan_item_rescheduled") -> dict[str, Any] | None:
        """Close an unfinished assessment so it cannot capture later answers."""
        self.ensure_schema()
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT run_id,status FROM specialized_assessment_run WHERE plan_item_id=?",
                (plan_item_id,),
            ).fetchone()
            if not row:
                return None
            if row["status"] == "active":
                now = _now()
                connection.execute(
                    "UPDATE specialized_assessment_run SET status='cancelled',completed_at=? WHERE run_id=?",
                    (now, row["run_id"]),
                )
                connection.execute(
                    """INSERT INTO audit_event(audit_id,actor,action,child_id,summary_json,occurred_at)
                       SELECT ?, 'system', 'specialized_assessment_cancelled', child_id, ?, ?
                       FROM specialized_assessment_run WHERE run_id=?""",
                    (
                        str(uuid4()),
                        json.dumps({"run_id": row["run_id"], "reason": reason}, ensure_ascii=False),
                        now,
                        row["run_id"],
                    ),
                )
                connection.commit()
        return self.guided_status(row["run_id"])

    def guided_status(
        self, run_id: str, *, _reconcile: bool = True,
        _connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        """Project current receipts; a supplied connection is borrowed read-only."""
        if _connection is None:
            self.ensure_schema()
        else:
            # The caller owns schema setup and transaction lifetime, including rollback.
            _reconcile = False
        with nullcontext(_connection) if _connection is not None else self.database.connect() as connection:
            run = connection.execute("SELECT * FROM specialized_assessment_run WHERE run_id=?", (run_id,)).fetchone()
            if not run:
                raise KeyError(run_id)
            owner_blocked = run["status"] == "active" and not self._run_owner_active(connection, run_id)
            queue = json.loads(run["item_queue_json"])
            prior_assessment = connection.execute(
                "SELECT assessment_id FROM specialized_assessment_event WHERE child_id=? AND source_ref=? "
                "AND track=? AND assessment_type LIKE 'guided_%' LIMIT 1",
                (run["child_id"], run_id, run["track"]),
            ).fetchone()
            attempts = connection.execute(
                """SELECT attempt_id,diagnostic_item_id,domain_id,correctness,created_at
                   FROM specialized_assessment_run_attempt WHERE run_id=? ORDER BY created_at""", (run_id,)
            ).fetchall()
            from .learning_obligations import project_child_mastery_evidence, withdrawn_evidence_ids
            from .learning_state import LearningStateService
            from .local_supply import LocalSupplyError, validate_item
            accepted, references, projections = [], [], None
            withdrawn = withdrawn_evidence_ids(connection, run["child_id"])
            queued = {item["diagnostic_item_id"]: item["domain_id"] for item in queue}
            for attempt in attempts:
                source = connection.execute("""SELECT da.*,le.knowledge_node_id,le.correctness AS evidence_score,
                    kn.subject,kn.title,kn.track AS node_track
                    FROM diagnostic_attempt da JOIN learning_evidence le USING(evidence_id)
                    JOIN daily_session ds ON ds.session_id=da.session_id AND ds.child_id=da.child_id
                    JOIN knowledge_node kn ON kn.knowledge_node_id=le.knowledge_node_id
                    WHERE da.attempt_id=? AND da.child_id=? AND da.session_id=?
                      AND da.diagnostic_item_id=? AND le.child_id=da.child_id AND le.session_id=da.session_id
                      AND julianday(da.created_at)>=julianday(?) AND julianday(da.created_at)<=julianday(?)""",
                    (attempt["attempt_id"], run["child_id"], run["session_id"], attempt["diagnostic_item_id"],
                     run["started_at"], run["completed_at"] or _now())).fetchone()
                if (not source or source["evidence_id"] in withdrawn
                        or source["node_track"] != run["catalog_track"]
                        or queued.get(attempt["diagnostic_item_id"]) != attempt["domain_id"]
                        or self._mastery_domain(run["track"], source) != attempt["domain_id"]):
                    continue
                try:
                    item = validate_item(connection, run["child_id"], attempt["diagnostic_item_id"])
                    score, _ = LearningStateService._score_answer(item, source["submitted_answer"])
                except (LocalSupplyError, ValueError, TypeError, KeyError):
                    continue
                if (item["knowledge_node_id"] != source["knowledge_node_id"] or type(score) not in (int,float) or not math.isfinite(score)
                        or not 0 <= score <= 1 or score != source["correctness"] or score != source["evidence_score"]):
                    continue
                references.append({**dict(attempt), "correctness": score})
                node = source["knowledge_node_id"]
                if projections is None:
                    projections = project_child_mastery_evidence(connection, run["child_id"])
                receipt = next((item for item in projections.get(node, {}).get("effective_evidence", [])
                                if item["evidence_id"] == source["evidence_id"]), None)
                if receipt and receipt["independent"] and not receipt["hint_level"]:
                    accepted.append({**dict(attempt), "correctness": receipt["correctness"]})
            completed_ids = {row["diagnostic_item_id"] for row in attempts}
            blocked_ids = []
            for item in queue:
                if item["diagnostic_item_id"] in completed_ids:
                    continue
                try:
                    validate_item(connection, run["child_id"], item["diagnostic_item_id"])
                except (LocalSupplyError, ValueError, TypeError):
                    blocked_ids.append(item["diagnostic_item_id"])
        pending = next((item for item in queue if item["diagnostic_item_id"] not in completed_ids), None)
        blocked = pending and pending["diagnostic_item_id"] in blocked_ids and run["status"] == "active"
        result = {
            **{key: run[key] for key in ("run_id","child_id","session_id","plan_item_id","track","catalog_track","subject","phase","status","started_at","completed_at")},
            "total_items": len(queue), "completed_items": len(attempts),
            "next_item_id": pending["diagnostic_item_id"] if pending and run["status"] == "active" and not blocked and not owner_blocked else None,
            "execution_status": "blocked_owner_inactive" if owner_blocked else "blocked_source_unavailable" if blocked else run["status"],
            "blocked_item_ids": blocked_ids,
            "domain_results": self._aggregate_attempts(accepted),
            "reference_domain_results": self._aggregate_attempts(references),
            "qualified_items": len(accepted), "unverified_items": len(attempts) - len(accepted),
            "reference_items": len(references), "invalidated_items": len(attempts) - len(references),
            "qualification_denominator": len(accepted),
            "domain_result_basis": "current_qualified_independent_receipts_only",
            "reference_score_basis": "rescored_current_sources_including_unverified_rubrics_not_ability",
            "completion_scope": "execution_only",
            "learning_status": "observed_checks_not_mastery" if accepted else "needs_verification",
            "mastery_claim_allowed": False,
            "goal_attainment": "not_established",
        }
        if _reconcile and run["status"] == "completed":
            result["assessment_status"] = "invalidated" if prior_assessment else "not_recorded_unqualified"
            if prior_assessment:
                result["assessment_id"] = prior_assessment["assessment_id"]
            if result["domain_results"]:
                try:
                    assessment = self.record_assessment(
                        child_id=run["child_id"], track=run["track"], subject=run["subject"] or "",
                        assessment_type=f"guided_{run['phase']}", domain_results=result["domain_results"],
                        client_action_id=f"guided-assessment:{run['run_id']}", source_ref=run["run_id"],
                        notes="执行记录完成；仅汇总当前合格独立作答，不代表掌握或达标。",
                    )
                    result.update(assessment_status="recorded", assessment_id=assessment["assessment_id"])
                except Exception:
                    # Execution is already durable. A read/retry can reconcile the projection.
                    result["assessment_status"] = "pending_retry"
        return result

    def capture_guided_attempt(self, session_id: str, attempt: dict[str, Any]) -> dict[str, Any] | None:
        self.ensure_schema()
        with self.database.connect() as connection:
            if not connection.execute("""SELECT 1 FROM diagnostic_attempt da
                JOIN daily_session ds ON ds.session_id=da.session_id AND ds.child_id=da.child_id
                WHERE da.attempt_id=? AND da.session_id=?""", (attempt["attempt_id"], session_id)).fetchone():
                raise SpecializedProgramError("测评作答不属于原孩子与会话。")
            previous = connection.execute(
                """SELECT r.run_id FROM specialized_assessment_run_attempt a
                JOIN specialized_assessment_run r ON r.run_id=a.run_id
                JOIN diagnostic_attempt da ON da.attempt_id=a.attempt_id
                WHERE a.attempt_id=? AND r.session_id=? AND da.session_id=r.session_id AND da.child_id=r.child_id""",
                (attempt["attempt_id"], session_id),
            ).fetchone()
        if previous:
            return self.guided_status(previous["run_id"])
        run = self.active_guided_for_session(session_id)
        if not run:
            return None
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            from .learning_state import LearningStateService
            saved = LearningStateService._attempt(connection, attempt["attempt_id"])
            if saved["session_id"] != session_id or saved["child_id"] != run["child_id"]:
                raise SpecializedProgramError("测评作答不属于当前孩子与会话。")
            attempt = saved
            row = connection.execute("SELECT item_queue_json,status,started_at FROM specialized_assessment_run WHERE run_id=?", (run["run_id"],)).fetchone()
            if row["status"] != "active" or not self._run_owner_active(connection, run["run_id"]):
                connection.rollback()
                return self.guided_status(run["run_id"])
            if not connection.execute("SELECT 1 WHERE julianday(?)>=julianday(?) AND julianday(?)<=julianday(?)",
                                      (saved["created_at"], row["started_at"], saved["created_at"], _now())).fetchone():
                raise SpecializedProgramError("测评不能采纳本次执行前或未来的作答。")
            queue = json.loads(row["item_queue_json"])
            queued = next((item for item in queue if item["diagnostic_item_id"] == attempt["diagnostic_item_id"]), None)
            if not queued:
                return run
            from .local_supply import LocalSupplyError, validate_item
            try:
                validate_item(connection,run['child_id'],attempt['diagnostic_item_id'])
            except LocalSupplyError:
                return {**self.guided_status(run['run_id'],_connection=connection),
                        'capture_status':'blocked_source_unavailable','next_item_id':None}
            if type(attempt['correctness']) not in (int,float) or not math.isfinite(attempt['correctness']):
                # The original response already exists; never fabricate a score
                # merely to satisfy this older numeric execution journal.
                return {**self.guided_status(run['run_id'],_connection=connection),
                        'capture_status':'needs_review','next_item_id':None}
            connection.execute(
                """INSERT OR IGNORE INTO specialized_assessment_run_attempt(
                     run_id,attempt_id,diagnostic_item_id,domain_id,correctness,created_at
                   ) VALUES(?,?,?,?,?,?)""",
                (run["run_id"],attempt["attempt_id"],attempt["diagnostic_item_id"],queued["domain_id"],float(attempt["correctness"]),attempt["created_at"]),
            )
            count = connection.execute("SELECT COUNT(*) FROM specialized_assessment_run_attempt WHERE run_id=?", (run["run_id"],)).fetchone()[0]
            completed = int(count) >= len(queue)
            if completed:
                connection.execute("UPDATE specialized_assessment_run SET status='completed',completed_at=? WHERE run_id=?", (_now(),run["run_id"]))
            connection.commit()
        status = self.guided_status(run["run_id"])
        if completed:
            status["roadmap"] = self.roadmap(status["child_id"], status["track"])
        return status

    @staticmethod
    def _aggregate_attempts(attempts) -> dict[str, float]:
        grouped: dict[str, list[float]] = {}
        for row in attempts:
            grouped.setdefault(row["domain_id"], []).append(float(row["correctness"]))
        return {key: round(sum(values) / len(values), 3) for key, values in grouped.items()}

    def next_agent_action(self, child_id: str, track: str) -> dict[str, Any] | None:
        """Return a stage-bound action or an explicit source/scoring block."""
        roadmap = self.roadmap(child_id, track)
        candidates = [item for item in roadmap["domains"] if item["actionable"]]
        if not candidates:
            return None
        candidates.sort(key=_domain_priority)
        return self._next_action(track, candidates[0], 0)

    def stage_progress(self, child_id: str, track: str, *, as_of: str | None = None) -> dict[str, Any]:
        """Read current qualified stages without creating tasks or snapshot/schema writes."""
        from .specialized_stages import project_specialized_stages
        with self.database.connect() as connection:
            return project_specialized_stages(connection, child_id, track, as_of=as_of)

    def roadmap(self, child_id: str, track: str) -> dict[str, Any]:
        self.ensure_schema()
        if track not in self.tracks_for_child(child_id):
            raise SpecializedProgramError("该专项不适用于当前孩子。")
        domains = self._domains(track)
        with self.database.connect() as connection:
            completed_runs = connection.execute(
                "SELECT run_id FROM specialized_assessment_run WHERE child_id=? AND track=? AND status='completed'",
                (child_id, track),
            ).fetchall()
        run_statuses = {row["run_id"]: self.guided_status(row["run_id"]) for row in completed_runs}
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            assessments = connection.execute(
                """
                SELECT * FROM specialized_assessment_event
                WHERE child_id=? AND track=? AND julianday(occurred_at)<=julianday(?) ORDER BY occurred_at DESC
                """,
                (child_id, track, _now()),
            ).fetchall()
            exam_results = {}
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name='specialized_exam_run'").fetchone():
                from .exam_simulation import ExamSimulationService
                for assessment in assessments:
                    if not assessment['client_action_id'].startswith('exam-assessment:'):
                        continue
                    run = connection.execute('SELECT * FROM specialized_exam_run WHERE run_id=? AND child_id=? AND track=? AND subject=?',
                        (assessment['source_ref'],child_id,track,assessment['subject'])).fetchone()
                    if (run and run['status'] in {'completed','timed_out'}
                            and assessment['client_action_id']=='exam-assessment:'+run['run_id']
                            and assessment['assessment_type']==f"{run['assessment_scope'] or run['mode']}_paper_{run['status']}"):
                        exam_results[assessment['assessment_id']] = ExamSimulationService.project_current_result(connection,run)
            from .learning_feedback import build_learning_feedback
            learning = build_learning_feedback(connection, child_id)
            from .specialized_stages import project_specialized_stages
            stages = project_specialized_stages(connection, child_id, track, as_of=_now())
            catalog_track = "school" if track == "junior_exam" else track
            diagnostic_nodes = connection.execute(
                """SELECT di.diagnostic_item_id,kn.knowledge_node_id,kn.subject,kn.title
                   FROM diagnostic_item di JOIN knowledge_node kn ON kn.knowledge_node_id=di.knowledge_node_id
                   WHERE di.status='active' AND kn.track=?""", (catalog_track,)
            ).fetchall()
            from .local_supply import LocalSupplyError, validate_item
            owned_nodes = []
            for row in diagnostic_nodes:
                try:
                    validate_item(connection, child_id, row["diagnostic_item_id"])
                except (LocalSupplyError, ValueError, TypeError):
                    continue
                owned_nodes.append(row)
            try:
                goal = connection.execute(
                    """SELECT target_statement,target_horizon_json,default_weekly_minutes,default_weekly_sessions
                       FROM goal_program WHERE child_id=? AND track=? AND status='active'""",
                    (child_id, {"pet": "pet_path", "competition_math": "competition_math_path", "junior_exam": "junior_exam_readiness"}[track]),
                ).fetchone()
            except Exception:
                goal = None
        diagnostic_counts: dict[str, int] = {}
        for row in owned_nodes:
            domain_id = self._mastery_domain(track, row)
            if domain_id:
                diagnostic_counts[domain_id] = diagnostic_counts.get(domain_id, 0) + 1
        latest: dict[str, tuple[float, str, str]] = {}
        current_assessments = []
        as_of = datetime.fromisoformat(_now())
        for row in assessments:
            results = json.loads(row["domain_results_json"])
            evidence_type = "parent_reported_assessment"
            if row["assessment_type"].startswith("guided_"):
                current = run_statuses.get(row["source_ref"])
                results = current["domain_results"] if current and current["track"] == track else {}
                evidence_type = "qualified_guided_assessment"
            elif row['client_action_id'].startswith('exam-assessment:'):
                results = exam_results.get(row['assessment_id'],{}).get('domain_results',{})
                evidence_type = 'qualified_exam_assessment'
            results = {key: float(value) for key, value in results.items()
                       if key in self._domain_ids(track, row["subject"])
                       and isinstance(value, (int, float)) and math.isfinite(value) and 0 <= value <= 1}
            occurred = datetime.fromisoformat(row["occurred_at"])
            occurred = occurred if occurred.tzinfo else occurred.replace(tzinfo=timezone.utc)
            expired = (as_of - occurred).total_seconds() > REFERENCE_PLANNING_DAYS * 86400
            current_assessments.append({**dict(row), "current_evidence_status": "expired_reference" if results and expired else "current_reference" if results else "invalidated",
                                        "reference_basis": evidence_type,
                                        'current_exam_result': exam_results.get(row['assessment_id'])})
            for domain_id, score in results.items():
                latest.setdefault(domain_id, (float(score), row["occurred_at"], evidence_type))
        mastery_values: dict[str, list[tuple[int, int]]] = {}
        domain_evidence: dict[str, list[dict]] = {}
        for row in learning["nodes"]:
            if row["track"] != catalog_track:
                continue
            domain_id = self._mastery_domain(track, row)
            if domain_id:
                domain_evidence.setdefault(domain_id, []).append(row)
            count = row["independent_attempt_count"]
            if domain_id and count:
                mastery_values.setdefault(domain_id, []).append(
                    (row["independent_correct"], count)
                )
        domain_rows = []
        exam_references = {}
        for assessment in current_assessments:
            result = assessment['current_exam_result']
            if result and assessment['current_evidence_status']=='current_reference':
                for key,value in result['domain_results'].items():
                    if key in self._domain_ids(track,assessment['subject']):
                        exam_references.setdefault(key,(round(float(value)*100),assessment['source_ref']))
        stage_domains = {row["domain_id"]: row for row in stages["domains"]}
        for domain_id, label, subject in domains:
            assessment = latest.get(domain_id)
            values = mastery_values.get(domain_id, [])
            observations = domain_evidence.get(domain_id, [])
            assessment_time = datetime.fromisoformat(assessment[1]) if assessment else None
            if assessment_time and not assessment_time.tzinfo:
                assessment_time = assessment_time.replace(tzinfo=timezone.utc)
            assessment_age = max(0, (as_of - assessment_time).total_seconds() / 86400) if assessment_time else None
            assessment_current = assessment_age is not None and assessment_age <= REFERENCE_PLANNING_DAYS
            mastery_score = (
                sum(correct for correct, _ in values) / sum(count for _, count in values)
                if values else None
            )
            if mastery_score is not None:
                score, evidence_type = mastery_score, "qualified_independent_observations"
            elif assessment and assessment_current:
                score, evidence_type = assessment[0], assessment[2]
            else:
                score, evidence_type = None, "none"
            priority_status = ("recheck_needed" if any(row["status"] == "recheck_needed" for row in observations)
                               else "supported_only" if any(row["status"] == "supported_only" for row in observations)
                               else "current_observations" if observations else "baseline_needed")
            conflict = bool(assessment and assessment_current and observations and
                            ((assessment[0] >= .8 and priority_status in {"recheck_needed", "supported_only"})
                             or (mastery_score is not None and abs(assessment[0] - mastery_score) >= .2)))
            domain_rows.append({
                "domain_id": domain_id,
                "subject": subject,
                "label": label,
                "reference_score": round(score * 100) if score is not None else None,
                "status": "observed" if score is not None and score >= .8 else "developing" if score is not None and score >= .6 else "weak" if score is not None else "baseline_needed",
                "status_scope": "reference_score_band_not_ability",
                "reference_score_basis": evidence_type,
                "observed_correctness": round(mastery_score, 4) if mastery_score is not None else None,
                "mastery_claim_allowed": False,
                "goal_attainment": "not_established",
                "evidence_type": evidence_type,
                "priority_status": priority_status,
                "evidence_conflict": conflict,
                "assessment_reference_score": round(assessment[0] * 100) if assessment else None,
                'exam_reference_score': exam_references.get(domain_id,(None,None))[0],
                'exam_reference_source_ref': exam_references.get(domain_id,(None,None))[1],
                'exam_reference_score_scope': 'current_exam_reference_only' if domain_id in exam_references else None,
                "assessment_age_days": round(assessment_age, 1) if assessment_age is not None else None,
                "assessment_planning_status": "current_reference" if assessment_current else "expired_reference" if assessment else "none",
                "priority_evidence_refs": sorted({ref for row in observations for ref in row["evidence_refs"]}),
                "latest_learning_evidence_at": max((row["latest_evidence_at"] for row in observations), default=None),
                "priority_reason": ("当前有效作答仍需核对；旧参考分不覆盖该证据。" if priority_status == "recheck_needed"
                                    else "最近作答需要支持，先减少提示再检查。" if priority_status == "supported_only"
                                    else "按当前合格独立作答安排；参考分另行保留。" if mastery_score is not None
                                    else "仅有近期参考测评，仍需要独立诊断。" if assessment_current
                                    else "尚无当前合格作答或近期参考测评，需要补基线。"),
                "assessment_at": assessment[1] if assessment else None,
                "learning_evidence_count": sum(item[1] for item in values),
                "diagnostic_item_count": diagnostic_counts.get(domain_id, 0),
                "actionable": diagnostic_counts.get(domain_id, 0) > 0,
                "stage_progress": stage_domains[domain_id],
            })
        evidenced = [row for row in domain_rows if row["reference_score"] is not None]
        coverage = len(evidenced) / max(1, len(domain_rows))
        readiness = round(sum(row["reference_score"] for row in evidenced) / len(evidenced)) if evidenced else None
        if stages["current_stage"] == "baseline":
            phase = "baseline"
        elif any(row["priority_status"] == "recheck_needed" for row in domain_rows):
            phase = "foundation"
        elif stages["current_stage"] == "near_transfer":
            phase = "strengthening"
        else:
            phase = "transfer_and_simulation"
        ranked = sorted(
            domain_rows,
            key=_domain_priority,
        )
        actionable_ranked = [row for row in ranked if row["actionable"]]
        next_actions = [self._next_action(track, row, index) for index, row in enumerate(actionable_ranked[:3])]
        catalog_domain_count = sum(1 for row in domain_rows if row["actionable"])
        generated_at = _now()
        target = self._target_context(goal, as_of=datetime.fromisoformat(generated_at))
        payload = {
            "child_id": child_id,
            "track": track,
            "phase": phase,
            "phase_scope": "planning_only_not_ability_certification",
            "reference_planning_policy": {"version": "current_receipt_priority_v1", "reference_window_days": REFERENCE_PLANNING_DAYS,
                "expired_reference_action": "recheck_not_forget", "qualified_observation_window_days": learning["window_days"]},
            "stage_requirements": {"independent_check_required": True, "distinct_transfer_required": True,
                "delayed_retention_required": True, "certified_stage": None,
                "reason": "本阶段仅安排训练，未由专项平均分或考试日期认证进阶。"},
            "stage_progress": stages,
            "reference_readiness": readiness,
            "reference_readiness_basis": "mean_of_known_domain_reference_scores_not_ability_or_attainment",
            "unknown_domain_count": len(domain_rows) - len(evidenced),
            "reference_denominator": len(evidenced),
            "mastery_claim_allowed": False,
            "goal_attainment": "not_established",
            "pending_assessment_run_ids": [key for key, status in run_statuses.items() if status.get("assessment_status") == "pending_retry"],
            "evidence_coverage": round(coverage, 3),
            "evidence_coverage_basis": "known_reference_domains_not_qualified_learning_coverage",
            "qualified_learning_domain_count": len(mastery_values),
            "evidence_domain_count": len(evidenced),
            "total_domain_count": len(domain_rows),
            "catalog_domain_count": catalog_domain_count,
            "catalog_coverage": round(catalog_domain_count / max(1, len(domain_rows)), 3),
            "catalog_gap_domains": [row["domain_id"] for row in domain_rows if not row["actionable"]],
            "target": target,
            "stage_plan": self._stage_strategy(track, phase, target),
            "assessment_history": self._assessment_history(current_assessments),
            "whole_exam_analysis": self._whole_exam_analysis(domain_rows, current_assessments),
            "forecast": self._forecast(track, coverage, readiness),
            "domains": domain_rows,
            "next_actions": next_actions,
            "generated_at": generated_at,
        }
        self._save_snapshot(payload)
        return payload

    @staticmethod
    def _target_context(goal, *, as_of: datetime | None = None) -> dict[str, Any] | None:
        if not goal:
            return None
        horizon = json.loads(goal["target_horizon_json"])
        target_date = horizon.get("target_date")
        days_remaining = None
        if target_date:
            try:
                days_remaining = (date.fromisoformat(target_date) - family_today(as_of)).days
            except ValueError:
                target_date = None
        return {
            "statement": goal["target_statement"], "target_date": target_date,
            "days_remaining": days_remaining, "target_score": horizon.get("target_score"),
            "target_score_label": horizon.get("target_score_label"),
            "weekly_minutes": int(goal["default_weekly_minutes"]),
            "weekly_sessions": int(goal["default_weekly_sessions"]),
        }

    @staticmethod
    def _stage_strategy(track: str, phase: str, target: dict[str, Any] | None) -> dict[str, Any]:
        remaining = target.get("days_remaining") if target else None
        if track == "junior_exam":
            if remaining is not None and remaining <= 45:
                stage, focus = "round_3_simulation", "整卷节奏、易错回看和稳定得分；不再无依据扩大范围。"
            elif remaining is not None and remaining <= 120:
                stage, focus = "round_2_integration", "跨章节专题、近迁移与限时小卷，继续修复高频错因。"
            else:
                stage, focus = "round_1_foundation", "按七科证据补基础与章节漏洞，形成可复测的错题链。"
        elif track == "pet":
            stage = "pet_format_and_transfer" if phase == "transfer_and_simulation" else "pet_skill_building" if phase != "baseline" else "pet_baseline"
            focus = "听说读写分项训练，并逐步加入PET题型与完整模拟。"
        else:
            stage = "competition_transfer" if phase == "transfer_and_simulation" else "competition_strategy_building"
            focus = "先形成画图、列表、分类和验证策略，再做陌生情境迁移与限时综合题。"
        return {"stage": stage, "focus": focus, "scope": "calendar_planning_not_qualified_stage",
                "qualification_source": "stage_progress", "mastery_claim_allowed": False,
                "reassessment_rule": "薄弱能力域完成讲解、近迁移和至少一次延迟独立复测后再升阶；整卷成绩只作补充证据。"}

    @staticmethod
    def _assessment_history(rows) -> list[dict[str, Any]]:
        history = []
        for row in reversed(rows[:12]):
            current_score = (row["reference_basis"] == "parent_reported_assessment"
                             and row["current_evidence_status"] == "current_reference")
            score, maximum = (row["score"], row["max_score"]) if current_score else (None, None)
            exam = row.get('current_exam_result')
            if exam and row['current_evidence_status']=='current_reference' and exam['score'] is not None:
                score,maximum = exam['score'],exam['max_score']
            percentage = round(float(score) / float(maximum) * 100, 1) if score is not None and maximum else None
            history.append({"assessment_id": row["assessment_id"], "assessment_type": row["assessment_type"], "subject": row["subject"], "occurred_at": row["occurred_at"], "score": score, "max_score": maximum, "percentage": percentage,
                            "historical_reference": {"score": row["score"], "max_score": row["max_score"]},
                            "current_evidence_status": row["current_evidence_status"], "reference_basis": row["reference_basis"],
                            'score_scope': 'current_exam_reference_only' if exam else 'parent_reported_reference_only',
                            'score_boundary': exam['score_boundary'] if exam else None,
                            "mastery_claim_allowed": False})
        scored = [item for item in history if item["percentage"] is not None]
        for index, item in enumerate(scored):
            item["change_from_previous"] = None if index == 0 else round(item["percentage"] - scored[index - 1]["percentage"], 1)
        return history

    @staticmethod
    def _whole_exam_analysis(domains: list[dict[str, Any]], assessments) -> dict[str, Any]:
        weak = [row for row in domains if row["reference_score"] is not None and row["reference_score"] < 60]
        developing = [row for row in domains if row["reference_score"] is not None and 60 <= row["reference_score"] < 80]
        latest_exam = next((row for row in assessments if row.get('current_exam_result') or
                            (row['current_evidence_status']=='current_reference' and row["reference_basis"] == "parent_reported_assessment"
                            and row["score"] is not None and row["max_score"])), None)
        latest = None
        exam_review = None
        if latest_exam:
            result = latest_exam.get('current_exam_result')
            score,maximum = (result['score'],result['max_score']) if result else (latest_exam['score'],latest_exam['max_score'])
            if result:
                exam_review = {key:result[key] for key in ('score_status','verified_items','answered_items','total_items')}
                exam_review.update(source_ref=latest_exam['source_ref'],current_evidence_status=latest_exam['current_evidence_status'])
            if score is not None and maximum and latest_exam['current_evidence_status']=='current_reference':
                latest = {key:latest_exam[key] for key in ('assessment_type','subject','occurred_at')}
                latest.update(score=score,max_score=maximum,percentage=round(float(score)/float(maximum)*100,1),
                    score_scope='current_exam_reference_only' if result else 'parent_reported_reference_only',
                    score_boundary=result['score_boundary'] if result else '家长录入的参考成绩，不代表掌握。',
                    mastery_claim_allowed=False)
        exam_weak = [row for row in domains if row.get('exam_reference_score') is not None and row['exam_reference_score'] < 60]
        exam_developing = [row for row in domains if row.get('exam_reference_score') is not None and 60 <= row['exam_reference_score'] < 80]
        def reference_rows(rows):
            return [dict(domain_id=row['domain_id'],subject=row['subject'],label=row['label'],
                reference_score=row['exam_reference_score'],source_ref=row['exam_reference_source_ref'],
                score_scope='current_exam_reference_only',mastery_claim_allowed=False) for row in rows]
        return {
            "latest_exam": latest,
            'latest_exam_review': exam_review,
            'exam_reference_weak_domains': reference_rows(exam_weak),
            'exam_reference_developing_domains': reference_rows(exam_developing),
            "weak_domains": [{"domain_id": row["domain_id"], "subject": row["subject"], "label": row["label"], "reference_score": row["reference_score"]} for row in weak],
            "developing_domains": [{"domain_id": row["domain_id"], "subject": row["subject"], "label": row["label"], "reference_score": row["reference_score"]} for row in developing],
            "interpretation": "能力域结果用于决定下一轮诊断和复测；一次整卷分数不用于推断能力上限。",
        }

    def _save_snapshot(self, payload: dict[str, Any]) -> None:
        stable = {key: value for key, value in payload.items() if key != "generated_at"}
        raw = json.dumps(stable, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        now = payload["generated_at"]
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO specialized_program_snapshot(
                  snapshot_id, child_id, track, snapshot_date, payload_json,
                  payload_hash, generated_at
                ) VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(child_id, track, snapshot_date) DO UPDATE SET
                  payload_json=excluded.payload_json,
                  payload_hash=excluded.payload_hash,
                  generated_at=excluded.generated_at
                WHERE specialized_program_snapshot.payload_hash != excluded.payload_hash
                """,
                (str(uuid4()), payload["child_id"], payload["track"],
                 family_today(datetime.fromisoformat(now)).isoformat(), raw, digest, now),
            )
            connection.commit()

    @staticmethod
    def _forecast(track: str, coverage: float, readiness: int | None) -> dict[str, Any]:
        if coverage < .6 or readiness is None:
            return {
                "status": "insufficient_evidence",
                "statement": "证据覆盖不足，先完成基线诊断，不预测考试结果或获奖结果。",
            }
        return {
            "status": "reference_only",
            "statement": f"已有记录的能力域参考分均值约{readiness}分；未知领域不计入分母，不代表能力、掌握、达标或考试结果概率。",
        }

    @staticmethod
    def _next_action(track: str, domain: dict[str, Any], index: int) -> dict[str, Any]:
        minutes = 12 if track == "pet" else 15 if track == "competition_math" else 18
        if domain.get("priority_status") == "recheck_needed":
            action = f"核对{domain['label']}最近出错的步骤，再用不同题目检查"
            kind = "remediation"
        elif domain.get('exam_reference_score') is not None and domain['exam_reference_score'] < 60:
            action = f"核对最近整卷{domain['label']}的原作答，再用不同题目检查"
            kind = 'remediation'
        elif domain.get("priority_status") == "supported_only":
            action = f"减少{domain['label']}的提示，再核对能否独立完成"
            kind = "independence_check"
        elif domain.get("priority_status") == "baseline_needed" or domain["status"] == "baseline_needed":
            action = f"基线诊断 · {domain['label']}，保存独立作答证据"
            kind = "baseline"
        elif domain["status"] == "weak":
            action = f"讲清{domain['label']}中的一个错误，再做一道近迁移题"
            kind = "remediation"
        else:
            action = f"完成一项{domain['label']}迁移任务并口述思路"
            kind = "transfer"
        stage_progress = domain.get("stage_progress")
        stage_fields = {}
        if stage_progress:
            nodes = sorted(stage_progress["nodes"], key=lambda n: (
                n["status"] != "recheck_needed",
                ("baseline", "near_transfer", "far_transfer", "delayed_retention", "maintenance").index(n["current_stage"]),
                n["knowledge_node_id"]))
            selected = nodes[0]["next_action"] if nodes else {}
            stage_fields = {key: selected.get(key) for key in ("child_id", "track", "diagnostic_item_id", "knowledge_node_id", "source_version", "review_task_id", "not_before")}
            stage_fields.update(execution_status=selected.get("execution_status", "blocked"),
                stage_constraint=selected.get("stage_constraint", {"version": "specialized_stages_v2",
                    "current_stage": "baseline", "qualified_stage": None,
                    "prerequisite_version": None, "evidence_fingerprint": None, "blockers": stage_progress["blockers"]}))
            if kind == "transfer":
                kind = selected.get("stage", "baseline")
                action = f"核对{domain['label']}的当前阶段，按已绑定题源保存独立作答"
            if selected.get("execution_status", "blocked") == "blocked":
                action = f"核对{domain['label']}的题源或评分缺口，暂不认证进阶"
        return {
            "priority": index + 1,
            "domain_id": domain["domain_id"],
            "subject": domain["subject"],
            "title": action,
            "minutes": minutes,
            "action_type": kind,
            "evidence_refs": domain.get("priority_evidence_refs", []),
            "selection_reason": domain.get("priority_reason"),
            "mastery_claim_allowed": False,
            'exam_reference_score': domain.get('exam_reference_score'),
            'exam_reference_score_scope': domain.get('exam_reference_score_scope'),
            "completion_standard": "保存答案、提示等级、正确性和一句反思；无证据不更新能力判断。",
            **stage_fields,
        }

    @staticmethod
    def _domains(track: str) -> list[tuple[str, str, str]]:
        if track == "pet":
            return [
                (f"pet.{key}.{subskill_id}", f"{label} · {subskill_label}", "英语")
                for key, (label, subskills) in PET_DOMAINS.items()
                for subskill_id, subskill_label in subskills
            ]
        if track == "competition_math":
            return [(f"competition_math.{key}", label, "数学") for key, label in COMPETITION_DOMAINS.items()]
        return [
            (f"junior_exam.{subject}.{domain}", domain, subject)
            for subject, domains in JUNIOR_DOMAINS.items()
            for domain in domains
        ]

    @classmethod
    def _domain_ids(cls, track: str, subject: str) -> set[str]:
        return {domain_id for domain_id, _, domain_subject in cls._domains(track) if not subject or domain_subject == subject}

    @staticmethod
    def _mastery_domain(track: str, row) -> str | None:
        mapped = NODE_DOMAIN_HINTS.get(row["knowledge_node_id"])
        if mapped and mapped.startswith(track + "."):
            return mapped
        if track != "junior_exam":
            return None
        subject = str(row["subject"])
        title = str(row["title"])
        for domain in JUNIOR_DOMAINS.get(subject, ()):
            keywords = set(domain.replace("与", " ").replace("、", " ").split())
            if any(keyword and keyword in title for keyword in keywords):
                return f"junior_exam.{subject}.{domain}"
        return None

    @staticmethod
    def _assessment_dict(row) -> dict[str, Any]:
        result = dict(row)
        result["domain_results"] = json.loads(result.pop("domain_results_json"))
        return result
