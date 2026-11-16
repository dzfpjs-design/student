"""Local, durable conversational focus for camera mistakes and spaced reviews."""
from __future__ import annotations

import hashlib
from contextlib import nullcontext
import json
import re
from uuid import uuid4

from .database import Database
from .mistake_book import MistakeBookService, utc_now
from .mistake_capture import MistakeCaptureService, InvalidMistakeCaptureCandidateError
from .paper_scoring import PaperScoringService
from .learning_state import LearningStateService
from .learning_obligations import qualified_exact_score
from .local_supply import LocalSupplyError, validate_item


def subject_for_question(text: str, fallback: str = "数学") -> str:
    words = re.findall(r"[A-Za-z]{2,}", text)
    if len(words) >= 3 and (re.search(r"读音|划线|字母|单词|音标|英语|选词|填空|对话", text)
                            or len(words) >= len(re.findall(r"[\u4e00-\u9fff]", text))):
        return "英语"
    for subject, pattern in (("化学", r"元素|化学方程式|质量分数"), ("物理", r"电流|电压|电阻|浮力|压强|功率")):
        if re.search(pattern, text):
            return subject
    return fallback


def spoken_question_number(value: str) -> int:
    if value.isdigit():
        return int(value)
    digits = {char: index for index, char in enumerate("零一二三四五六七八九")}
    digits["两"] = 2
    if "十" in value:
        tens, ones = value.split("十", 1)
        return digits.get(tens, 1) * 10 + digits.get(ones, 0)
    return digits.get(value, 0)


