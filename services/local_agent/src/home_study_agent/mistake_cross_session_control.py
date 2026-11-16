"""Local, confirmed control of a scored answer from an earlier study session."""
from __future__ import annotations

from datetime import datetime, timedelta
import json
import re
from zoneinfo import ZoneInfo

from .agent_runtime import InvalidAgentPlanError
from .local_supply import digest
from .mistake_capture import InvalidMistakeCaptureCandidateError


def request(text: str) -> dict | None:
    value = re.sub(r"[，。！？、\s]", "", text or "")
    if not value or not re.search(r"(?:上次|昨天|前天|之前|前几天).{0,12}(?:题|回答|答案|作答)", value):
        return None
    if re.search(r"(?:不要|别|先不|暂不|不想|无需|不用).{0,14}(?:撤|取消|作废|改成|改为|换成|更正)", value):
        return None
    if re.search(r"(?:如果|假如|老师说|同学说|是不是|为什么|怎么会|吗)$", value):
        return None
    changed = re.search(r"(?:答案|回答|作答)(?:要)?(?:改成|改为|换成|替换成|更正为)(.+)$", value)
    if changed and changed[1]:
        return {"control": "replace", "replacement_answer": changed[1],
                "scope": _scope(value)}
    if re.search(r"(?:撤回|撤销|作废|撤掉|删掉|不算数|不作数)", value):
        return {"control": "withdraw", "replacement_answer": None,
                "scope": _scope(value)}
    return None


def _scope(value: str) -> str:
    if "前天" in value:
        return "day_before_yesterday"
    if "昨天" in value:
        return "yesterday"
    return "latest"


def _local_date(value: str):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.date()
        return parsed.astimezone(ZoneInfo("Asia/Shanghai")).date()
    except (TypeError, ValueError):
        return None


def _case_snapshot(row) -> dict:
    value = {key: row[key] for key in
             ("mistake_id", "child_id", "knowledge_node_id", "subject",
              "prompt_summary", "submitted_answer", "source_type", "source_ref")}
    value["hash"] = digest(value)
    return value


