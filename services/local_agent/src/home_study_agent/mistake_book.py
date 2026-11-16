from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import sqlite3
from uuid import uuid4

from .database import Database
from .learning_obligations import NEW_TRANSFER_STAGES, _question_catalog, cancel_unstarted_plans, project_child_mastery_evidence, project_item_exposure, qualified_exact_score, tutor_question_history
from .learning_state import ITEMS, LearningStateService
from .learning_grade_scope import SCHOOL_GRADES


STAGE_ORDER = (
    "repair_check",
    "near_transfer",
    "representation_transfer",
    "discrimination",
    "far_transfer",
    "delayed_48h",
    "delayed_7d",
)

TRANSFER_STAGES = NEW_TRANSFER_STAGES
PHONICS_ANSWERS = {
    item[0]: (item[4]["keywords"][0], item[4]["keywords"][2:])
    for item in ITEMS if item[1] == "node-p4-english-phonics-discrimination"
}
PHONICS_SOUNDS = {"bag": ("æ", "eɪ"), "sit": ("ɪ", "aɪ"), "hot": ("ɒ|ɑ", "əʊ|oʊ")}

GATE_REQUIREMENTS = {
    "concept_gap": {"near_transfer": 2, "representation_transfer": 1, "discrimination": 1, "delayed_48h": 1, "delayed_7d": 1},
    "prerequisite_gap": {"near_transfer": 2, "representation_transfer": 1, "discrimination": 1, "delayed_48h": 1, "delayed_7d": 1},
    "reading_gap": {"near_transfer": 2, "discrimination": 1, "delayed_48h": 1},
    "method_gap": {"near_transfer": 2, "discrimination": 1, "delayed_48h": 1},
    "execution_error": {"repair_check": 1, "delayed_48h": 1},
    "retrieval_failure": {"repair_check": 1, "delayed_48h": 1, "delayed_7d": 1},
    "transfer_gap": {"representation_transfer": 1, "discrimination": 1, "delayed_48h": 1, "delayed_7d": 1},
    "incidental": {"repair_check": 1},
    "unknown": {"near_transfer": 1, "delayed_48h": 1},
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _search_text(value: str) -> str:
    return "".join(character.lower() for character in value if character.isalnum())


def _character_pairs(value: str) -> set[str]:
    # Latin fragments such as "ra" must not connect pronunciation to grammar.
    pairs = {run[index:index + 2] for run in re.findall(r'[\u4e00-\u9fff]+', value)
             for index in range(len(run) - 1)}
    words = set(re.findall(r'[a-z]{2,}', value.lower())) - {'the', 'and', 'of', 'in', 'an', 'is', 'to'}
    return pairs | words


class MistakeBookService:
    def __init__(self, database: Database):
        self.database = database

    def _diagnosis_service(self):
        from .mistake_diagnosis import MistakeDiagnosisService
        return MistakeDiagnosisService(self.database)

    @staticmethod
    def _pending_revision_mistake(connection, mistake_id: str) -> bool:
        return connection.execute(
            """SELECT 1 FROM paper_scoring_revision_mistake_link AS link
               LEFT JOIN paper_scoring_dialogue_revision_reconciliation AS receipt
                 ON receipt.revision_id=link.revision_id
               WHERE link.mistake_id=? AND receipt.revision_id IS NULL""",
            (mistake_id,),
        ).fetchone() is not None

    @staticmethod
    def _retire_parallel_review(connection, child_id, source_evidence_id, now):
        """Keep one remediation authority for a reliably confirmed mistake."""
        if not source_evidence_id:
            return
        rows = connection.execute(
            """SELECT review_task_id,knowledge_node_id FROM review_task
            WHERE child_id=? AND source_evidence_id=? AND status='pending'""",
            (child_id, source_evidence_id),
        ).fetchall()
        for row in rows:
            connection.execute(
                "UPDATE review_task SET status='cancelled' WHERE review_task_id=? AND status='pending'",
                (row['review_task_id'],),
            )
            cancel_unstarted_plans(
                connection, child_id, 'due_review', row['review_task_id'], now,
            )
        if rows:
            for node_id in sorted({row['knowledge_node_id'] for row in rows}):
                LearningStateService._replay_mastery_scope(
                    connection, child_id, node_id, now,
                    'mistake_replaces_generic_review',
                )

    def diagnosis_state(self, mistake_id: str, child_id: str) -> dict:
        return self._diagnosis_service().get(mistake_id, child_id)

    def propose_diagnosis_hypotheses(self, mistake_id, child_id, hypotheses, expected_version,
                                    client_action_id, supersedes_event_id=None):
        return self._diagnosis_service().propose(mistake_id, child_id, hypotheses, expected_version,
                                                client_action_id, supersedes_event_id)

    def start_diagnosis_probe(self, mistake_id, child_id, diagnostic_item_id, predictions,
                              expected_version, client_action_id):
        return self._diagnosis_service().start_probe(mistake_id, child_id, diagnostic_item_id, predictions,
                                                    expected_version, client_action_id)

    def answer_diagnosis_probe(self, mistake_id, child_id, probe_event_id, answer, expected_version,
                               client_action_id, supersedes_event_id=None):
        return self._diagnosis_service().answer_probe(mistake_id, child_id, probe_event_id, answer,
                                                     expected_version, client_action_id, supersedes_event_id)

    def attach_diagnosis_evidence(self, mistake_id, child_id, answer_event_id, evidence_id,
                                  expected_version, client_action_id):
        return self._diagnosis_service().attach_evidence(mistake_id, child_id, answer_event_id, evidence_id,
                                                        expected_version, client_action_id)

    def retract_diagnosis_event(self, mistake_id, child_id, target_event_id, reason,
                                expected_version, client_action_id):
        return self._diagnosis_service().retract(mistake_id, child_id, target_event_id, reason,
                                                expected_version, client_action_id)

    def capture_from_attempt(self, attempt: dict) -> dict | None:
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            stored = connection.execute("SELECT * FROM diagnostic_attempt WHERE attempt_id=?", (attempt.get("attempt_id"),)).fetchone()
            if not stored or any(attempt.get(key) != stored[key] for key in
                    ("child_id", "session_id", "evidence_id", "diagnostic_item_id")):
                raise UnknownMistakeReferenceError("作答回执不属于指定孩子、会话或题目。")
            receipt = LearningStateService._attempt(connection, stored["attempt_id"])
            if receipt["score_status"] not in {"verified_local", "practice_only"} or receipt["correctness"] >= 1:
                return None
            item = connection.execute(
                """
                SELECT di.diagnostic_item_id, di.knowledge_node_id, di.prompt,
                       kn.subject, kn.title AS knowledge_title,le.confidence
                FROM diagnostic_item di
                JOIN knowledge_node kn ON kn.knowledge_node_id = di.knowledge_node_id
                JOIN learning_evidence le ON le.knowledge_node_id=di.knowledge_node_id
                WHERE di.diagnostic_item_id = ? AND le.evidence_id=? AND le.child_id=?
                """,
                (receipt["diagnostic_item_id"], receipt["evidence_id"], receipt["child_id"]),
            ).fetchone()
            if not item:
                return None
            return self.capture(
                child_id=receipt["child_id"], knowledge_node_id=item["knowledge_node_id"],
                subject=item["subject"], title=item["knowledge_title"], prompt_summary=item["prompt"],
                submitted_answer=receipt["submitted_answer"], source_type="diagnostic",
                source_ref=receipt["diagnostic_item_id"], confidence=item["confidence"],
                client_action_id=f"mistake:auto:{receipt['attempt_id']}", diagnostic_item_id=receipt["diagnostic_item_id"],
                source_evidence_id=receipt["evidence_id"], fingerprint_override=f"diagnostic:{receipt['diagnostic_item_id']}",
                _connection=connection, _qualified_authority_type="agent_generated_rubric",
            )

    def get(self, mistake_id: str, child_id: str) -> dict:
        with self.database.connect() as connection:
            self._owned_case(connection, mistake_id, child_id)
            if self._pending_revision_mistake(connection, mistake_id):
                raise UnknownMistakeReferenceError("错题评分仍在对账，请稍后重试。")
            return self._detail(connection, mistake_id)

    def current_for_session(self, session_id: str) -> dict | None:
        """Return the durable mistake activity owned by the current session.

        The active daily-plan item is authoritative.  Falling back to the most
        actionable child case lets a voice request resume after an app restart
        before the plan item has been reopened in the renderer.
        """
        with self.database.connect() as connection:
            session = connection.execute(
                "SELECT child_id FROM daily_session WHERE session_id = ? AND status = 'active'",
                (session_id,),
            ).fetchone()
            if not session:
                return None
            row = None
            if self._table_exists(connection, "mistake_dialogue"):
                row = connection.execute(
                    """
                    SELECT mc.mistake_id, md.phase FROM mistake_dialogue md
                    JOIN mistake_case mc ON mc.mistake_id = md.mistake_id
                    WHERE md.session_id = ? AND md.child_id = ? AND mc.child_id = ?
                      AND md.phase NOT IN ('paused', 'closed') AND mc.status != 'excluded'
                    """,
                    (session_id, session["child_id"], session["child_id"]),
                ).fetchone()
            if row and not self._pending_revision_mistake(connection, row["mistake_id"]):
                detail = self._detail(connection, row["mistake_id"])
                detail["next_activity"] = self.next_activity(row["mistake_id"], session["child_id"])
                if row["phase"] != "scheduled" or detail["next_activity"]["kind"] == "attempt":
                    return detail
            plan_rows = connection.execute(
                """
                SELECT mc.mistake_id
                FROM plan_item pi
                JOIN daily_plan dp ON dp.plan_id = pi.plan_id
                JOIN mistake_case mc ON mc.mistake_id = pi.source_ref
                WHERE dp.session_id = ? AND dp.status = 'active'
                  AND pi.source_type = 'mistake_review'
                  AND pi.status IN ('active', 'paused', 'pending')
                  AND mc.child_id = ? AND mc.status != 'excluded'
                ORDER BY CASE pi.status WHEN 'active' THEN 0 WHEN 'paused' THEN 1 ELSE 2 END,
                         pi.position
                """,
                (session_id, session["child_id"]),
            ).fetchall()
            queue_rows = connection.execute(
                    """
                    SELECT mistake_id FROM mistake_case
                    WHERE child_id = ? AND status != 'excluded'
                    ORDER BY CASE status
                      WHEN 'awaiting_confirmation' THEN 0
                      WHEN 'diagnosing' THEN 1
                      WHEN 'relapsed' THEN 2
                      WHEN 'remediating' THEN 3
                      WHEN 'near_transfer' THEN 4
                      WHEN 'mixed_transfer' THEN 5
                      WHEN 'fragile' THEN 6
                      WHEN 'delayed_verification' THEN 7
                      ELSE 8 END,
                      updated_at, created_at, rowid
                    """,
                    (session["child_id"],),
                ).fetchall()
            scheduled = None
            seen = set()
            for row in [*plan_rows, *queue_rows]:
                if self._pending_revision_mistake(connection, row["mistake_id"]):
                    continue
                if row["mistake_id"] in seen:
                    continue
                seen.add(row["mistake_id"])
                detail = self._detail(connection, row["mistake_id"])
                if detail["status"] == "mastered":
                    continue
                detail["next_activity"] = self.next_activity(row["mistake_id"], session["child_id"])
                if detail["next_activity"]["kind"] != "scheduled":
                    return detail
                if scheduled is None:
                    scheduled = detail
            return scheduled

    def knowledge_candidates(
        self, child_id: str, query: str, subject: str | None = None, limit: int = 8
    ) -> dict:
        normalized_query = _search_text(query)
        if len(normalized_query) < 2:
            raise InvalidMistakeInputError("请至少输入两个可识别字符。")
        with self.database.connect() as connection:
            child = connection.execute(
                "SELECT grade FROM child_profile WHERE child_id = ? AND status = 'active'",
                (child_id,),
            ).fetchone()
            if not child:
                raise UnknownMistakeReferenceError("找不到孩子。")
            parameters: list[object] = [f"{child['grade']}%"]
            foundations = SCHOOL_GRADES[:SCHOOL_GRADES.index(child['grade'])] if child['grade'] in SCHOOL_GRADES else ()
            grade_clause = 'grade_band LIKE ?'
            if foundations:
                grade_clause += " OR (track='school' AND grade_band IN (" + ','.join('?' for _ in foundations) + '))'
                parameters.extend(foundations)
            subject_clause = ""
            if subject:
                subject_clause = " AND subject = ?"
                parameters.append(subject)
            rows = connection.execute(
                f"""
                SELECT knowledge_node_id, node_type, track, subject, grade_band,
                       code, title, description
                FROM knowledge_node
                WHERE status = 'active' AND node_type = 'skill'
                  AND ({grade_clause}) {subject_clause}
                """,
                parameters,
            ).fetchall()
            query_pairs = _character_pairs(query)
            candidates = []
            for row in rows:
                item = dict(row)
                searchable = f"{item['title']} {item['description']}"
                title_terms = _character_pairs(item['title'])
                matched = query_pairs & _character_pairs(searchable)
                score = len(matched) / max(len(title_terms), 1)
                if query_pairs and title_terms and (title_terms <= query_pairs or query_pairs <= title_terms):
                    score += 3
                item["match_score"] = round(score, 3)
                item['match_terms'] = sorted(matched)
                item['grade_relation'] = 'current' if item['grade_band'].startswith(child['grade']) else 'foundation'
                candidates.append(item)
            candidates.sort(
                key=lambda item: (
                    item["match_score"], item['grade_relation'] == 'current', item["title"]
                ),
                reverse=True,
            )
            return {"items": candidates[: max(1, min(limit, 20))]}

    def capture(
        self,
        *,
        child_id: str,
        knowledge_node_id: str,
        subject: str,
        title: str,
        prompt_summary: str,
        submitted_answer: str,
        source_type: str,
        source_ref: str | None,
        confidence: float,
        client_action_id: str,
        diagnostic_item_id: str | None = None,
        source_evidence_id: str | None = None,
        fingerprint_override: str | None = None,
        _connection: sqlite3.Connection | None = None,
        _qualified_authority_type: str | None = None,
        _defer_commit: bool = False,
    ) -> dict:
        if _defer_commit and _connection is None:
            raise InvalidMistakeInputError("延迟提交必须由调用方提供事务连接。")
        if _qualified_authority_type is not None and (
            _qualified_authority_type != "agent_generated_rubric"
            or source_type != "diagnostic"
            or not diagnostic_item_id
            or not source_evidence_id
        ):
            raise InvalidMistakeInputError("自动确认错题必须来自已评分的智能体题目。")
        now = utc_now()
        fingerprint_source = fingerprint_override or json.dumps(
            [knowledge_node_id, source_type, source_ref or "", " ".join(prompt_summary.split()).lower()],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        fingerprint = hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()
        with (nullcontext(_connection) if _connection is not None else self.database.connect()) as connection:
            if not connection.in_transaction:
                connection.execute("BEGIN IMMEDIATE")
            existing_action = connection.execute(
                "SELECT mistake_id FROM mistake_case WHERE client_action_id = ?",
                (client_action_id,),
            ).fetchone()
            if existing_action:
                self._owned_case(connection, existing_action["mistake_id"], child_id)
                return self._detail(connection, existing_action["mistake_id"])
            child = connection.execute(
                "SELECT 1 FROM child_profile WHERE child_id = ? AND status = 'active'",
                (child_id,),
            ).fetchone()
            node = connection.execute(
                "SELECT title, subject FROM knowledge_node WHERE knowledge_node_id = ? AND status = 'active'",
                (knowledge_node_id,),
            ).fetchone()
            if not child or not node:
                raise UnknownMistakeReferenceError("找不到孩子或知识节点。")
            if source_evidence_id and not connection.execute(
                "SELECT 1 FROM learning_evidence WHERE evidence_id = ? AND child_id = ? AND knowledge_node_id = ?",
                (source_evidence_id, child_id, knowledge_node_id),
            ).fetchone():
                raise UnknownMistakeReferenceError("来源证据不属于当前孩子和知识点。")
            if diagnostic_item_id:
                scope, parameters = self._item_scope(connection, child_id)
                if not connection.execute(
                    f"SELECT 1 FROM diagnostic_item di WHERE di.diagnostic_item_id = ? AND di.knowledge_node_id = ? {scope}",
                    [diagnostic_item_id, knowledge_node_id, *parameters],
                ).fetchone():
                    raise UnknownMistakeReferenceError("来源题目不属于当前孩子和知识点。")
            cluster = self._ensure_cluster(connection, child_id, knowledge_node_id, subject or node["subject"], title or node["title"], now)
            existing = connection.execute(
                "SELECT mistake_id, status FROM mistake_case WHERE child_id = ? AND fingerprint = ?",
                (child_id, fingerprint),
            ).fetchone()
            occurrence_action = f"{client_action_id}:occurrence"
            if existing:
                if not connection.execute(
                    "SELECT 1 FROM mistake_occurrence WHERE client_action_id = ?",
                    (occurrence_action,),
                ).fetchone():
                    connection.execute(
                        """
                        INSERT INTO mistake_occurrence(
                          occurrence_id, mistake_id, child_id, evidence_id,
                          submitted_answer, source_ref, occurred_at, client_action_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (str(uuid4()), existing["mistake_id"], child_id, source_evidence_id, submitted_answer, source_ref, now, occurrence_action),
                    )
                    connection.execute(
                        "UPDATE mistake_cluster SET occurrence_count = occurrence_count + 1, updated_at = ? WHERE cluster_id = ?",
                        (now, cluster["cluster_id"]),
                    )
                if existing["status"] == "mastered":
                    self._transition(connection, existing["mistake_id"], child_id, "mastered", "relapsed", "同类错误再次出现", f"{client_action_id}:relapse", now)
                    connection.execute(
                        "UPDATE mistake_cluster SET relapse_count = relapse_count + 1, diagnosis_status = 'confirmed_gap', updated_at = ? WHERE cluster_id = ?",
                        (now, cluster["cluster_id"]),
                    )
                elif existing["status"] == "awaiting_confirmation" and _qualified_authority_type:
                    self._transition(
                        connection, existing["mistake_id"], child_id,
                        "awaiting_confirmation", "diagnosing",
                        "智能体出题时冻结的答案和评分规则已确认本次作答错误",
                        f"{client_action_id}:qualified-score", now,
                    )
                    connection.execute(
                        "UPDATE mistake_case SET confirmed_at=? WHERE mistake_id=?",
                        (now, existing["mistake_id"]),
                    )
                    connection.execute(
                        "UPDATE mistake_cluster SET diagnosis_status='confirmed_gap', updated_at=? WHERE cluster_id=?",
                        (now, cluster["cluster_id"]),
                    )
                if _qualified_authority_type:
                    self._retire_parallel_review(connection, child_id, source_evidence_id, now)
                self._refresh_cluster_pattern(connection, cluster["cluster_id"], now)
                if not _defer_commit:
                    connection.commit()
                return self._detail(connection, existing["mistake_id"])

            initial_status = (
                "relapsed" if cluster["diagnosis_status"] == "stable"
                else "diagnosing" if _qualified_authority_type
                else "awaiting_confirmation"
            )
            mistake_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO mistake_case(
                  mistake_id, cluster_id, child_id, knowledge_node_id, diagnostic_item_id,
                  source_evidence_id, subject, title, prompt_summary, submitted_answer,
                  source_type, source_ref, fingerprint, confidence, status,
                  created_at, updated_at, client_action_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mistake_id, cluster["cluster_id"], child_id, knowledge_node_id, diagnostic_item_id,
                    source_evidence_id, subject or node["subject"], title or node["title"], prompt_summary,
                    submitted_answer, source_type, source_ref, fingerprint, confidence, initial_status,
                    now, now, client_action_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO mistake_occurrence(
                  occurrence_id, mistake_id, child_id, evidence_id,
                  submitted_answer, source_ref, occurred_at, client_action_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (str(uuid4()), mistake_id, child_id, source_evidence_id, submitted_answer, source_ref, now, occurrence_action),
            )
            connection.execute(
                "INSERT INTO mistake_status_event(event_id, mistake_id, child_id, from_status, to_status, reason, created_at, client_action_id) VALUES (?, ?, ?, NULL, ?, ?, ?, ?)",
                (
                    str(uuid4()), mistake_id, child_id, initial_status,
                    "智能体出题时冻结的答案和评分规则已确认本次作答错误"
                    if _qualified_authority_type else "作答记录已进入待评分队列",
                    now, f"{client_action_id}:status",
                ),
            )
            if _qualified_authority_type:
                connection.execute(
                    "UPDATE mistake_case SET confirmed_at=? WHERE mistake_id=?",
                    (now, mistake_id),
                )
                self._retire_parallel_review(connection, child_id, source_evidence_id, now)
            connection.execute(
                "UPDATE mistake_cluster SET occurrence_count = occurrence_count + 1, relapse_count = relapse_count + ?, updated_at = ? WHERE cluster_id = ?",
                (1 if initial_status == "relapsed" else 0, now, cluster["cluster_id"]),
            )
            if initial_status in {"relapsed", "diagnosing"}:
                connection.execute(
                    "UPDATE mistake_cluster SET diagnosis_status = 'confirmed_gap' WHERE cluster_id = ?",
                    (cluster["cluster_id"],),
                )
            self._refresh_cluster_pattern(connection, cluster["cluster_id"], now)
            if not _defer_commit:
                connection.commit()
            return self._detail(connection, mistake_id)

    def list(self, child_id: str, status: str | None = None) -> dict:
        with self.database.connect() as connection:
            child = connection.execute("SELECT 1 FROM child_profile WHERE child_id = ?", (child_id,)).fetchone()
            if not child:
                raise UnknownMistakeReferenceError("找不到孩子。")
            query = """SELECT mc.mistake_id FROM mistake_case AS mc
                       WHERE mc.child_id = ? AND NOT EXISTS (
                         SELECT 1 FROM paper_scoring_revision_mistake_link AS link
                         LEFT JOIN paper_scoring_dialogue_revision_reconciliation AS receipt
                           ON receipt.revision_id=link.revision_id
                         WHERE link.mistake_id=mc.mistake_id AND receipt.revision_id IS NULL
                       )"""
            params: list[object] = [child_id]
            query += " ORDER BY mc.updated_at DESC"
            rows = connection.execute(query, params).fetchall()
            projections = project_child_mastery_evidence(connection, child_id, as_of=utc_now())
            items = [self._detail(connection, row["mistake_id"], _projections=projections) for row in rows]
            groups = {"awaiting_confirmation": {"awaiting_confirmation"},
                      "today_strengthening": {"diagnosing", "remediating", "near_transfer", "mixed_transfer", "relapsed"},
                      "due_review": {"delayed_verification", "fragile"}, "mastered": {"mastered"}}
            return {
                "items": [item for item in items if not status or item["status"] == status],
                "counts": {key: sum(item["status"] in states for item in items) for key, states in groups.items()},
            }

    def confirm(self, mistake_id: str, child_id: str, decision: str, client_action_id: str) -> dict:
        # Exclusion changes evidence used by already-approved reports.  Acquire
        # before opening the write transaction and serialize with SMTP DATA.
        if decision != 'exclude':
            return self._confirm_impl(mistake_id, child_id, decision, client_action_id)
        from .parent_reports import _report_effect_guard
        with _report_effect_guard(self.database, timeout=2):
            return self._confirm_impl(mistake_id, child_id, decision, client_action_id)

    def _confirm_impl(self, mistake_id: str, child_id: str, decision: str, client_action_id: str) -> dict:
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            mistake = self._owned_case(connection, mistake_id, child_id)
            existing = connection.execute(
                "SELECT 1 FROM mistake_status_event WHERE client_action_id = ?",
                (client_action_id,),
            ).fetchone()
            if existing:
                return self._detail(connection, mistake_id)
            if decision == "exclude":
                target = "excluded"
                reason = "孩子或家长指出题干、实际作答或题目归属识别无效"
            elif decision == "confirm":
                # New scored sources enter diagnosis automatically.  Keep the
                # historical command idempotent for old clients and journals,
                # but do not treat it as new correctness evidence.
                if mistake["confirmed_at"] and mistake["status"] not in {
                    "awaiting_confirmation", "relapsed", "excluded"
                }:
                    connection.commit()
                    return self._detail(connection, mistake_id)
                if mistake["status"] not in ("awaiting_confirmation", "relapsed"):
                    raise InvalidMistakeStateError("这道错题当前不需要再次确认。")
                target = "diagnosing"
                reason = "历史兼容确认；新记录必须由合格评分权威自动确认"
            else:
                raise InvalidMistakeInputError("确认动作无效。")
            self._transition(connection, mistake_id, child_id, mistake["status"], target, reason, client_action_id, now)
            if target == "diagnosing":
                connection.execute("UPDATE mistake_case SET confirmed_at = ? WHERE mistake_id = ?", (now, mistake_id))
            elif target == "excluded":
                self._after_exclusion(connection, mistake, child_id, now, "mistake_excluded")
            connection.commit()
            return self._detail(connection, mistake_id)

    def confirm_from_qualified_scoring(
        self, mistake_id: str, child_id: str, decision_id: str,
        authority_type: str, client_action_id: str,
        _connection: sqlite3.Connection | None = None,
    ) -> dict:
        """Advance a scored mistake without asking the child to judge correctness."""
        if not decision_id or authority_type not in {
            "teacher_mark", "published_answer_key", "agent_generated_rubric"
        }:
            raise InvalidMistakeInputError("错题必须绑定合格评分依据。")
        now = utc_now()
        with (nullcontext(_connection) if _connection is not None else self.database.connect()) as connection:
            if not connection.in_transaction:
                connection.execute("BEGIN IMMEDIATE")
            mistake = self._owned_case(connection, mistake_id, child_id)
            existing = connection.execute(
                "SELECT 1 FROM mistake_status_event WHERE client_action_id=?",
                (client_action_id,),
            ).fetchone()
            if existing:
                if _connection is None:
                    connection.commit()
                return self._detail(connection, mistake_id)
            decision = connection.execute(
                """SELECT 1 FROM paper_scoring_effective_decision AS decision
                   WHERE decision.decision_id=? AND decision.child_id=?
                     AND decision.authority_type=? AND decision.correctness=0
                     AND decision.effective_status='qualified'""",
                (decision_id, child_id, authority_type),
            ).fetchone()
            if not decision:
                raise InvalidMistakeInputError("评分依据已变化，不能自动建立错题。")
            if mistake["status"] not in ("awaiting_confirmation", "relapsed"):
                if _connection is None:
                    connection.commit()
                return self._detail(connection, mistake_id)
            self._transition(
                connection, mistake_id, child_id, mistake["status"], "diagnosing",
                f"合格评分依据自动确认错题：{authority_type}:{decision_id}",
                client_action_id, now,
            )
            connection.execute(
                "UPDATE mistake_case SET confirmed_at=? WHERE mistake_id=?", (now, mistake_id)
            )
            if _connection is None:
                connection.commit()
            return self._detail(connection, mistake_id)

    @classmethod
    def exclude_invalid_scoring_authority(
        cls, connection, mistake_id: str, child_id: str,
        client_action_id: str, now: str,
    ) -> None:
        """Retract a derived mistake when its immutable score authority expires."""
        mistake = connection.execute(
            "SELECT * FROM mistake_case WHERE mistake_id=? AND child_id=?",
            (mistake_id, child_id),
        ).fetchone()
        if not mistake or mistake["status"] == "excluded":
            return
        cls._transition(
            connection, mistake_id, child_id, mistake["status"], "excluded",
            "纸面主观题评分权威已撤回或发生变化", client_action_id, now,
        )
        cls._after_exclusion(
            connection, mistake, child_id, now, "paper_scoring_authority_withdrawn"
        )

    @staticmethod
    def _after_exclusion(connection, mistake, child_id: str, now: str, reason: str) -> None:
        from .learning_obligations import withdraw_excluded_reviews
        withdraw_excluded_reviews(connection, child_id, now)
        LearningStateService._replay_mastery_scope(
            connection, child_id, mistake["knowledge_node_id"], now, reason
        )
        # Preserve source events for audit, but retract this case from the
        # active weakness projection used by memory and review.
        count = connection.execute(
            "SELECT COUNT(*) FROM mistake_occurrence o JOIN mistake_case c ON c.mistake_id=o.mistake_id WHERE c.cluster_id=? AND c.status!='excluded'",
            (mistake["cluster_id"],),
        ).fetchone()[0]
        relapses = connection.execute(
            "SELECT COUNT(*) FROM mistake_status_event e JOIN mistake_case c ON c.mistake_id=e.mistake_id WHERE c.cluster_id=? AND c.status!='excluded' AND e.to_status='relapsed'",
            (mistake["cluster_id"],),
        ).fetchone()[0]
        connection.execute(
            "UPDATE mistake_cluster SET occurrence_count=?, relapse_count=?, diagnosis_status=?, updated_at=? WHERE cluster_id=?",
            (count, relapses, "emerging_pattern" if count >= 2 else "suspected", now, mistake["cluster_id"]),
        )

    def diagnose(self, mistake_id: str, child_id: str, error_type: str, explanation: str, confidence: float, client_action_id: str) -> dict:
        now = utc_now()
        requirements = GATE_REQUIREMENTS.get(error_type)
        if not requirements:
            raise InvalidMistakeInputError("错因类型无效。")
        requirements = self._candidate_requirements(requirements)
        with self.database.connect() as connection:
            mistake = self._owned_case(connection, mistake_id, child_id)
            existing = connection.execute("SELECT 1 FROM mistake_diagnosis WHERE client_action_id = ?", (client_action_id,)).fetchone()
            if existing:
                return self._detail(connection, mistake_id)
            if mistake["status"] not in ("diagnosing", "relapsed", "remediating"):
                raise InvalidMistakeStateError("请先确认错题，再进行诊断。")
            connection.execute(
                """
                INSERT INTO mistake_diagnosis(
                  diagnosis_id, mistake_id, child_id, error_type, explanation,
                  confidence, status, created_at, client_action_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (str(uuid4()), mistake_id, child_id, error_type, explanation, confidence, "candidate", now, client_action_id),
            )
            connection.execute(
                """
                INSERT INTO mistake_mastery_gate(
                  mistake_id, error_type, requirements_json,
                  delayed_48h_due_at, delayed_7d_due_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(mistake_id) DO UPDATE SET
                  error_type = excluded.error_type,
                  requirements_json = excluded.requirements_json,
                  delayed_48h_due_at = excluded.delayed_48h_due_at,
                  delayed_7d_due_at = excluded.delayed_7d_due_at,
                  updated_at = excluded.updated_at
                """,
                (
                    mistake_id, error_type, json.dumps(requirements, ensure_ascii=False),
                    (_parse_time(now) + timedelta(hours=48)).isoformat() if requirements.get("delayed_48h") else None,
                    (_parse_time(now) + timedelta(days=7)).isoformat() if requirements.get("delayed_7d") else None,
                    now,
                ),
            )
            self._transition(connection, mistake_id, child_id, mistake["status"], "remediating", "错因候选已形成，需要补救后验证", f"{client_action_id}:status", now)
            # Confidence in an explanation is not evidence confirming its cause.
            cluster_status = "suspected"
            connection.execute(
                "UPDATE mistake_cluster SET diagnosis_status = ?, updated_at = ? WHERE cluster_id = ?",
                (cluster_status, now, mistake["cluster_id"]),
            )
            self._refresh_cluster_pattern(connection, mistake["cluster_id"], now)
            connection.commit()
            return self._detail(connection, mistake_id)

    def add_remediation(self, mistake_id: str, child_id: str, step_type: str, outcome: str, notes: str | None, client_action_id: str) -> dict:
        now = utc_now()
        allowed_steps = {
            "concept_explanation", "prerequisite_review", "problem_restatement",
            "method_comparison", "execution_check", "active_recall", "representation_change",
        }
        if step_type not in allowed_steps or outcome not in {"completed", "still_stuck", "skipped"}:
            raise InvalidMistakeInputError("补救步骤或结果无效。")
        with self.database.connect() as connection:
            mistake = self._owned_case(connection, mistake_id, child_id)
            existing = connection.execute("SELECT 1 FROM remediation_step WHERE client_action_id = ?", (client_action_id,)).fetchone()
            if existing:
                return self._detail(connection, mistake_id)
            if mistake["status"] not in ("remediating", "diagnosing", "relapsed"):
                raise InvalidMistakeStateError("当前错题不在补救阶段。")
            if not connection.execute("SELECT 1 FROM mistake_mastery_gate WHERE mistake_id = ?", (mistake_id,)).fetchone():
                raise InvalidMistakeStateError("请先完成错因诊断。")
            connection.execute(
                "INSERT INTO remediation_step(step_id, mistake_id, child_id, step_type, outcome, notes, created_at, client_action_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (str(uuid4()), mistake_id, child_id, step_type, outcome, notes, now, client_action_id),
            )
            if outcome == "completed":
                target = "near_transfer"
                reason = "补救完成，进入独立变式验证"
            else:
                target = "remediating"
                reason = "补救尚未完成，保留当前阶段"
            if mistake["status"] != target:
                self._transition(connection, mistake_id, child_id, mistake["status"], target, reason, f"{client_action_id}:status", now)
            connection.commit()
            return self._detail(connection, mistake_id)

    def submit_attempt(self, mistake_id: str, child_id: str, stage: str, diagnostic_item_id: str, answer: str, hint_level: int, client_action_id: str, *, _session_id: str | None = None, _correction_of_attempt_id: str | None = None, _trusted_prompt_id: str | None = None, _pet_media_scope: dict | None = None) -> dict:
        if stage not in STAGE_ORDER or hint_level < 0 or hint_level > 5:
            raise InvalidMistakeInputError("变式阶段或提示等级无效。")
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            mistake = self._owned_case(connection, mistake_id, child_id)
            correction = None
            if _correction_of_attempt_id is not None:
                correction = connection.execute('''SELECT ma.*,le.session_id FROM mistake_attempt ma
                    JOIN learning_evidence le USING(evidence_id)
                    WHERE ma.attempt_id=? AND ma.mistake_id=? AND ma.child_id=?
                      AND ma.stage=? AND ma.diagnostic_item_id=?''',
                    (_correction_of_attempt_id, mistake_id, child_id, stage,
                     diagnostic_item_id)).fetchone()
                if (not correction or correction['session_id'] != _session_id
                        or connection.execute('SELECT 1 FROM learning_evidence_withdrawal WHERE evidence_id=?',
                                              (correction['evidence_id'],)).fetchone()):
                    raise InvalidMistakeInputError('被更正作答的归属、题目或评分权已经变化。')
            existing = connection.execute("SELECT * FROM mistake_attempt WHERE client_action_id = ?", (client_action_id,)).fetchone()
            if existing:
                recorded = connection.execute('SELECT session_id FROM learning_evidence WHERE evidence_id=?',
                    (existing['evidence_id'],)).fetchone()
                if any(existing[key] != value for key, value in {
                    "mistake_id": mistake_id, "child_id": child_id, "stage": stage,
                    "diagnostic_item_id": diagnostic_item_id, "submitted_answer": answer, "hint_level": hint_level,
                }.items()):
                    raise InvalidMistakeInputError("作答标识已用于另一条作答。")
                if _session_id is not None and recorded and recorded['session_id'] not in (None, _session_id):
                    raise InvalidMistakeInputError('作答标识已用于另一学习会话。')
                return self._detail(connection, mistake_id)
            if _session_id is not None:
                session = connection.execute('SELECT child_id,status,state FROM daily_session WHERE session_id=?',
                    (_session_id,)).fetchone()
                if not session or session['child_id'] != child_id or session['status'] != 'active' or session['state'] != 'study':
                    raise InvalidMistakeInputError('作答需要同一孩子当前正在学习的会话。')
            gate = connection.execute("SELECT * FROM mistake_mastery_gate WHERE mistake_id = ?", (mistake_id,)).fetchone()
            if not gate:
                raise InvalidMistakeStateError("请先完成错因诊断。")
            summary = self._gate_summary(connection, mistake_id)
            mistake = {**dict(mistake), "status": self._gate_status(mistake["status"], summary)}
            if mistake["status"] not in {"near_transfer", "mixed_transfer", "delayed_verification", "fragile"}:
                raise InvalidMistakeStateError("当前错题不在独立验证阶段。")
            if not correction and (not summary["missing"] or summary["missing"][0] != stage):
                raise InvalidMistakeStateError("请按当前掌握门槛完成验证，不能跳过或重复阶段。")
            due_field = "delayed_48h_due_at" if stage == "delayed_48h" else "delayed_7d_due_at" if stage == "delayed_7d" else None
            if due_field and not summary[due_field]:
                raise InvalidMistakeStateError("延迟复测时间尚未确定。")
            if due_field and _parse_time(summary[due_field]) > _parse_time(now):
                raise MistakeReviewNotDueError(summary[due_field])
            scope, parameters = self._item_scope(connection, child_id, stage, mistake_id)
            item = connection.execute(
                f"""
                SELECT diagnostic_item_id, knowledge_node_id, answer_type,
                       expected_answer_json, explanation, assessment_mode
                FROM diagnostic_item di
                WHERE diagnostic_item_id = ? AND status = 'active'
                  {scope}
                """,
                [diagnostic_item_id, *parameters],
            ).fetchone()
            if not item or item["knowledge_node_id"] != mistake["knowledge_node_id"]:
                raise UnknownMistakeReferenceError("变式题不存在或不属于同一知识点。")
            if stage in TRANSFER_STAGES and (
                diagnostic_item_id == mistake["diagnostic_item_id"]
                or connection.execute(
                    """SELECT 1 FROM mistake_attempt WHERE child_id = ? AND diagnostic_item_id = ?
                       AND (? IS NULL OR attempt_id != ?)""",
                    (child_id, diagnostic_item_id, _correction_of_attempt_id,
                     _correction_of_attempt_id),
                ).fetchone()
            ):
                raise InvalidMistakeStateError("迁移验证需要尚未做过的新题。")
            correctness, feedback = self._score_answer(item, answer)
            from .pet_skill_gate import pet_skill_gate
            pet_scope = {}
            if _trusted_prompt_id is not None and _pet_media_scope is not None:
                pinned = connection.execute(
                    "SELECT item_version FROM diagnostic_prompt WHERE prompt_id=? AND child_id=? AND origin_session_id=?",
                    (_trusted_prompt_id, child_id, _session_id),
                ).fetchone()
                if not pinned:
                    raise InvalidMistakeInputError("听力作答的题目、孩子或会话绑定已经变化。")
                pet_scope = dict(_pet_media_scope, session_id=_session_id,
                                 prompt_id=_trusted_prompt_id, item_version=pinned[0])
            modality = pet_skill_gate(connection, child_id, diagnostic_item_id, answer=answer, **pet_scope)
            if modality["modality"] == "listening":
                from .pet_listening_scoring import qualified_listening_score
                qualified = qualified_listening_score(connection, child_id, diagnostic_item_id,
                                                       answer, **pet_scope)
                if qualified is not None:
                    correctness = qualified[1]
                    feedback = ("本次原题听力练习回答正确。" if correctness == 1
                                else "本次原题听力练习答案尚未匹配，请核对。")
            else:
                qualified = qualified_exact_score(connection, child_id, diagnostic_item_id, answer,
                                                  stage=stage, mistake_id=mistake_id)
            reliable = modality["skill_scoring_allowed"] and qualified is not None and qualified[1] == correctness
            independent = int(reliable and hint_level == 0)
            if correction:
                # The child has already heard outcome feedback. The correction
                # fixes the record but cannot retroactively prove independence.
                independent = 0
            if not reliable:
                feedback = "作答已保存，当前评分方式不能可靠确认对错，需要进一步核实。"
            if stage == "repair_check" and connection.execute(
                """SELECT 1 FROM mistake_attempt
                   WHERE child_id = ? AND diagnostic_item_id = ?
                     AND julianday(created_at) >= julianday(?)""",
                (child_id, diagnostic_item_id, gate["updated_at"]),
            ).fetchone():
                # Every attempt exposes feedback; immediately repeating it is
                # practice, not a new independent check.
                independent = 0
            assistance = project_item_exposure(connection, child_id, diagnostic_item_id, as_of=now)
            if assistance["tutor_assisted"] or assistance["answer_repeated"]:
                independent = 0
            transfer_exposure = None
            if stage in TRANSFER_STAGES and not correction:
                identities, seen = self._question_history(connection, child_id)
                identity = identities.get(diagnostic_item_id)
                transfer_exposure = seen.get(identity)
                if identity and identity == identities.get(mistake['diagnostic_item_id']):
                    transfer_exposure = transfer_exposure or 'original_question'
                if transfer_exposure:
                    independent = 0
                    feedback = '你的回答已保存。这道题以前已经做过或讲解过，不能把这次作答当作新题迁移通过；需要另选一道有可靠来源的新题。'
            evidence_summary = f"错题本{stage}作答"
            if correction:
                evidence_summary += ";independence_reason=answer_corrected_after_feedback"
            elif assistance["tutor_assisted"]:
                evidence_summary += ";independence_reason=tutor_assistance_within_24h"
            elif transfer_exposure:
                evidence_summary += (';independence_reason=transfer_question_previously_taught'
                    if transfer_exposure.startswith('tutor:') else ';independence_reason=transfer_question_previously_seen')
            elif assistance["answer_repeated"]:
                evidence_summary += ";independence_reason=same_item_within_24h"
            elif not reliable:
                evidence_summary += ";independence_reason=unreliable_scoring"
            confidence = 1.0 if reliable else 0.0 if correctness is None else .35
            evidence_id = str(uuid4())
            evidence_action = f"mistake-attempt:{client_action_id}"
            LearningStateService._insert_evidence(
                connection, evidence_id, child_id, _session_id, mistake["knowledge_node_id"],
                f"mistake_{stage}", correctness, hint_level, independent, confidence,
                evidence_summary, evidence_action, now,
            )
            connection.execute(
                """
                INSERT INTO mistake_attempt(
                  attempt_id, mistake_id, child_id, diagnostic_item_id, evidence_id,
                  stage, submitted_answer, correctness, hint_level, independent,
                  feedback, created_at, client_action_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (str(uuid4()), mistake_id, child_id, diagnostic_item_id, evidence_id, stage, answer, correctness, hint_level, independent, feedback, now, client_action_id),
            )
            LearningStateService._update_mastery(
                connection, child_id, mistake["knowledge_node_id"], evidence_id,
                correctness, hint_level, bool(independent), confidence, now,
            )
            next_action = self._evaluate_progress(connection, mistake, stage, now, client_action_id)
            connection.commit()
            detail = self._detail(connection, mistake_id)
            detail["next_action"] = next_action
            projections = project_child_mastery_evidence(connection, child_id, as_of=now)
            receipt = next((row for row in projections.get(mistake["knowledge_node_id"], {}).get("effective_evidence", [])
                            if row["evidence_id"] == evidence_id), None)
            detail["attempt_result"] = {"correctness": correctness, "feedback": feedback,
                "independent": bool(receipt and receipt["independent"]),
                "score_status": "verified_local" if receipt else "practice_only" if reliable else "needs_review"}
            return detail

    def reconcile_after_answer_correction(self, mistake_id, child_id, client_action_id,
                                          now, *, connection):
        """Rebuild gate status after one attempt loses score authority."""
        if not connection.in_transaction:
            raise ValueError('MISTAKE_CORRECTION_TRANSACTION_REQUIRED')
        current = self._owned_case(connection, mistake_id, child_id)
        gate = self._gate_summary(connection, mistake_id)
        if not gate:
            return None
        immediate_missing = [stage for stage in gate['missing']
            if stage not in {'delayed_48h', 'delayed_7d'}]
        if immediate_missing:
            connection.execute('''UPDATE mistake_mastery_gate
                SET delayed_48h_due_at=NULL,delayed_7d_due_at=NULL,updated_at=?
                WHERE mistake_id=?''', (now, mistake_id))
        else:
            connection.execute('''UPDATE mistake_mastery_gate
                SET delayed_48h_due_at=?,delayed_7d_due_at=?,updated_at=?
                WHERE mistake_id=?''', (gate.get('delayed_48h_due_at'),
                gate.get('delayed_7d_due_at'), now, mistake_id))
        if not gate['missing']:
            target = 'mastered'
        elif gate['missing'][0] in {'repair_check', 'near_transfer'}:
            target = 'near_transfer'
        elif gate['missing'][0] in TRANSFER_STAGES:
            target = 'mixed_transfer'
        else:
            target = 'fragile'
        if current['status'] != target:
            self._transition(connection, mistake_id, child_id, current['status'], target,
                '更正作答后按当前有效证据重算掌握门槛',
                client_action_id + ':correction-status', now)
        return self._gate_summary(connection, mistake_id)

    def weakness_clusters(self, child_id: str) -> dict:
        with self.database.connect() as connection:
            if not connection.execute("SELECT 1 FROM child_profile WHERE child_id = ?", (child_id,)).fetchone():
                raise UnknownMistakeReferenceError("找不到孩子。")
            rows = connection.execute(
                """
                SELECT mc.cluster_id, mc.knowledge_node_id, mc.subject, mc.title,
                       mc.diagnosis_status, mc.occurrence_count, mc.relapse_count,
                       mc.updated_at, ms.mastery_probability, ms.hint_dependency,
                       ms.status_confidence, ms.next_review_at
                FROM mistake_cluster mc
                LEFT JOIN mastery_state ms ON ms.child_id = mc.child_id
                  AND ms.knowledge_node_id = mc.knowledge_node_id
                WHERE mc.child_id = ?
                  AND EXISTS (SELECT 1 FROM mistake_case c WHERE c.cluster_id=mc.cluster_id AND c.status!='excluded')
                ORDER BY mc.relapse_count DESC, mc.occurrence_count DESC, mc.updated_at DESC
                """,
                (child_id,),
            ).fetchall()
            from .learning_state import LearningStateService
            context = LearningStateService._current_learning_context(connection, child_id)
            items = []
            for row in rows:
                current = LearningStateService._current_node_evidence(context, row["knowledge_node_id"])
                items.append({**dict(row), "current_evidence": current, "readiness": current["readiness"],
                    "mastery_fields_source": "historical_cache", "mastery_claim_allowed": False})
            return {"items": items}

    def next_activity(self, mistake_id: str, child_id: str) -> dict:
        with self.database.connect() as connection:
            mistake = self._owned_case(connection, mistake_id, child_id)
            gate = self._gate_summary(connection, mistake_id)
            mistake = {**dict(mistake), "status": self._gate_status(mistake["status"], gate)}
            if mistake["status"] == "awaiting_confirmation":
                return {"kind": "confirmation", "stage": None, "item": None}
            if mistake["status"] in ("diagnosing", "relapsed"):
                return {"kind": "diagnosis", "stage": None, "item": None}
            if mistake["status"] == "remediating":
                return {"kind": "remediation", "stage": None, "item": None}
            if mistake["status"] == "mastered":
                return {"kind": "mastered", "stage": None, "item": None}
            if mistake["status"] == "excluded":
                return {"kind": "excluded", "stage": None, "item": None}
            if not gate or not gate["missing"]:
                return {"kind": "complete", "stage": None, "item": None}
            stage = gate["missing"][0]
            due_at = gate.get(f"{stage}_due_at") if stage in ("delayed_48h", "delayed_7d") else None
            if due_at and _parse_time(due_at) > datetime.now(timezone.utc):
                return {"kind": "scheduled", "stage": stage, "due_at": due_at, "item": None}
            transfer_stage = stage in TRANSFER_STAGES
            transfer_clause = """
              AND di.diagnostic_item_id != ?
              AND NOT EXISTS (
                SELECT 1 FROM mistake_attempt used
                WHERE used.child_id = ? AND used.diagnostic_item_id = di.diagnostic_item_id
              )
            """ if transfer_stage else ""
            parameters: list[object] = [mistake_id, mistake["knowledge_node_id"]]
            if transfer_stage:
                parameters.extend([mistake["diagnostic_item_id"] or "", child_id])
            scope, scope_parameters = self._item_scope(connection, child_id, stage, mistake_id)
            parameters.extend(scope_parameters)
            parameters.append(mistake["diagnostic_item_id"] or "")
            candidates = connection.execute(
                f"""
                SELECT di.diagnostic_item_id, di.prompt, di.answer_type,
                       di.difficulty, di.assessment_mode,
                       COUNT(ma.attempt_id) AS prior_attempts
                FROM diagnostic_item di
                LEFT JOIN mistake_attempt ma
                  ON ma.diagnostic_item_id = di.diagnostic_item_id
                 AND ma.mistake_id = ?
                WHERE di.knowledge_node_id = ? AND di.status = 'active'
                  {transfer_clause}
                  {scope}
                GROUP BY di.diagnostic_item_id
                ORDER BY CASE WHEN di.diagnostic_item_id = ? THEN 1 ELSE 0 END,
                         CASE WHEN COUNT(ma.attempt_id) = 0 THEN 0 ELSE 1 END,
                         COUNT(ma.attempt_id), di.difficulty, di.diagnostic_item_id
                """,
                parameters,
            ).fetchall()
            identities, seen = self._question_history(connection, child_id) if transfer_stage else ({}, {})
            original_identity = identities.get(mistake["diagnostic_item_id"])
            # Prefer a source-qualified assessment over reference-only practice;
            # preserve the existing freshness/difficulty order within each group.
            candidates = sorted(candidates, key=lambda row: not LearningStateService._qualified_review_item(
                connection, child_id, row['diagnostic_item_id']))
            item = next((row for row in candidates if not transfer_stage or (
                identities.get(row["diagnostic_item_id"]) not in seen
                and identities.get(row["diagnostic_item_id"]) != original_identity)), None)
            if not item:
                if self._table_exists(connection, "variant_candidate"):
                    waiting = connection.execute(
                        """
                        SELECT COUNT(*) AS count FROM variant_candidate
                        WHERE mistake_id = ? AND status = 'needs_review' AND stage = ?
                        """,
                        (mistake_id, stage),
                    ).fetchone()["count"]
                    if waiting:
                        return {
                            "kind": "awaiting_parent_review", "stage": stage,
                            "candidate_count": int(waiting),
                            "reason": "新的变式题已通过本地结构校验，正在等待家长核对答案后启用。",
                            "item": None,
                        }
                if self._table_exists(connection, "variant_generation_job"):
                    job = connection.execute(
                        """
                        SELECT status, error_code FROM variant_generation_job
                        WHERE mistake_id = ? ORDER BY created_at DESC LIMIT 1
                        """,
                        (mistake_id,),
                    ).fetchone()
                    if job and job["status"] in {"pending", "processing"}:
                        return {
                            "kind": "preparing_variant", "stage": stage,
                            "reason": "正在准备并校验新的变式题，不会重复原题。", "item": None,
                        }
                reason = (
                    "当前知识点没有尚未做过或讲解过且经过验证的变式题，不能用重复原题冒充迁移证据。"
                    if transfer_stage else "当前知识点还没有经过验证的复测题。"
                )
                return {"kind": "blocked", "stage": stage, "reason": reason, "item": None}
            serialized = dict(item)
            freshness = "new" if int(serialized.pop("prior_attempts") or 0) == 0 else "spaced_reuse"
            return {
                "kind": "attempt", "stage": stage, "due_at": due_at,
                "variant_freshness": freshness, "item": serialized,
            }

    @staticmethod
    def remediation_prompt(detail: dict) -> str:
        diagnosis = detail.get("diagnosis") or {}
        prompts = {
            "concept_gap": "先不用原来的步骤。请用自己的话说出这道题真正考的概念，再举一个反例。",
            "prerequisite_gap": "先退回前置知识。请说出这一步依赖的公式或规则，以及它什么时候能用。",
            "reading_gap": "先不计算。请把已知条件、要解决的问题和容易漏掉的限制分别说一遍。",
            "method_gap": "请比较两种可能方法，说出你为什么选择其中一种，再只完成关键第一步。",
            "execution_error": "请按符号、括号、单位、抄写四项逐项检查，并说出你发现的具体位置。",
            "retrieval_failure": "先遮住答案，回忆关键规则，再用一句话说明它如何用于这道题。",
            "transfer_gap": "请先说原题方法不变的核心，再指出新题表面上改变了什么。",
            "incidental": "请独立重做关键一步，并说明这次用什么检查避免偶发失误。",
            "unknown": "先说你从哪一步开始不确定，以及当时为什么选择了那个答案。",
        }
        return prompts.get(str(diagnosis.get("error_type") or "unknown"), prompts["unknown"])

    def _evaluate_progress(self, connection, mistake, stage: str, now: str, client_action_id: str) -> str:
        projections = project_child_mastery_evidence(connection, mistake["child_id"], as_of=now)
        effective = {row["evidence_id"]: row for row in projections.get(mistake["knowledge_node_id"], {}).get("effective_evidence", [])}
        latest = connection.execute(
            "SELECT evidence_id FROM mistake_attempt WHERE client_action_id = ?",
            (client_action_id,),
        ).fetchone()
        latest = effective.get(latest["evidence_id"])
        if latest and stage in {"delayed_48h", "delayed_7d"} and (latest["correctness"] < 1 or not latest["independent"]):
            interval = timedelta(hours=48) if stage == "delayed_48h" else timedelta(days=7)
            connection.execute(
                f"UPDATE mistake_mastery_gate SET {stage}_due_at = ? WHERE mistake_id = ?",
                ((_parse_time(now) + interval).isoformat(), mistake["mistake_id"]),
            )
        recent = connection.execute(
            """
            SELECT evidence_id FROM mistake_attempt
            WHERE mistake_id = ? AND stage = ?
              AND created_at > COALESCE((SELECT MAX(created_at) FROM remediation_step WHERE mistake_id = ?), '')
            ORDER BY created_at DESC LIMIT 2
            """,
            (mistake["mistake_id"], stage, mistake["mistake_id"]),
        ).fetchall()
        recent = [effective[row["evidence_id"]] for row in recent if row["evidence_id"] in effective]
        current = connection.execute("SELECT status, cluster_id FROM mistake_case WHERE mistake_id = ?", (mistake["mistake_id"],)).fetchone()
        if latest and len(recent) == 2 and all(float(row["correctness"]) < 1 for row in recent):
            if current["status"] != "remediating":
                self._transition(connection, mistake["mistake_id"], mistake["child_id"], current["status"], "remediating", "同级连续两次未通过，停止加题并退回补救", f"{client_action_id}:status", now)
            return "return_to_remediation"
        gate = self._gate_summary(connection, mistake["mistake_id"], _projections=projections)
        missing = gate["missing"]
        if not missing:
            target = "mastered"
            next_action = "mastered"
        elif any(item in missing for item in ("repair_check", "near_transfer")):
            target = "near_transfer"
            next_action = "continue_near_transfer"
        elif any(item in missing for item in ("representation_transfer", "discrimination", "far_transfer")):
            target = "mixed_transfer"
            next_action = "continue_mixed_transfer"
        else:
            target = "fragile"
            next_action = "wait_for_delayed_review"
            if stage not in {"delayed_48h", "delayed_7d"}:
                # Spacing starts after successful immediate verification, not
                # an earlier diagnosis that may have sat unfinished for days.
                for delayed_stage, interval in (("delayed_48h", timedelta(hours=48)), ("delayed_7d", timedelta(days=7))):
                    if delayed_stage in missing:
                        connection.execute(
                            f"UPDATE mistake_mastery_gate SET {delayed_stage}_due_at = ? WHERE mistake_id = ?",
                            ((_parse_time(now) + interval).isoformat(), mistake["mistake_id"]),
                        )
        if current["status"] != target:
            self._transition(connection, mistake["mistake_id"], mistake["child_id"], current["status"], target, "根据掌握门槛更新错题阶段", f"{client_action_id}:status", now)
        if target == "mastered":
            remaining = connection.execute(
                "SELECT COUNT(*) FROM mistake_case WHERE cluster_id = ? AND status NOT IN ('mastered', 'excluded')",
                (current["cluster_id"],),
            ).fetchone()[0]
            if remaining == 0:
                connection.execute(
                    "UPDATE mistake_cluster SET diagnosis_status = 'stable', updated_at = ? WHERE cluster_id = ?",
                    (now, current["cluster_id"]),
                )
        elif target in ("mixed_transfer", "fragile"):
            connection.execute(
                "UPDATE mistake_cluster SET diagnosis_status = 'improving', updated_at = ? WHERE cluster_id = ?",
                (now, current["cluster_id"]),
            )
        return next_action

    @staticmethod
    def _candidate_requirements(requirements: dict) -> dict:
        return {stage: max(count, requirements.get(stage, 0))
                for stage, count in {**requirements, **GATE_REQUIREMENTS["unknown"]}.items()}

    @staticmethod
    def _gate_status(status: str, gate: dict | None) -> str:
        ranks = {"near_transfer": 0, "mixed_transfer": 1, "fragile": 2,
                 "delayed_verification": 2, "mastered": 3}
        if status not in ranks or not gate or not gate["missing"]:
            return status
        missing = gate["missing"][0]
        target = "near_transfer" if missing in {"repair_check", "near_transfer"} else "mixed_transfer" if missing in TRANSFER_STAGES else "fragile"
        return target if ranks[target] < ranks[status] else status

    @staticmethod
    def _question_history(connection, child_id: str) -> tuple[dict, dict]:
        # Use source-backed identities, including invalidated prior exposures,
        # exactly as the shared evidence projection does.
        catalog, identities = _question_catalog(connection)
        from .learning_evidence_withdrawal import replacement_map, terminal_replacement
        replacements = replacement_map(connection, child_id)
        first_seen = {}
        first_times = {}
        for exposure in connection.execute("""SELECT a.diagnostic_item_id,a.evidence_id,a.created_at FROM (
            SELECT diagnostic_item_id,evidence_id,created_at,child_id FROM diagnostic_attempt
            UNION ALL SELECT diagnostic_item_id,evidence_id,created_at,child_id FROM mistake_attempt) a
            JOIN learning_evidence le ON le.evidence_id=a.evidence_id AND le.child_id=a.child_id
            WHERE a.child_id=? AND julianday(a.created_at)<=julianday(?)
            ORDER BY julianday(a.created_at),le.rowid""", (child_id, utc_now())):
            identity = identities.get(exposure["diagnostic_item_id"])
            if identity:
                first_seen.setdefault(identity, terminal_replacement(replacements, exposure["evidence_id"]))
                first_times.setdefault(identity, _parse_time(exposure['created_at']))
        for identity, (occurred, reference) in tutor_question_history(
                connection, child_id, catalog, identities, as_of=utc_now()).items():
            # At equal timestamps, do not claim the answer preceded the hint.
            if identity not in first_times or occurred <= first_times[identity]:
                first_seen[identity] = reference
        return identities, first_seen

    def _gate_summary(self, connection, mistake_id: str, *, _projections=None, _retention_basis=None) -> dict | None:
        gate = connection.execute("SELECT * FROM mistake_mastery_gate WHERE mistake_id = ?", (mistake_id,)).fetchone()
        if not gate:
            return None
        requirements = self._candidate_requirements(json.loads(gate["requirements_json"]))
        case = connection.execute("SELECT child_id,knowledge_node_id,diagnostic_item_id FROM mistake_case WHERE mistake_id=?", (mistake_id,)).fetchone()
        projections = (_projections if _projections is not None else
                       project_child_mastery_evidence(connection, case["child_id"], as_of=utc_now()))
        effective = {row["evidence_id"]: row for row in projections.get(case["knowledge_node_id"], {}).get("effective_evidence", [])}
        rows = connection.execute(
            """
            SELECT ma.stage, ma.evidence_id, ma.created_at
            FROM mistake_attempt ma
            JOIN mistake_case mc ON mc.mistake_id = ma.mistake_id AND mc.child_id = ma.child_id
            JOIN learning_evidence le ON le.evidence_id = ma.evidence_id
              AND le.child_id = ma.child_id AND le.knowledge_node_id = mc.knowledge_node_id
            WHERE ma.mistake_id = ? AND ma.correctness >= 1 AND ma.independent = 1
              AND ma.hint_level = 0 AND le.correctness >= 1 AND le.independent = 1 AND le.hint_level = 0
              AND julianday(ma.created_at) >= julianday(?)
            ORDER BY julianday(ma.created_at), ma.rowid
            """,
            (mistake_id, gate["updated_at"]),
        ).fetchall()
        # Identity, score and independence are projected from current sources,
        # never from the persisted gate or the original confidence alone.
        identities, first_seen = self._question_history(connection, case["child_id"])
        passed = {}
        pass_times = {}
        pass_evidence = {}
        delayed = []
        used_transfer = {identities.get(case["diagnostic_item_id"])}
        for row in rows:
            receipt = effective.get(row["evidence_id"])
            if (not receipt or receipt["mistake_id"] != mistake_id or not receipt["independent"]
                    or receipt["hint_level"] or receipt["correctness"] != 1):
                continue
            identity, stage = receipt["question_identity"], row["stage"]
            if stage in {"delayed_48h", "delayed_7d"}:
                delayed.append(receipt)
                continue
            if stage in TRANSFER_STAGES:
                if identity in used_transfer or first_seen.get(identity) != row["evidence_id"]:
                    continue
                used_transfer.add(identity)
            passed.setdefault(stage, set()).add(identity)
            pass_times.setdefault(stage, []).append(_parse_time(row["created_at"]))
            pass_evidence.setdefault(stage, []).append(row['evidence_id'])
        immediate = {stage: count for stage, count in requirements.items() if stage not in {"delayed_48h", "delayed_7d"} and count}
        complete_immediate = all(len(passed.get(stage, ())) >= count for stage, count in immediate.items())
        anchor = max(t for stage, count in immediate.items() for t in pass_times[stage][:count]) if complete_immediate else None
        due_dates = {}
        for stage, interval in (("delayed_48h", timedelta(hours=48)), ("delayed_7d", timedelta(days=7))):
            stored = _parse_time(gate[f"{stage}_due_at"]) if gate[f"{stage}_due_at"] else None
            due = max(stored, anchor + interval) if stored and anchor else anchor + interval if anchor else stored
            due_dates[f"{stage}_due_at"] = due.isoformat() if due else None
            if anchor and due:
                for receipt in delayed:
                    if receipt["stage"] == stage and _parse_time(receipt["occurred_at"]) >= due:
                        passed.setdefault(stage, set()).add(receipt["question_identity"])
                        if _retention_basis is not None:
                            _retention_basis[receipt['evidence_id']] = sorted({
                                evidence for required_stage, count in immediate.items()
                                for evidence in pass_evidence[required_stage][:count]})
        satisfied = {stage: len(identities) for stage, identities in passed.items()}
        missing = [stage for stage in STAGE_ORDER if requirements.get(stage, 0) > satisfied.get(stage, 0)]
        return {
            "error_type": gate["error_type"],
            "requirements": requirements,
            "satisfied": satisfied,
            "missing": missing,
            **due_dates,
        }

    @staticmethod
    def _score_answer(item, answer: str) -> tuple[float | None, str]:
        phonics = PHONICS_ANSWERS.get(item["diagnostic_item_id"])
        if not phonics:
            return LearningStateService._score_answer(item, answer)
        target, peers = phonics
        sound, peer_sound = PHONICS_SOUNDS[target]
        # These seed rubrics mix the answer with distractors. Only a clear
        # selection or a supported comparison proves the odd-one-out answer.
        def normalize(text: str) -> str:
            return re.sub(r"[\s，,。.!！;；]+", "", text.lower())

        correct = normalize(answer) == normalize(item["explanation"])
        if not correct:
            choice = rf"(?:(?:i(?:choose|select)|theansweris|答案是|我选|选择|选|是){target}|{target}(?:/(?:{sound})/)?(?:不同|的?(?:读音|发音)(?:是)?不同|isdifferent|istheoddoneout)?)"
            pairs = (rf"{peers[0]}(?:and|和|与)?{peers[1]}", rf"{peers[1]}(?:and|和|与)?{peers[0]}")
            same_words = r"(?:一样|相同|读音相同|发音相同|arethesame|havethesamesound)"
            same = rf"(?:{'|'.join(pairs)})(?:{same_words}|/(?:{peer_sound})/{same_words}?)"
            clauses = [normalize(part) for part in re.split(r"[,，。.!！;；]|\bbecause\b|\bso\b|因为|所以", answer.lower()) if normalize(part)]
            correct = bool(clauses) and any(re.fullmatch(choice, part) for part in clauses) and all(
                re.fullmatch(rf"(?:{choice}|{same})", part) for part in clauses
            )
        return (
            (1.0, "回答正确。") if correct else
            (0.0, f"还没有得到明确且一致的读音判断。参考思路：{item['explanation']}")
        )

    @staticmethod
    def _item_scope(connection, child_id: str, stage: str | None = None, mistake_id: str | None = None) -> tuple[str, list[object]]:
        from .local_supply import LocalSupplyError, validate_item
        supplied = connection.execute("""SELECT diagnostic_item_id FROM diagnostic_item
            WHERE status='active' AND (source_ref LIKE 'parent_approved_%' OR source_ref LIKE 'local_verified_%')""").fetchall()
        eligible = []
        for row in supplied:
            try:
                item = validate_item(connection, child_id, row[0],
                                     stage=stage if stage in TRANSFER_STAGES or stage in {'delayed_48h', 'delayed_7d'} else None,
                                     mistake_id=mistake_id)
                # validate_item enforces explicit stage/root review for specialized
                # sources too; generic parallel drills remain ineligible.
                eligible.append(row[0])
            except LocalSupplyError:
                continue
        ordinary = "COALESCE(di.source_ref,'') NOT LIKE 'parent_approved_%' AND COALESCE(di.source_ref,'') NOT LIKE 'local_verified_%'"
        return "AND ((" + ordinary + ") OR di.diagnostic_item_id IN (" + ",".join("?" for _ in eligible) + "))", eligible

    def _detail(self, connection, mistake_id: str, *, _projections=None) -> dict:
        row = connection.execute(
            """
            SELECT mc.*, cluster.diagnosis_status, cluster.occurrence_count,
                   cluster.relapse_count, kn.title AS knowledge_title, kn.code AS knowledge_code,
                   kn.track AS knowledge_track
            FROM mistake_case mc
            JOIN mistake_cluster cluster ON cluster.cluster_id = mc.cluster_id
            JOIN knowledge_node kn ON kn.knowledge_node_id = mc.knowledge_node_id
            WHERE mc.mistake_id = ?
            """,
            (mistake_id,),
        ).fetchone()
        if not row:
            raise UnknownMistakeError(mistake_id)
        result = dict(row)
        from .paper_formula_review import for_mistake
        review = for_mistake(connection, mistake_id, row['child_id'])
        if review:
            result['question_review'] = {**review, 'local_only': True, 'cloud_transmission_allowed': False}
            if review['status'] == 'confirmed':
                result['recorded_prompt_summary'] = result['prompt_summary']
                result['prompt_summary'] = review['question']
        result["knowledge_node"] = {
            "knowledge_node_id": result.pop("knowledge_node_id"),
            "title": result.pop("knowledge_title"),
            "code": result.pop("knowledge_code"),
            "track": result.pop("knowledge_track"),
        }
        result["mastery_gate"] = self._gate_summary(connection, mistake_id, _projections=_projections)
        result["recorded_status"] = result["status"]
        result["status"] = self._gate_status(result["status"], result["mastery_gate"])
        if result["status"] != result["recorded_status"] and result["diagnosis_status"] in {"stable", "improving"}:
            result["diagnosis_status"] = "suspected"
        latest_diagnosis = connection.execute(
            "SELECT error_type, explanation, confidence, status, created_at FROM mistake_diagnosis WHERE mistake_id = ? ORDER BY created_at DESC LIMIT 1",
            (mistake_id,),
        ).fetchone()
        result["diagnosis"] = dict(latest_diagnosis) if latest_diagnosis else None
        return result

    @staticmethod
    def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone() is not None

    @staticmethod
    def _ensure_cluster(connection, child_id: str, knowledge_node_id: str, subject: str, title: str, now: str):
        row = connection.execute(
            "SELECT * FROM mistake_cluster WHERE child_id = ? AND knowledge_node_id = ?",
            (child_id, knowledge_node_id),
        ).fetchone()
        if row:
            return row
        cluster_id = str(uuid4())
        connection.execute(
            "INSERT INTO mistake_cluster(cluster_id, child_id, knowledge_node_id, subject, title, diagnosis_status, occurrence_count, relapse_count, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'suspected', 0, 0, ?, ?)",
            (cluster_id, child_id, knowledge_node_id, subject, title, now, now),
        )
        return connection.execute("SELECT * FROM mistake_cluster WHERE cluster_id = ?", (cluster_id,)).fetchone()

    @staticmethod
    def _refresh_cluster_pattern(connection, cluster_id: str, now: str) -> None:
        row = connection.execute("SELECT occurrence_count, diagnosis_status FROM mistake_cluster WHERE cluster_id = ?", (cluster_id,)).fetchone()
        if row and row["occurrence_count"] >= 2 and row["diagnosis_status"] == "suspected":
            connection.execute(
                "UPDATE mistake_cluster SET diagnosis_status = 'emerging_pattern', updated_at = ? WHERE cluster_id = ?",
                (now, cluster_id),
            )

    @staticmethod
    def _transition(connection, mistake_id: str, child_id: str, from_status: str | None, to_status: str, reason: str, client_action_id: str, now: str) -> None:
        existing = connection.execute("SELECT 1 FROM mistake_status_event WHERE client_action_id = ?", (client_action_id,)).fetchone()
        if existing:
            return
        connection.execute("UPDATE mistake_case SET status = ?, updated_at = ? WHERE mistake_id = ? AND child_id = ?", (to_status, now, mistake_id, child_id))
        connection.execute(
            "INSERT INTO mistake_status_event(event_id, mistake_id, child_id, from_status, to_status, reason, created_at, client_action_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (str(uuid4()), mistake_id, child_id, from_status, to_status, reason, now, client_action_id),
        )

    @staticmethod
    def _owned_case(connection, mistake_id: str, child_id: str):
        row = connection.execute("SELECT * FROM mistake_case WHERE mistake_id = ? AND child_id = ?", (mistake_id, child_id)).fetchone()
        if not row:
            raise UnknownMistakeError(mistake_id)
        return row


class UnknownMistakeError(Exception):
    pass


class UnknownMistakeReferenceError(Exception):
    pass


class InvalidMistakeStateError(Exception):
    pass


class InvalidMistakeInputError(Exception):
    pass


class MistakeReviewNotDueError(Exception):
    def __init__(self, due_at: str):
        self.due_at = due_at
        super().__init__(due_at)
