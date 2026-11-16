"""Versioned open-answer candidates and explicitly authorized human reviews.

No keyword/length scorer or calibrated automatic certification lives here.
HTTP adapters must derive reviewer identity/role from parent authorization,
never from model output or a child's request body.
"""
from datetime import datetime, timezone, timedelta
import hashlib
import json
from uuid import uuid4

from .learning_obligations import question_identity, project_item_exposure, withdrawn_evidence_ids, project_child_mastery_evidence


RUBRIC_VERSION = "open_answer_rubric_v1"
RUBRIC_TABLES = ("rubric_assessment", "rubric_review_event")
PROFILES = {
    "math_process": ("answer", "method", "reasoning"),
    "chinese_open": ("task", "evidence", "reasoning", "expression"),
    "english_open": ("task", "coherence", "language"),
}
DIMENSION_CRITERIA = {
    "math_process": {
        "answer": "结果、单位和适用条件符合题意，不能只看最终数字相同。",
        "method": "解题方法适用于本题，列式与运算步骤正确且能从孩子回答追溯。",
        "reasoning": "关键步骤有有效依据，能解释数量关系；照抄结论不算推理。",
    },
    "chinese_open": {
        "task": "直接回答题目要求，核心判断准确，不以篇幅或关键词覆盖代替完成任务。",
        "evidence": "使用相关、准确的文本依据支持判断；有给定材料时须核对原文。",
        "reasoning": "说明文本依据为何支持判断，不把重复结论当作分析。",
        "expression": "表意清楚、组织连贯，用语适合题目要求；轻微形式错误不掩盖内容评价。",
    },
    "english_open": {
        "task": "完成交际目的和题目要求的要点，事实与信息完整；堆词不算完成任务。",
        "coherence": "句子和要点间关系清楚，组织适合文本类型，不以连接词数量代替连贯性。",
        "language": "词汇、语法和语域能准确表达意图；仅评价文字，不推断发音或口语流利度。",
    },
}
SCORE_ANCHORS = {
    "0": "缺少该维度的有效证据，或存在使结论不能成立的核心错误。",
    "1": "部分满足要求，但仍有影响任务、方法或论证成立的缺漏，需要实质修正。",
    "2": "满足该维度要求，有可追溯证据；允许不影响正确性或表达目的的轻微瑕疵。",
    "3": "准确、完整且清晰地满足该维度要求，关键判断有充分依据。",
}


def profile_for(subject):
    return {"数学": "math_process", "语文": "chinese_open", "英语": "english_open"}.get(subject)


def rubric_dimensions(subject):
    profile = profile_for(subject)
    return [{"dimension_id": name, "criterion": DIMENSION_CRITERIA[profile][name],
             "max_score": 3, "pass_score": 2, "score_anchors": dict(SCORE_ANCHORS)}
            for name in PROFILES.get(profile, ())]