def _candidates(service, session_id: str, child_id: str, scope: str):
    from .mistake_scored_correction import validate_binding

    with service.database.connect() as c:
        current = c.execute("SELECT started_at FROM daily_session WHERE session_id=? AND child_id=?",
                            (session_id, child_id)).fetchone()
        current_date = _local_date(current["started_at"]) if current else None
        wanted = (current_date - timedelta(days=1) if scope == "yesterday" and current_date
                  else current_date - timedelta(days=2) if scope == "day_before_yesterday" and current_date
                  else None)
        candidates = []
        mastery = c.execute("""SELECT ma.*,le.session_id,le.knowledge_node_id,
                le.hint_level,le.occurred_at,s.started_at
            FROM mistake_attempt ma JOIN learning_evidence le USING(evidence_id)
            JOIN daily_session s ON s.session_id=le.session_id
            WHERE ma.child_id=? AND le.session_id<>?""", (child_id, session_id)).fetchall()
        diagnosis = c.execute("""SELECT da.*,evidence.mistake_id AS mistake_id,
                le.knowledge_node_id,le.hint_level,
                le.occurred_at,s.started_at,answer.event_id AS answer_event_id,
                answer.client_action_id AS answer_action_id,
                json_extract(answer.payload_json,'$.probe_event_id') AS probe_event_id
            FROM diagnostic_attempt da JOIN learning_evidence le USING(evidence_id)
            JOIN daily_session s ON s.session_id=da.session_id
            JOIN mistake_diagnosis_event evidence
              ON evidence.kind='evidence'
             AND json_extract(evidence.payload_json,'$.evidence_id')=da.evidence_id
            JOIN mistake_diagnosis_event answer
              ON answer.event_id=json_extract(evidence.payload_json,'$.answer_event_id')
            WHERE da.child_id=? AND da.session_id<>?
              AND NOT EXISTS(SELECT 1 FROM mistake_diagnosis_event later
                WHERE later.mistake_id=evidence.mistake_id
                  AND ((later.kind='retraction' AND later.reference_event_id=answer.event_id)
                       OR later.supersedes_event_id=answer.event_id))""",
            (child_id, session_id)).fetchall()
        rows = []
        for row in mastery:
            item = c.execute("SELECT * FROM diagnostic_item WHERE diagnostic_item_id=?",
                             (row["diagnostic_item_id"],)).fetchone()
            if not item:
                continue
            action_id = row["client_action_id"]
            action_id = action_id[:-8] if action_id.endswith(":attempt") else action_id
            binding = {"version": 1, "kind": "mastery_answer", "mistake_id": row["mistake_id"],
                "child_id": child_id, "session_id": row["session_id"], "action_id": action_id,
                "attempt_id": row["attempt_id"], "evidence_id": row["evidence_id"],
                "diagnostic_item_id": row["diagnostic_item_id"], "answer": row["submitted_answer"],
                "hint_level": int(row["hint_level"]), "item_version": digest(dict(item)),
                "answer_event_id": None, "stage": row["stage"]}
            binding["binding_hash"] = digest(binding)
            case = c.execute("SELECT * FROM mistake_case WHERE mistake_id=? AND child_id=?",
                             (row["mistake_id"], child_id)).fetchone()
            if case:
                rows.append((row, binding, _case_snapshot(case)))
        for row in diagnosis:
            item = c.execute("SELECT * FROM diagnostic_item WHERE diagnostic_item_id=?",
                             (row["diagnostic_item_id"],)).fetchone()
            if not item:
                continue
            action_id = row["answer_action_id"]
            action_id = action_id[:-7] if action_id.endswith(":answer") else action_id
            binding = {"version": 1, "kind": "diagnosis_answer", "mistake_id": row["mistake_id"],
                "child_id": child_id, "session_id": row["session_id"], "action_id": action_id,
                "attempt_id": row["attempt_id"], "evidence_id": row["evidence_id"],
                "diagnostic_item_id": row["diagnostic_item_id"], "answer": row["submitted_answer"],
                "hint_level": int(row["hint_level"]), "item_version": digest(dict(item)),
                "answer_event_id": row["answer_event_id"], "probe_event_id": row["probe_event_id"]}
            binding["binding_hash"] = digest(binding)
            case = c.execute("SELECT * FROM mistake_case WHERE mistake_id=? AND child_id=?",
                             (row["mistake_id"], child_id)).fetchone()
            if case:
                rows.append((row, binding, _case_snapshot(case)))
        rows.sort(key=lambda value: value[0]["occurred_at"], reverse=True)
        for row, binding, case in rows:
            if wanted and _local_date(row["occurred_at"]) != wanted:
                continue
            origin = {"session_id": row["session_id"], "child_id": child_id,
                      "mistake_id": row["mistake_id"]}
            try:
                validate_binding(c, origin, binding)
            except InvalidAgentPlanError:
                continue
            candidates.append(dict(binding=binding, case=case,
                origin_session_id=row["session_id"], origin_started_at=row["started_at"]))
            if scope == "latest" or len(candidates) >= 5:
                break
    return candidates


def _proposal(found: dict, parsed: dict, text: str, action_id: str) -> dict:
    value = {"version": 1, "control": parsed["control"],
        "replacement_answer": parsed.get("replacement_answer"), "request_utterance": text,
        "offer_action_id": action_id, **found}
    value["proposal_hash"] = digest(value)
    return value


def _candidate_list_readback(state: dict) -> str:
    parts = []
    for index, item in enumerate(state["historical_scored_candidates"], 1):
        case = item["case"]
        parts.append(f"第{index}道，{case['subject']}：{case['prompt_summary'][:120]}")
    return ("我在这个时间范围内找到不止一道已评分的题。" + "；".join(parts)
            + "。请说“第一道”或“第二道”，我会读回答案后再请你确认；也可以说“取消”。")