class MistakeDialogueService:
    def __init__(self, database: Database):
        self.database = database
        self.book = MistakeBookService(database)
        self.capture = MistakeCaptureService(database, self.book)
        self.paper_scoring = PaperScoringService(database)
        self.learning = LearningStateService(database)
        self._ensure_scoring_reconciliation_schema()

    def _ensure_scoring_reconciliation_schema(self) -> None:
        with self.database.connect() as connection:
            if not hasattr(connection, "executescript"):
                return
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS paper_scoring_dialogue_reconciliation (
                  decision_id TEXT PRIMARY KEY
                    REFERENCES paper_scoring_decision(decision_id) ON DELETE CASCADE,
                  request_id TEXT NOT NULL UNIQUE
                    REFERENCES paper_scoring_request(request_id) ON DELETE CASCADE,
                  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                  session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
                  outcome TEXT NOT NULL CHECK(outcome IN (
                    'mistake_created','correct_verification_scheduled','incorrect_unmapped'
                  )),
                  mistake_id TEXT REFERENCES mistake_case(mistake_id) ON DELETE SET NULL,
                  created_at TEXT NOT NULL,
                  client_action_id TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS paper_scoring_dialogue_revision_reconciliation (
                  revision_id TEXT PRIMARY KEY
                    REFERENCES paper_scoring_decision_revision(revision_id) ON DELETE CASCADE,
                  request_id TEXT NOT NULL
                    REFERENCES paper_scoring_request(request_id) ON DELETE CASCADE,
                  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                  session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
                  outcome TEXT NOT NULL CHECK(outcome IN (
                    'mistake_created','correct_verification_scheduled','incorrect_unmapped'
                  )),
                  mistake_id TEXT REFERENCES mistake_case(mistake_id) ON DELETE SET NULL,
                  created_at TEXT NOT NULL,
                  client_action_id TEXT NOT NULL UNIQUE
                );
                """
            )
            connection.commit()

    def _active_child(self, session_id: str) -> str:
        with self.database.connect() as c:
            row = c.execute("SELECT child_id FROM daily_session WHERE session_id=? AND status='active' AND state='study'",
                            (session_id,)).fetchone()
        if not row:
            raise InvalidMistakeCaptureCandidateError("当前学习会话已结束或已切换孩子，请回到正在进行的会话。")
        return row["child_id"]

    def _child(self, session_id: str) -> str:
        with self.database.connect() as c:
            row = c.execute("SELECT child_id FROM daily_session WHERE session_id = ?", (session_id,)).fetchone()
        if not row:
            raise InvalidMistakeCaptureCandidateError("找不到当前学习会话。")
        return row["child_id"]

    def _hold_foundation(self, focus):
        if not focus['state'].get('foundation_origin'):
            return False
        from .foundation_remediation import continuation_hold
        with self.database.connect() as c:
            hold = continuation_hold(c, focus)
        if hold:
            focus['state']['foundation_hold'] = {**(focus['state'].get('foundation_hold') or {}), **hold}
            focus['state'].pop('teaching_requested', None)
        else:
            focus['state'].pop('foundation_hold', None)
        return bool(hold)

    def state(self, session_id: str) -> dict | None:
        child_id = self._child(session_id)
        with self.database.connect() as c:
            c.execute('BEGIN')
            row = c.execute("SELECT * FROM mistake_dialogue WHERE session_id = ? AND child_id = ?", (session_id, child_id)).fetchone()
            from .agent_runtime import AgentRuntimeStore
            answer_target = AgentRuntimeStore.local_mistake_answer_target(c, session_id, child_id) if row else None
            from .mistake_dialogue_reference import current_binding
            dialogue_binding = current_binding(c, session_id, child_id) if row else None
        if not row:
            return None
        value = dict(row)
        value["state"] = json.loads(value.pop("state_json"))
        value['answer_binding'] = (answer_target or {}).get('binding')
        value['dialogue_binding'] = dialogue_binding
        self._refresh_diagnosis(value)
        self._hold_foundation(value)
        return value

    def _refresh_diagnosis(self, focus):
        if focus.get("mistake_id") and focus["state"].get("diagnosis"):
            current = self.book.diagnosis_state(focus["mistake_id"], focus["child_id"])
            focus["state"]["diagnosis"] = {k: current[k] for k in
                ("version", "projection_version", "hypotheses", "assessment", "next_action")}
            action = current['next_action']
            pending = focus['state'].get('question_confirmation') or {}
            review = self.book.get(focus['mistake_id'], focus['child_id']).get('question_review') if pending else None
            current_readback = bool(review and review['status'] == 'confirmed'
                and all(review.get(k) == pending.get(k) for k in ('review_id', 'version', 'question')))
            if (focus.get('phase') not in {'paused', 'closed'} and not focus['state'].get('pending_operation')
                    and focus['state'].get('question_confirmation', {}).get('mode') != 'source_repair'
                    and not current_readback
                    and not (focus['state'].get('teaching_requested') and not focus['state'].get('teaching_turn_id')
                             and self.teaching_request(focus))
                    and (action['kind'] == 'revise_hypotheses'
                         or action.get('reason') == 'original_question_review_required')):
                self._sync(focus, advance=False)

    def _save(self, focus: dict, message: str, action_id: str, payload: str) -> dict:
        if (focus.get('phase') == 'remediation' and focus.get('mistake_id')
                and focus['state'].get('question_confirmation', {}).get('mode') != 'source_repair'
                and focus['state'].get('teaching_requested')
                and not (focus['state'].get('next_activity') or {}).get('item')):
            review = self.book.get(focus['mistake_id'], focus['child_id']).get('question_review')
            if review and review['status'] == 'confirmed' and not self.teaching_request(focus):
                from .mistake_question_confirmation import offer
                message = offer(focus, review, action_id, self.database)
        result = {**focus, "response_text": message, "cloud_transmission_allowed": False}
        with self.database.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            previous = c.execute("SELECT * FROM mistake_dialogue_event WHERE client_action_id=?", (action_id,)).fetchone()
            if previous:
                if (previous["session_id"] != focus["session_id"] or previous["child_id"] != focus["child_id"]
                        or previous["request_hash"] != self._hash(payload)):
                    raise InvalidMistakeCaptureCandidateError("对话动作与已保存内容冲突。")
                return json.loads(previous["result_json"])
            pending_row = c.execute("SELECT mistake_id,state_json FROM mistake_dialogue WHERE session_id=? AND child_id=?",
                                    (focus["session_id"], focus["child_id"])).fetchone()
            previous_state = json.loads(pending_row['state_json']) if pending_row else {}
            pending = previous_state.get('pending_operation')
            if pending and pending["action_id"] != action_id and focus["state"].get("pending_operation") != pending:
                raise InvalidMistakeCaptureCandidateError("上一份作答仍在保存，不能被另一个对话动作覆盖。")
            if (focus['state'].get('foundation_review_handoff') and pending_row
                    and pending_row['mistake_id'] == focus['mistake_id'] and previous_state.get('foundation_origin')
                    and not focus['state'].get('foundation_origin')):
                from .foundation_remediation import review_handoff
                handoff = review_handoff(c, {**focus, 'state': previous_state})
                if handoff != focus['state']['foundation_review_handoff']:
                    raise InvalidMistakeCaptureCandidateError('复习任务已经变化，保留原订正进度，未切换旧任务归属。')
            if (focus['state'].get('foundation_hold') or {}).get('reason') == 'time_budget':
                origin = focus['state']['foundation_origin']
                item = c.execute('''SELECT 1 FROM plan_item pi JOIN daily_plan dp USING(plan_id)
                    WHERE pi.plan_item_id=? AND dp.child_id=? AND dp.session_id=?
                    AND pi.status='active' AND pi.task_type='goal_prerequisite' ''',
                    (origin['plan_item_id'], focus['child_id'], focus['session_id'])).fetchone()
                if item:
                    from .repository import StudyRepository
                    StudyRepository(self.database).pause_plan_item(origin['plan_item_id'], action_id+':foundation-budget',
                        reason='foundation_time_budget', connection=c)
            self._write_focus(c, focus)
            from .agent_runtime import AgentRuntimeStore
            answer_target = AgentRuntimeStore.local_mistake_answer_target(c, focus['session_id'], focus['child_id'])
            result['answer_binding'] = (answer_target or {}).get('binding')
            from .mistake_dialogue_reference import current_binding
            result['dialogue_binding'] = current_binding(c, focus['session_id'], focus['child_id'])
            c.execute("INSERT INTO mistake_dialogue_event VALUES (?, ?, ?, ?, ?, ?)",
                (action_id, focus["session_id"], focus["child_id"], self._hash(payload),
                 json.dumps(result, ensure_ascii=False), utc_now()))
            c.commit()
        return result

    @staticmethod
    def _write_focus(c, focus):
        if not c.execute("SELECT 1 FROM daily_session WHERE session_id=? AND child_id=? AND status='active' AND state='study'",
                         (focus["session_id"], focus["child_id"])).fetchone():
            raise InvalidMistakeCaptureCandidateError("当前学习会话或孩子已变化。")
        if focus.get("mistake_id") and not c.execute("SELECT 1 FROM mistake_case WHERE mistake_id=? AND child_id=?",
                                                    (focus["mistake_id"], focus["child_id"])).fetchone():
            raise InvalidMistakeCaptureCandidateError("错题不属于当前孩子。")
        c.execute("""INSERT INTO mistake_dialogue(session_id,child_id,candidate_id,mistake_id,phase,state_json,updated_at)
            VALUES (?,?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET candidate_id=excluded.candidate_id,
            mistake_id=excluded.mistake_id,phase=excluded.phase,state_json=excluded.state_json,updated_at=excluded.updated_at
            WHERE mistake_dialogue.child_id=excluded.child_id""",
            (focus["session_id"], focus["child_id"], focus.get("candidate_id"), focus.get("mistake_id"),
             focus["phase"], json.dumps(focus["state"], ensure_ascii=False), utc_now()))

    def _checkpoint(self, focus, operation):
        with self.database.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT mistake_id,phase,state_json FROM mistake_dialogue WHERE session_id=? AND child_id=?",
                            (focus["session_id"], focus["child_id"])).fetchone()
            stored = json.loads(row["state_json"]) if row else {}
            previous = stored.get("pending_operation")
            if previous and previous != operation:
                raise InvalidMistakeCaptureCandidateError("上一份作答还在保存，请先重试或继续。")
            if row and not previous:
                self._answer_write_gate(c, focus, operation)
                def binding(state):
                    activity = state.get("next_activity") or {}
                    return (state.get("diagnosis_probe_id"), state.get("diagnosis_hinted"), state.get("attempt_hinted"),
                            activity.get("stage"), (activity.get("item") or {}).get("diagnostic_item_id"))
                if (row["mistake_id"] != focus.get("mistake_id") or row["phase"] != focus["phase"]
                        or binding(stored) != binding(focus["state"])):
                    raise InvalidMistakeCaptureCandidateError("对话已推进或提示条件已变化，请先读取当前问题。")
                if operation['kind'] == 'mastery_answer':
                    from .mistake_plan_completion import active_binding
                    operation['plan_item_binding'] = active_binding(c, focus)
            focus["state"]["pending_operation"] = operation
            self._write_focus(c, focus)
            c.commit()

    @staticmethod
    def _answer_write_gate(connection, focus, operation):
        from .mistake_pending_withdrawal import assert_not_withdrawn
        assert_not_withdrawn(connection, focus, operation)
        from .foundation_remediation import continuation_hold
        if continuation_hold(connection, focus):
            from .agent_runtime import InvalidAgentPlanError
            raise InvalidAgentPlanError('ANSWER_FOUNDATION_SCOPE_CHANGED')
        if operation.get('correction_of'):
            if operation.get('historical_scored_control'):
                from .mistake_cross_session_control import validate_operation
                validate_operation(connection, focus, operation)
                return
            from .mistake_pending_answer import explicit_replacement
            from .mistake_scored_correction import validate_binding, model_authorized_replacement
            validate_binding(connection, focus, operation['correction_of'])
            if (explicit_replacement(operation['utterance']) == operation['answer']
                    or model_authorized_replacement(connection, focus, operation['utterance'],
                                                    operation.get('authorized_turn_id')) == operation['answer']):
                return
        from .mistake_listening_delivery import assert_answer_ready
        assert_answer_ready(connection, focus, operation)
        origin = operation.get('authorized_turn_id')
        if origin is None:
            return
        from .agent_runtime import AgentRuntimeStore, InvalidAgentPlanError
        from .mistake_pending_replacement import replacement_authorized
        if replacement_authorized(connection, focus, operation):
            return
        target = AgentRuntimeStore.local_mistake_answer_target(connection, focus['session_id'], focus['child_id'], authorized_turn_id=origin)
        if target is None:
            raise InvalidAgentPlanError('ANSWER_TARGET_CHANGED')
        AgentRuntimeStore.validate_local_answer_write(connection, origin, 'mistake.dialogue',
            child_id=focus['child_id'], session_id=focus['session_id'], binding=target['binding'], answer=operation['utterance'])

    @staticmethod
    def _hash(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()

    def _replay(self, session_id: str, action_id: str, payload: str) -> dict | None:
        with self.database.connect() as c:
            row = c.execute("SELECT * FROM mistake_dialogue_event WHERE client_action_id = ?", (action_id,)).fetchone()
        if row:
            if row["session_id"] != session_id or row["child_id"] != self._child(session_id) or row["request_hash"] != self._hash(payload):
                raise InvalidMistakeCaptureCandidateError("这次对话动作已用于其他内容，不能重复覆盖。")
            result = json.loads(row["result_json"])
            self._refresh_diagnosis(result)
            gate = result["state"].get("last_mastery_gate")
            if gate and result.get("mistake_id"):
                current = self.book.get(result["mistake_id"], result["child_id"])["mastery_gate"]
                if current and any(current["satisfied"].get(stage, 0) < count for stage, count in gate["satisfied"].items()):
                    result["state"]["last_mastery_gate"] = current
                    result["response_text"] = "历史作答记录仍保留，但有效证据已变化，目前不能沿用当时的通过结论。" + self.prompt(result)
            return result
        return None

    def begin(self, session_id: str, artifact_id: str, action_id: str, candidate_id: str | None = None) -> dict:
        self._active_child(session_id)
        payload = json.dumps(["begin", artifact_id, candidate_id])
        replay = self._replay(session_id, action_id, payload)
        if replay:
            return replay
        child_id = self._child(session_id)
        with self.database.connect() as c:
            self.capture._owned_artifact(c, artifact_id, child_id)
        previous = self.state(session_id)
        if previous and previous["state"].get("artifact_id") == artifact_id and previous["phase"] not in {"closed", "paused"} and not candidate_id:
            return self._save(previous, self.prompt(previous), action_id, payload)
        from .mistake_recapture import receipt_target, begin as begin_recapture
        with self.database.connect() as c:
            recapture = receipt_target(c, session_id, child_id, artifact_id)
        if recapture:
            return begin_recapture(self, previous, recapture, artifact_id, candidate_id, action_id, payload)
        items = self.capture.prepare(artifact_id, child_id, f"{action_id}:prepare")["items"]
        candidate = next((x for x in items if x["status"] == "pending"
                          and not x.get("scoring_request_id")
                          and (not candidate_id or x["candidate_id"] == candidate_id)), None)
        if candidate_id and not candidate:
            raise InvalidMistakeCaptureCandidateError("这道候选题不在当前页面或已经处理。")
        focus = {"session_id": session_id, "child_id": child_id, "candidate_id": None, "mistake_id": None,
                 "phase": "closed", "state": {"artifact_id": artifact_id}}
        if candidate:
            self._select(focus, candidate)
        return self._save(focus, self.prompt(focus), action_id, payload)

    def _select(self, focus: dict, candidate: dict) -> None:
        state = {"artifact_id": candidate["artifact_id"], "question_text": candidate["question_text"],
                 "confidence": candidate["confidence"], "submitted_answer": "", "text_confirmed": False,
                 "subject": subject_for_question(candidate["question_text"]), "question_index": candidate["question_index"]}
        visual = self.capture.visual_answer_for_candidate(candidate["candidate_id"], focus["child_id"])
        if visual:
            state.update(
                submitted_answer=visual["answer_text"],
                answer_source="visual_unverified",
                visual_answer_evidence_id=visual["visual_answer_evidence_id"],
            )
        focus.update(candidate_id=candidate["candidate_id"], mistake_id=None, phase="candidate", state=state)
        self._match(focus)
        self._try_automatic_fact_scoring(
            focus, f"automatic-paper-fact:{candidate['candidate_id']}"
        )

    def _try_automatic_fact_scoring(self, focus: dict, action_id: str) -> dict | None:
        """Score only when OCR facts are strong; correctness never comes from OCR alone."""
        state = focus["state"]
        if (state.get("answer_source") != "visual_unverified"
                or not state.get("submitted_answer")
                or not state.get("visual_answer_evidence_id")
                or float(state.get("confidence") or 0) < .90):
            return None
        visual = self.capture.visual_answer_for_candidate(
            focus["candidate_id"], focus["child_id"], minimum_confidence=.85,
        )
        if (not visual
                or visual["visual_answer_evidence_id"] != state["visual_answer_evidence_id"]
                or not str(visual.get("answer_text") or "").strip()):
            return None
        state["automatic_fact_source"] = "qualified_visual_observation"
        state["automatic_fact_confidence"] = min(
            float(state["confidence"]), float(visual["confidence"])
        )
        return self._request_paper_scoring(
            focus, action_id, fact_source="qualified_visual_observation",
            fact_confidence=state["automatic_fact_confidence"],
        )

    def _capture_confirmed_candidate(self, focus: dict, action_id: str) -> dict:
        state = focus["state"]
        decision_id = state.get("scoring_decision_id")
        if decision_id:
            with self.database.connect() as connection:
                revision = connection.execute(
                    """SELECT 1 FROM paper_scoring_decision_revision
                       WHERE revision_id=? AND child_id=?""",
                    (decision_id, focus["child_id"]),
                ).fetchone()
            if revision:
                mistake = self.book.capture(
                    child_id=focus["child_id"],
                    knowledge_node_id=state["knowledge_node_id"],
                    subject=state["subject"], title=state["knowledge_title"],
                    prompt_summary=state["question_text"],
                    submitted_answer=state["submitted_answer"],
                    source_type="document_camera",
                    source_ref=f"{state['artifact_id']}#{focus['candidate_id']}",
                    confidence=float(state.get("confidence") or 1.0),
                    fingerprint_override=f"paper-scoring-revision:{decision_id}:{focus['candidate_id']}",
                    client_action_id=f"{action_id}:revision-mistake",
                )
                focus["mistake_id"] = mistake["mistake_id"]
                confirmed = self.book.confirm_from_qualified_scoring(
                    mistake["mistake_id"], focus["child_id"], decision_id,
                    state["scoring_authority_type"], f"{action_id}:qualified-score-confirm",
                )
                with self.database.connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        """INSERT OR IGNORE INTO paper_scoring_revision_mistake_link(
                             revision_id,mistake_id,child_id,created_at,client_action_id
                           ) VALUES(?,?,?,?,?)""",
                        (decision_id, mistake["mistake_id"], focus["child_id"],
                         utc_now(), f"{action_id}:revision-link"),
                    )
                    connection.commit()
                state["text_confirmed"] = True
                self._sync(focus)
                return {"mistake": confirmed}
        result = self.capture.capture(
            focus["candidate_id"], focus["child_id"], state["knowledge_node_id"],
            state["subject"], state["knowledge_title"], state["submitted_answer"],
            f"{action_id}:capture",
            promotion_source="qualified_scoring_authority",
        )
        focus["mistake_id"] = result["mistake"]["mistake_id"]
        authority_type = state.get("scoring_authority_type")
        if decision_id and authority_type:
            self.book.confirm_from_qualified_scoring(
                focus["mistake_id"], focus["child_id"], decision_id,
                authority_type, f"{action_id}:qualified-score-confirm",
            )
        else:
            self.book.confirm(
                focus["mistake_id"], focus["child_id"], "confirm", f"{action_id}:confirm"
            )
        state["text_confirmed"] = True
        self._sync(focus)
        return result

    def _request_paper_scoring(
        self, focus: dict, action_id: str, *,
        fact_source: str = "child_spoken_readback", fact_confidence: float = 1.0,
    ) -> dict:
        state = focus["state"]
        request = self.paper_scoring.request(
            candidate_id=focus["candidate_id"], child_id=focus["child_id"],
            session_id=focus["session_id"], question_text=state["question_text"],
            answer_text=state["submitted_answer"],
            answer_source=state.get("answer_source") or "child_spoken",
            visual_answer_evidence_id=state.get("visual_answer_evidence_id"),
            client_action_id=f"{action_id}:paper-scoring",
            fact_source=fact_source, fact_confidence=fact_confidence,
        )
        state.update(
            text_confirmed=True,
            scoring_request_id=request["request_id"],
            scoring_request_status=request["status"],
        )
        decision = self.paper_scoring.resolve(
            request["request_id"], focus["child_id"], f"{action_id}:paper-scoring-decision"
        )
        if not decision:
            focus["phase"] = "scoring_pending"
            return {"request": request, "decision": None}
        if decision.get("root_decision_id") and decision["decision_id"] != decision["root_decision_id"]:
            focus["phase"] = "scoring_pending"
            return {"request": request, "decision": None}
        return self._apply_paper_scoring_decision(focus, decision, action_id, request=request)

    def _apply_paper_scoring_decision(
        self, focus: dict, decision: dict, action_id: str, *, request: dict | None = None,
    ) -> dict:
        state = focus["state"]
        state.update(
            scoring_request_status="scored",
            scoring_decision_id=decision["decision_id"],
            scoring_authority_type=decision["authority_type"],
            scoring_authority_ref=decision["authority_ref"],
            scored_correctness=decision["correctness"],
        )
        if decision["correctness"] >= 1:
            if state.get("knowledge_node_id"):
                verification = self.paper_scoring.schedule_correct_verification(
                    decision_id=decision["decision_id"], child_id=focus["child_id"],
                    knowledge_node_id=state["knowledge_node_id"],
                    client_action_id=f"{action_id}:independent-verification",
                )
                state.update(
                    correct_verification_status=verification["status"],
                    correct_verification_obligation_id=verification["obligation_id"],
                    correct_verification_review_task_id=verification.get("review_task_id"),
                    correct_verification_due_at=verification.get("due_at"),
                )
            else:
                state["correct_verification_status"] = "blocked_knowledge_mapping"
            focus["phase"] = "scored_correct"
        elif state.get("knowledge_node_id"):
            self._capture_confirmed_candidate(focus, f"{action_id}:authority-scored-wrong")
        else:
            focus["phase"] = "scored_incorrect_unmapped"
        return {"request": request, "decision": decision}

    def reconcile_pending_scoring(self, limit: int = 8) -> list[dict]:
        """Apply late scoring decisions to the durable mistake workflow.

        Answer-key discovery is asynchronous.  A child must never need to say
        "continue" to make a qualified teacher/key/generated-rubric decision
        become a mistake or an independent correct-answer verification.
        """
        self._ensure_scoring_reconciliation_schema()
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT dialogue.*,request.request_id,decision.decision_id,
                          decision.root_decision_id,
                          decision.authority_type,decision.authority_ref,
                          decision.correctness,decision.confidence,
                          decision.authority_snapshot_json
                   FROM mistake_dialogue AS dialogue
                   JOIN paper_scoring_request AS request
                     ON request.candidate_id=dialogue.candidate_id
                    AND request.child_id=dialogue.child_id
                   JOIN paper_scoring_effective_decision AS decision USING(request_id)
                   LEFT JOIN paper_scoring_dialogue_reconciliation AS receipt
                     ON receipt.decision_id=decision.root_decision_id
                   LEFT JOIN paper_scoring_dialogue_revision_reconciliation AS revision_receipt
                     ON revision_receipt.revision_id=decision.decision_id
                   WHERE decision.effective_status='qualified'
                     AND ((decision.decision_id=decision.root_decision_id
                           AND dialogue.phase='scoring_pending' AND receipt.decision_id IS NULL)
                       OR (decision.decision_id<>decision.root_decision_id
                           AND revision_receipt.revision_id IS NULL))
                   ORDER BY dialogue.updated_at,decision.created_at LIMIT ?""",
                (max(1, min(int(limit), 50)),),
            ).fetchall()
        results = []
        for row in rows:
            state = json.loads(row["state_json"])
            if state.get("scoring_request_id") != row["request_id"]:
                continue
            decision = {
                "decision_id": row["decision_id"], "request_id": row["request_id"],
                "child_id": row["child_id"], "authority_type": row["authority_type"],
                "authority_ref": row["authority_ref"], "correctness": row["correctness"],
                "confidence": row["confidence"],
                "authority_snapshot": json.loads(row["authority_snapshot_json"]),
            }
            focus = {
                "session_id": row["session_id"], "child_id": row["child_id"],
                "candidate_id": row["candidate_id"], "mistake_id": None if row["phase"] != "scoring_pending" else row["mistake_id"],
                "phase": row["phase"], "state": state,
            }
            action_id = f"paper-scoring-reconcile:{row['decision_id']}"
            if (row["decision_id"] != row["root_decision_id"]
                    and decision["correctness"] == 0
                    and state.get("knowledge_node_id")):
                results.append(self._reconcile_revision_wrong(row, focus, decision, action_id))
                continue
            self._apply_paper_scoring_decision(focus, decision, action_id)
            outcome = (
                "mistake_created" if focus.get("mistake_id") else
                "correct_verification_scheduled" if decision["correctness"] >= 1 else
                "incorrect_unmapped"
            )
            from .paper_scoring import _run_reconciliation_withdrawal_fence
            def persist_reconciliation():
                with self.database.connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    current = connection.execute(
                        "SELECT phase,state_json FROM mistake_dialogue WHERE session_id=? AND child_id=?",
                        (row["session_id"], row["child_id"]),
                    ).fetchone()
                    current_state = json.loads(current["state_json"]) if current else {}
                    if current and current_state.get("scoring_request_id") == row["request_id"]:
                        connection.execute(
                            """UPDATE mistake_dialogue SET mistake_id=?,phase=?,state_json=?,updated_at=?
                               WHERE session_id=? AND child_id=?""",
                            (focus.get("mistake_id"), focus["phase"],
                             json.dumps(focus["state"], ensure_ascii=False), utc_now(),
                             row["session_id"], row["child_id"]),
                        )
                        root = connection.execute(
                            "SELECT decision_id FROM paper_scoring_decision WHERE request_id=?",
                            (row["request_id"],),
                        ).fetchone()
                        if root["decision_id"] == row["decision_id"]:
                            connection.execute(
                                """INSERT OR IGNORE INTO paper_scoring_dialogue_reconciliation(
                                     decision_id,request_id,child_id,session_id,outcome,mistake_id,
                                     created_at,client_action_id
                                   ) VALUES(?,?,?,?,?,?,?,?)""",
                                (row["decision_id"], row["request_id"], row["child_id"],
                                 row["session_id"], outcome, focus.get("mistake_id"), utc_now(), action_id),
                            )
                        else:
                            connection.execute(
                                """INSERT OR IGNORE INTO paper_scoring_dialogue_revision_reconciliation(
                                     revision_id,request_id,child_id,session_id,outcome,mistake_id,
                                     created_at,client_action_id
                                   ) VALUES(?,?,?,?,?,?,?,?)""",
                                (row["decision_id"], row["request_id"], row["child_id"],
                                 row["session_id"], outcome, focus.get("mistake_id"), utc_now(), action_id),
                            )
                            if decision["correctness"] >= 1:
                                from .paper_correct_revision_verification import resume_pending_supply
                                resumed = resume_pending_supply(connection, row["child_id"])
                                verification_id = focus["state"].get("correct_verification_obligation_id")
                                check = next(
                                    (item for item in resumed if item["obligation_id"] == verification_id), None
                                )
                                if check:
                                    focus["state"].update(
                                        correct_verification_status=check["status"],
                                        correct_verification_review_task_id=check.get("review_task_id"),
                                        correct_verification_due_at=check.get("due_at"),
                                    )
                                    connection.execute(
                                        "UPDATE mistake_dialogue SET state_json=? WHERE session_id=? AND child_id=?",
                                        (json.dumps(focus["state"], ensure_ascii=False),
                                         row["session_id"], row["child_id"]),
                                    )
                    connection.commit()
            _run_reconciliation_withdrawal_fence(self.database, persist_reconciliation)
            results.append({
                "decision_id": row["decision_id"], "outcome": outcome,
                "mistake_id": focus.get("mistake_id"), "phase": focus["phase"],
            })
        return results

    def _reconcile_revision_wrong(self, row, focus: dict, decision: dict, action_id: str) -> dict:
        state = focus["state"]
        capture_action = f"{action_id}:authority-scored-wrong"
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """SELECT 1 FROM paper_scoring_dialogue_revision_reconciliation
                   WHERE revision_id=?""",
                (decision["decision_id"],),
            ).fetchone()
            if existing:
                connection.rollback()
                return {"decision_id": decision["decision_id"], "outcome": "already_reconciled",
                        "mistake_id": None, "phase": focus["phase"]}
            mistake = self.book.capture(
                child_id=focus["child_id"], knowledge_node_id=state["knowledge_node_id"],
                subject=state["subject"], title=state["knowledge_title"],
                prompt_summary=state["question_text"], submitted_answer=state["submitted_answer"],
                source_type="document_camera",
                source_ref=f"{state['artifact_id']}#{focus['candidate_id']}",
                confidence=float(state.get("confidence") or 1.0),
                fingerprint_override=f"paper-scoring-revision:{decision['decision_id']}:{focus['candidate_id']}",
                client_action_id=f"{capture_action}:revision-mistake",
                _connection=connection,
                _defer_commit=True,
            )
            mistake_id = mistake["mistake_id"]
            self.book.confirm_from_qualified_scoring(
                mistake_id, focus["child_id"], decision["decision_id"],
                decision["authority_type"], f"{capture_action}:qualified-score-confirm",
                _connection=connection,
            )
            connection.execute(
                """INSERT INTO paper_scoring_revision_mistake_link(
                     revision_id,mistake_id,child_id,created_at,client_action_id
                   ) VALUES(?,?,?,?,?)""",
                (decision["decision_id"], mistake_id, focus["child_id"], utc_now(),
                 f"{capture_action}:revision-link"),
            )
            state.update(
                scoring_request_status="scored", scoring_decision_id=decision["decision_id"],
                scoring_authority_type=decision["authority_type"],
                scoring_authority_ref=decision["authority_ref"], scored_correctness=0,
                text_confirmed=True,
            )
            connection.execute(
                """UPDATE mistake_dialogue SET mistake_id=?,phase='diagnosis',state_json=?,updated_at=?
                   WHERE session_id=? AND child_id=?""",
                (mistake_id, json.dumps(state, ensure_ascii=False), utc_now(),
                 row["session_id"], row["child_id"]),
            )
            connection.execute(
                """INSERT INTO paper_scoring_dialogue_revision_reconciliation(
                     revision_id,request_id,child_id,session_id,outcome,mistake_id,
                     created_at,client_action_id
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (decision["decision_id"], row["request_id"], row["child_id"], row["session_id"],
                 "mistake_created", mistake_id, utc_now(), action_id),
            )
            connection.commit()
        return {"decision_id": decision["decision_id"], "outcome": "mistake_created",
                "mistake_id": mistake_id, "phase": "diagnosis"}

    @staticmethod
    def _scoring_notice(result: dict, focus: dict) -> str:
        decision = result.get("decision")
        if not decision:
            return ""
        source = {
            "teacher_mark": "老师的批改标记",
            "published_answer_key": "这套习题的标准答案",
            "agent_generated_rubric": "出题时保存的答案与评分规则",
        }[decision["authority_type"]]
        if decision["correctness"] >= 1:
            verification = focus.get("state", {}).get("correct_verification_status")
            if verification == "pending":
                return f"{source}显示这道题作答正确，不会加入错题本。我已经安排换一道题独立复测，复测前不会把它算作已掌握。"
            if verification == "pending_supply":
                return f"{source}显示这道题作答正确，不会加入错题本。当前还缺少合格的不同题面，我会保留复测任务，补齐后再验证掌握。"
            return f"{source}显示这道题作答正确，不会加入错题本；知识点还需独立复测后才能算掌握。"
        if focus.get("mistake_id"):
            return f"{source}显示这道题需要订正，我已经把它加入错题闭环。"
        return f"{source}显示这道题需要订正；知识点还在自动核对，核对完成后再进入错题闭环。"

    def _match(self, focus: dict) -> None:
        state = focus["state"]
        candidates = self.book.knowledge_candidates(focus["child_id"], state["question_text"], state["subject"])["items"]
        # A zero score is absence of evidence, not permission to pick an arbitrary skill.
        reliable = [item for item in candidates if item["match_score"] > 0.05]
        state["knowledge_candidates"] = reliable[:3]
        state["knowledge_node_id"] = reliable[0]["knowledge_node_id"] if reliable else None
        state["knowledge_title"] = reliable[0]["title"] if reliable else "待核对知识点"

    def _advance(self, focus: dict) -> None:
        items = self.capture.list(focus["state"]["artifact_id"], focus["child_id"])["items"]
        candidate = next((x for x in items if x["status"] == "pending"
                          and not x.get("scoring_request_id")), None)
        if candidate:
            self._select(focus, candidate)
        else:
            focus["phase"] = "closed"

    def open_review(self, session_id: str, action_id: str, *, mistake_id=None, origin=None) -> dict | None:
        child_id = self._active_child(session_id)
        payload = 'open_review' if mistake_id is None else json.dumps({'mistake_id': mistake_id, 'origin': origin}, sort_keys=True)
        replay = self._replay(session_id, action_id, payload)
        if replay:
            return replay
        current = self.book.get(mistake_id, child_id) if mistake_id else self.book.current_for_session(session_id)
        if not current:
            return None
        focus = {"session_id": session_id, "child_id": self._child(session_id), "candidate_id": None,
                 "mistake_id": current["mistake_id"], "phase": "diagnosis",
                 "state": {"question_text": current["prompt_summary"], "subject": current["subject"],
                           "submitted_answer": current["submitted_answer"]}}
        with self.database.connect() as c:
            previous = c.execute("SELECT session_id,state_json FROM mistake_dialogue WHERE child_id=? AND mistake_id=? ORDER BY updated_at DESC LIMIT 1",
                                 (child_id, current["mistake_id"])).fetchone()
        if previous:
            focus["state"].update(json.loads(previous['state_json']))
        focus['state'].pop('deferred_for_other_tasks', None)
        focus['state'].pop('resume_phase', None)
        if origin:
            focus['state']['foundation_origin'] = origin
        elif previous and previous['session_id'] != session_id:
            focus['state'].pop('foundation_origin', None)
            focus['state'].pop('foundation_hold', None)
        elif focus['state'].get('foundation_origin'):
            from .foundation_remediation import review_handoff
            with self.database.connect() as c:
                handoff = review_handoff(c, focus)
            if handoff:
                focus['state']['foundation_review_handoff'] = handoff
                focus['state'].pop('foundation_origin', None)
                focus['state'].pop('foundation_hold', None)
        review = current.get('question_review') or {}
        if (previous and previous['session_id'] != session_id
                and review.get('authority') in {'child_spoken_correction', 'child_confirmed_recapture'} and review.get('status') == 'confirmed'
                and not focus['state'].get('pending_operation') and not self._hold_foundation(focus)):
            # Reopening a revised original needs a fresh session readback, not
            # another source mutation or an automatic replacement exercise.
            for key in ('question_confirmation', 'ocr_confirmation_id', 'teaching_turn_id', 'next_activity', 'diagnosis_probe_id'):
                focus['state'].pop(key, None)
            focus['state'].update(question_text=review['question'], question_review_status='confirmed', teaching_requested=True)
            focus['phase'] = 'remediation'
            return self._save(focus, '我们先重新核对之前更正的原题。', action_id, payload)
        self._sync(focus)
        return self._save(focus, self.prompt(focus), action_id, payload)

    def _sync(self, focus: dict, *, advance=True) -> None:
        if not focus.get("mistake_id"):
            return
        if focus['state'].get('question_confirmation', {}).get('mode') == 'source_repair':
            focus['phase'] = 'remediation'
            return
        if self._hold_foundation(focus):
            return
        from .paper_formula_review import for_mistake
        with self.database.connect() as c:
            review = for_mistake(c, focus['mistake_id'], focus['child_id'])
        if review:
            focus['state']['question_review_status'] = review['status']
            focus['state']['question_text'] = review.get('question', review['original_question'])
        elif focus['state'].get('question_review_status'):
            focus['state']['question_review_status'] = 'invalidated'
            focus['state'].pop('question_text', None)
        activity = self.book.next_activity(focus["mistake_id"], focus["child_id"])
        focus["state"]["next_activity"] = activity
        kind = activity["kind"]
        focus["phase"] = {"confirmation": "confirm", "diagnosis": "diagnosis", "remediation": "remediation",
                          "attempt": "attempt", "scheduled": "scheduled"}.get(kind, "scheduled")
        state, case, child = focus["state"], focus["mistake_id"], focus["child_id"]
        if state.get("pending_operation"):
            focus["phase"] = "diagnosis_probe" if state["pending_operation"]["kind"] == "diagnosis_answer" else "attempt"
            return
        if kind in {"confirmation", "diagnosis"}:
            return
        diagnosis = self.book.diagnosis_state(case, child)
        if not diagnosis["version"] and advance:
            detail = self.book.get(case, child)
            candidate = detail.get("diagnosis") or {}
            error = candidate.get("error_type", "unknown")
            diagnosis = self.book.propose_diagnosis_hypotheses(case, child, [{"error_type": error,
                "stance": "unknown" if error == "unknown" else "candidate", "explanation": candidate.get("explanation") or "尚未确定错因",
                "confidence": candidate.get("confidence", .6)}], 0, f"mistake-dialogue:{case}:initial-hypotheses")
        state["diagnosis"] = {k: diagnosis[k] for k in ("version", "projection_version", "hypotheses", "assessment", "next_action")}
        round_id = self._hash(json.dumps(sorted({h["event_id"] for h in diagnosis["hypotheses"]})))
        state["diagnosis_round"] = round_id
        action = diagnosis["next_action"]["kind"]
        if action == 'revise_hypotheses' or diagnosis['next_action'].get('reason') == 'original_question_review_required':
            # Keep history, but never restore the old question or confirmation
            # as the active voice target after a source correction.
            for key in ('diagnosis_probe_id', 'question_confirmation', 'ocr_confirmation_id', 'teaching_requested'):
                state.pop(key, None)
            state['next_activity'] = dict(diagnosis['next_action'])
        if action in {"revise_hypotheses", "propose_hypotheses"}:
            focus["phase"] = "diagnosis"
            return
        if action == "blocked":
            focus["phase"] = ('remediation' if diagnosis['next_action'].get('reason') == 'original_question_review_required'
                              else 'scheduled')
            return
        probes = diagnosis["probes"]
        pending = next((p for p in probes if not p["binding_status"] and (not p["answer"] or not p["evidence"])), None)
        if not pending and state.get("diagnosis_verification_for") == round_id:
            return
        if not pending and state.get("diagnosis_remediation_for") == round_id:
            with self.database.connect() as c:
                step = c.execute("""SELECT outcome FROM remediation_step WHERE mistake_id=? AND child_id=?
                    AND rowid>? ORDER BY rowid DESC LIMIT 1""",
                    (case, child, state.get("diagnosis_remediation_after_rowid", 0))).fetchone()
            if step and step["outcome"] == "completed":
                state["diagnosis_verification_for"] = round_id
                return
            focus["phase"] = "remediation"
            return
        if action == "mastery_verification":
            state["diagnosis_verification_for"] = round_id
            return
        if not pending and action != "remediation" and len(probes) < 2 and advance:
            item = self._probe_item(focus, diagnosis)
            if item:
                # Without a registered discriminating prediction, collect an
                # observation with unknown cause, never invent a cause rubric.
                diagnosis = self.book.start_diagnosis_probe(case, child, item, {}, diagnosis["version"],
                    f"mistake-dialogue:{case}:probe:{round_id}:{item}")
                pending = next(p for p in diagnosis["probes"] if p["event_id"] == diagnosis["event_id"])
                state["diagnosis"] = {k: diagnosis[k] for k in ("version", "projection_version", "hypotheses", "assessment", "next_action")}
        if pending:
            state["diagnosis_probe_id"] = pending["event_id"]
            state["next_activity"] = {"kind": "diagnosis_probe", "item": pending["binding"]["item"],
                                      "question": pending["question"], "probe_event_id": pending["event_id"]}
            focus["phase"] = "diagnosis_probe"
            return
        if not advance:
            return
        detail = self.book.get(case, child)
        if detail["status"] in {"diagnosing", "remediating", "relapsed"}:
            state["diagnosis_remediation_for"] = round_id
            with self.database.connect() as c:
                state["diagnosis_remediation_after_rowid"] = c.execute("SELECT COALESCE(MAX(rowid),0) FROM remediation_step").fetchone()[0]
            state["teaching_requested"] = True
            focus["phase"] = "remediation"
        else:
            state["diagnosis_verification_for"] = round_id

    def _probe_item(self, focus, diagnosis):
        from .standard_relocation_provenance import business_snapshot
        with self.database.connect() as c, business_snapshot(c):
            case = c.execute("SELECT knowledge_node_id,diagnostic_item_id FROM mistake_case WHERE mistake_id=? AND child_id=?",
                             (focus["mistake_id"], focus["child_id"])).fetchone()
            identities, seen = self.book._question_history(c, focus["child_id"])
            excluded = set(seen) | {identities.get(case["diagnostic_item_id"])}
            excluded.update(p["binding"]["question_identity"] for p in diagnosis["probes"])
            for row in c.execute("SELECT diagnostic_item_id,expected_answer_json FROM diagnostic_item WHERE knowledge_node_id=? AND status='active' ORDER BY diagnostic_item_id", (case["knowledge_node_id"],)):
                item_id = row["diagnostic_item_id"]
                if identities.get(item_id) in excluded:
                    continue
                try:
                    validate_item(c, focus["child_id"], item_id, mistake_id=focus["mistake_id"])
                    answers = json.loads(row["expected_answer_json"]).get("exact_answers") or []
                    if answers and qualified_exact_score(c, focus["child_id"], item_id, str(answers[0]), mistake_id=focus["mistake_id"]):
                        return item_id
                except (LocalSupplyError, ValueError, TypeError, KeyError):
                    continue
        return None

    def _answer_probe(self, focus, text, action_id, *, supersedes=None, answer=None, authorized_turn_id=None):
        diagnosis = self.book.diagnosis_state(focus["mistake_id"], focus["child_id"])
        probe_id = focus["state"].get("diagnosis_probe_id")
        probe = next((p for p in diagnosis["probes"] if p["event_id"] == probe_id), None)
        if not probe or probe["binding_status"]:
            self._sync(focus)
            return self._save(focus, "刚才的题目或来源发生了变化，这次不会按另一道题评分。" + self.prompt(focus), action_id, text)
        operation = {"kind": "diagnosis_answer", "action_id": action_id, "utterance": text,
            "probe_event_id": probe_id, "answer": text if answer is None else answer, "supersedes_event_id": supersedes,
            "hint_level": 1 if focus["state"].get("diagnosis_hinted") == probe_id else 0}
        if authorized_turn_id is not None:
            operation['authorized_turn_id'] = authorized_turn_id
        if probe["answer"] and not supersedes:
            operation.update(answer_event_id=probe["answer"]["event_id"], answer=probe["answer"]["answer"])
        self._checkpoint(focus, operation)
        return self._resume_operation(focus, text, action_id)

    def _diagnosis_action_applied(self, focus, text, action_id):
        with self.database.connect() as c:
            old = c.execute("SELECT mistake_id,child_id,explanation FROM mistake_diagnosis WHERE client_action_id=?",
                            (action_id + ":diagnose",)).fetchone()
            if old:
                if (old["mistake_id"] != focus["mistake_id"] or old["child_id"] != focus["child_id"] or old["explanation"] != text):
                    raise InvalidMistakeCaptureCandidateError("诊断动作与已保存内容冲突。")
                return True
            if self.book._table_exists(c, "mistake_diagnosis_event"):
                old = c.execute("SELECT mistake_id,child_id,request_json FROM mistake_diagnosis_event WHERE client_action_id=?",
                                (action_id + ":hypotheses",)).fetchone()
                if old:
                    request = json.loads(old["request_json"])
                    if (old["mistake_id"] != focus["mistake_id"] or old["child_id"] != focus["child_id"]
                            or request["hypotheses"][0]["explanation"] != text):
                        raise InvalidMistakeCaptureCandidateError("诊断动作与已保存内容冲突。")
                    return True
        return False

    def _resume_operation(self, focus, text, action_id):
        state = focus["state"]
        operation = dict(state["pending_operation"])
        key = operation["action_id"]
        try:
            if operation["kind"] == "scored_withdrawal":
                result = self._withdraw_operation(focus, operation)
                state.pop('last_scored_answer', None)
                if (operation.get('withdrawal_of') or {}).get('kind') == 'diagnosis_answer':
                    state.pop('last_diagnosis_utterance', None)
                state.pop('diagnosis_remediation_for', None)
                state.pop('diagnosis_verification_for', None)
                prefix = ('已撤回刚才答案的评分效力；原作答仍保留在历史中，'
                          '但不再参与掌握或错因判断。')
            elif operation["kind"] == "diagnosis_answer":
                result = self._score_operation(focus, operation)
                state["last_diagnosis_probe_id"] = operation["probe_event_id"]
                state["last_diagnosis_utterance"] = operation["utterance"]
                state.pop("diagnosis_hinted", None)
                prefix = "回答和评分依据已保存；这些观察还不能确定错因。"
            else:
                result = self._score_operation(focus, operation)
                if result.get('plan_completion'):
                    focus['plan_completion'] = result['plan_completion']
                state.pop("attempt_hinted", None)
                state["last_mastery_gate"] = result.get("mastery_gate")
                score = result.get("attempt_result") or {}
                if score.get("score_status") == "verified_local" and score.get("independent") and score.get("correctness") == 1:
                    prefix = "这次独立回答通过了，进度已经保存。"
                elif score.get("score_status") in {"needs_review", "practice_only"} or not score:
                    prefix = "这次作答已保存，是否达到独立掌握要以当前有效证据和复测门槛为准。"
                else:
                    prefix = "这次还有需要订正的地方，先不用着急。"
            from .mistake_scored_correction import scored_binding
            with self.database.connect() as c:
                latest_scored = scored_binding(c, focus, operation)
            if latest_scored:
                state['last_scored_answer'] = latest_scored
            if operation.get('correction_of'):
                prefix = '更正后的答案和评分依据已保存；旧作答仍保留在历史中，但不再参与掌握或错因判断。'
            state.pop("pending_operation", None)
            state.pop("pending_error", None)
            self._sync(focus)
            saved = self._save(focus, prefix + self.prompt(focus), key, operation["utterance"])
            if action_id != key:
                return self._save(focus, saved["response_text"], action_id, text)
            return saved
        except Exception as error:
            from .agent_runtime import InvalidAgentPlanError
            if isinstance(error, InvalidAgentPlanError):
                raise
            state["pending_operation"] = operation
            state["pending_error"] = type(error).__name__
            self._checkpoint(focus, operation)
            return {**focus, "response_text": "刚才的回答已保存，后续处理还没有完成。说“重试”可以接着处理，不必重答。",
                    "cloud_transmission_allowed": False}

    def _withdraw_operation(self, focus, operation):
        from .diagnostic_prompt import _TransactionDatabase
        from .mistake_scored_correction import finalize_withdrawal
        from .parent_reports import _report_effect_guard
        # The borrowed scorer runs in this transaction; acquire the report
        # disclosure guard before the write lock, never from inside it.
        with _report_effect_guard(self.database, timeout=2), self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            book = MistakeBookService(_TransactionDatabase(c))
            result = finalize_withdrawal(c, focus, operation, book)
            c.commit()
            return result

    def _score_operation(self, focus, operation):
        from .diagnostic_prompt import _TransactionDatabase
        from .parent_reports import _report_effect_guard
        key = operation['action_id']
        # A correction revokes an existing scored evidence row. Ordinary new
        # answers remain available while a report is being transmitted.
        guard = _report_effect_guard(self.database) if operation.get('correction_of') else nullcontext()
        with guard, self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            from .mistake_pending_withdrawal import assert_not_withdrawn
            assert_not_withdrawn(c, focus, operation)
            scoring_focus = focus
            if focus['phase'] == 'paused':
                row = c.execute('SELECT phase,state_json,mistake_id FROM mistake_dialogue WHERE session_id=? AND child_id=?',
                                (focus['session_id'], focus['child_id'])).fetchone()
                current = json.loads(row['state_json']) if row else {}
                phase = current.get('resume_phase')
                if (not row or row['phase'] != 'paused' or row['mistake_id'] != focus['mistake_id']
                        or current.get('pending_operation') != operation or phase not in {'attempt', 'diagnosis_probe'}):
                    raise InvalidMistakeCaptureCandidateError('待恢复的原回答或暂停状态已经变化。')
                current.pop('resume_phase', None)
                current.pop('deferred_for_other_tasks', None)
                scoring_focus = {**focus, 'phase': phase, 'state': current}
                c.execute('UPDATE mistake_dialogue SET phase=?,state_json=? WHERE session_id=? AND child_id=?',
                    (phase, json.dumps(current, ensure_ascii=False), focus['session_id'], focus['child_id']))
            book = MistakeBookService(_TransactionDatabase(c))
            from .mistake_listening_delivery import assert_answer_ready, save_answer, settle as settle_listening
            listening_binding = (None if operation.get('correction_of') else
                                 assert_answer_ready(c, scoring_focus, operation))
            save_answer(c, listening_binding, operation)
            if operation['kind'] == 'mastery_answer':
                old = c.execute('SELECT 1 FROM mistake_attempt WHERE client_action_id=?', (key + ':attempt',)).fetchone()
                if not old:
                    self._answer_write_gate(c, scoring_focus, operation)
                result = book.submit_attempt(focus['mistake_id'], focus['child_id'], operation['stage'],
                    operation['item_id'], operation['answer'], operation['hint_level'], key + ':attempt',
                    _session_id=focus['session_id'],
                    _correction_of_attempt_id=(operation.get('correction_of') or {}).get('attempt_id'),
                    _trusted_prompt_id=(listening_binding or {}).get('prompt_id'),
                    _pet_media_scope=(listening_binding or {}).get('media_scope'))
                from .mistake_plan_completion import settle
                completion = settle(c, scoring_focus, operation, book)
                if completion:
                    result['plan_completion'] = completion
            else:
                diagnosis = book.diagnosis_state(focus['mistake_id'], focus['child_id'])
                probe = next(p for p in diagnosis['probes'] if p['event_id'] == operation['probe_event_id'])
                saved_answer = c.execute('SELECT event_id FROM mistake_diagnosis_event WHERE client_action_id=? AND child_id=? AND mistake_id=?',
                    (key + ':answer', focus['child_id'], focus['mistake_id'])).fetchone()
                answer_id = operation.get('answer_event_id') or (saved_answer[0] if saved_answer else None)
                old = c.execute('SELECT * FROM diagnostic_attempt WHERE client_action_id=?',
                                (f'mistake-diagnosis-score:{answer_id}',)).fetchone() if answer_id else None
                if not old:
                    self._answer_write_gate(c, scoring_focus, operation)
                if not operation.get('answer_event_id'):
                    answer_id = book.answer_diagnosis_probe(focus['mistake_id'], focus['child_id'], operation['probe_event_id'],
                        operation['answer'], diagnosis['version'], key + ':answer', operation.get('supersedes_event_id'))['event_id']
                item_id = probe['binding']['item']['diagnostic_item_id']
                if old:
                    conditions = c.execute('SELECT hint_level FROM learning_evidence WHERE evidence_id=? AND child_id=?',
                                           (old['evidence_id'], focus['child_id'])).fetchone()
                    if (old['child_id'] != focus['child_id']
                            or old['diagnostic_item_id'] != item_id or old['submitted_answer'] != operation['answer']
                            or not conditions or conditions[0] != operation['hint_level']):
                        raise InvalidMistakeCaptureCandidateError('评分回执归属、内容或提示条件冲突。')
                receipt = (LearningStateService._attempt(c, old['attempt_id']) if old else
                           LearningStateService(_TransactionDatabase(c)).submit_diagnostic(focus['session_id'], item_id,
                               operation['answer'], f'mistake-diagnosis-score:{answer_id}', hint_level=operation['hint_level']))
                current = book.diagnosis_state(focus['mistake_id'], focus['child_id'])
                book.attach_diagnosis_evidence(focus['mistake_id'], focus['child_id'], answer_id,
                    receipt['evidence_id'], current['version'], key + ':evidence')
                result = receipt
            settle_listening(c, listening_binding)
            if operation.get('correction_of'):
                from .mistake_scored_correction import finalize
                evidence_id = (c.execute('SELECT evidence_id FROM mistake_attempt WHERE client_action_id=?',
                               (key + ':attempt',)).fetchone()[0] if operation['kind'] == 'mastery_answer'
                               else receipt['evidence_id'])
                result['answer_correction'] = finalize(c, scoring_focus, operation, evidence_id, book)
            c.commit()
            if scoring_focus is not focus:
                focus['phase'] = scoring_focus['phase']
                focus['state'].pop('resume_phase', None)
                focus['state'].pop('deferred_for_other_tasks', None)
            return result

    def prompt(self, focus: dict) -> str:
        state, phase = focus["state"], focus["phase"]
        pending = state.get('question_confirmation') or {}
        if state.get('foundation_hold'):
            return state['foundation_hold']['message']
        if state.get("pending_operation"):
            return "刚才的回答已保存，还有处理步骤待恢复。说“重试”可以接着处理，不必重答。"
        if pending.get('mode') == 'source_repair' and phase not in {'paused', 'closed'}:
            if pending.get('recapture'):
                from .mistake_recapture import prompt
                return prompt(focus)
            from .mistake_source_revision import readback
            return readback(pending)
        if phase == 'remediation' and state.get('question_review_status') in {'pending', 'invalidated'}:
            return '这道公式题的题干还需要对照原题核对，先不沿用可能识别错的公式讲解。核对记录已经留给家长。'
        if phase == "diagnosis_probe":
            return state.get("next_activity", {}).get("question") or "请独立完成刚才的题，不确定也可以告诉我。"
        if phase == "diagnosis" and state.get("diagnosis", {}).get("next_action", {}).get("kind") == "revise_hypotheses":
            return "原题或原回答有了修订。先不沿用旧错因，请重新说说哪些地方还不确定。"
        if phase == "remediation" and state.get("diagnosis_remediation_for"):
            return "目前还不能确定错因，先一起梳理原题的条件和方法；补救完成后还要做新题和延迟复测。"
        if phase == "candidate":
            if state.get("answer_source") == "visual_unverified" and state.get("submitted_answer"):
                return (
                    f"我看到一道待核对的题：{state['question_text'][:700]}。"
                    f"我还读到你写的是“{state['submitted_answer']}”。"
                    "这里只核对识别内容，还没判定正误。我有没有读错题目或你实际写的答案？"
                )
            return (
                f"我看到一道待核对的题：{state['question_text'][:700]}。我有没有读错题目？"
                "这里只确认识别内容；旁边的参考内容不一定是本题标准答案，还不判定正误。"
            )
        if phase == "answer":
            return "好，这道题先留在这里。你当时实际写的答案是什么？记不清也可以告诉我，不用猜。"
        if phase == "confirm" and not focus.get("mistake_id"):
            return f"我记下的原答案是“{state['submitted_answer']}”。这是你当时实际写的答案吗？这里只确认记录，还不请你判断对错。"
        if phase == "scoring_pending":
            return "题目和你实际写的答案已确认。我会再用老师批改、标准答案或出题时保存的评分规则核对；没核对前不会记成错题，也不会改变掌握度。"
        if phase == "scored_correct":
            return "这道题已有可靠依据判定为正确，不进入错题本。可以继续下一道。"
        if phase == "scored_incorrect_unmapped":
            return "这道题已有可靠依据判定需要订正，但知识点还没可靠对应。系统会继续核对，不请你来判断。"
        if phase == "paused":
            return "好，我们先停在这里。刚才的题和你说过的内容都留着，下次可以继续。"
        if phase == "closed":
            return "本页候选已经整理完了，保存的错题和后续复习安排都会保留。"
        if focus.get("mistake_id"):
            from .agent_capability_executor import AgentCapabilityExecutor
            current = self.book.get(focus["mistake_id"], focus["child_id"])
            current["next_activity"] = self.book.next_activity(focus["mistake_id"], focus["child_id"])
            return AgentCapabilityExecutor._mistake_activity_prompt(current)
        return "我们继续核对刚才这道题。"

    def respond(self, session_id: str, utterance: str, action_id: str, *, authorized_turn_id: str | None = None) -> dict:
        self._active_child(session_id)
        text = utterance.strip()
        if not text or len(text) > 4000:
            raise InvalidMistakeCaptureCandidateError("这次回答为空或过长，请分段说。")
        replay = self._replay(session_id, action_id, text)
        if replay:
            return replay
        focus = self.state(session_id)
        from .mistake_cross_session_control import request as historical_request, offer as offer_historical
        historical = historical_request(text)
        if historical and not (focus or {}).get('state', {}).get('pending_operation'):
            return offer_historical(self, session_id, text, action_id, historical)
        if not focus:
            raise InvalidMistakeCaptureCandidateError("还没有正在核对的题目。")
        state = focus["state"]
        from .mistake_cross_session_control import handle_pending as handle_historical
        historical_result = handle_historical(self, focus, text, action_id)
        if historical_result:
            return historical_result
        if self._hold_foundation(focus):
            state['foundation_hold']['utterance'] = text
            return self._save(focus, self.prompt(focus), action_id, text)
        from .mistake_repair_cancel import requested as cancel_requested, cancel
        if cancel_requested(text):
            return cancel(self.database, focus, text, action_id, authorized_turn_id)
        unfinished_teaching = bool(state.pop("teaching_requested", None)
            and not state.get('teaching_turn_id') and state.get('ocr_confirmation_id')
            and not (state.get('next_activity') or {}).get('item'))
        compact = re.sub(r"[，。！？、\s]", "", text)
        from .mistake_recapture import select_reply
        recapture_reply = select_reply(self, focus, text, action_id)
        if recapture_reply:
            return recapture_reply
        correction = re.match(r"(?:错题题干改成|题干改成|题目是|题目应该是)[，,:：\s]*(.+)", text, re.DOTALL)
        if (correction and focus.get('mistake_id') and focus['phase'] not in {'paused', 'closed'}
                and not state.get('pending_operation')):
            from .mistake_source_revision import offer
            return offer(self, focus, correction[1], action_id, text)
        if state.get('question_confirmation') and focus['phase'] == 'remediation' and not re.search(r'暂停|先停|先不做|关闭|等一下|以后再做|跳过', text):
            from .mistake_question_confirmation import approve
            if approve(self.database, focus, text, action_id, authorized_turn_id):
                return self._save(focus, '好，我们按刚才核对的这道题继续讲解。', action_id, text)
            if state.pop('question_repair_error', None):
                self._sync(focus, advance=False)
                return self._save(focus, '原题或核对记录已经变化，刚才的更正没有覆盖新内容。请重新读出要更正的完整题干。', action_id, text)
            state['teaching_requested'] = True
            if 'replacement_answer' in state['question_confirmation']:
                from .mistake_source_revision import readback
                return self._save(focus, readback(state['question_confirmation']), action_id, text)
            return self._save(focus, '还没有确认当前更正题目；如果内容不对，先保留核对记录，不会按旧题讲解。', action_id, text)
        if state.get("pending_operation") and compact not in {"暂停", "先停一下", "先不做了", "关闭错题整理", "等一下", "以后再做"}:
            if action_id == state["pending_operation"]["action_id"] and text != state["pending_operation"]["utterance"]:
                raise InvalidMistakeCaptureCandidateError("这次作答动作已用于其他内容。")
            from .mistake_pending_answer import authorized, binding, explicit_replacement
            replacement_answer = explicit_replacement(text)
            if replacement_answer is not None and authorized(
                    self.database, focus, text, authorized_turn_id,
                    control='replace', replacement_answer=replacement_answer):
                from .mistake_pending_replacement import replace_pending
                return replace_pending(self, focus, text, action_id, authorized_turn_id, replacement_answer)
            with self.database.connect() as c:
                row = c.execute('''SELECT arguments_json FROM agent_step s JOIN agent_plan p USING(plan_id)
                    WHERE p.agent_turn_id=? AND s.step_index=0 AND s.capability_name='mistake.dialogue' ''',
                    (authorized_turn_id,)).fetchone() if authorized_turn_id else None
            try:
                planned = json.loads(row[0]) if row else {}
            except (TypeError, ValueError):
                planned = {}
            if (planned.get('pending_answer_control') == 'replace'
                    and authorized(self.database, focus, text, authorized_turn_id,
                                   control='replace', replacement_answer=planned.get('replacement_answer'))):
                from .mistake_pending_replacement import replace_pending
                return replace_pending(self, focus, text, action_id, authorized_turn_id,
                                       planned['replacement_answer'])
            if authorized(self.database, focus, text, authorized_turn_id, control='withdraw'):
                from .mistake_pending_withdrawal import withdraw
                return withdraw(self, focus, text, action_id, authorized_turn_id)
            if action_id != state['pending_operation']['action_id'] and not authorized(self.database, focus, text, authorized_turn_id):
                state['pending_response'] = {'utterance': text, 'action_id': action_id,
                    'operation': binding(focus), 'created_at': utc_now()}
                return self._save(focus, '上一份回答已经保留，后续处理还没完成。你刚才补充的话也已保存，我先不处理旧答案。'
                    '可以让我接着处理原回答，也可以先暂停；这句话不会被当成新的评分。', action_id, text)
            return self._resume_operation(focus, text, action_id)
        revised = re.match(r"刚才(?:的)?答案(?:改成|应该是)[，,:：\s]*(.+)", text)
        from .mistake_pending_answer import explicit_replacement
        scored_replacement = explicit_replacement(text)
        if scored_replacement is None and state.get('last_scored_answer') and authorized_turn_id:
            from .mistake_scored_correction import model_authorized_replacement
            with self.database.connect() as c:
                scored_replacement = model_authorized_replacement(
                    c, focus, text, authorized_turn_id)
        if (focus.get('mistake_id') and scored_replacement is not None
                and state.get('last_scored_answer')):
            from .mistake_scored_correction import replace_scored
            return replace_scored(self, focus, text, action_id, authorized_turn_id,
                                  scored_replacement)
        from .mistake_scored_correction import (
            explicit_withdrawal as explicit_scored_withdrawal,
            model_authorized_withdrawal,
            withdraw_scored,
        )
        scored_withdrawal = explicit_scored_withdrawal(text)
        if (not scored_withdrawal and state.get('last_scored_answer')
                and authorized_turn_id):
            with self.database.connect() as c:
                scored_withdrawal = model_authorized_withdrawal(
                    c, focus, text, authorized_turn_id)
        if (focus.get('mistake_id') and scored_withdrawal
                and state.get('last_scored_answer')):
            return withdraw_scored(self, focus, text, action_id, authorized_turn_id)
        if focus.get("mistake_id") and (revised or compact == "撤回刚才的回答") and state.get("last_diagnosis_probe_id"):
            diagnosis = self.book.diagnosis_state(focus["mistake_id"], focus["child_id"])
            probe = next((p for p in diagnosis["probes"] if p["event_id"] == state["last_diagnosis_probe_id"]), None)
            if probe and probe["answer"]:
                if revised:
                    state["diagnosis_probe_id"] = probe["event_id"]
                    return self._answer_probe(focus, text, action_id, supersedes=probe["answer"]["event_id"], answer=revised[1], authorized_turn_id=authorized_turn_id)
                self.book.retract_diagnosis_event(focus["mistake_id"], focus["child_id"], probe["answer"]["event_id"], text,
                    diagnosis["version"], action_id + ":retract-answer")
                state.pop("diagnosis_remediation_for", None)
                state.pop("diagnosis_verification_for", None)
                self._sync(focus)
                return self._save(focus, "已撤回刚才的鉴别回答，原记录保留，相关判断会重新核对。" + self.prompt(focus), action_id, text)
        positive = compact in {"对", "是", "是的", "对的", "没错", "准确", "确认", "确认是错题", "题干正确", "题目完整", "都对", "确实错了", "是我做错了", "保存", "保存这道错题", "确认加入错题本"}
        negative = compact in {"不是", "不是错题", "这题没错", "我做对了", "这是对勾", "跳过", "这道跳过", "不对"}
        readback_positive = compact in {"对", "是", "是的", "对的", "没错", "准确", "确认", "题干正确", "题目完整", "都对"}
        correctness_claim = compact in {"确认是错题", "确实错了", "是我做错了", "确实是我做错了", "保存这道错题", "确认加入错题本",
                                        "不是错题", "这不是错题", "这题没错", "我做对了", "我这题做对了", "这是对勾"}
        message = None
        if re.search(r"记得.*(?:错题|这题)|(?:昨天|上次|刚才).*(?:哪道题|什么题|哪道错题|卡在哪里|卡在哪一步)", compact):
            detail = self.book.get(focus["mistake_id"], focus["child_id"]) if focus.get("mistake_id") else {}
            explanation = (detail.get("diagnosis") or {}).get("explanation")
            message = f"记得，保存的题目是：{state.get('question_text', '')}。你当时说原答案是“{state.get('submitted_answer') or '还没说'}”。"
            if explanation:
                message += f"你当时说“{explanation}”，这仍是待验证的错因线索。"
            message += self.prompt(focus)
        elif compact in {"下一道", "下一道错题", "继续整理", "继续整理错题"} and state.get("artifact_id"):
            if (focus.get("candidate_id") and not focus.get("mistake_id")
                    and not state.get("scoring_request_id")):
                self.capture.skip(focus["candidate_id"], focus["child_id"], f"{action_id}:skip")
            self._advance(focus)
        elif selected := re.fullmatch(r"(?:我要看|看|选|是)?第?([0-9]+|[一二两三四五六七八九十]{1,3})题", compact):
            candidates = self.capture.list(state["artifact_id"], focus["child_id"])["items"] if state.get("artifact_id") else []
            matches = [x for x in candidates if x["status"] == "pending"
                       and not x.get("scoring_request_id") and re.match(
                rf"^\s*(?:[（(][A-Za-z ]*[)）]\s*)?{spoken_question_number(selected[1])}[.．、)）]", x["question_text"])]
            if len(matches) == 1:
                self._select(focus, matches[0])
            elif len(matches) > 1:
                message = "这页有重复的题号，还不能确定你说的是哪一道。请把目标题单独放到画面中间，再说“重新拍题”。"
            else:
                message = "还没能从这页可靠定位你说的题号。把那道题单独放在画面中间，我重新拍一下。"
        elif compact in {"暂停", "先停一下", "先不做了", "关闭错题整理", "等一下", "以后再做"}:
            if focus["phase"] != "paused":
                state["resume_phase"] = focus["phase"]
            state["deferred_for_other_tasks"] = compact == "以后再做"
            focus["phase"] = "paused"
        elif compact in {"继续", "继续错题", "继续订正", "继续刚才的题", "重试", "再说一遍", "再讲一遍", "开始复习错题"}:
            if focus["phase"] == "paused":
                focus["phase"] = state.pop("resume_phase", "candidate")
                state.pop("deferred_for_other_tasks", None)
            if focus["phase"] == "scoring_pending":
                self.reconcile_pending_scoring(8)
                refreshed = self.state(session_id)
                if refreshed:
                    focus = refreshed
                    state = focus["state"]
                message = self.prompt(focus)
            elif unfinished_teaching and focus['phase'] == 'remediation':
                # Revalidate the original confirmation before replaying teaching;
                # syncing the next activity here would skip the failed lesson.
                state['teaching_requested'] = True
                if self.teaching_request(focus):
                    return self._save(focus, '接着刚才核对的原题重新讲解，不需要再录题。', action_id, text)
                review = self.book.get(focus['mistake_id'], focus['child_id']).get('question_review')
                if review:
                    return self._save(focus, '原题核对记录发生变化，先重新确认，不会沿用旧题讲解。', action_id, text)
                state.pop('teaching_requested', None)
            if focus.get("mistake_id"):
                self._sync(focus)
            if focus["phase"] == "diagnosis_probe":
                diagnosis = self.book.diagnosis_state(focus["mistake_id"], focus["child_id"])
                pending = next((p for p in diagnosis["probes"] if p["event_id"] == state.get("diagnosis_probe_id")), None)
                if pending and pending["answer"] and not pending["evidence"]:
                    return self._answer_probe(focus, text, action_id, authorized_turn_id=authorized_turn_id)
            if focus["phase"] == "remediation":
                state["teaching_requested"] = True
        elif focus["phase"] in {"paused", "closed"}:
            message = "这段订正已经暂停。想接着做时告诉我“继续错题”。"
        elif corrected := re.match(r"(?:错题题干改成|题干改成|题目是|题目应该是)[，,:：\s]*(.+)", text):
            if focus.get("mistake_id"):
                from .mistake_source_revision import offer
                return offer(self, focus, corrected[1], action_id, text)
            else:
                self.capture.edit(focus["candidate_id"], focus["child_id"], corrected[1], f"{action_id}:edit")
                state.update(question_text=corrected[1], text_confirmed=False, manually_corrected=True,
                             subject=subject_for_question(corrected[1], state["subject"]))
                self._match(focus)
                focus["phase"] = "candidate"
        elif correctness_claim:
            state["child_scoring_dispute"] = {
                "utterance": text,
                "recorded_at": utc_now(),
                "requires_authority_recheck": True,
            }
            if focus.get("mistake_id"):
                message = "我记下你对判定有异议，但不会只凭这句话删除错题。我会重新核对老师批改、对应版本的标准答案或出题时冻结的评分规则；如果是题干、答案或题目归属读错了，请直接告诉我哪一处。"
            else:
                message = "我先保留你对对错的说法，但不用你来当评分器。请只确认我有没有读错题目和你实际写的答案。"
        elif negative and focus["phase"] in {"candidate", "answer", "confirm"}:
            if compact in {"不对", "不是"}:
                message = "哪里不对？题干可以说“题目是”再读给我，原答案可以说“我当时写的是”来更正。"
            elif focus.get("mistake_id"):
                self.book.confirm(focus["mistake_id"], focus["child_id"], "exclude", f"{action_id}:exclude")
                focus["phase"] = "closed"
                message = "已排除这道题，不会继续把它当作你的薄弱点。"
            else:
                self.capture.skip(focus["candidate_id"], focus["child_id"], f"{action_id}:skip")
                self._advance(focus)
        elif compact in {"跳过", "这道跳过"} and focus.get("mistake_id"):
            state["resume_phase"] = focus["phase"]
            focus["phase"] = "paused"
            message = "这次先跳过，错题和复习安排仍然保留。"
        elif negative and focus.get("mistake_id") and compact not in {"不对", "不是"}:
            self.book.confirm(focus["mistake_id"], focus["child_id"], "exclude", f"{action_id}:exclude")
            focus["phase"] = "closed"
            message = "已排除这道题，不再据此安排强化；之前的操作记录会保留供核对。"
        elif focus["phase"] == "candidate" and readback_positive:
            state["text_confirmed"] = True
            if state.get("answer_source") == "visual_unverified" and state.get("submitted_answer"):
                scoring = self._request_paper_scoring(focus, action_id)
                message = self._scoring_notice(scoring, focus) + self.prompt(focus)
            else:
                focus["phase"] = "answer"
        elif focus["phase"] in {"candidate", "answer", "confirm"} and not focus.get("mistake_id"):
            explicit_answer = re.match(r"(?:我(?:当时|原来)?(?:写的|选的|选了|写了|选|写)|(?:原来|当时|错题原)?答案)(?:是|的是)?[，,:：\s]*(.+)", text)
            if readback_positive and focus["phase"] == "confirm":
                scoring = self._request_paper_scoring(focus, action_id)
                message = self._scoring_notice(scoring, focus) + self.prompt(focus)
            elif explicit_answer or focus["phase"] == "answer":
                if re.search(r"记不清|不记得|忘了|忘记|没写|没做|不知道", text):
                    message = "没关系，我先保留这道候选题，不猜你的原答案，也不据此判断错因。找到原来的作答后再告诉我，或者先跳过。"
                else:
                    state["submitted_answer"] = explicit_answer[1] if explicit_answer else text
                    state["answer_source"] = "child_spoken"
                    focus["phase"] = "confirm"
            elif not state.get("knowledge_node_id"):
                subject = next((s for s in ("数学", "英语", "语文", "物理", "化学") if s in text), state["subject"])
                state["subject"] = subject
                candidates = self.book.knowledge_candidates(focus["child_id"], state["question_text"] + " " + text, subject)["items"]
                reliable = [x for x in candidates if x["match_score"] > 0.05]
                if reliable:
                    state.update(knowledge_node_id=reliable[0]["knowledge_node_id"], knowledge_title=reliable[0]["title"])
            else:
                message = self.prompt(focus)
        elif focus["phase"] == "confirm":
            if positive:
                self.book.confirm(focus["mistake_id"], focus["child_id"], "confirm", f"{action_id}:confirm")
                self._sync(focus)
        elif focus["phase"] == "diagnosis" and readback_positive:
            message = (
                "这里不需要你确认这是不是错题，评分依据已经保存。"
                "请告诉我你从哪一步开始不确定，或者当时为什么会选择那个答案。"
            )
        elif focus["phase"] == "diagnosis":
            diagnosis = self.book.diagnosis_state(focus["mistake_id"], focus["child_id"])
            if self._diagnosis_action_applied(focus, text, action_id):
                self._sync(focus)
            elif diagnosis["version"]:
                event_ids = sorted({h["event_id"] for h in diagnosis["hypotheses"]})
                self.book.propose_diagnosis_hypotheses(focus["mistake_id"], focus["child_id"],
                    [{"error_type": "unknown", "stance": "unknown", "explanation": text, "confidence": .6}],
                    diagnosis["version"], action_id + ":hypotheses", event_ids[0] if event_ids else None)
            else:
                # Spoken explanations, including denials, are observations, not
                # keyword-certified causal labels.
                self.book.diagnose(focus["mistake_id"], focus["child_id"], "unknown", text, .6, f"{action_id}:diagnose")
            self._sync(focus)
        elif focus["phase"] == "diagnosis_probe":
            if re.search(r"不知道|不会|提示|讲一下|怎么做", compact):
                state["diagnosis_hinted"] = state.get("diagnosis_probe_id")
                state["teaching_requested"] = True
                state["latest_question"] = text
                message = "可以先看提示；这次会保留帮助记录，不算独立鉴别或掌握。"
            else:
                return self._answer_probe(focus, text, action_id, authorized_turn_id=authorized_turn_id)
        elif focus["phase"] == "remediation":
            understood = bool(re.search(r"懂了|明白了|会了|能说清|补懂了", compact)) and not re.search(r"不懂|没懂|没明白|不太懂|还不会|没会", compact)
            if (state.get('diagnosis') or {}).get('next_action', {}).get('reason') == 'original_question_review_required':
                message = self.prompt(focus)
            elif understood:
                self.book.add_remediation(focus["mistake_id"], focus["child_id"], "problem_restatement", "completed", text, f"{action_id}:remediate")
                state["diagnosis_verification_for"] = state.get("diagnosis_round")
                self._sync(focus)
                message = "我们用一道新题看看是否真的会了，不只凭“听懂了”判断。" + self.prompt(focus)
            else:
                self.book.add_remediation(focus["mistake_id"], focus["child_id"], "problem_restatement", "still_stuck", text, f"{action_id}:remediate")
                state["teaching_requested"] = True
                state["latest_question"] = text
        elif focus["phase"] == "attempt":
            shown = (state.get("next_activity", {}).get("item") or {}).get("diagnostic_item_id")
            self._sync(focus)
            activity = state.get("next_activity", {})
            if activity.get("kind") == "attempt":
                if shown != activity["item"]["diagnostic_item_id"]:
                    message = "题目或复测条件发生了变化，这次不会按另一道题评分。" + self.prompt(focus)
                elif re.search(r"不知道|不会|提示|讲一下|怎么做", compact):
                    state["attempt_hinted"] = True
                    state["teaching_requested"] = True
                    state["latest_question"] = text
                    message = "可以，我们先看提示。这次会记为有帮助的练习，不算独立掌握。"
                else:
                    answer = re.sub(r"^(?:错题答案是|这题答案是|答案是|我选|我认为是)[，,:：\s]*", "", text)
                    operation = {"kind": "mastery_answer", "action_id": action_id, "utterance": text,
                        "stage": activity["stage"], "item_id": activity["item"]["diagnostic_item_id"],
                        "answer": answer, "hint_level": 1 if state.get("attempt_hinted") else 0}
                    if authorized_turn_id is not None:
                        operation['authorized_turn_id'] = authorized_turn_id
                    self._checkpoint(focus, operation)
                    return self._resume_operation(focus, text, action_id)
        elif focus["phase"] == "scheduled":
            self._sync(focus)
        return self._save(focus, message or self.prompt(focus), action_id, text)

    def teaching_request(self, focus: dict) -> dict | None:
        state = focus["state"]
        if state.get('question_confirmation', {}).get('mode') == 'source_repair':
            return None
        if not state.get("teaching_requested") or not focus.get("mistake_id"):
            return None
        if self._hold_foundation(focus):
            return None
        current = self.book.get(focus["mistake_id"], focus["child_id"])
        if (current.get('question_review') or {}).get('status') in {'pending', 'invalidated'}:
            return None
        if state.get('question_review_status') and not current.get('question_review'):
            return None
        check = (state.get("next_activity") or {}).get("item")
        question = (check or {}).get("prompt") or current["prompt_summary"]
        confirmation_id = None
        if current.get('question_review') and not check:
            from .tutor_ocr_confirmation import OcrTutorConfirmationService, OcrTutorConfirmationError, normalize_confirmation_text
            with self.database.connect() as c:
                confirmations = c.execute('''SELECT confirmation_id,confirmed_text FROM tutor_ocr_confirmation
                    WHERE child_id=? AND session_id=? AND artifact_id=?
                    AND revoked_at IS NULL ORDER BY confirmed_at DESC''',
                    (focus['child_id'], focus['session_id'], current['source_ref'].partition('#')[0])).fetchall()
            for row in confirmations:
                if normalize_confirmation_text(row['confirmed_text']) != normalize_confirmation_text(question):
                    continue
                try:
                    OcrTutorConfirmationService(self.database).resolve_for_tutor(
                        focus['session_id'], row['confirmation_id'])
                    confirmation_id = row['confirmation_id']
                    break
                except OcrTutorConfirmationError:
                    continue
            if not confirmation_id:
                return None
        explanation = (current.get("diagnosis") or {}).get("explanation", "尚未确定")
        request = {"subject": current["subject"], "question": question,
                "instruction": f"孩子原答案：{current['submitted_answer']}。孩子自述卡点：{explanation}。这只是错因假设，不可给孩子贴标签。"
                    f"最新追问：{state.get('latest_question', '')}。请温和分步讲解当前题目的方法，边讲边板书；最后问孩子是否准备尝试，不要自造测验或宣布掌握。",
                "client_action_id": f"mistake-teaching:{focus['session_id']}:{focus['mistake_id']}:{self._hash(json.dumps(state, sort_keys=True, ensure_ascii=False))[:20]}"}
        if confirmation_id:
            request['ocr_confirmation_id'] = confirmation_id
            request['client_action_id'] += ':' + self._hash(confirmation_id)[:12]
        return request