def open_profile_for_evidence(c, child_id, evidence_id):
    row = c.execute('''SELECT di.* FROM diagnostic_attempt da JOIN diagnostic_item di USING(diagnostic_item_id)
        WHERE da.evidence_id=? AND da.child_id=?''',(evidence_id,child_id)).fetchone()
    if not row or row['answer_type']!='open_response':
        return None
    from .local_supply import validate_item
    from .open_response_profile import profile_for_item
    item = validate_item(c,child_id,row['diagnostic_item_id'])
    node = c.execute('SELECT * FROM knowledge_node WHERE knowledge_node_id=?',(item['knowledge_node_id'],)).fetchone()
    return profile_for_item(item,dict(node))


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _time(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("RUBRIC_TIMEZONE_REQUIRED")
    return parsed.astimezone(timezone.utc)


def _prior_answer_safe_after(c, child_id, step_id):
    """Earliest safe cutoff from owned time proof, never prior score authority."""
    step = c.execute("""SELECT s.arguments_json,p.session_id,t.created_at
        FROM agent_step s JOIN agent_plan p USING(plan_id)
        JOIN agent_turn t ON t.agent_turn_id=p.agent_turn_id AND t.child_id=p.child_id AND t.session_id=p.session_id
        JOIN daily_session ds ON ds.session_id=p.session_id AND ds.child_id=p.child_id
        WHERE s.step_id=? AND p.child_id=? AND s.capability_name='learning.evaluate_response'""",
        (step_id, child_id)).fetchone()
    if not step:
        return None
    try:
        args = json.loads(step['arguments_json'])
        if (not isinstance(args, dict) or not isinstance(args.get('evidence_id'), str)
                or not args['evidence_id'] or not isinstance(args.get('response'), str)):
            return None
        answers = c.execute("""SELECT le.occurred_at,da.created_at,da.submitted_answer
            FROM learning_evidence le JOIN diagnostic_attempt da
              ON da.evidence_id=le.evidence_id AND da.child_id=le.child_id AND da.session_id=le.session_id
            WHERE le.evidence_id=? AND le.child_id=? AND le.session_id=? AND le.evidence_type='diagnostic_answer'""",
            (args['evidence_id'], child_id, step['session_id'])).fetchall()
        if len(answers) != 1 or answers[0]['submitted_answer'] != args['response']:
            return None
        answer_time = _time(answers[0]['occurred_at'])
        step_time = _time(step['created_at'])
        if answer_time == _time(answers[0]['created_at']) and answer_time <= step_time:
            return max(step_time, answer_time + timedelta(hours=24))
    except (ValueError, TypeError, AttributeError, KeyError, OverflowError):
        pass
    return None


def _prior_answer_outside_window(c, child_id, step_id, occurred):
    """Recover only an owned answer time; never restore a prior score or item."""
    safe_after = _prior_answer_safe_after(c, child_id, step_id)
    try:
        return safe_after is not None and safe_after <= occurred
    except TypeError:
        return False


def _prior_question_time(payload_json, *, connection=None):
    payload = json.loads(payload_json)
    if not isinstance(payload, dict) or not isinstance(payload.get('question'), str) or not payload['question'].strip():
        raise ValueError('RUBRIC_PRIOR_QUESTION_UNAVAILABLE')
    return _assessment_identity(payload, connection=connection), _time(payload['responded_at'])


def _assessment_identity(payload, *, connection):
    material = payload.get('material') or {}
    visual = material.get('visual')
    if visual is not None:
        from .visual_question_evidence import _digest
        content = visual.get('content_identity') if isinstance(visual, dict) else None
        if not isinstance(content, str) or len(content) != 64 or any(ch not in '0123456789abcdef' for ch in content):
            raise ValueError('RUBRIC_VISUAL_PRIOR_IDENTITY_UNAVAILABLE')
        return 'exact_visual_question_v1:' + _digest({'prompt': ' '.join(payload['question'].split()),
                                                     'visual_content': content})
    if material.get('diagnostic_item_id') and connection is not None:
        from .visual_question_evidence import frozen_visual
        item = {'diagnostic_item_id': material['diagnostic_item_id'], 'prompt': payload['question']}
        if frozen_visual(connection, item) is not None:
            original = connection.execute('SELECT * FROM diagnostic_item WHERE diagnostic_item_id=?',
                (material['diagnostic_item_id'],)).fetchone()
            if not original or hashlib.sha256(_json(dict(original)).encode()).hexdigest() != material.get('source_version_hash'):
                raise ValueError('RUBRIC_VISUAL_PRIOR_IDENTITY_UNAVAILABLE')
            return question_identity(item, connection=connection)
    return question_identity({'prompt': payload['question']})


def evaluation_source_boundary(c, child_id, *, evidence_id=None, question=None, objective_id=None, ordinary_projection=None):
    """Local provenance restrictions, never a grant of cloud authorization."""
    reasons, sources = [], []
    if evidence_id:
        rows = c.execute("""SELECT di.diagnostic_item_id,di.source_ref,di.prompt FROM diagnostic_item di
            JOIN diagnostic_attempt da USING(diagnostic_item_id) JOIN learning_evidence le USING(evidence_id)
            WHERE le.evidence_id=? AND le.child_id=? AND da.child_id=le.child_id AND da.session_id=le.session_id""",
            (evidence_id, child_id)).fetchall()
        if len(rows) != 1:
            reasons.append("diagnostic_source_unavailable")
    else:
        normalized = " ".join((question or "").split())
        rows = [row for row in c.execute("SELECT diagnostic_item_id,source_ref,prompt FROM diagnostic_item")
                if normalized and " ".join(row["prompt"].split()) == normalized]
    for row in rows:
        sources.append({"diagnostic_item_id": row["diagnostic_item_id"], "source_ref": row["source_ref"]})
        if row["source_ref"] != "hsc_original_v1":
            reasons.append("diagnostic_source_local_or_unknown")

    def restricted(value):
        if isinstance(value, dict):
            return (value.get("local_only") is True or value.get("cloud_transmission_allowed") is False
                    or any(restricted(item) for item in value.values()))
        return isinstance(value, list) and any(restricted(item) for item in value)

    if objective_id:
        actions = c.execute("""SELECT t.agent_turn_id,t.action_json,s.arguments_json FROM agent_plan p
            JOIN agent_turn t ON t.agent_turn_id=p.agent_turn_id AND t.child_id=p.child_id
            LEFT JOIN agent_step s ON s.plan_id=p.plan_id WHERE p.objective_id=? AND p.child_id=?""",
            (objective_id, child_id)).fetchall()
        for row in actions:
            try:
                action = json.loads(row["action_json"] or "{}")
                arguments = json.loads(row["arguments_json"] or "{}")
                if ordinary_projection is not None and isinstance(action, dict):
                    from .ordinary_learning_text_projection import INPUT_KEY, boundary_action
                    if INPUT_KEY in action:
                        action = boundary_action(c, row['agent_turn_id'], action, ordinary_projection)
                if isinstance(action, dict):
                    # Atomic companion context is appended only after planning and
                    # is never part of the evaluator payload.  Do not let its
                    # local-only marker disable evaluation of the separately
                    # reconstructed question/response.  Every other local source,
                    # especially document context, continues to fence cloud use.
                    action = {key: value for key, value in action.items()
                              if key != "local_learning_context"}
                if restricted(action) or restricted(arguments):
                    reasons.append("objective_contains_local_only_action")
                    break
            except (TypeError, ValueError):
                reasons.append("objective_source_policy_unknown")
                break
    return {"version": "evaluation_source_boundary_v1", "local_only": True,
            "cloud_transmission_allowed": False, "cloud_evaluator_blocked": bool(reasons),
            "reasons": sorted(set(reasons)), "sources": sources,
            "authorization": "source_check_is_not_runtime_cloud_authorization"}


class RubricEvidenceService:
    def __init__(self, database):
        self.database = database

    def initialize(self):
        with self.database.connect() as c:
            for row in c.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name IN ('rubric_assessment','rubric_review_event')"):
                if row[0].lower().endswith("no_delete"):
                    c.execute('DROP TRIGGER "' + row[0].replace('"', '""') + '"')
            c.execute("""CREATE TABLE IF NOT EXISTS rubric_assessment (
                assessment_id TEXT PRIMARY KEY,
                step_id TEXT NOT NULL UNIQUE REFERENCES agent_step(step_id) ON DELETE CASCADE,
                child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                model_call_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,fingerprint TEXT NOT NULL,created_at TEXT NOT NULL)""")
            c.execute("""CREATE TABLE IF NOT EXISTS rubric_review_event (
                event_id TEXT PRIMARY KEY,
                assessment_id TEXT NOT NULL REFERENCES rubric_assessment(assessment_id) ON DELETE CASCADE,
                child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                revision INTEGER NOT NULL CHECK(revision>0),
                operation TEXT NOT NULL CHECK(operation IN ('approve','retract')),
                reviewer_id TEXT NOT NULL,reviewer_role TEXT NOT NULL,payload_json TEXT NOT NULL,
                request_json TEXT NOT NULL,client_action_id TEXT NOT NULL,created_at TEXT NOT NULL,
                UNIQUE(assessment_id,revision),UNIQUE(child_id,client_action_id))""")
            for table in RUBRIC_TABLES:
                c.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_no_update BEFORE UPDATE ON {table}
                    BEGIN SELECT RAISE(ABORT,'RUBRIC_HISTORY_IMMUTABLE'); END""")
            c.commit()

    @staticmethod
    def available(c):
        return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='rubric_assessment'").fetchone() is not None

    @staticmethod
    def _diagnostic_source_version(c, child_id, evidence_id):
        if not evidence_id:
            return None
        rows = c.execute("""SELECT le.*,da.submitted_answer,da.created_at AS attempt_created_at,
                da.diagnostic_item_id,da.correctness AS diagnostic_correctness
            FROM learning_evidence le JOIN diagnostic_attempt da
              ON da.evidence_id=le.evidence_id AND da.child_id=le.child_id AND da.session_id=le.session_id
            WHERE le.evidence_id=? AND le.child_id=?""", (evidence_id, child_id)).fetchall()
        if len(rows) != 1:
            return "unavailable"
        return hashlib.sha256(_json(dict(rows[0])).encode()).hexdigest()

    @staticmethod
    def _material(c, child_id, question, subject, evidence_id, diagnostic_item_id=None):
        if evidence_id:
            rows = c.execute("""SELECT di.* FROM learning_evidence le JOIN diagnostic_attempt da
                ON da.evidence_id=le.evidence_id AND da.child_id=le.child_id
                JOIN diagnostic_item di ON di.diagnostic_item_id=da.diagnostic_item_id
                WHERE le.evidence_id=? AND le.child_id=?""", (evidence_id, child_id)).fetchall()
        elif diagnostic_item_id:
            rows = c.execute("SELECT * FROM diagnostic_item WHERE diagnostic_item_id=?", (diagnostic_item_id,)).fetchall()
        else:
            rows = c.execute("""SELECT di.* FROM diagnostic_item di JOIN knowledge_node kn USING(knowledge_node_id)
                WHERE trim(di.prompt)=? AND kn.subject=?""", (question, subject)).fetchall()
        if len(rows) != 1:
            return {"status": "unbound"}
        item = dict(rows[0])
        # Parent review needs the passage, but answer keys never enter a child receipt.
        result = {"status": "bound", "diagnostic_item_id": item["diagnostic_item_id"],
                "stimulus_text": item["stimulus_text"], "stimulus_asset": item["stimulus_asset"],
                "source_version_hash": hashlib.sha256(_json(item).encode()).hexdigest()}
        from .visual_question_evidence import current_visual
        from .local_supply import LocalSupplyError
        try:
            visual = current_visual(c, child_id, item)
        except (ValueError, TypeError, KeyError, LocalSupplyError):
            visual = {'version': 'rubric_visual_material_v1', 'status': 'unavailable',
                'source_current': False, 'image_availability': 'unavailable', 'binding_version': None,
                'materials': [], 'reason': 'RUBRIC_VISUAL_SOURCE_CHANGED',
                'local_only': True, 'cloud_transmission_allowed': False}
        if visual is not None:
            result['visual'] = visual
        return result

    def freeze(self, c, record, evaluation, call_id, now, *, source_kind="model_candidate"):
        open_profile = open_profile_for_evidence(c,record['child_id'],record.get('source_evidence_id'))
        profile = open_profile['profile_id'] if open_profile else profile_for(record["subject"])
        local = source_kind in {"local_criteria_mapping", "local_open_review", "exam_open_review"}
        if source_kind == "local_criteria_mapping":
            profile = "local_criteria_mapping"
        if not profile or record.get("evidence_binding") and not local:
            return None
        if local and (call_id is not None or not record.get("source_evidence_id")):
            raise ValueError("RUBRIC_LOCAL_SOURCE_REQUIRED")
        old = c.execute("SELECT assessment_id FROM rubric_assessment WHERE step_id=?", (record["step_id"],)).fetchone()
        if old:
            return self.get(record["child_id"], old[0], connection=c)
        source = c.execute("""SELECT t.created_at FROM agent_step s JOIN agent_plan p USING(plan_id)
            JOIN agent_turn t ON t.agent_turn_id=p.agent_turn_id AND t.child_id=p.child_id
            WHERE s.step_id=? AND p.child_id=? AND p.objective_id=?""",
            (record["step_id"], record["child_id"], record["objective_id"])).fetchone()
        proposed = evaluation.get("rubric_dimensions")
        proposed = proposed if isinstance(proposed, list) else []
        dimensions = []
        material = self._material(c, record["child_id"], record.get("question_asked"), record["subject"], record.get("source_evidence_id"))
        passage = material.get("stimulus_text")
        definitions = open_profile['dimensions'] if open_profile else rubric_dimensions(record['subject'])
        for definition in ([] if source_kind == "local_criteria_mapping" else definitions):
            name = definition["dimension_id"]
            matches = [item for item in proposed if isinstance(item, dict) and item.get("dimension_id") == name]
            item = matches[0] if len(matches) == 1 else {}
            valid = (type(item.get("score")) is int and 0 <= item["score"] <= 3
                     and isinstance(item.get("rationale"), str) and bool(item["rationale"].strip())
                     and self._quotes(item.get("response_quotes"), record["child_response"], required=item["score"] > 0))
            if "source_quotes" in item:
                valid = valid and self._quotes(item["source_quotes"], passage or record.get("question_asked") or "", required=False)
            if passage and name == "evidence" and item.get("score", 0) and valid:
                valid = self._quotes(item.get("source_quotes"), passage)
            dimensions.append({**definition,
                "score": item["score"] if valid else None,
                "response_quotes": item["response_quotes"] if valid else [],
                "source_quotes": item.get("source_quotes", []) if valid else [],
                "rationale": item["rationale"][:800] if valid else "not_assessed"})
        payload = {"rubric_version": RUBRIC_VERSION, "profile": profile, "source_kind": source_kind,
            "objective_id": record["objective_id"],
            "session_id": record["session_id"], "subject": record["subject"], "question": record.get("question_asked"),
            "response": record["child_response"], "criteria": json.loads(record["success_criteria_json"]),
            "dimensions": dimensions, "responded_at": source[0] if source else None,
            "assessment_modality": "text_only", "automatic_quality_status": "not_calibrated",
            "scorer_provenance": record.get("scorer_provenance"),
            "calibration_execution": record.get('calibration'),
            "calibration_binding": ((record.get('calibration') or {}).get('assessment_binding')),
            "source_evidence_id": record.get("source_evidence_id"),
            "source_evidence_version_hash": self._diagnostic_source_version(c, record["child_id"], record.get("source_evidence_id")),
            "material": material,
            "independence_eligible": evaluation.get("independence_eligible"),
            "condition_authority": "unknown_until_human_observation_review", "automatic_certification": False}
        if source_kind == 'exam_open_review':
            payload.update(exam_slot=record['exam_slot'],responded_at=record['responded_at'])
        assessment_id = str(uuid4())
        encoded = _json(payload)
        # An empty logical call reference means no model was used. No model_call
        # or fabricated provider receipt is created for local candidates.
        c.execute("INSERT INTO rubric_assessment VALUES (?,?,?,?,?,?,?)", (assessment_id, record["step_id"],
            record["child_id"], call_id or "", encoded, hashlib.sha256(encoded.encode()).hexdigest(), now.isoformat()))
        return self.get(record["child_id"], assessment_id, connection=c)

    @staticmethod
    def _quotes(values, response, *, required=True):
        return (isinstance(values, list) and (bool(values) or not required) and len(values) <= 12
                and all(isinstance(value, str) and value.strip() and value in response for value in values))

    @staticmethod
    def _event(row):
        return {**json.loads(row["payload_json"]), "event_id": row["event_id"], "assessment_id": row["assessment_id"],
                "local_only": True, "cloud_transmission_allowed": False,
                "review_revision": row["revision"], "operation": row["operation"], "reviewer_id": row["reviewer_id"],
                "reviewer_role": row["reviewer_role"], "created_at": row["created_at"]}

    def _owned(self, c, child_id, assessment_id):
        if not self.available(c):
            raise ValueError("RUBRIC_SCHEMA_UNAVAILABLE")
        row = c.execute("SELECT * FROM rubric_assessment WHERE assessment_id=? AND child_id=?", (assessment_id, child_id)).fetchone()
        if not row:
            raise ValueError("RUBRIC_ASSESSMENT_NOT_OWNED")
        return row

    @staticmethod
    def _source_current(c, row, payload):
        if payload.get('source_kind') == 'exam_open_review':
            from .exam_simulation import ExamSimulationService
            return ExamSimulationService.validate_rubric_source(c,row,payload)
        source = c.execute("""SELECT s.arguments_json,ao.success_criteria_json,t.created_at
            FROM agent_step s JOIN agent_plan p USING(plan_id)
            JOIN agent_objective ao ON ao.objective_id=p.objective_id AND ao.child_id=p.child_id
            JOIN agent_turn t ON t.agent_turn_id=p.agent_turn_id AND t.child_id=p.child_id
            WHERE s.step_id=? AND p.child_id=? AND p.objective_id=? AND p.session_id=?""",
            (row["step_id"], row["child_id"], payload["objective_id"], payload["session_id"])).fetchone()
        if not source:
            return False
        try:
            args = json.loads(source["arguments_json"])
            criteria = json.loads(source["success_criteria_json"])
        except (ValueError, TypeError):
            return False
        if not isinstance(args, dict) or not isinstance(criteria, list):
            return False
        material = RubricEvidenceService._material(c, row['child_id'], payload['question'], payload['subject'],
            payload['source_evidence_id'], (payload.get('material') or {}).get('diagnostic_item_id'))
        if material.get('visual', {}).get('status') == 'unavailable':
            return False
        return (args.get("response") == payload["response"] and args.get("question_asked") == payload["question"]
                and args.get("subject") == payload["subject"] and args.get("evidence_id") == payload["source_evidence_id"]
                and RubricEvidenceService._diagnostic_source_version(c, row["child_id"], payload["source_evidence_id"])
                    == payload.get("source_evidence_version_hash")
                and criteria == payload["criteria"] and source["created_at"] == payload["responded_at"]
                and material == payload.get("material"))

    def get(self, child_id, assessment_id, *, connection=None):
        if connection is None:
            with self.database.connect() as c:
                c.execute("BEGIN")
                return self.get(child_id, assessment_id, connection=c)
        row = self._owned(connection, child_id, assessment_id)
        payload = json.loads(row["payload_json"])
        latest = connection.execute("SELECT * FROM rubric_review_event WHERE assessment_id=? ORDER BY revision DESC LIMIT 1", (assessment_id,)).fetchone()
        review = self._event(latest) if latest else None
        status = review["score_status"] if review else "candidate"
        source_current = self._source_current(connection, row, payload)
        if 'visual' not in (payload.get('material') or {}) and (payload.get('material') or {}).get('diagnostic_item_id'):
            material = self._material(connection, child_id, payload['question'], payload['subject'],
                payload['source_evidence_id'], payload['material']['diagnostic_item_id'])
            if material.get('visual') is not None:
                # Old text-only snapshots never acquire a visual review binding on read.
                source_current = False
                payload = {**payload, 'material': {**payload['material'], 'visual': material['visual']}}
        if not source_current and status != "withdrawn":
            status = "source_changed"
        if not source_current and (payload.get('material') or {}).get('visual') is not None:
            visual = payload['material']['visual']
            payload = {**payload, 'material': {**payload['material'], 'visual': {**visual,
                'status': 'unavailable', 'source_current': False, 'image_availability': 'unavailable',
                'reason': 'RUBRIC_VISUAL_SOURCE_CHANGED'}}}
        return {**payload, "assessment_id": assessment_id, "step_id": row["step_id"], "child_id": child_id,
                "local_only": True, "cloud_transmission_allowed": False,
                "fingerprint": row["fingerprint"], "created_at": row["created_at"], "score_status": status,
                "source_status": "current" if source_current else "changed",
                "review_revision": review["review_revision"] if review else 0, "review": review}

    def history(self, child_id, assessment_id):
        with self.database.connect() as c:
            self._owned(c, child_id, assessment_id)
            return [self._event(row) for row in c.execute("SELECT * FROM rubric_review_event WHERE assessment_id=? ORDER BY revision", (assessment_id,))]

    def list_for_child(self, child_id, *, limit=50, before=None):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("RUBRIC_LIST_LIMIT_INVALID")
        with self.database.connect() as c:
            c.execute("BEGIN")
            if not self.available(c):
                return {"items": [], "next_cursor": None, "local_only": True, "cloud_transmission_allowed": False}
            cursor = None
            if before is not None:
                cursor = c.execute("SELECT rowid FROM rubric_assessment WHERE assessment_id=? AND child_id=?", (before, child_id)).fetchone()
                if not cursor:
                    raise ValueError("RUBRIC_LIST_CURSOR_INVALID")
            rows = c.execute("""SELECT assessment_id FROM rubric_assessment WHERE child_id=? AND (? IS NULL OR rowid<?)
                ORDER BY rowid DESC LIMIT ?""", (child_id, cursor[0] if cursor else None, cursor[0] if cursor else None, limit + 1)).fetchall()
            return {"items": [self.get(child_id, row[0], connection=c) for row in rows[:limit]],
                    "next_cursor": rows[limit - 1][0] if len(rows) > limit else None,
                    "local_only": True, "cloud_transmission_allowed": False}

    def diagnostic_candidate_input(self, child_id, evidence_id):
        """Open-score adapter, not a reliable-score receipt or runtime authorization."""
        with self.database.connect() as c:
            row = c.execute("""SELECT da.submitted_answer,di.prompt,kn.subject,le.session_id
                FROM learning_evidence le JOIN diagnostic_attempt da ON da.evidence_id=le.evidence_id
                JOIN diagnostic_item di ON di.diagnostic_item_id=da.diagnostic_item_id
                JOIN knowledge_node kn ON kn.knowledge_node_id=di.knowledge_node_id
                JOIN daily_session ds ON ds.session_id=da.session_id AND ds.child_id=da.child_id
                WHERE le.evidence_id=? AND le.child_id=? AND da.child_id=le.child_id AND da.session_id=le.session_id""",
                (evidence_id, child_id)).fetchone()
            if not row:
                raise ValueError("RUBRIC_DIAGNOSTIC_NOT_OWNED")
            return {"score_status": "candidate", "session_id": row["session_id"],
                    "local_only": True, "cloud_transmission_allowed": False,
                    "evaluation_route": "local_criterion_review_required",
                    "execution_method": "prepare_local_evaluation",
                    "source_boundary": evaluation_source_boundary(c, child_id, evidence_id=evidence_id),
                    "capability": "learning.evaluate_response", "arguments": {
                        "evidence_id": evidence_id, "response": row["submitted_answer"],
                        "question_asked": row["prompt"], "subject": row["subject"]}}

    def qualification(self, child_id, assessment_id, *, connection=None):
        """Current conditions for admission to the gate, not mastery or a probability."""
        if connection is None:
            with self.database.connect() as c:
                c.execute("BEGIN")
                return self.qualification(child_id, assessment_id, connection=c)
        row = self._owned(connection, child_id, assessment_id)
        receipt = self.reviewed_receipt(connection, child_id, row["step_id"],
            assessment_id=assessment_id, now=datetime.now(timezone.utc))
        from .pet_audio_rubric import is_speaking, current_audio_review
        current = self.get(child_id, assessment_id, connection=connection)
        if is_speaking(connection, current['material'].get('diagnostic_item_id')):
            return self._typed_audio_qualification(connection,child_id,current,receipt)
        if is_speaking(connection, current['material'].get('diagnostic_item_id')) and not current_audio_review(connection, child_id, current):
            receipt = None
        from .writing_rubric import is_writing, current_writing_review
        if is_writing(connection, current['material'].get('diagnostic_item_id')) and not current_writing_review(connection, child_id, current):
            receipt = None
        if not receipt:
            receipt = self.calibrated_receipt(connection, child_id, row['step_id'],
                assessment_id=assessment_id, now=datetime.now(timezone.utc))
            # Text calibration never replaces original audio review/delivery.
            if is_speaking(connection, current['material'].get('diagnostic_item_id')):
                receipt = None
        return {"assessment_id": assessment_id, "local_only": True, "cloud_transmission_allowed": False,
            "eligible_for_mastery_gate": bool(receipt
            and receipt["correctness"] == 1 and receipt["independent"] and receipt["hint_level"] == 0),
            "receipt": receipt, "score_qualified": bool(receipt), "automatic_certification": False}

    def _typed_audio_qualification(self,c,child_id,current,human_receipt):
        # Human full review stays authoritative. Audio-only machine dimensions
        # cannot synthesize English content/criterion coverage or independence.
        from .pet_audio_rubric import current_audio_review
        from .rubric_calibration import RubricCalibrationService
        if not current_audio_review(c,child_id,current):human_receipt=None
        audio=RubricCalibrationService(self.database).original_audio_dimension_receipt(c,child_id,current['assessment_id'],as_of=datetime.now(timezone.utc))
        return {'assessment_id':current['assessment_id'],'local_only':True,'cloud_transmission_allowed':False,
            'eligible_for_mastery_gate':bool(human_receipt and human_receipt['correctness']==1 and human_receipt['independent']and human_receipt['hint_level']==0),
            'receipt':human_receipt,'score_qualified':bool(human_receipt),'automatic_certification':False,
            'original_audio_dimensions':audio,'overall_audio_and_content_status':'human_reviewed'if human_receipt else'partial_unqualified'}

    def audio_context(self, child_id, assessment_id, *, connection=None):
        if connection is None:
            with self.database.connect() as c:
                c.execute('BEGIN')
                return self.audio_context(child_id, assessment_id, connection=c)
        from .pet_audio_rubric import PROFILE, dimensions, is_speaking
        from .pet_speaking_review import prepare_speaking_audio_review
        from .database import local_media_scope
        current = self.get(child_id, assessment_id, connection=connection)
        required = is_speaking(connection, current['material'].get('diagnostic_item_id'))
        result = {'required': required, 'profile': PROFILE if required else None, 'dimensions': dimensions() if required else [],
                  'bindings': [], 'blockers': [], 'local_only': True, 'cloud_transmission_allowed': False}
        if not required:
            return result
        scope = local_media_scope(connection)
        if not scope:
            result['blockers'] = ['RUBRIC_ORIGINAL_AUDIO_RUNTIME_UNAVAILABLE']
            return result
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name='pet_media_binding'").fetchone():
            for row in connection.execute('''SELECT mb.media_id FROM pet_media_binding mb JOIN diagnostic_prompt dp USING(prompt_id)
                JOIN diagnostic_attempt da ON da.attempt_id=dp.attempt_id AND da.child_id=dp.child_id
                WHERE mb.child_id=? AND mb.session_id=? AND mb.kind='speaking' AND da.evidence_id=? ORDER BY mb.created_at,mb.media_id''',
                (child_id, current['session_id'], current['source_evidence_id'])):
                try:
                    result['bindings'].append(prepare_speaking_audio_review(connection, current['session_id'], media_id=row[0], assessment_id=assessment_id, **scope))
                except (ValueError, KeyError, TypeError):
                    continue
        if not result['bindings']:
            result['blockers'] = ['RUBRIC_ORIGINAL_AUDIO_MISSING_OR_CHANGED']
        return result

    def local_source_available(self, c, child_id, step_id, assessment_id):
        try:
            row = self._owned(c, child_id, assessment_id)
            current = self.get(child_id, assessment_id, connection=c)
        except ValueError:
            return False
        return (row["step_id"] == step_id and not row["model_call_id"]
                and current.get("source_kind") in {"local_criteria_mapping", "local_open_review"}
                and current["source_status"] == "current" and bool(current["source_evidence_id"])
                and current["source_evidence_id"] not in withdrawn_evidence_ids(c, child_id))

    def writing_context(self, child_id, assessment_id, *, connection=None):
        if connection is None:
            with self.database.connect() as c:
                c.execute('BEGIN')
                return self.writing_context(child_id, assessment_id, connection=c)
        from .writing_rubric import PROFILE, is_writing, prepare_writing_review
        current = self.get(child_id, assessment_id, connection=connection)
        required = is_writing(connection, current['material'].get('diagnostic_item_id'))
        result = {'required':required, 'profile':PROFILE if required else None, 'binding':None, 'blockers':[],
                  'local_only':True, 'cloud_transmission_allowed':False}
        if required:
            try:
                result['binding'] = prepare_writing_review(connection, child_id, assessment_id)
                result['profile'] = result['binding']['profile']
            except (ValueError, KeyError, TypeError) as error:
                result['blockers'] = [str(error)]
        return result

    def review(self, child_id, assessment_id, *, expected_revision, client_action_id, reviewer_id, reviewer_role,
               operation, reason, dimensions=None, criterion_evidence=None, independent=None, hint_level=None,
               review_profile=None, audio_evidence=None, writing_evidence=None, visual_evidence=None):
        if (type(expected_revision) is not int or expected_revision < 0 or reviewer_role not in {"parent", "teacher"}
                or operation not in {"approve", "retract"}):
            raise ValueError("RUBRIC_REVIEW_INVALID")
        if any(not isinstance(value, str) or not value.strip() or len(value) > limit
               for value, limit in ((client_action_id, 200), (reviewer_id, 200), (reason, 2000))):
            raise ValueError("RUBRIC_REVIEW_IDENTITY_AND_REASON_REQUIRED")
        request_values = {"assessment_id": assessment_id, "expected_revision": expected_revision,
            "reviewer_id": reviewer_id, "reviewer_role": reviewer_role, "operation": operation, "reason": reason,
            "dimensions": dimensions, "criterion_evidence": criterion_evidence, "independent": independent, "hint_level": hint_level}
        if review_profile is not None or audio_evidence is not None:
            request_values.update(review_profile=review_profile, audio_evidence=audio_evidence)
        if writing_evidence is not None:
            request_values['writing_evidence'] = writing_evidence
        if visual_evidence is not None:
            request_values['visual_evidence'] = visual_evidence
        request = _json(request_values)
        from .rubric_calibration_transaction import calibrated_write
        # A review may replace or retract an existing scoring authority and
        # its downstream mistake evidence. Serialize before the write lock.
        with self.database.external_effect_guard(timeout=2), calibrated_write(self.database, child_id) as c:
            row = self._owned(c, child_id, assessment_id)
            prior = c.execute("SELECT * FROM rubric_review_event WHERE child_id=? AND client_action_id=?", (child_id, client_action_id)).fetchone()
            if prior:
                if prior["request_json"] != request:
                    raise ValueError("RUBRIC_IDEMPOTENCY_CONFLICT")
                return self._event(prior)
            current = self.get(child_id, assessment_id, connection=c)
            from .pet_audio_rubric import PROFILE, dimensions as audio_dimensions, is_speaking, CRITERIA
            audio = review_profile == PROFILE
            from .writing_rubric import REVIEW_PROFILES, validate_writing_review, is_writing, writing_dimensions
            writing = isinstance(review_profile,str) and review_profile in REVIEW_PROFILES
            writing_domain = writing or is_writing(c, current['material'].get('diagnostic_item_id')) or bool(
                current['review'] and current['review'].get('review_profile') in REVIEW_PROFILES)
            if current["review_revision"] != expected_revision:
                raise ValueError("RUBRIC_REVISION_CONFLICT")
            if operation == "approve" and current["source_status"] != "current":
                raise ValueError("RUBRIC_SOURCE_CHANGED")
            if (operation=='approve' and current.get('source_kind')=='exam_open_review'
                    and writing_domain and (not writing or writing_evidence is None)):
                raise ValueError('RUBRIC_WRITING_EVIDENCE_REQUIRED')
            if operation == "retract":
                if not current["review"] or current["review"]["operation"] == "retract":
                    raise ValueError("RUBRIC_REVIEW_NOT_ACTIVE")
                if any(value is not None for value in (dimensions, criterion_evidence, independent, hint_level, review_profile, audio_evidence, writing_evidence, visual_evidence)):
                    raise ValueError("RUBRIC_RETRACTION_HAS_NO_SCORE")
            else:
                from .visual_question_evidence import validate_display_evidence
                validate_display_evidence(c, child_id, current, visual_evidence)
                definitions = current['dimensions']
                if writing or writing_evidence is not None:
                    if not writing or audio_evidence is not None or not isinstance(writing_evidence, dict) or set(writing_evidence) != {'binding_version','task_point_evidence'}:
                        raise ValueError('RUBRIC_WRITING_EVIDENCE_REQUIRED')
                    from .writing_rubric import validate_task_point_evidence
                    binding = validate_writing_review(c, child_id, assessment_id, binding_version=writing_evidence['binding_version'])
                    if binding['profile']!=review_profile:
                        raise ValueError('RUBRIC_WRITING_PROFILE_CHANGED')
                    validate_task_point_evidence(binding, writing_evidence['task_point_evidence'], current['response'])
                    definitions = writing_dimensions(c,child_id,binding['diagnostic_item_id'])
                elif review_profile is not None or audio_evidence is not None:
                    if not audio or not is_speaking(c, current['material'].get('diagnostic_item_id')):
                        raise ValueError('RUBRIC_AUDIO_PROFILE_REQUIRED')
                    if not isinstance(audio_evidence, dict) or set(audio_evidence) != {'media_id', 'review'}:
                        raise ValueError('RUBRIC_AUDIO_EVIDENCE_REQUIRED')
                    from .pet_speaking_review import validate_speaking_audio_review
                    from .database import local_media_scope
                    scope = local_media_scope(c)
                    if not scope:
                        raise ValueError('RUBRIC_ORIGINAL_AUDIO_RUNTIME_UNAVAILABLE')
                    validate_speaking_audio_review(c, current['session_id'], media_id=audio_evidence['media_id'],
                        assessment_id=assessment_id, review=audio_evidence['review'], **scope)
                    definitions = audio_dimensions()
                if (type(independent) is not bool or type(hint_level) is not int or not 0 <= hint_level <= 5
                        or not isinstance(dimensions, list) or len(dimensions) != len(definitions)):
                    raise ValueError("RUBRIC_DIMENSIONS_AND_CONDITIONS_REQUIRED")
                frozen_dimensions = {item["dimension_id"]: item for item in definitions}
                required = set(frozen_dimensions)
                seen = set()
                for item in dimensions:
                    if (not isinstance(item, dict) or not isinstance(item.get("dimension_id"), str)
                            or set(item) - {"dimension_id", "score", "response_quotes", "source_quotes", "rationale"}
                            or item["dimension_id"] not in required or item["dimension_id"] in seen
                            or type(item.get("score")) is not int or not 0 <= item["score"] <= frozen_dimensions[item["dimension_id"]]["max_score"]
                            or not isinstance(item.get("rationale"), str) or not item["rationale"].strip()
                            or len(item["rationale"]) > 800
                            or not self._quotes(item.get("response_quotes"), current["response"], required=item["score"] > 0 and not (audio and item['dimension_id'] in CRITERIA))):
                        raise ValueError("RUBRIC_DIMENSION_EVIDENCE_INVALID")
                    passage = current["material"].get("stimulus_text")
                    if (item["dimension_id"] == "evidence" and passage
                            and item["score"] > 0 and not self._quotes(item.get("source_quotes"), passage)):
                        raise ValueError("RUBRIC_SOURCE_QUOTE_REQUIRED")
                    if "source_quotes" in item and not self._quotes(item["source_quotes"], passage or current["question"], required=False):
                        raise ValueError("RUBRIC_SOURCE_QUOTE_INVALID")
                    seen.add(item["dimension_id"])
                if not isinstance(criterion_evidence, list) or not criterion_evidence:
                    raise ValueError("RUBRIC_CRITERION_EVIDENCE_REQUIRED")
                if any(not isinstance(item, dict) or item.get("criterion") not in current["criteria"]
                       or not self._quotes([item.get("response_quote")], current["response"]) for item in criterion_evidence):
                    raise ValueError("RUBRIC_CRITERION_EVIDENCE_INVALID")
            payload = {"score_status": "reviewed_human" if operation == "approve" else "withdrawn",
                "local_only": True, "cloud_transmission_allowed": False,
                "dimensions": dimensions, "criterion_evidence": criterion_evidence, "independent": independent,
                "hint_level": hint_level, "reason": reason, "passed": operation == "approve" and all(
                    item["score"] >= frozen_dimensions[item["dimension_id"]]["pass_score"] for item in dimensions),
                "automatic_certification": False, "assessment_fingerprint": row["fingerprint"]}
            if audio:
                payload.update(review_profile=PROFILE, audio_evidence=audio_evidence,
                               assessment_modality='original_audio_human_review', calibration_status='human_judgment_not_calibrated_exam')
            if writing:
                payload.update(review_profile=review_profile, writing_evidence=writing_evidence,
                    assessment_modality='original_writing_human_review', calibration_status='human_judgment_not_calibrated_exam')
            if visual_evidence is not None:
                payload['visual_evidence'] = visual_evidence
            from .school_open_reviews import is_school_item
            school = is_school_item(c, current['material'].get('diagnostic_item_id'))
            if operation == 'approve' and school and not writing_domain:
                from .school_spoken_score import review_origin, calibration_only
                # Blind labels train/check a scorer; they are not child work
                # reviews and must not create practice obligations.
                if not calibration_only(current):
                    payload['school_spoken_original_version'] = review_origin(c, child_id, current)
                    payload['assessment_modality'] = 'reviewed_spoken_reasoning_text'
            event_id = str(uuid4())
            now = datetime.now(timezone.utc).isoformat()
            c.execute("INSERT INTO rubric_review_event VALUES (?,?,?,?,?,?,?,?,?,?,?)", (event_id, assessment_id,
                child_id, expected_revision + 1, operation, reviewer_id, reviewer_role, _json(payload), request,
                client_action_id, now))
            if writing_domain or school:
                node = c.execute('''SELECT kn.track,kn.knowledge_node_id FROM diagnostic_item di
                    JOIN knowledge_node kn USING(knowledge_node_id) WHERE di.diagnostic_item_id=?''',
                    (current['material'].get('diagnostic_item_id'),)).fetchone()
                if node and node['track']=='pet':
                    from .writing_stage_reviews import apply_writing_stage_review
                    apply_writing_stage_review(c, child_id, assessment_id, as_of=now)
                elif node:
                    from .school_open_reviews import apply_school_review
                    apply_school_review(c, child_id, assessment_id, as_of=now)
                    from .learning_state import LearningStateService
                    LearningStateService(None).replay_mastery(child_id,node['knowledge_node_id'],as_of=now,
                        connection=c,reason='school_open_original_review')
            # A paper decision derived from the former calibrated model score
            # must not survive a human correction or retraction.  Reconciliation
            # runs in this same transaction so no stale mistake/review obligation
            # can become visible between the two journals.
            from .paper_scoring import PaperScoringService
            PaperScoringService.reconcile_subjective_decisions(
                c, child_id=child_id, assessment_id=assessment_id
            )
            c.commit()
            return self._event(c.execute("SELECT * FROM rubric_review_event WHERE event_id=?", (event_id,)).fetchone())

    def calibrated_receipt(self, c, child_id, step_id, *, now, assessment_id=None, calibration_service=None):
        """Current score proof, with independent response conditions kept separate.

        The optional service is a host/test dependency, never an HTTP payload.
        Production resolves authority from the signed local journal. Neither a
        model's independence claim nor an arbitrary criterion mapping is proof.
        """
        if not self.available(c):
            return None
        if assessment_id:
            rows = c.execute('''SELECT * FROM rubric_assessment
                WHERE child_id=? AND step_id=? AND assessment_id=?''',
                (child_id, step_id, assessment_id)).fetchall()
        else:
            rows = c.execute('SELECT * FROM rubric_assessment WHERE child_id=? AND step_id=?',
                (child_id, step_id)).fetchall()
        # A step is not an assessment identity. Legacy callers that do not bind
        # one explicitly may proceed only when the lookup is unambiguous.
        if len(rows) != 1:
            return None
        row = rows[0]
        from .rubric_calibration import RubricCalibrationService
        service = calibration_service or RubricCalibrationService(self.database)
        try:
            payload = json.loads(row['payload_json'])
            binding = payload.get('calibration_binding')
            if not isinstance(binding, dict) or not binding.get('protocol_id'):
                return None
            current = self.get(child_id, row['assessment_id'], connection=c)
            if current.get('source_status') != 'current' or current['material'].get('visual') is not None:
                # A text calibration certificate does not qualify visual interpretation.
                return None
            # A later human decision is not silently replaced by an earlier model.
            if current.get('review_revision', 0) or current.get('review'):
                return None
            # Reject ineligible originals before replaying the calibration corpus.
            # Eligible candidates still require the complete live authority check.
            proof = service.qualified_score_receipt(c, child_id, row['assessment_id'],
                protocol_id=binding['protocol_id'], as_of=now)
            if not proof.get('score_qualified'):
                return None
            current = self.get(child_id, row['assessment_id'], connection=c)
            if (current.get('source_status') != 'current' or current['material'].get('visual') is not None
                    or current.get('review_revision', 0) or current.get('review')):
                return None
            source = c.execute('''SELECT le.*, da.attempt_id, da.submitted_answer, di.prompt
                FROM learning_evidence le JOIN diagnostic_attempt da
                    ON da.evidence_id=le.evidence_id AND da.child_id=le.child_id AND da.session_id=le.session_id
                JOIN diagnostic_item di USING(diagnostic_item_id)
                WHERE le.evidence_id=? AND le.child_id=? AND le.session_id=?''',
                (proof['source_evidence_id'], child_id, current['session_id'])).fetchone()
            if not source or source['prompt'] != current['question'] or source['submitted_answer'] != current['response']:
                return None
            occurred = _time(source['occurred_at'])
            objective = c.execute('SELECT * FROM agent_objective WHERE objective_id=? AND child_id=?',
                                 (current['objective_id'], child_id)).fetchone()
            if not objective or occurred > now:
                return None
            item = proof['diagnostic_item_id']
            from .learning_obligations import project_item_exposures
            exposure, before = project_item_exposures(c, child_id, item,
                as_of=(occurred.isoformat(), (occurred - timedelta(milliseconds=1)).isoformat()))
            independent = False
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='diagnostic_prompt'").fetchone():
                prompt = c.execute('''SELECT * FROM diagnostic_prompt WHERE attempt_id=? AND child_id=?
                    AND origin_session_id=? AND diagnostic_item_id=? AND status='answered' ''',
                    (source['attempt_id'], child_id, current['session_id'], item)).fetchone()
                if prompt:
                    from .listening_review_submission import scoring_independence
                    independent = scoring_independence(c, prompt)
            independent = bool(independent and _time(objective['created_at']) <= occurred and source['hint_level'] == 0
                and current.get('independence_eligible') is not False
                and not exposure.get('tutor_assisted') and not before.get('answer_repeated'))
            for other in c.execute('''SELECT da.evidence_id FROM diagnostic_attempt da
                JOIN diagnostic_item di USING(diagnostic_item_id)
                WHERE da.child_id=? AND trim(di.prompt)=? AND julianday(da.created_at)=julianday(?)''',
                (child_id, current['question'], occurred.isoformat())):
                if other[0] != proof['source_evidence_id']:
                    independent = False
            criteria = json.loads(objective['success_criteria_json'])
            coverage = []
            for dimension in proof['dimensions']:
                criterion = dimension.get('criterion')
                if criterion not in criteria or dimension.get('score') not in (2, 3):
                    continue
                for quote in dimension.get('response_quotes', []):
                    if isinstance(quote, str) and quote.strip() and quote in current['response']:
                        coverage.append({'criterion': criterion, 'response_quote': quote})
                        break
            return {'evidence_id': 'calibrated:' + row['assessment_id'] + ':' + proof['certificate_event_id'],
                'assessment_id': row['assessment_id'], 'review_revision': 0,
                'score_status': 'calibrated_model', 'score_authority': proof['score_authority'],
                'criterion_mapping_status': 'calibrated_dimension_exact', 'calibration_receipt': proof,
                'question_identity': exposure.get('question_identity') or question_identity({'prompt': current['question']}),
                'occurred_at': occurred.isoformat(), 'diagnostic_item_id': item, 'knowledge_node_id': None,
                'correctness': int(proof['passed']), 'independent': independent,
                'hint_level': source['hint_level'], 'criterion_evidence': coverage}
        except (ValueError, KeyError, TypeError, AttributeError):
            return None

    def reviewed_receipt(self, c, child_id, step_id, *, now, assessment_id=None, _prior_index=None):
        if not self.available(c):
            return None
        if assessment_id:
            rows = c.execute('''SELECT * FROM rubric_assessment
                WHERE child_id=? AND step_id=? AND assessment_id=?''',
                (child_id, step_id, assessment_id)).fetchall()
        else:
            rows = c.execute('SELECT * FROM rubric_assessment WHERE child_id=? AND step_id=?',
                (child_id, step_id)).fetchall()
        if len(rows) != 1:
            return None
        row = rows[0]
        current = self.get(child_id, row["assessment_id"], connection=c)
        review = current["review"]
        if current["score_status"] != "reviewed_human" or not review:
            return None
        from .visual_question_evidence import validate_display_evidence
        try:
            validate_display_evidence(c, child_id, current, review.get('visual_evidence'))
        except (ValueError, TypeError, KeyError):
            return None
        from .pet_audio_rubric import PROFILE, current_audio_review
        if review.get('review_profile') == PROFILE and not current_audio_review(c, child_id, current):
            return None
        from .writing_rubric import is_writing, current_writing_review
        if is_writing(c, current['material'].get('diagnostic_item_id')) and not current_writing_review(c, child_id, current):
            return None
        from .school_open_reviews import is_school_item
        if is_school_item(c, current['material'].get('diagnostic_item_id')) and not is_writing(c, current['material'].get('diagnostic_item_id')):
            from .school_spoken_score import review_origin
            try:
                if review.get('school_spoken_original_version') != review_origin(c, child_id, current):
                    return None
            except (ValueError, KeyError, TypeError):
                return None
        local = current.get("source_kind") in {"local_criteria_mapping", "local_open_review", "exam_open_review"} and not row["model_call_id"]
        model = c.execute("""SELECT status,verification_status,safety_status FROM model_call
            WHERE call_id=? AND child_id=? AND session_id=? AND purpose='learning_outcome_evaluation'""",
            (row["model_call_id"], child_id, current["session_id"])).fetchone()
        if not local and (not model or model["status"] != "completed" or model["verification_status"] not in {"verified", "partially_verified"}
                or model["safety_status"] != "passed"):
            return None
        try:
            occurred = _time(current["responded_at"])
            if current.get('source_kind') == 'exam_open_review':
                created = c.execute('SELECT started_at FROM specialized_exam_run WHERE run_id=? AND child_id=? AND session_id=?',
                    (current['exam_slot']['run_id'],child_id,current['session_id'])).fetchone()[0]
            else:
                created = c.execute("SELECT created_at FROM agent_objective WHERE objective_id=?", (current["objective_id"],)).fetchone()[0]
            if not _time(created) <= occurred <= now or not current["question"]:
                return None
        except (ValueError, TypeError, AttributeError):
            return None
        identity = _assessment_identity(current, connection=c)
        independent = review["independent"] and review["hint_level"] == 0 and current["independence_eligible"] is not False
        if current["source_evidence_id"]:
            if current["source_evidence_id"] in withdrawn_evidence_ids(c, child_id):
                return None
            source = c.execute("""SELECT le.hint_level,le.occurred_at,di.prompt,da.submitted_answer FROM learning_evidence le
                JOIN diagnostic_attempt da ON da.evidence_id=le.evidence_id AND da.child_id=le.child_id
                JOIN diagnostic_item di ON di.diagnostic_item_id=da.diagnostic_item_id
                WHERE le.evidence_id=? AND le.child_id=? AND le.session_id=? AND da.session_id=le.session_id""",
                (current["source_evidence_id"], child_id, current["session_id"])).fetchone()
            if (not source or source["prompt"] != current["question"] or source["submitted_answer"] != current["response"]):
                return None
            if source["hint_level"] != 0:
                independent = False
            # Diagnosis submission, not later model completion, is the answer time.
            try:
                occurred = _time(source["occurred_at"])
                if not _time(created) <= occurred <= _time(current["responded_at"]):
                    return None
            except (ValueError, TypeError, AttributeError):
                return None
        if _prior_index is not None:
            if _prior_index.assisted(c, child_id, row['assessment_id'], identity, occurred):
                independent = False
        else:
            for prior in c.execute("SELECT step_id,payload_json FROM rubric_assessment WHERE child_id=? AND rowid<(SELECT rowid FROM rubric_assessment WHERE assessment_id=?)", (child_id, row["assessment_id"])):
                try:
                    prior_identity, prior_time = _prior_question_time(prior['payload_json'], connection=c)
                    if prior_identity == identity and timedelta(0) <= occurred - prior_time < timedelta(hours=24):
                        independent = False
                except (ValueError, TypeError, AttributeError, KeyError):
                    # Broken optional history must not erase the current score,
                    # but cannot establish independence without owned time proof.
                    if not _prior_answer_outside_window(c, child_id, prior['step_id'], occurred):
                        independent = False
        pinned_item = current["material"].get("diagnostic_item_id")
        items = ([(pinned_item,)] if pinned_item else c.execute(
            "SELECT diagnostic_item_id FROM diagnostic_item WHERE trim(prompt)=?", (current["question"],)).fetchall())
        if len(items) == 1:
            from .learning_obligations import project_item_exposures
            exposure, earlier = project_item_exposures(c, child_id, items[0][0],
                as_of=(occurred.isoformat(), (occurred - timedelta(milliseconds=1)).isoformat()))
            if exposure.get("tutor_assisted") or earlier.get("answer_repeated"):
                independent = False
            simultaneous = c.execute("""SELECT da.evidence_id FROM diagnostic_attempt da JOIN diagnostic_item di USING(diagnostic_item_id)
                WHERE da.child_id=? AND trim(di.prompt)=? AND julianday(da.created_at)=julianday(?)""",
                (child_id, current["question"], occurred.isoformat())).fetchall()
            if any(row[0] != current["source_evidence_id"] for row in simultaneous):
                independent = False
            identity = exposure.get("question_identity") or identity
        if current.get("source_kind") == "local_criteria_mapping":
            projections = project_child_mastery_evidence(c, child_id, as_of=now.isoformat())
            reliable = next((item for projection in projections.values() for item in projection["effective_evidence"]
                             if item["evidence_id"] == current["source_evidence_id"]), None)
            if not reliable or reliable["correctness"] != 1 or not reliable["independent"] or reliable["hint_level"]:
                return None
            independent = independent and bool(reliable["independent"])
        return {"evidence_id": f"rubric:{row['assessment_id']}:{review['review_revision']}",
                "assessment_id": row["assessment_id"], "review_revision": review["review_revision"],
                "score_status": "verified_local" if current.get("source_kind") == "local_criteria_mapping" else "reviewed_human",
                "criterion_mapping_status": "reviewed_human", "question_identity": identity, "occurred_at": occurred.isoformat(),
                "diagnostic_item_id": pinned_item,
                "knowledge_node_id": None, "correctness": int(review["passed"]), "independent": independent,
                "hint_level": review["hint_level"], "criterion_evidence": review["criterion_evidence"]}