def _readback(proposal: dict) -> str:
    case = proposal["case"]
    answer = proposal["binding"]["answer"]
    question = case["prompt_summary"][:500]
    if proposal["control"] == "replace":
        action = f"把当时回答“{answer}”改成“{proposal['replacement_answer']}”"
    else:
        action = f"撤回当时回答“{answer}”的评分效力"
    return (f"我在本机找到的是{case['subject']}这道题：{question}。你要{action}吗？"
            "说“确认”我才会执行；不是这道就说“取消”。")


def offer(service, session_id: str, text: str, action_id: str, parsed: dict) -> dict:
    child_id = service._active_child(session_id)
    found = _candidates(service, session_id, child_id, parsed["scope"])
    if not found:
        raise InvalidMistakeCaptureCandidateError(
            "没有找到这个孩子对应时间范围内、仍然有效的已评分错题回答。")
    if len(found) > 1:
        focus = {"session_id": session_id, "child_id": child_id, "candidate_id": None,
            "mistake_id": None, "phase": "diagnosis",
            "state": {"historical_scored_candidates": found,
                      "historical_scored_request": {**parsed, "request_utterance": text}}}
        return service._save(focus, _candidate_list_readback(focus["state"]), action_id, text)
    proposal = _proposal(found[0], parsed, text, action_id)
    case = found[0]["case"]
    focus = {"session_id": session_id, "child_id": child_id, "candidate_id": None,
        "mistake_id": case["mistake_id"], "phase": "diagnosis",
        "state": {"question_text": case["prompt_summary"], "subject": case["subject"],
                  "submitted_answer": case["submitted_answer"],
                  "historical_scored_control": proposal}}
    return service._save(focus, _readback(proposal), action_id, text)


def _valid_proposal(c, focus: dict, proposal: dict, *, require_offer=True,
                    allow_withdrawn=False) -> None:
    from .mistake_scored_correction import validate_binding

    if (not isinstance(proposal, dict) or proposal.get("version") != 1
            or proposal.get("proposal_hash") != digest(
                {key: value for key, value in proposal.items() if key != "proposal_hash"})
            or proposal.get("control") not in {"replace", "withdraw"}
            or ((proposal.get("replacement_answer") is None)
                != (proposal.get("control") == "withdraw"))
            or proposal.get("origin_session_id") == focus["session_id"]
            or proposal.get("binding", {}).get("session_id") != proposal.get("origin_session_id")
            or proposal.get("binding", {}).get("child_id") != focus["child_id"]
            or proposal.get("binding", {}).get("mistake_id") != focus["mistake_id"]):
        raise InvalidAgentPlanError("HISTORICAL_SCORED_CONTROL_CHANGED")
    row = c.execute("SELECT * FROM mistake_case WHERE mistake_id=? AND child_id=?",
                    (focus["mistake_id"], focus["child_id"])).fetchone()
    if not row or _case_snapshot(row) != proposal["case"]:
        raise InvalidAgentPlanError("HISTORICAL_SCORED_SOURCE_CHANGED")
    origin = {"session_id": proposal["origin_session_id"], "child_id": focus["child_id"],
              "mistake_id": focus["mistake_id"]}
    try:
        validate_binding(c, origin, proposal["binding"])
    except InvalidAgentPlanError as error:
        if not (allow_withdrawn and str(error) == "SCORED_ANSWER_ALREADY_WITHDRAWN"):
            raise
    if require_offer:
        event = c.execute("""SELECT result_json FROM mistake_dialogue_event
            WHERE client_action_id=? AND session_id=? AND child_id=?""",
            (proposal["offer_action_id"], focus["session_id"], focus["child_id"])).fetchone()
        try:
            recorded = json.loads(event[0])["state"]["historical_scored_control"] if event else None
        except (TypeError, ValueError, KeyError):
            recorded = None
        if recorded != proposal:
            raise InvalidAgentPlanError("HISTORICAL_SCORED_OFFER_CHANGED")


