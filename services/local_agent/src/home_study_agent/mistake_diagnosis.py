"""Append-only differential observations, separate from mastery and causal facts.

Call the existing MistakeBookService.diagnose first to establish the mastery gate.
Then propose -> start_probe -> answer_probe -> existing scoring API -> attach_evidence.
Predictions must be registered before the answer and its scoring receipt exist.
All writes require the stream's expected_version and an idempotency key. Revisions
name supersedes_event_id; retracting a revision does not restore its predecessor.
get/project never install schema or persist inferred status. version tracks events;
projection_version also changes when externally owned evidence loses authority.
Hypothesis status stays candidate: prediction agreement is not causal validation.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from uuid import uuid4

from . import mistake_book
from .learning_obligations import _question_catalog, project_child_mastery_evidence, withdrawn_evidence_ids
from .local_supply import LocalSupplyError, validate_item


RULESET = "mistake_differential_observation_v1"


class DiagnosisInputError(ValueError):
    pass


class DiagnosisConflictError(DiagnosisInputError):
    pass


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _text(value, name, limit=8000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise DiagnosisInputError(f"{name} must be nonempty text (max {limit}).")
    return value


class MistakeDiagnosisService:
    def __init__(self, database):
        self.database = database

    def ensure_schema(self, connection=None):
        if connection is None:
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self.ensure_schema(connection)
                connection.commit()
            return
        # Business revisions stay append-only. Authorized PrivacyLifecycle hard
        # deletion must be able to remove the stream and all of its snapshots.
        connection.execute("SAVEPOINT mistake_diagnosis_schema")
        try:
            connection.execute("DROP TRIGGER IF EXISTS mistake_diagnosis_event_no_delete")
            foreign_keys = connection.execute("PRAGMA foreign_key_list(mistake_diagnosis_event)").fetchall()
            if foreign_keys and any(row["on_delete"] != "CASCADE" for row in foreign_keys):
                self._create_event_table(connection, "mistake_diagnosis_event_rebuild")
                connection.execute("""INSERT INTO mistake_diagnosis_event_rebuild
                    SELECT * FROM mistake_diagnosis_event ORDER BY mistake_id,version""")
                connection.execute("DROP TABLE mistake_diagnosis_event")
                connection.execute("ALTER TABLE mistake_diagnosis_event_rebuild RENAME TO mistake_diagnosis_event")
            else:
                self._create_event_table(connection, "mistake_diagnosis_event")
            self._ensure_event_triggers(connection)
            connection.execute("RELEASE mistake_diagnosis_schema")
        except Exception:
            connection.execute("ROLLBACK TO mistake_diagnosis_schema")
            connection.execute("RELEASE mistake_diagnosis_schema")
            raise

    @staticmethod
    def _create_event_table(connection, table):
        if table not in {"mistake_diagnosis_event", "mistake_diagnosis_event_rebuild"}:
            raise ValueError("Invalid diagnosis schema table.")
        connection.execute(f"""CREATE TABLE IF NOT EXISTS {table} (
            event_id TEXT PRIMARY KEY,
            mistake_id TEXT NOT NULL REFERENCES mistake_case(mistake_id) ON DELETE CASCADE,
            child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
            version INTEGER NOT NULL CHECK(version>0),
            schema_version INTEGER NOT NULL DEFAULT 1 CHECK(schema_version=1),
            kind TEXT NOT NULL CHECK(kind IN ('hypotheses','probe','answer','evidence','retraction')),
            payload_json TEXT NOT NULL,
            request_json TEXT NOT NULL,
            reference_event_id TEXT REFERENCES {table}(event_id) ON DELETE CASCADE,
            supersedes_event_id TEXT REFERENCES {table}(event_id) ON DELETE CASCADE,
            client_action_id TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            UNIQUE(mistake_id,version)
        )""")

    @staticmethod
    def _ensure_event_triggers(connection):
        connection.execute("""CREATE TRIGGER IF NOT EXISTS mistake_diagnosis_event_owner
            BEFORE INSERT ON mistake_diagnosis_event BEGIN
            SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM mistake_case
                WHERE mistake_id=NEW.mistake_id AND child_id=NEW.child_id)
                THEN RAISE(ABORT,'diagnosis ownership mismatch') END;
            SELECT CASE WHEN EXISTS (SELECT 1 FROM mistake_diagnosis_event
                WHERE event_id IN (NEW.reference_event_id,NEW.supersedes_event_id)
                AND (mistake_id!=NEW.mistake_id OR child_id!=NEW.child_id))
                THEN RAISE(ABORT,'diagnosis reference ownership mismatch') END;
            END""")
        connection.execute("""CREATE TRIGGER IF NOT EXISTS mistake_diagnosis_event_no_update
            BEFORE UPDATE ON mistake_diagnosis_event BEGIN
            SELECT RAISE(ABORT,'diagnosis events are append-only'); END""")

    @staticmethod
    def _case(connection, mistake_id, child_id):
        row = connection.execute("SELECT * FROM mistake_case WHERE mistake_id=? AND child_id=?",
                                 (mistake_id, child_id)).fetchone()
        if not row:
            raise DiagnosisInputError("Mistake does not belong to this child.")
        return dict(row)

    @staticmethod
    def _events(connection, mistake_id):
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='mistake_diagnosis_event'").fetchone():
            return []
        rows = connection.execute("SELECT * FROM mistake_diagnosis_event WHERE mistake_id=? ORDER BY version",
                                  (mistake_id,)).fetchall()
        owner = connection.execute("SELECT child_id FROM mistake_case WHERE mistake_id=?", (mistake_id,)).fetchone()
        if any(r["child_id"] != owner[0] for r in rows):
            raise DiagnosisInputError("Diagnosis history ownership is inconsistent.")
        events = [{**dict(r), "payload": json.loads(r["payload_json"])} for r in rows]
        # Retraction is final; retracting a revision never resurrects its predecessor.
        removed = {e["supersedes_event_id"] for e in events if e["supersedes_event_id"]}
        removed.update(e["reference_event_id"] for e in events if e["kind"] == "retraction")
        active = {}
        for e in events:
            parents = e["payload"].get("hypothesis_event_ids", [])
            if e["reference_event_id"] and e["kind"] != "retraction":
                parents = [*parents, e["reference_event_id"]]
            e["active"] = e["event_id"] not in removed and all(active.get(p, False) for p in parents)
            active[e["event_id"]] = e["active"]
        return events

    @staticmethod
    def _item(connection, item_id):
        raw = connection.execute("SELECT * FROM diagnostic_item WHERE diagnostic_item_id=?", (item_id,)).fetchone()
        if not raw:
            raise DiagnosisInputError("Unknown diagnostic item.")
        value = dict(raw)
        # Persist the version, never expose the answer rubric in a child-facing question.
        result = {k: value.get(k) for k in ("diagnostic_item_id", "knowledge_node_id", "prompt",
                   "stimulus_text", "stimulus_asset", "source_ref")}
        result["version"] = _digest(value)
        return result

    def _original(self, connection, case):
        original = {k: case[k] for k in ("knowledge_node_id", "diagnostic_item_id", "submitted_answer",
                    "prompt_summary", "source_type", "source_ref", "source_evidence_id")}
        original["item"] = self._item(connection, case["diagnostic_item_id"]) if case["diagnostic_item_id"] else None
        evidence = connection.execute("SELECT * FROM learning_evidence WHERE evidence_id=?",
                                      (case["source_evidence_id"],)).fetchone()
        original["evidence_version"] = _digest(dict(evidence)) if evidence else None
        from .paper_formula_review import for_mistake
        review = for_mistake(connection, case["mistake_id"], case["child_id"])
        if review:
            # Corrections leave the OCR record intact, but change what this
            # diagnosis is about. Bind the review receipt, including undo/re-edit.
            original["question_review"] = {key: review.get(key) for key in ("status", "review_id", "version")}
            if review["status"] == "confirmed":
                original["recorded_prompt_summary"] = original["prompt_summary"]
                original["prompt_summary"] = review["question"]
        original["version"] = _digest(original)
        return original

    @staticmethod
    def _reference(events, event_id, kind):
        found = next((e for e in events if e["event_id"] == event_id and e["kind"] == kind and e["active"]), None)
        if not found:
            raise DiagnosisInputError("Reference must be an active event in this mistake.")
        return found

    def get(self, mistake_id, child_id):
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            return self.project(connection, mistake_id, child_id)

    def _write(self, mistake_id, child_id, kind, request, expected_version, client_action_id):
        _text(client_action_id, "client_action_id", 200)
        if type(expected_version) is not int or expected_version < 0:
            raise DiagnosisInputError("expected_version must be a nonnegative integer.")
        try:
            canonical = _json({"kind": kind, **request})
        except (TypeError, ValueError) as error:
            raise DiagnosisInputError("Request must contain finite JSON values.") from error
        with self.database.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            case = self._case(c, mistake_id, child_id)
            self.ensure_schema(c)
            previous = c.execute("SELECT * FROM mistake_diagnosis_event WHERE client_action_id=?", (client_action_id,)).fetchone()
            if previous:
                if (previous["mistake_id"] != mistake_id or previous["child_id"] != child_id
                        or previous["request_json"] != canonical):
                    raise DiagnosisConflictError("Action already used with a different request.")
                state = self.project(c, mistake_id, child_id)
                state["event_id"] = previous["event_id"]
                return state
            events = self._events(c, mistake_id)
            version = events[-1]["version"] if events else 0
            if version != expected_version:
                raise DiagnosisConflictError(f"Expected version {expected_version}, current version {version}.")
            if case["status"] == "excluded" and kind != "retraction":
                raise DiagnosisInputError("Excluded mistake cannot receive diagnosis events.")
            if not c.execute("SELECT 1 FROM mistake_mastery_gate WHERE mistake_id=?", (mistake_id,)).fetchone():
                raise DiagnosisInputError("Call diagnose before starting differential assessment.")
            event_id, now = str(uuid4()), mistake_book.utc_now()
            payload, reference, supersedes = self._prepare(c, case, events, kind, request, event_id)
            c.execute("""INSERT INTO mistake_diagnosis_event(event_id,mistake_id,child_id,version,kind,
                payload_json,request_json,reference_event_id,supersedes_event_id,client_action_id,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (event_id, mistake_id, child_id, version + 1, kind, _json(payload), canonical,
                 reference, supersedes, client_action_id, now))
            state = self.project(c, mistake_id, child_id)
            c.commit()
            return state

    def propose(self, mistake_id, child_id, hypotheses, expected_version, client_action_id, supersedes_event_id=None):
        return self._write(mistake_id, child_id, "hypotheses", {"hypotheses": hypotheses,
            "supersedes_event_id": supersedes_event_id}, expected_version, client_action_id)

    def start_probe(self, mistake_id, child_id, diagnostic_item_id, predictions, expected_version, client_action_id):
        return self._write(mistake_id, child_id, "probe", {"diagnostic_item_id": diagnostic_item_id,
            "predictions": predictions}, expected_version, client_action_id)

    def answer_probe(self, mistake_id, child_id, probe_event_id, answer, expected_version, client_action_id, supersedes_event_id=None):
        return self._write(mistake_id, child_id, "answer", {"probe_event_id": probe_event_id,
            "answer": answer, "supersedes_event_id": supersedes_event_id}, expected_version, client_action_id)

    def attach_evidence(self, mistake_id, child_id, answer_event_id, evidence_id, expected_version, client_action_id):
        return self._write(mistake_id, child_id, "evidence", {"answer_event_id": answer_event_id,
            "evidence_id": evidence_id}, expected_version, client_action_id)

    def retract(self, mistake_id, child_id, target_event_id, reason, expected_version, client_action_id):
        return self._write(mistake_id, child_id, "retraction", {"target_event_id": target_event_id,
            "reason": reason}, expected_version, client_action_id)

    def _prepare(self, c, case, events, kind, request, event_id):
        supersedes = request.get("supersedes_event_id")
        if supersedes:
            self._reference(events, supersedes, kind)
        payload = dict(request)
        if kind == "hypotheses":
            if not supersedes and any(e["active"] and e["kind"] == "hypotheses" for e in events):
                raise DiagnosisConflictError("Revise the current hypothesis set explicitly.")
            hypotheses = request["hypotheses"]
            if not isinstance(hypotheses, list) or not 1 <= len(hypotheses) <= 9:
                raise DiagnosisInputError("Provide 1 to 9 hypotheses.")
            validated, seen = [], set()
            for h in hypotheses:
                if not isinstance(h, dict) or set(h) - {"error_type", "stance", "explanation", "confidence"}:
                    raise DiagnosisInputError("Invalid hypothesis fields.")
                error, stance = h.get("error_type"), h.get("stance")
                if (not isinstance(error, str) or error not in mistake_book.GATE_REQUIREMENTS or error in seen
                        or not isinstance(stance, str) or stance not in {"candidate", "denied", "unknown"}
                        or (error == "unknown") != (stance == "unknown")):
                    raise DiagnosisInputError("Invalid hypothesis type or stance.")
                _text(h.get("explanation"), "explanation")
                confidence = h.get("confidence")
                if confidence is not None and (type(confidence) not in (float, int)
                        or not math.isfinite(confidence) or not 0 <= confidence <= 1):
                    raise DiagnosisInputError("Invalid candidate confidence.")
                seen.add(error)
                validated.append({**h, "hypothesis_id": str(uuid4()), "event_id": event_id, "status": "candidate"})
            if "unknown" not in seen:
                validated.append({"hypothesis_id": str(uuid4()), "event_id": event_id, "error_type": "unknown",
                    "stance": "unknown", "status": "candidate", "explanation": "原因尚未确定，也可能有其他原因。"})
            payload["hypotheses"] = validated
            payload["original"] = self._original(c, case)
            return payload, None, supersedes
        if kind == "probe":
            predictions = request["predictions"]
            state = self.project(c, case["mistake_id"], case["child_id"])
            hypotheses = {h["hypothesis_id"]: h for h in state["hypotheses"]}
            if not hypotheses:
                raise DiagnosisInputError("Propose hypotheses before asking a question.")
            if any(not h["original_current"] for h in hypotheses.values()):
                raise DiagnosisInputError("Revise hypotheses after the original answer or source changes.")
            if (not isinstance(predictions, dict) or any(k not in hypotheses or hypotheses[k]["stance"] != "candidate"
                    or not isinstance(v, str) or v not in {"correct", "incorrect"} for k, v in predictions.items())):
                raise DiagnosisInputError("Predictions must reference current candidate hypotheses.")
            _text(request["diagnostic_item_id"], "diagnostic_item_id", 200)
            item = self._item(c, request["diagnostic_item_id"])
            if item["knowledge_node_id"] != case["knowledge_node_id"]:
                raise DiagnosisInputError("Probe must belong to the mistake's knowledge node.")
            try:
                validate_item(c, case["child_id"], request["diagnostic_item_id"], mistake_id=case["mistake_id"])
            except (LocalSupplyError, ValueError, TypeError, KeyError) as error:
                raise DiagnosisInputError("Probe source is not currently available to this child and mistake.") from error
            _, identities = _question_catalog(c)
            payload["binding"] = {"item": item, "original": self._original(c, case),
                                  "question_identity": identities.get(request["diagnostic_item_id"])}
            payload["question"] = "请独立完成这道题；不确定也可以如实回答。\n" + item["prompt"]
            payload["follow_up_question"] = "你是怎样理解和处理这道题的？哪些地方还不确定？"
            payload["hypothesis_event_ids"] = sorted({h["event_id"] for h in hypotheses.values()})
            payload["evidence_after_rowid"] = self._evidence_watermark(c)
            return payload, None, None
        if kind == "answer":
            probe = self._reference(events, request["probe_event_id"], "probe")
            _text(request["answer"], "answer")
            existing = [e for e in events if e["active"] and e["kind"] == "answer" and e["reference_event_id"] == probe["event_id"]]
            if existing and supersedes != existing[-1]["event_id"]:
                raise DiagnosisConflictError("Revise the current answer explicitly.")
            if supersedes and self._reference(events, supersedes, "answer")["reference_event_id"] != probe["event_id"]:
                raise DiagnosisInputError("Answer revision must belong to the same probe.")
            payload["binding_version"] = _digest(probe["payload"]["binding"])
            payload["evidence_after_rowid"] = self._evidence_watermark(c)
            return payload, probe["event_id"], supersedes
        if kind == "evidence":
            answer = self._reference(events, request["answer_event_id"], "answer")
            _text(request["evidence_id"], "evidence_id", 200)
            evidence = c.execute("SELECT * FROM learning_evidence WHERE evidence_id=? AND child_id=? AND knowledge_node_id=?",
                (request["evidence_id"], case["child_id"], case["knowledge_node_id"])).fetchone()
            if not evidence:
                raise DiagnosisInputError("Evidence must belong to this child and knowledge node.")
            if any(e["active"] and e["kind"] == "evidence" and e["payload"]["evidence_id"] == request["evidence_id"] for e in events):
                raise DiagnosisConflictError("Evidence is already attached; retract its old attachment first.")
            payload["evidence_version"] = _digest(dict(evidence))
            payload["receipt_version"] = _digest(self._receipts(c, request["evidence_id"]))
            payload["recorded_score"] = {k: evidence[k] for k in (
                "correctness", "confidence", "independent", "hint_level", "occurred_at")}
            projection = project_child_mastery_evidence(c, case["child_id"], as_of=mistake_book.utc_now()).get(case["knowledge_node_id"], {})
            effective = {r["evidence_id"]: r for r in projection.get("effective_evidence", [])}
            excluded = {r["evidence_id"]: r["reason"] for r in projection.get("excluded_evidence", [])}
            probe = self._reference(events, answer["reference_event_id"], "probe")
            reason = self._eligibility(c, case, probe, answer, {"payload": payload}, effective, excluded,
                                      self._original(c, case), withdrawn_evidence_ids(c, case["child_id"]))
            payload["recorded_qualification"] = {"score_eligible": reason is None, "reason": reason,
                "ruleset": RULESET, "meaning": "score_binding_only_not_causal_confirmation"}
            payload["remediation_after_rowid"] = c.execute("SELECT COALESCE(MAX(rowid),0) FROM remediation_step").fetchone()[0]
            return payload, answer["event_id"], None
        if kind == "retraction":
            _text(request["reason"], "reason")
            target = next((e for e in events if e["event_id"] == request["target_event_id"]), None)
            if not target or not target["active"] or target["kind"] == "retraction":
                raise DiagnosisInputError("Only an active non-retraction event may be withdrawn.")
            return payload, target["event_id"], None
        raise DiagnosisInputError("Unknown event kind.")

    @staticmethod
    def _receipts(c, evidence_id):
        return [dict(r) for table in ("diagnostic_attempt", "mistake_attempt")
                for r in c.execute(f"SELECT * FROM {table} WHERE evidence_id=?", (evidence_id,))]

    @staticmethod
    def _evidence_watermark(c):
        return c.execute("SELECT COALESCE(MAX(rowid),0) FROM learning_evidence").fetchone()[0]

    def _binding_issue(self, c, case, binding, original, withdrawn):
        if case["status"] == "excluded" or case["source_evidence_id"] in withdrawn:
            return "original_withdrawn"
        if original.get("question_review", {}).get("status", "confirmed") != "confirmed":
            return "original_question_review_required"
        if binding["original"] != original:
            return "original_version_changed"
        item_id = binding["item"]["diagnostic_item_id"]
        try:
            if binding["item"] != self._item(c, item_id):
                return "question_source_changed"
            validate_item(c, case["child_id"], item_id, mistake_id=case["mistake_id"])
        except (LocalSupplyError, ValueError, TypeError, KeyError):
            return "question_source_unavailable"
        return None

    @staticmethod
    def _observable_answer(c, item_id, answer, correctness):
        if correctness == 1:
            return True
        # An exact scorer's zero may mean a non-answer. Only interpretable wrong
        # responses can test a prediction; this does not change practice scoring.
        from .learning_state import normalize_spoken_math
        rubric = json.loads(c.execute("SELECT expected_answer_json FROM diagnostic_item WHERE diagnostic_item_id=?",
                                      (item_id,)).fetchone()[0])
        values = [str(v) for v in rubric.get("exact_answers", [])]
        value = normalize_spoken_math(unicodedata.normalize("NFKC", answer)).strip("。，,.!！ ")
        value = re.sub(r"^(?:答案是|答案为|我选|选择|选)", "", value).strip()
        numeric = r"[+-]?\d+(?:/\d+|\.\d+)?"
        if values and all(re.fullmatch(numeric, v) for v in values):
            return re.fullmatch(numeric, value) is not None
        return False

    def _eligibility(self, c, case, probe, answer, attachment, effective, excluded, original, withdrawn):
        payload, binding = attachment["payload"], probe["payload"]["binding"]
        evidence_id = payload["evidence_id"]
        evidence = c.execute("SELECT * FROM learning_evidence WHERE evidence_id=?", (evidence_id,)).fetchone()
        receipts = self._receipts(c, evidence_id)
        binding_issue = self._binding_issue(c, case, binding, original, withdrawn)
        if binding_issue:
            return binding_issue
        if not evidence or _digest(dict(evidence)) != payload["evidence_version"] or _digest(receipts) != payload["receipt_version"]:
            return "evidence_version_changed"
        rowid = c.execute("SELECT rowid FROM learning_evidence WHERE evidence_id=?", (evidence_id,)).fetchone()[0]
        if rowid <= probe["payload"]["evidence_after_rowid"]:
            return "evidence_predates_probe"
        if rowid <= answer["payload"]["evidence_after_rowid"]:
            return "evidence_predates_answer"
        if evidence_id not in effective:
            return excluded.get(evidence_id, "unqualified_evidence")
        receipt, qualified = receipts[0], effective[evidence_id]
        if (receipt["diagnostic_item_id"] != binding["item"]["diagnostic_item_id"]
                or receipt["submitted_answer"] != answer["payload"]["answer"]
                or qualified["mistake_id"] not in (None, case["mistake_id"])):
            return "answer_or_scope_mismatch"
        if mistake_book._parse_time(qualified["occurred_at"]) < mistake_book._parse_time(answer["created_at"]):
            return "evidence_predates_answer"
        if not qualified["independent"] or qualified["hint_level"]:
            return "not_independent"
        if qualified["question_identity"] != binding["question_identity"]:
            return "question_identity_changed"
        if not self._observable_answer(c, receipt["diagnostic_item_id"], receipt["submitted_answer"], qualified["correctness"]):
            return "answer_not_observable"
        return None

    def project(self, connection, mistake_id, child_id):
        case = self._case(connection, mistake_id, child_id)
        events = self._events(connection, mistake_id)
        active = [e for e in events if e["active"]]
        original = self._original(connection, case)
        original_ready = original.get("question_review", {}).get("status", "confirmed") == "confirmed"
        hypotheses = [{**h, "original_current": original_ready and e["payload"]["original"] == original}
                      for e in active if e["kind"] == "hypotheses" for h in e["payload"]["hypotheses"]]
        projection = project_child_mastery_evidence(connection, child_id, as_of=mistake_book.utc_now()).get(case["knowledge_node_id"], {})
        effective = {r["evidence_id"]: r for r in projection.get("effective_evidence", [])}
        excluded = {r["evidence_id"]: r["reason"] for r in projection.get("excluded_evidence", [])}
        withdrawn = withdrawn_evidence_ids(connection, child_id)
        support, counter, insufficiency, probes, used = [], [], [], [], set()
        identities, first_seen = mistake_book.MistakeBookService._question_history(connection, child_id)
        original_identity = identities.get(case["diagnostic_item_id"])
        for probe in [e for e in active if e["kind"] == "probe"]:
            value = {"event_id": probe["event_id"], **probe["payload"], "answer": None, "evidence": []}
            value["binding_status"] = self._binding_issue(connection, case, value["binding"], original, withdrawn)
            if value["binding_status"]:
                insufficiency.append(value["binding_status"])
            answer = next((e for e in active if e["kind"] == "answer" and e["reference_event_id"] == probe["event_id"]), None)
            if answer:
                value["answer"] = {"event_id": answer["event_id"], "answer": answer["payload"]["answer"], "version": answer["version"]}
                attachments = [e for e in active if e["kind"] == "evidence" and e["reference_event_id"] == answer["event_id"]]
                for attachment in attachments:
                    reason = self._eligibility(connection, case, probe, answer, attachment, effective, excluded, original, withdrawn)
                    identity = value["binding"]["question_identity"]
                    predictions = value["predictions"]
                    if not reason and (not identity or identity == original_identity or identity in used):
                        reason = "same_question_not_replication"
                    if not reason and first_seen.get(identity) != attachment["payload"]["evidence_id"]:
                        reason = "previously_seen_question"
                    if not reason and set(predictions.values()) != {"correct", "incorrect"}:
                        reason = "non_discriminating_predictions"
                    result = {"event_id": attachment["event_id"], "evidence_id": attachment["payload"]["evidence_id"],
                              "eligible": reason is None, "reason": reason}
                    value["evidence"].append(result)
                    if reason:
                        insufficiency.append(reason)
                        continue
                    used.add(identity)
                    observed = "correct" if effective[result["evidence_id"]]["correctness"] == 1 else "incorrect"
                    for hypothesis_id, predicted in predictions.items():
                        (support if observed == predicted else counter).append({"hypothesis_id": hypothesis_id,
                            "probe_event_id": probe["event_id"], "evidence_id": result["evidence_id"],
                            "question_identity": identity, "predicted": predicted, "observed": observed,
                            "meaning": "prediction_match" if predicted == observed else "prediction_mismatch"})
            probes.append(value)
        for h in hypotheses:
            positive = sum(r["hypothesis_id"] == h["hypothesis_id"] for r in support)
            negative = sum(r["hypothesis_id"] == h["hypothesis_id"] for r in counter)
            h["result"] = ("mixed" if positive and negative else "supported_pattern" if positive >= 2
                           else "counterevidence_pattern" if negative >= 2 else "insufficient")
        patterns = [h for h in hypotheses if h["result"] == "supported_pattern"]
        if len(patterns) > 1:
            insufficiency.append("ambiguous_hypotheses")
        if len(used) < 2:
            insufficiency.append("replication_required")
        insufficiency.append("causal_discrimination_not_validated")
        assessment = {"status": "pattern_observed" if patterns else "insufficient", "causal_status": "unconfirmed",
                      "ruleset": RULESET, "support": support, "counterevidence": counter,
                      "insufficiency": sorted(set(insufficiency)),
                      "statement": "这些观察只检验预先登记的作答预测，不能单独确定错因。"}
        if case["status"] == "excluded" or case["source_evidence_id"] in withdrawn:
            action = {"kind": "blocked", "reason": "original_withdrawn"}
        elif not original_ready:
            action = {"kind": "blocked", "reason": "original_question_review_required"}
        elif not hypotheses:
            action = {"kind": "propose_hypotheses"}
        elif any(not h["original_current"] for h in hypotheses):
            action = {"kind": "revise_hypotheses", "reason": "original_version_changed",
                      "event_ids": sorted({h["event_id"] for h in hypotheses if not h["original_current"]})}
        elif any(not p["answer"] and not p["binding_status"] for p in probes):
            action = {"kind": "answer_probe", "probe_event_id": next(p["event_id"] for p in probes if not p["answer"] and not p["binding_status"])}
        elif any(p["answer"] and not p["evidence"] and not p["binding_status"] for p in probes):
            action = {"kind": "await_score", "answer_event_id": next(p["answer"]["event_id"] for p in probes if p["answer"] and not p["evidence"] and not p["binding_status"])}
        elif len(patterns) == 1:
            error_type = patterns[0]["error_type"]
            detail = {"diagnosis": {"error_type": "unknown" if error_type == "incidental" else error_type}}
            action = {"kind": "remediation", "hypothesis_id": patterns[0]["hypothesis_id"],
                      "prompt": mistake_book.MistakeBookService.remediation_prompt(detail), "basis": "tentative_pattern",
                      "causal_status": "unconfirmed"}
            basis = {r["evidence_id"] for r in support if r["hypothesis_id"] == patterns[0]["hypothesis_id"]}
            cutoff = max(e["payload"]["remediation_after_rowid"] for e in active
                         if e["kind"] == "evidence" and e["payload"]["evidence_id"] in basis)
            step = connection.execute("""SELECT step_id,outcome FROM remediation_step
                WHERE mistake_id=? AND child_id=? AND rowid>? ORDER BY rowid DESC LIMIT 1""",
                (mistake_id, child_id, cutoff)).fetchone()
            if step and step["outcome"] == "completed":
                action = {"kind": "mastery_verification", "remediation_step_id": step["step_id"],
                          "reason": "execution_recorded_not_cause_confirmation", "mistake_id": mistake_id}
        else:
            action = {"kind": "additional_probe" if probes else "start_probe", "knowledge_node_id": case["knowledge_node_id"],
                      "exclude_question_identities": sorted(set(first_seen) | used | ({original_identity} if original_identity else set()))}
        return {"mistake_id": mistake_id, "version": events[-1]["version"] if events else 0,
                "event_id": events[-1]["event_id"] if events else None, "hypotheses": hypotheses, "probes": probes,
                "projection_version": _digest({"original": original, "evidence": projection.get("evidence_fingerprint"),
                    "hypotheses": hypotheses, "probes": probes, "assessment": assessment, "next_action": action}),
                "assessment": assessment, "next_action": action,
                "mastery_minimum": dict(mistake_book.GATE_REQUIREMENTS["unknown"]),
                "history": [{k: e[k] for k in ("event_id", "version", "kind", "payload", "reference_event_id",
                             "supersedes_event_id", "created_at", "active")} for e in events]}
