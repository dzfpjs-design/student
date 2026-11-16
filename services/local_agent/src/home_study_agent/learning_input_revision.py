"""Local, read-back-confirmed revisions of homework and classroom statements."""
from datetime import datetime, timezone
import hashlib
import json
import re
from uuid import uuid4

from .agent_continuation import ContinuationStore


LOCAL = {"local_only": True, "cloud_transmission_allowed": False}
CAPS = {"learning_inputs.propose_revision", "learning_inputs.select_target",
        "learning_inputs.confirm_revision", "learning_inputs.cancel_revision"}
FIELDS = {"homework": {"subject", "title", "range_text", "expected_minutes", "special_requirements"},
          "recall": {"subject", "summary"}}
LABELS = {"subject": "学科", "title": "内容", "range_text": "范围", "expected_minutes": "预计分钟数",
          "special_requirements": "要求", "summary": "课堂内容", "status": "记录状态", "confirmation_status": "确认状态"}


class InputRevisionError(ValueError):
    pass


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _version(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _compact(value):
    return "".join(str(value).split()).strip("，。！？,.!?")


def duration_minutes(quote):
    if not isinstance(quote, str):
        return None
    text = re.sub(r"(?:分钟|分)$", "", _compact(quote))
    if re.fullmatch(r"[0-9]+", text):
        value = int(text)
    elif re.fullmatch(r"(?:[一二两三四五六七八九]百(?:零[一二两三四五六七八九]|[一二两三四五六七八九]十[一二两三四五六七八九]?)?|[一二两三四五六七八九]?十[一二两三四五六七八九]?|[一二两三四五六七八九])", text):
        digits = dict(zip("一二两三四五六七八九", (1, 2, 2, 3, 4, 5, 6, 7, 8, 9)))
        value, digit = 0, 0
        for char in text:
            if char in {"十", "百"}:
                value += (digit or 1) * (10 if char == "十" else 100)
                digit = 0
            else:
                digit = digits.get(char, 0)
        value += digit
    else:
        return None
    return value if 1 <= value <= 600 else None


def validate_arguments(capability, arguments):
    allowed = ({"kind", "operation", "target_quote", "request_quote", "changes", "change_quotes"}
               if capability == "learning_inputs.propose_revision" else {"selection_quote"}
               if capability == "learning_inputs.select_target" else {"confirmation_quote"})
    if capability not in CAPS or not isinstance(arguments, dict) or set(arguments) - allowed:
        raise InputRevisionError("INPUT_REVISION_ARGUMENTS_INVALID")


class LearningInputRevisionService:
    def __init__(self, database):
        self.database = database

    def initialize(self):
        with self.database.connect() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS learning_input_arrival(
                arrival_id TEXT PRIMARY KEY,child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
                client_action_id TEXT NOT NULL UNIQUE,utterance TEXT NOT NULL,snapshot_json TEXT NOT NULL,
                pending_json TEXT,created_at TEXT NOT NULL)""")
            c.execute("""CREATE TABLE IF NOT EXISTS learning_input_proposal(
                request_id TEXT PRIMARY KEY REFERENCES learning_input_arrival(arrival_id) ON DELETE CASCADE,
                child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
                state TEXT NOT NULL,proposal_json TEXT NOT NULL,readback_version TEXT NOT NULL,
                offered_by_turn_id TEXT NOT NULL,response_text TEXT NOT NULL)""")
            c.execute("""CREATE TABLE IF NOT EXISTS learning_input_revision_event(
                event_id TEXT PRIMARY KEY,child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
                target_kind TEXT NOT NULL,target_id TEXT NOT NULL,operation TEXT NOT NULL,
                before_json TEXT NOT NULL,after_json TEXT NOT NULL,request_id TEXT NOT NULL,
                confirmation_arrival_id TEXT NOT NULL,undo_event_id TEXT,impact_json TEXT NOT NULL,
                created_at TEXT NOT NULL,client_action_id TEXT NOT NULL UNIQUE)""")
            c.execute("""CREATE TABLE IF NOT EXISTS learning_input_command(
                turn_id TEXT PRIMARY KEY REFERENCES agent_turn(agent_turn_id) ON DELETE CASCADE,
                child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                result_json TEXT NOT NULL)""")
            c.commit()

    @staticmethod
    def _session(c, sid):
        row = c.execute("""SELECT ds.*,cp.status AS child_status FROM daily_session ds
            JOIN child_profile cp USING(child_id) WHERE ds.session_id=?""", (sid,)).fetchone()
        if not row or row["status"] != "active" or row["child_status"] != "active" or row["state"] in {"completed", "evolving"}:
            raise InputRevisionError("INPUT_REVISION_SESSION_INACTIVE")
        return row

    def _item(self, c, child_id, sid, kind, target):
        if kind == "homework":
            row = c.execute("SELECT * FROM homework_item WHERE homework_id=? AND child_id=? AND session_id=?", (target, child_id, sid)).fetchone()
        elif kind == "recall":
            row = c.execute("""SELECT r.* FROM classroom_recall r JOIN daily_session s USING(session_id)
                WHERE r.recall_id=? AND r.session_id=? AND s.child_id=?""", (target, sid, child_id)).fetchone()
        else:
            raise InputRevisionError("INPUT_REVISION_KIND_INVALID")
        if not row:
            return None
        record = dict(row)
        plans = [dict(r) for r in c.execute("""SELECT pi.plan_item_id,pi.plan_id,pi.status,pi.started_at,pi.completed_at,
            pi.subject,pi.title,pi.task_type,pi.expected_minutes,pi.source_type,pi.source_ref,pi.active_seconds,
            pi.actual_minutes,pi.active_segment_started_at,
            dp.status AS daily_plan_status,dp.total_minutes AS original_plan_minutes,dp.plan_date,dp.version AS daily_plan_version
            FROM plan_item pi JOIN daily_plan dp USING(plan_id)
            WHERE dp.child_id=? AND (pi.homework_id=? OR (pi.source_type='school_homework' AND pi.source_ref=?))
            ORDER BY pi.plan_item_id""", (child_id, target, target))] if kind == "homework" else []
        event = c.execute("SELECT event_id FROM learning_input_revision_event WHERE child_id=? AND target_kind=? AND target_id=? ORDER BY rowid DESC LIMIT 1",
                          (child_id, kind, target)).fetchone()
        return {"kind": kind, "target_id": target, "record": record, "plans": plans,
                "last_event_id": event[0] if event else None,
                "version": _version([record, plans, event[0] if event else None])}

    def pending(self, session_id, *, connection=None):
        if connection is None:
            with self.database.connect() as c:
                return self.pending(session_id, connection=c)
        row = connection.execute("""SELECT * FROM learning_input_proposal WHERE session_id=?
            AND state IN ('clarification_required','awaiting_confirmation') ORDER BY rowid DESC LIMIT 1""", (session_id,)).fetchone()
        return {**dict(row), **LOCAL} if row else None

    def capture_arrival(self, session_id, client_action_id, utterance, *, connection=None):
        if connection is None:
            with self.database.connect() as c:
                c.execute("BEGIN IMMEDIATE")
                result = self.capture_arrival(session_id, client_action_id, utterance, connection=c)
                c.commit()
                return result
        c = connection
        session = self._session(c, session_id)
        old = c.execute("SELECT * FROM learning_input_arrival WHERE client_action_id=?", (client_action_id,)).fetchone()
        if old:
            if old["session_id"] != session_id or old["child_id"] != session["child_id"] or old["utterance"] != utterance:
                raise InputRevisionError("INPUT_REVISION_ARRIVAL_CONFLICT")
            return {"arrival_id": old["arrival_id"], "pending": json.loads(old["pending_json"] or "null"), **LOCAL}
        items = []
        from .paper_learning_selection import arrival_binding
        paper_binding=arrival_binding(c,session['child_id'],session_id)
        if paper_binding is not None:
            items.append({'kind':'paper_learning_selection_arrival','binding':paper_binding})
        for kind, table, id_column in (("homework", "homework_item", "homework_id"), ("recall", "classroom_recall", "recall_id")):
            for row in c.execute(f"SELECT {id_column} FROM {table} WHERE session_id=? ORDER BY created_at,rowid", (session_id,)):
                item = self._item(c, session["child_id"], session_id, kind, row[0])
                if item:
                    items.append(item)
        # A goal task is not a homework statement. Freeze its identity at arrival,
        # so later activity requests cannot pause whichever task happens to be current.
        captured = {plan["plan_item_id"] for item in items for plan in item.get("plans", [])}
        active_tasks = [dict(row) for row in c.execute("""SELECT pi.plan_item_id,pi.plan_id,
            pi.status,pi.started_at,pi.completed_at,pi.subject,pi.title,pi.task_type,
            pi.expected_minutes,pi.source_type,pi.source_ref,pi.active_seconds,
            pi.actual_minutes,pi.active_segment_started_at,dp.status AS daily_plan_status,
            dp.total_minutes AS original_plan_minutes,dp.plan_date,dp.version AS daily_plan_version
            FROM plan_item pi JOIN daily_plan dp USING(plan_id)
            WHERE dp.child_id=? AND dp.session_id=? AND dp.status='active'
              AND pi.status='active' AND pi.source_type!='planned_break'
            ORDER BY pi.plan_item_id""", (session["child_id"], session_id))
            if row["plan_item_id"] not in captured]
        if active_tasks:
            items.append({"kind": "task_execution_arrival", "plans": active_tasks, **LOCAL})
        pending = self.pending(session_id, connection=c)
        prior = c.execute("""SELECT t.agent_turn_id,t.tool_status,t.response_text,p.status AS plan_status,s.status AS step_status
            FROM agent_turn t LEFT JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id
            LEFT JOIN agent_step s ON s.plan_id=p.plan_id AND s.step_index=0
            WHERE t.session_id=? ORDER BY t.rowid DESC LIMIT 1""", (session_id,)).fetchone()
        binding = {key: pending[key] for key in ("request_id", "readback_version", "state")} if (
            pending and prior and prior["agent_turn_id"] == pending["offered_by_turn_id"]
            and prior["tool_status"] == "completed" and prior["response_text"] == pending["response_text"]
            and prior["plan_status"] != "cancelled" and prior["step_status"] != "cancelled") else None
        aid = str(uuid4())
        c.execute("INSERT INTO learning_input_arrival VALUES (?,?,?,?,?,?,?,?)",
            (aid, session["child_id"], session_id, client_action_id, utterance, _json(items), _json(binding), datetime.now(timezone.utc).isoformat()))
        return {"arrival_id": aid, "pending": binding, **LOCAL}

    @staticmethod
    def _quote(utterance, quote):
        if not isinstance(quote, str) or not quote.strip() or quote not in utterance:
            raise InputRevisionError("INPUT_REVISION_QUOTE_NOT_GROUNDED")
        return quote

    @staticmethod
    def confirmation_choice(utterance):
        text = _compact(utterance)
        text = re.sub(r"[，,。！!、]", "", text)
        if text in {"确认", "对", "对的", "是", "是的", "没错", "好", "好的", "可以", "没问题", "嗯", "嗯嗯", "确认修改", "确认删除", "确认撤销"} or re.fullmatch(
                r"(?:(?:好|好的|可以|没问题|对|对的|是的|确认)(?:那)?)?(?:就)?(?:按你(?:刚才)?说的|这样)(?:改|修改|删除|撤销|处理)(?:吧|就行|就好)?", text):
            return "confirm"
        if text in {"取消", "取消这次修改", "不改了", "先不改了", "先别改", "不要改", "别改了", "不用改", "算了", "算了不改了", "不是", "不对"}:
            return "cancel"
        return None

    @staticmethod
    def _matches(items, quote):
        text = _compact(quote)
        return [item for item in items if text and any(text in _compact(str(item["record"].get(field) or ""))
            for field in ("subject", "title", "summary", "range_text"))]

    def execute(self, turn_id):
        with self.database.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            turn = c.execute("""SELECT t.*,p.plan_id,p.context_manifest_json,s.capability_name,s.capability_version,s.arguments_json,
                s.status AS step_status,p.status AS plan_status,p.child_id AS plan_child,p.session_id AS plan_session
                FROM agent_turn t JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id JOIN agent_step s USING(plan_id)
                WHERE t.agent_turn_id=? AND s.step_index=0""", (turn_id,)).fetchone()
            if not turn or turn["capability_name"] not in CAPS or turn["capability_version"] != "1":
                raise InputRevisionError("INPUT_REVISION_COMMAND_INVALID")
            old = c.execute("SELECT result_json FROM learning_input_command WHERE turn_id=? AND child_id=?", (turn_id, turn["child_id"])).fetchone()
            if old:
                return self.replay_result(c, turn_id)
            self._session(c, turn["session_id"])
            if (turn["step_status"] != "running" or turn["plan_status"] != "running" or turn["tool_status"] != "pending"
                    or turn["plan_child"] != turn["child_id"] or turn["plan_session"] != turn["session_id"]
                    or not ContinuationStore.execution_allowed(c, turn_id, running=True)):
                raise InputRevisionError("INPUT_REVISION_AUTHORIZATION_CHANGED")
            manifest = json.loads(turn["context_manifest_json"])
            if manifest.get('voice_control_binding'):
                from .voice_control_intent import VoiceControlIntent
                VoiceControlIntent.inspect_execution(c, turn_id)
            arrival = c.execute("SELECT * FROM learning_input_arrival WHERE arrival_id=? AND child_id=? AND session_id=? AND client_action_id=?",
                (manifest.get("learning_input_arrival_id"), turn["child_id"], turn["session_id"], turn["client_action_id"])).fetchone()
            latest = c.execute("SELECT arrival_id FROM learning_input_arrival WHERE session_id=? ORDER BY rowid DESC LIMIT 1", (turn["session_id"],)).fetchone()
            if not arrival or latest[0] != arrival["arrival_id"]:
                raise InputRevisionError("INPUT_REVISION_ARRIVAL_SUPERSEDED")
            args = json.loads(turn["arguments_json"])
            validate_arguments(turn["capability_name"], args)
            if turn["transcript"] != arrival["utterance"] or turn["intent"] == "runtime_continuation":
                raise InputRevisionError("INPUT_REVISION_UTTERANCE_CHANGED")
            if turn["capability_name"] == "learning_inputs.propose_revision":
                result = self._propose(c, arrival, args, turn_id)
            elif (manifest.get('voice_control_binding') or {}).get('control') == 'learning_inputs.resume_draft':
                if turn['capability_name'] != 'learning_inputs.select_target' or args != {'selection_quote': arrival['utterance']}:
                    raise InputRevisionError('INPUT_RETURN_COMMAND_INVALID')
                from .learning_input_return import reoffer
                result = reoffer(self, c, arrival, turn_id, manifest['voice_control_binding']['targets'])
            else:
                result = self._respond(c, arrival, args, turn_id, turn["capability_name"])
            result = {**result, **LOCAL}
            c.execute("INSERT INTO learning_input_command VALUES (?,?,?)", (turn_id, turn["child_id"], _json(result)))
            c.commit()
            return result

    def replay_result(self, c, turn_id):
        row = c.execute("""SELECT t.*,p.context_manifest_json,p.child_id AS plan_child,p.session_id AS plan_session,
            p.status AS plan_status,s.status AS step_status,r.result_json,ds.status AS session_status,ds.child_id AS session_child
            FROM learning_input_command r JOIN agent_turn t ON t.agent_turn_id=r.turn_id AND t.child_id=r.child_id
            JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id JOIN agent_step s USING(plan_id)
            JOIN daily_session ds ON ds.session_id=t.session_id WHERE t.agent_turn_id=? AND s.step_index=0""", (turn_id,)).fetchone()
        if not row:
            return None
        aid = json.loads(row["context_manifest_json"]).get("learning_input_arrival_id")
        arrival = c.execute("SELECT * FROM learning_input_arrival WHERE arrival_id=? AND child_id=? AND session_id=? AND client_action_id=?",
                            (aid, row["child_id"], row["session_id"], row["client_action_id"])).fetchone()
        if (not arrival or arrival["utterance"] != row["transcript"] or row["child_id"] != row["plan_child"]
                or row["child_id"] != row["session_child"] or row["session_id"] != row["plan_session"]):
            raise InputRevisionError("INPUT_REVISION_REPLAY_BINDING_CHANGED")
        result = json.loads(row["result_json"])
        if result["status"] not in {"awaiting_confirmation", "clarification_required"}:
            return result
        proposal = c.execute("SELECT * FROM learning_input_proposal WHERE request_id=? AND child_id=? AND session_id=?",
                             (result["request_id"], row["child_id"], row["session_id"])).fetchone()
        latest = c.execute("SELECT arrival_id FROM learning_input_arrival WHERE session_id=? ORDER BY rowid DESC LIMIT 1", (row["session_id"],)).fetchone()
        current = bool(proposal and proposal["state"] == result["status"]
                       and proposal["readback_version"] == result["readback_version"]
                       and proposal["offered_by_turn_id"] == turn_id and latest and latest[0] == aid
                       and row["session_status"] == "active" and row["plan_status"] != "cancelled" and row["step_status"] != "cancelled")
        if current and json.loads(proposal['proposal_json']).get('intake'):
            from .learning_input_intake import valid
            current = valid(self, c, row['child_id'], row['session_id'], json.loads(proposal['proposal_json']))
        elif current and result["status"] == "awaiting_confirmation":
            saved = json.loads(proposal["proposal_json"])
            if saved['operation'] == 'add':
                from .learning_input_omission import current as addition_current
                current = addition_current(c, row['child_id'], row['session_id'], saved)
            else:
                target = saved['target']
                item = self._item(c, row["child_id"], row["session_id"], target["kind"], target["target_id"])
                current = bool(item and item["version"] == target["version"])
        if not current:
            return {"status": "superseded", "reason": "proposal_no_longer_current", "requires_child_reply": False,
                    "response_text": "这次提议已经处理或发生变化，不能再用旧确认执行。需要继续修改时请重新说明。", **LOCAL}
        return result

    def propose(self, turn_id):
        return self.execute(turn_id)

    def select_target(self, turn_id):
        return self.execute(turn_id)

    def confirm(self, turn_id):
        return self.execute(turn_id)

    def cancel(self, turn_id):
        return self.execute(turn_id)

    def _propose(self, c, arrival, args, turn_id):
        kind, operation = args.get("kind"), args.get("operation")
        if operation == 'add':
            from .learning_input_omission import propose
            return propose(c, arrival, args, turn_id)
        if kind not in FIELDS or operation not in {"update", "delete", "undo"}:
            raise InputRevisionError("INPUT_REVISION_OPERATION_INVALID")
        self._quote(arrival["utterance"], args.get("request_quote"))
        changes, quotes = args.get("changes", {}), args.get("change_quotes", {})
        if not isinstance(changes, dict) or not isinstance(quotes, dict) or set(changes) - FIELDS[kind]:
            raise InputRevisionError("INPUT_REVISION_FIELDS_INVALID")
        if operation == "update" and not changes or operation != "update" and changes:
            raise InputRevisionError("INPUT_REVISION_CHANGES_REQUIRED")
        for field, value in changes.items():
            quote = self._quote(arrival["utterance"], quotes.get(field))
            if field == "expected_minutes":
                if type(value) is not int or duration_minutes(quote) != value:
                    raise InputRevisionError("INPUT_REVISION_DURATION_NOT_GROUNDED")
            elif not isinstance(value, str) or not value.strip() or len(value) > 1000 or value != quote:
                raise InputRevisionError("INPUT_REVISION_VALUE_NOT_GROUNDED")
        items = [i for i in json.loads(arrival["snapshot_json"]) if i["kind"] == kind]
        target_quote = args.get("target_quote", "")
        self._quote(arrival["utterance"], target_quote)
        matches = self._matches(items, target_quote)
        if operation == "undo" and not matches and _compact(target_quote) in {"刚才", "刚才的修改", "上次修改", "上一条修改"}:
            event_ids = {i["last_event_id"] for i in items if i["last_event_id"]}
            latest = c.execute("SELECT event_id,target_id FROM learning_input_revision_event WHERE child_id=? AND session_id=? ORDER BY rowid DESC LIMIT 1",
                (arrival["child_id"], arrival["session_id"])).fetchone()
            matches = [i for i in items if latest and latest[0] in event_ids and i["target_id"] == latest[1]]
        proposal = {"kind": kind, "operation": operation, "changes": changes, "request_quote": args["request_quote"],
                    "candidates": matches or items, "target": matches[0] if len(matches) == 1 else None}
        from .learning_input_intake import begin, needs_ownership
        if kind == 'homework' and operation == 'update' and needs_ownership(arrival['utterance']):
            begin(proposal, arrival, args)
        c.execute("UPDATE learning_input_proposal SET state='cancelled' WHERE session_id=? AND state IN ('clarification_required','awaiting_confirmation')", (arrival["session_id"],))
        return self._offer(c, arrival, proposal, turn_id)

    def _offer(self, c, arrival, proposal, turn_id):
        if proposal.get('intake') and proposal.get('target'):
            from .learning_input_intake import missing, offer
            if missing(proposal):
                return offer(c, arrival, proposal, turn_id)
        target = proposal["target"]
        if target:
            before = target["record"]
            if proposal["operation"] == "undo":
                event = c.execute("SELECT * FROM learning_input_revision_event WHERE event_id=? AND child_id=? AND target_id=?",
                    (target["last_event_id"], arrival["child_id"], target["target_id"])).fetchone()
                if not event or json.loads(event["after_json"]) != before:
                    return {"status": "blocked", "reason": "undo_basis_unavailable", "response_text": "这项没有可直接撤销的当前修订，需要重新说明要改什么。"}
                old = json.loads(event["before_json"])
                fields = FIELDS[proposal["kind"]] | ({"status", "confirmation_status"} if proposal["kind"] == "homework" else {"confirmation_status", "record_kind", "candidate_nodes_json", "textbook_mapping_json", "evidence_sources_json", "source_refs_json", "confidence"})
                proposal["changes"] = ({'status': 'cancelled'} if event['operation'] == 'add' else
                                       {k: old[k] for k in fields if old.get(k) != before.get(k)})
                proposal["undo_event_id"] = event["event_id"]
                if not proposal["changes"]:
                    return {"status": "blocked", "reason": "undo_has_no_change", "response_text": "这条修订没有可撤销的内容变化，原记录保持不变。"}
            label = before["subject"] + "：" + before.get("title", before.get("summary", ""))
            details = "；".join(f"{LABELS.get(k, k)}从“{before.get(k) or '空'}”改为“{v}”" for k, v in proposal["changes"].items() if k in LABELS)
            if proposal["operation"] == "delete":
                details = "撤回这条当前记录，不删除原始历史"
            impact = "；已有执行记录和学习证据保持不变，受影响的未开始计划需要重排" if target["plans"] else ""
            response = f"确认处理{label}：{details}{impact}。这样改对吗？"
            state = "awaiting_confirmation"
        else:
            labels = [i["record"]["subject"] + "：" + i["record"].get("title", i["record"].get("summary", "")) for i in proposal["candidates"]]
            response = "你指的是哪一项？" + "；".join(labels) if labels else "没有找到这条记录。请说清学科和原来的内容，我还没有修改。"
            state = "clarification_required"
        version = _version([proposal, turn_id, response])
        c.execute("""INSERT INTO learning_input_proposal VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(request_id) DO UPDATE SET state=excluded.state,proposal_json=excluded.proposal_json,
            readback_version=excluded.readback_version,offered_by_turn_id=excluded.offered_by_turn_id,response_text=excluded.response_text""",
            (arrival["arrival_id"], arrival["child_id"], arrival["session_id"], state, _json(proposal), version, turn_id, response))
        return {"status": state, "request_id": arrival["arrival_id"], "readback_version": version,
                "target_id": target["target_id"] if target else None, "response_text": response, "requires_child_reply": True,
                **({'input_provenance': proposal['intake']} if proposal.get('intake') else {})}

    def _respond(self, c, arrival, args, turn_id, capability):
        binding = json.loads(arrival["pending_json"] or "null")
        pending = c.execute("SELECT * FROM learning_input_proposal WHERE request_id=? AND child_id=? AND session_id=?",
            (binding["request_id"], arrival["child_id"], arrival["session_id"])).fetchone() if binding else None
        if not pending or pending["readback_version"] != binding["readback_version"] or pending["state"] != binding["state"]:
            raise InputRevisionError("INPUT_REVISION_NO_CURRENT_READBACK")
        offered = c.execute("""SELECT 1 FROM agent_turn t JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id
            JOIN agent_step s USING(plan_id) WHERE t.agent_turn_id=? AND t.child_id=? AND t.session_id=?
              AND t.tool_status='completed' AND t.response_text=? AND p.status!='cancelled'
              AND s.step_index=0 AND s.status!='cancelled'""",
            (pending["offered_by_turn_id"], arrival["child_id"], arrival["session_id"], pending["response_text"])).fetchone()
        if not offered:
            raise InputRevisionError("INPUT_REVISION_NO_CURRENT_READBACK")
        proposal = json.loads(pending["proposal_json"])
        if capability == "learning_inputs.select_target":
            if pending["state"] != "clarification_required":
                raise InputRevisionError("INPUT_REVISION_NOT_CHOOSING")
            quote = self._quote(arrival["utterance"], args.get("selection_quote"))
            if proposal.get('intake') and proposal.get('target'):
                if quote != arrival['utterance']:
                    raise InputRevisionError('INPUT_REVISION_SLOT_QUOTE_CHANGED')
                from .learning_input_intake import respond
                return respond(self, c, arrival, pending, proposal, turn_id)
            matches = self._matches(proposal["candidates"], quote)
            proposal["target"] = matches[0] if len(matches) == 1 else None
            original = c.execute("SELECT * FROM learning_input_arrival WHERE arrival_id=?", (pending["request_id"],)).fetchone()
            return self._offer(c, original, proposal, turn_id)
        quote = self._quote(arrival["utterance"], args.get("confirmation_quote"))
        choice = self.confirmation_choice(arrival["utterance"])
        if quote != arrival["utterance"] or choice != ("cancel" if capability == "learning_inputs.cancel_revision" else "confirm"):
            raise InputRevisionError("INPUT_REVISION_CONFIRMATION_NOT_EXPLICIT")
        if choice == "cancel":
            c.execute("UPDATE learning_input_proposal SET state='cancelled' WHERE request_id=?", (pending["request_id"],))
            return {"status": "cancelled", "response_text": "已取消这次提议，原记录没有修改。"}
        if proposal.get('intake'):
            from .learning_input_intake import missing, valid
            if missing(proposal) or not valid(self, c, arrival['child_id'], arrival['session_id'], proposal):
                c.execute("UPDATE learning_input_proposal SET state='conflict' WHERE request_id=?", (pending['request_id'],))
                return {'status': 'conflict', 'response_text': '字段、来源或计划已经变化，不能使用刚才的确认。请重新核对作业。'}
        if proposal['operation'] == 'add':
            from .learning_input_omission import confirm
            return confirm(self, c, arrival, pending, proposal, turn_id)
        if pending["state"] != "awaiting_confirmation" or not proposal["target"]:
            raise InputRevisionError("INPUT_REVISION_TARGET_UNRESOLVED")
        target = proposal["target"]
        current = self._item(c, arrival["child_id"], arrival["session_id"], proposal["kind"], target["target_id"])
        if not current or current["version"] != target["version"]:
            c.execute("UPDATE learning_input_proposal SET state='conflict' WHERE request_id=?", (pending["request_id"],))
            return {"status": "conflict", "reason": "source_or_plan_changed", "response_text": "这条记录或执行进度已经变化，刚才的确认不能覆盖新内容。请重新说明要改哪一项。"}
        before, changes = current["record"], dict(proposal["changes"])
        kind, operation = proposal["kind"], proposal["operation"]
        if operation == "delete":
            changes = {"status": "cancelled"} if kind == "homework" else {"confirmation_status": "rejected"}
        elif kind == "recall" and operation == "update" and "summary" in changes:
            unknown = any(term in changes["summary"] for term in ("不知道", "想不起来", "不记得", "记不清", "不确定"))
            changes.update(confirmation_status="unknown" if unknown else "child_confirmed", record_kind="child_statement", candidate_nodes_json="[]",
                           textbook_mapping_json=None, evidence_sources_json="[]", source_refs_json="[]", confidence=0.0 if unknown else 1.0)
        elif kind == "recall" and operation == "update" and "subject" in changes:
            changes.update(candidate_nodes_json="[]", textbook_mapping_json=None)
        table, id_column = ("homework_item", "homework_id") if kind == "homework" else ("classroom_recall", "recall_id")
        if all(before.get(key) == value for key, value in changes.items()):
            c.execute("UPDATE learning_input_proposal SET state='unchanged' WHERE request_id=?", (pending["request_id"],))
            return {"status": "unchanged", "response_text": "这项记录已经是你确认的内容，没有重复修改，原计划保持不变。"}
        c.execute(f"UPDATE {table} SET " + ",".join(f"{k}=?" for k in changes) + f" WHERE {id_column}=?",
                  (*changes.values(), target["target_id"]))
        after = {**before, **changes}
        cancelled = []
        now = datetime.now(timezone.utc).isoformat()
        for item in current["plans"]:
            if (item["status"] in {"pending", "rescheduled"} and item["started_at"] is None
                    and item["completed_at"] is None and not item["active_seconds"]
                    and item["actual_minutes"] in (None, 0) and not item["active_segment_started_at"]):
                c.execute("UPDATE plan_item SET status='cancelled' WHERE plan_item_id=?", (item["plan_item_id"],))
                c.execute("""INSERT INTO plan_item_event(event_id,plan_item_id,event_type,occurred_at,payload_json,client_action_id)
                    VALUES (?,?,'input_revision_invalidated',?,?,?)""", (str(uuid4()), item["plan_item_id"], now,
                    _json({"request_id": pending["request_id"]}), f"input-revision:{turn_id}:{item['plan_item_id']}"))
                cancelled.append(item["plan_item_id"])
        impact = {"cancelled_unstarted_plan_ids": cancelled, "preserved_execution_plan_ids": [p["plan_item_id"] for p in current["plans"] if p["plan_item_id"] not in cancelled],
                  "learning_evidence_changed": False, "requires_replan": bool(current["plans"])}
        if proposal.get('intake'):
            impact['input_provenance'] = proposal['intake']
        eid = str(uuid4())
        if impact["requires_replan"]:
            revised = self._item(c, arrival["child_id"], arrival["session_id"], kind, target["target_id"])
            artifact = c.execute("SELECT * FROM capture_artifact WHERE artifact_id=? AND child_id=? AND session_id=?",
                                 (after.get("source_artifact_id"), arrival["child_id"], arrival["session_id"])).fetchone()
            rule = c.execute("SELECT latest_end_time,max_learning_minutes,task_segment_minutes,break_minutes,pet_min_minutes,version FROM family_rule WHERE child_id=?",
                             (arrival["child_id"],)).fetchone()
            impact["replan_obligation"] = {
                "version": "learning_input_replan_v1", "obligation_id": f"input-revision:{eid}", "revision_event_id": eid,
                "status": "pending", "child_id": arrival["child_id"], "session_id": arrival["session_id"],
                "target_kind": kind, "target_id": target["target_id"], "operation": operation,
                "before_source_version": current["version"], "expected_source_version": _version([after, revised["plans"], eid]), "revised_source": after,
                "original_plan_items": current["plans"], "cancelled_unstarted_plan_ids": cancelled,
                "preserve_plan_item_ids": impact["preserved_execution_plan_ids"],
                "budget_snapshot": dict(rule) if rule else None,
                "planning_context_snapshot": dict(context) if (context := c.execute(
                    "SELECT * FROM session_planning_context WHERE session_id=?", (arrival["session_id"],)).fetchone()) else None,
                "artifact_fingerprint": _version(dict(artifact)) if artifact else None,
                "learning_evidence_changed": False, **LOCAL,
            }
        c.execute("INSERT INTO learning_input_revision_event VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (eid, arrival["child_id"], arrival["session_id"], kind, target["target_id"], operation, _json(before), _json(after),
             pending["request_id"], arrival["arrival_id"], proposal.get("undo_event_id"), _json(impact), now, turn_id))
        c.execute("UPDATE learning_input_proposal SET state='applied' WHERE request_id=?", (pending["request_id"],))
        return {"status": "applied", "event_id": eid, "target_id": target["target_id"], "impact": impact,
                "response_text": "修订已保存，原始历史保留。" + ("受影响的计划需要重新安排，已经执行的记录和学习证据没有改动。" if impact["requires_replan"] else "")}

    def history(self, child_id, kind, target_id):
        with self.database.connect() as c:
            return {"items": [dict(r) for r in c.execute("SELECT * FROM learning_input_revision_event WHERE child_id=? AND target_kind=? AND target_id=? ORDER BY rowid",
                (child_id, kind, target_id))], **LOCAL}

    def inspect_replan_obligation(self, child_id, event_id, *, connection=None):
        if connection is None:
            with self.database.connect() as c:
                c.execute("BEGIN")
                return self.inspect_replan_obligation(child_id, event_id, connection=c)
        if not connection.in_transaction:
            connection.execute("BEGIN")
        row = connection.execute("SELECT * FROM learning_input_revision_event WHERE event_id=? AND child_id=?",
                                 (event_id, child_id)).fetchone()
        if not row:
            raise InputRevisionError("INPUT_REVISION_EVENT_NOT_FOUND")
        obligation = json.loads(row["impact_json"]).get("replan_obligation")
        if not obligation:
            return {"status": "none", **LOCAL}
        source = self._item(connection, child_id, row["session_id"], row["target_kind"], row["target_id"])
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='learning_input_replan_receipt'").fetchone():
            receipt = connection.execute("""SELECT result_json,source_record_json FROM learning_input_replan_receipt
                WHERE event_id=? AND child_id=? AND session_id=?""", (event_id, child_id, row["session_id"])).fetchone()
            if receipt:
                actual = json.loads(receipt["result_json"])
                saved = actual.get("receipt") or {}
                if (saved.get("event_id"), saved.get("child_id"), saved.get("session_id")) != (event_id, child_id, row["session_id"]):
                    raise InputRevisionError("INPUT_REPLAN_RECEIPT_BINDING_INVALID")
                current = bool(source and source["last_event_id"] == event_id
                               and _json(source["record"]) == receipt["source_record_json"])
                original = json.loads(receipt["source_record_json"])
                if current and original.get("source_artifact_id"):
                    artifact = connection.execute("SELECT * FROM capture_artifact WHERE artifact_id=? AND child_id=? AND session_id=?",
                        (original["source_artifact_id"], child_id, row["session_id"])).fetchone()
                    current = bool(artifact and _version(dict(artifact)) == obligation.get("artifact_fingerprint"))
                # The immutable receipt describes the write, not today's task state.
                # Ordinary plan/timer progression must not recreate a pending write.
                return {"status": "recorded", "obligation": obligation, "source_current": current,
                        "receipt": saved, "newitems": actual["newitems"], "unplaced": actual["unplaced"], **LOCAL}
        return {"status": "pending", "obligation": obligation,
                "source_current": bool(source and source["version"] == obligation["expected_source_version"]), **LOCAL}