def validate_operation(c, focus: dict, operation: dict, *, allow_withdrawn=False) -> dict:
    proposal = operation.get("historical_scored_control")
    _valid_proposal(c, focus, proposal, allow_withdrawn=allow_withdrawn)
    binding = proposal["binding"]
    if operation.get("action_id") == proposal["offer_action_id"]:
        raise InvalidAgentPlanError("HISTORICAL_SCORED_CONFIRMATION_MISSING")
    if proposal["control"] == "withdraw":
        if operation.get("kind") != "scored_withdrawal" or operation.get("withdrawal_of") != binding:
            raise InvalidAgentPlanError("HISTORICAL_SCORED_WITHDRAWAL_CHANGED")
    elif (operation.get("correction_of") != binding
          or operation.get("answer") != proposal["replacement_answer"]
          or operation.get("kind") != binding["kind"]):
        raise InvalidAgentPlanError("HISTORICAL_SCORED_REPLACEMENT_CHANGED")
    return proposal


def handle_pending(service, focus: dict, text: str, action_id: str) -> dict | None:
    candidates = focus["state"].get("historical_scored_candidates")
    if candidates:
        compact = re.sub(r"[，。！？、\s]", "", text)
        if re.search(r"(?:取消|先不|不要)(?:了)?$", compact):
            focus["state"].pop("historical_scored_candidates", None)
            focus["state"].pop("historical_scored_request", None)
            focus["phase"] = "paused"
            return service._save(focus, "已取消，没有撤回或更改任何历史回答。", action_id, text)
        selected = re.fullmatch(r"(?:是|选|我说的是)?第?([1-5一二两三四五])道(?:题)?", compact)
        numbers = {"1": 1, "一": 1, "2": 2, "二": 2, "两": 2, "3": 3, "三": 3,
                   "4": 4, "四": 4, "5": 5, "五": 5}
        index = numbers.get(selected[1]) if selected else None
        if not index or index > len(candidates):
            return service._save(focus, _candidate_list_readback(focus["state"]), action_id, text)
        parsed = focus["state"]["historical_scored_request"]
        proposal = _proposal(candidates[index - 1], parsed,
                             parsed["request_utterance"], action_id)
        case = candidates[index - 1]["case"]
        focus["mistake_id"] = case["mistake_id"]
        focus["state"] = {"question_text": case["prompt_summary"], "subject": case["subject"],
                          "submitted_answer": case["submitted_answer"],
                          "historical_scored_control": proposal}
        return service._save(focus, _readback(proposal), action_id, text)
    proposal = focus["state"].get("historical_scored_control")
    if not proposal:
        return None
    compact = re.sub(r"[，。！？、\s]", "", text)
    if re.search(r"(?:取消|不是这道|不对|先不|不要)(?:了|这道)?$", compact):
        focus["state"].pop("historical_scored_control", None)
        focus["phase"] = "paused"
        return service._save(focus, "已取消，没有撤回或更改任何历史回答。", action_id, text)
    if compact not in {"确认", "对", "是", "是的", "对的", "就是这道", "对就是这道",
                       "确认撤回", "确认更正", "对就是这道确认撤回"}:
        return service._save(focus, _readback(proposal), action_id, text)
    with service.database.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            _valid_proposal(c, focus, proposal)
        except InvalidAgentPlanError as error:
            raise InvalidMistakeCaptureCandidateError(
                "历史题目、评分或来源已变化，刚才的确认没有执行。请重新说明要核对的题目。") from error
        binding = proposal["binding"]
        if proposal["control"] == "withdraw":
            operation = {"kind": "scored_withdrawal", "action_id": action_id,
                "utterance": text, "withdrawal_of": binding,
                "historical_scored_control": proposal}
        else:
            operation = {"kind": binding["kind"], "action_id": action_id,
                "utterance": text, "answer": proposal["replacement_answer"],
                "hint_level": binding["hint_level"], "correction_of": binding,
                "historical_scored_control": proposal}
            if binding["kind"] == "mastery_answer":
                operation.update(stage=binding["stage"], item_id=binding["diagnostic_item_id"])
            else:
                operation.update(probe_event_id=binding["probe_event_id"],
                                 supersedes_event_id=binding["answer_event_id"])
        focus["state"]["pending_operation"] = operation
        focus["state"].pop("historical_scored_control", None)
        service._write_focus(c, focus)
        c.commit()
    return service._resume_operation(focus, text, action_id)
