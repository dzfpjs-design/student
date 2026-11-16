"""Durable result-triggered work, never a synthetic child utterance."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import hashlib
import unicodedata
from uuid import uuid4

from .database import Database


MAX_CHAIN_STEPS = 4
LEASE_SECONDS = 120
CONTINUATION_TTL_SECONDS = 300

# Answers, confirmations, identity and privacy grants require fresh child input.
RESULT_CONTINUATION_CAPABILITIES = frozenset({
    "conversation.respond", "learning.explain", "learning.request_response",
    "learning.capture_question", "document.capture_question", "document.retry_capture",
    "planning.describe", "planning.generate", "planning.reorder", "task.start", "task.pause",
    "checkin.update_context", "recall.add", "recall.add_batch", "homework.add", "homework.add_batch",
    "session.transition", "diagnostic.start", "diagnostic.replay", "pet_vocabulary.start",
    "device.status", "device.connect", "observation.pause", "observation.resume", "document.monitor_control",
})


def written_item_identities(capability: str, arguments: dict) -> set[tuple[str, str, str]]:
    family = capability.split(".")[0]
    if capability not in {"homework.add", "homework.add_batch", "recall.add", "recall.add_batch"}:
        return set()
    items = arguments.get("items", []) if capability.endswith("_batch") else [arguments]
    def normalized(value):
        text = unicodedata.normalize("NFKC", str(value)).casefold()
        return "".join(c for c in text if not c.isspace() and not unicodedata.category(c).startswith("P"))
    return {(family, normalized(item.get("subject", "")), normalized(item.get("title" if family == "homework" else "summary", "")))
            for item in items if isinstance(item, dict)}


class ContinuationStore:
    def __init__(self, database: Database):
        self.database = database
        with database.connect() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS agent_continuation (
                  parent_turn_id TEXT PRIMARY KEY REFERENCES agent_turn(agent_turn_id) ON DELETE CASCADE,
                  root_turn_id TEXT NOT NULL REFERENCES agent_turn(agent_turn_id) ON DELETE CASCADE,
                  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                  session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
                  objective_id TEXT NOT NULL REFERENCES agent_objective(objective_id) ON DELETE CASCADE,
                  depth INTEGER NOT NULL,
                  status TEXT NOT NULL CHECK(status IN ('pending','planning','completed','blocked','superseded')),
                  lease_token TEXT,
                  lease_until TEXT,
                  next_turn_id TEXT REFERENCES agent_turn(agent_turn_id) ON DELETE SET NULL,
                  execution_admitted_at TEXT,
                  reason TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_continuation_pending
                  ON agent_continuation(session_id,status,created_at);
            """)
            columns = {row[1] for row in c.execute("PRAGMA table_info(agent_continuation)")}
            if "execution_admitted_at" not in columns:
                c.execute("ALTER TABLE agent_continuation ADD COLUMN execution_admitted_at TEXT")
                c.commit()
        self.ensure_source_deletion()

    def ensure_source_deletion(self) -> None:
        with self.database.connect() as c:
            if c.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN ('agent_turn','agent_objective')").fetchone()[0] != 2:
                return
            c.executescript("""
                CREATE TRIGGER IF NOT EXISTS agent_continuation_delete_descendants
                BEFORE DELETE ON agent_turn BEGIN
                  DELETE FROM agent_turn WHERE agent_turn_id!=OLD.agent_turn_id AND agent_turn_id IN (
                    SELECT next_turn_id FROM agent_continuation
                    WHERE root_turn_id=OLD.agent_turn_id OR parent_turn_id=OLD.agent_turn_id
                  );
                END;
                CREATE TRIGGER IF NOT EXISTS agent_continuation_delete_objective
                BEFORE DELETE ON agent_objective BEGIN
                  DELETE FROM agent_turn WHERE intent='runtime_continuation' AND agent_turn_id IN (
                    SELECT agent_turn_id FROM agent_plan WHERE objective_id=OLD.objective_id
                  );
                END;
            """)

    @staticmethod
    def enqueue(connection, turn_id: str) -> None:
        row = connection.execute("""SELECT p.*, t.action_json FROM agent_plan p
            JOIN agent_turn t ON t.agent_turn_id=p.agent_turn_id
            JOIN agent_objective o ON o.objective_id=p.objective_id
            WHERE p.agent_turn_id=? AND p.status='completed' AND t.tool_status='completed'
              AND o.status='active'""", (turn_id,)).fetchone()
        if not row:
            return
        plan, action = json.loads(row["plan_json"]), json.loads(row["action_json"])
        if plan.get("after_execution") != "replan" or plan.get("requires_child_reply"):
            return
        previous = connection.execute("SELECT root_turn_id,depth FROM agent_continuation WHERE next_turn_id=?", (turn_id,)).fetchone()
        root, depth = (previous["root_turn_id"], previous["depth"] + 1) if previous else (turn_id, 1)
        original = connection.execute("SELECT p.plan_json,t.transcript FROM agent_plan p JOIN agent_turn t ON t.agent_turn_id=p.agent_turn_id WHERE p.agent_turn_id=?", (root,)).fetchone()
        scope = json.loads(original["plan_json"]).get("continuation_scope", []) if original else []
        grounded = bool(scope) and all(isinstance(item, dict) and isinstance(item.get("request_quote"), str)
                                       and item["request_quote"] in original["transcript"] for item in scope)
        now = datetime.now(timezone.utc).isoformat()
        status, reason = ("blocked", "CHAIN_BUDGET_REACHED") if depth > MAX_CHAIN_STEPS else ("pending", None)
        if not grounded:
            status, reason = "blocked", "CONTINUATION_SCOPE_MISSING"
        elif ((action.get('local_only') or action.get('cloud_transmission_allowed') is False)
                and not ContinuationStore.document_followup(connection, turn_id)):
            # Persist intent without granting content transmission or execution.
            status, reason = 'blocked', 'LOCAL_CONTINUATION_AUTHORIZATION_REQUIRED'
        connection.execute("""INSERT INTO agent_continuation(parent_turn_id,root_turn_id,child_id,
            session_id,objective_id,depth,status,reason,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(parent_turn_id) DO NOTHING""",
            (turn_id, root, row["child_id"], row["session_id"], row["objective_id"], depth, status, reason, now, now))
        if status == 'blocked' and reason == 'LOCAL_CONTINUATION_AUTHORIZATION_REQUIRED':
            from .document_goal_handoff import enqueue as enqueue_document_goal
            try:
                enqueue_document_goal(connection, turn_id)
            except (ValueError, TypeError, KeyError):
                connection.execute("UPDATE agent_continuation SET reason='DOCUMENT_GOAL_UNAVAILABLE' WHERE parent_turn_id=?", (turn_id,))

    @staticmethod
    def document_followup(c, turn_id):
        from .voice_control_intent import VoiceControlIntent
        row = c.execute('''SELECT t.*,p.plan_json,p.context_manifest_json FROM agent_turn t
            JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id AND p.child_id=t.child_id AND p.session_id=t.session_id
            WHERE t.agent_turn_id=?''', (turn_id,)).fetchone()
        if not row or row['tool_error_code'] == 'SOURCE_DELETED':
            return None
        action, plan = json.loads(row['action_json']), json.loads(row['plan_json'])
        binding = json.loads(row['context_manifest_json']).get('voice_control_binding') or {}
        followup = (binding.get('targets') or {}).get('document_stop_followup')
        quote = VoiceControlIntent.document_stop_question(row['transcript'])
        if (not followup or not quote or any(followup.get(k) != v for k, v in quote.items())
                or action.get('type') != 'control_document_monitor' or action.get('command') != 'pause'
                or action.get('local_only') is not True or action.get('cloud_transmission_allowed') is not False
                or binding.get('control') != 'document.pause'
                or plan.get('continuation_scope') != [{'capability': 'conversation.respond', 'arguments': {},
                                                       'request_quote': quote['request_quote']}]):
            return None
        journal = c.execute('''SELECT * FROM voice_control_intent_request
            WHERE request_key=? AND child_id=? AND session_id=? AND status='completed' ''',
            (binding.get('request_key'), row['child_id'], row['session_id'])).fetchone()
        if (not journal or json.loads(journal['binding_json']) != binding['targets']
                or json.loads(journal['decision_json']).get('origin') != 'explicit_local_document_stop'
                or journal['fingerprint'] != hashlib.sha256(json.dumps(
                    [row['child_id'], row['session_id'], row['transcript']], ensure_ascii=False).encode()).hexdigest()):
            return None
        return followup

    @staticmethod
    def _document_source_valid(c, work, followup):
        from .agent_runtime import AgentRuntimeStore, InvalidAgentPlanError, InvalidDeviceReceiptError
        from .voice_control_intent import VoiceControlIntent
        root = c.execute('''SELECT t.*,s.result_json FROM agent_turn t JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id
            JOIN agent_step s ON s.plan_id=p.plan_id AND s.step_index=0
            WHERE t.agent_turn_id=? AND t.child_id=? AND t.session_id=?
            AND t.tool_status='completed' AND p.status='completed' AND s.status='completed' ''',
            (work['root_turn_id'], work['child_id'], work['session_id'])).fetchone()
        if not root or not VoiceControlIntent.scope_valid(c, work['session_id'], work['child_id'], root['client_action_id'], require_cloud=False):
            return False
        try:
            AgentRuntimeStore.validate_device_receipt(c, work['root_turn_id'], json.loads(root['result_json']))
            action = json.loads(root['action_json'])
            if action.get('voice_input_id'):
                voice = AgentRuntimeStore.resolve_captured_voice(c, session_id=work['session_id'], child_id=work['child_id'],
                    voice_input_id=action['voice_input_id'], transcript=root['transcript'])
                if AgentRuntimeStore.voice_capture_snapshot(voice) != action.get('voice_input_binding'):
                    return False
        except (InvalidAgentPlanError, InvalidDeviceReceiptError, ValueError, TypeError):
            return False
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='deletion_job'").fetchone() and c.execute(
                """SELECT 1 FROM deletion_job WHERE child_id=? AND target_type='child_scope'
                AND scope IN ('learning','knowledge','media','tutor','all_history') AND status!='completed'""", (work['child_id'],)).fetchone():
            return False
        monitor = c.execute('''SELECT status,generation FROM document_monitor_session WHERE monitor_id=? AND child_id=? AND session_id=?''',
            (followup['monitor_id'], work['child_id'], work['session_id'])).fetchone()
        if not monitor or monitor['status'] != 'paused' or monitor['generation'] != followup['paused_generation']:
            return False
        revision = followup.get('revision')
        if revision:
            current = c.execute('''SELECT r.revision_id,r.artifact_id,r.content_sha256,r.created_at
                FROM document_page_revision r JOIN capture_artifact a ON a.artifact_id=r.artifact_id
                AND a.child_id=r.child_id AND a.session_id=r.session_id
                WHERE r.revision_id=? AND r.monitor_id=? AND r.child_id=? AND r.session_id=?
                AND a.artifact_type='document_ocr' AND a.status IN ('completed','quality_warning')''',
                (revision['revision_id'], followup['monitor_id'], work['child_id'], work['session_id'])).fetchone()
            if not current or dict(current) != revision:
                return False
            from .document_monitoring import DocumentMonitoringService
            body = c.execute('SELECT extracted_text FROM capture_artifact WHERE artifact_id=?', (revision['artifact_id'],)).fetchone()[0]
            if body is None or hashlib.sha256(DocumentMonitoringService._normalized_text(body).encode()).hexdigest() != revision['content_sha256']:
                return False
        return True

    @classmethod
    def _neutral_origin(cls, c, work):
        """Read-only followups keep their root objective unchanged, not unbound authority."""
        row = c.execute('''SELECT t.intent,t.client_action_id,t.action_json,p.plan_json,
            s.capability_name,s.arguments_json,s.execution_location,r.transcript,rp.plan_json AS root_plan
            FROM agent_turn t JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id
              AND p.child_id=t.child_id AND p.session_id=t.session_id AND p.objective_id IS NULL
            JOIN agent_step s ON s.plan_id=p.plan_id AND s.step_index=0
            JOIN agent_turn r ON r.agent_turn_id=? AND r.child_id=t.child_id AND r.session_id=t.session_id
              AND r.tool_status='completed'
            JOIN agent_plan rp ON rp.agent_turn_id=r.agent_turn_id AND rp.child_id=r.child_id
              AND rp.session_id=r.session_id AND rp.objective_id=?
            JOIN agent_turn parent ON parent.agent_turn_id=? AND parent.child_id=t.child_id
              AND parent.session_id=t.session_id AND parent.tool_status='completed'
            JOIN agent_plan pp ON pp.agent_turn_id=parent.agent_turn_id AND pp.child_id=t.child_id
              AND pp.session_id=t.session_id AND pp.objective_id=rp.objective_id
            WHERE t.agent_turn_id=? AND t.child_id=? AND t.session_id=?''',
            (work['root_turn_id'], work['objective_id'], work['parent_turn_id'],
             work['next_turn_id'], work['child_id'], work['session_id'])).fetchone()
        if not row or row['intent'] != 'runtime_continuation' or row['client_action_id'] != 'continuation:' + work['parent_turn_id']:
            return False
        if c.execute('''SELECT 1 FROM agent_turn t WHERE t.agent_turn_id IN (?,?,?)
            AND (t.tool_error_code='SOURCE_DELETED' OR EXISTS (SELECT 1 FROM agent_plan p
                JOIN agent_step s USING(plan_id) WHERE p.agent_turn_id=t.agent_turn_id AND s.error_code='SOURCE_DELETED'))''',
            (work['root_turn_id'], work['parent_turn_id'], work['next_turn_id'])).fetchone():
            return False
        locations = {'device.status': 'client_device_bridge', 'planning.describe': 'server'}
        if locations.get(row['capability_name']) != row['execution_location']:
            return False
        try:
            action, plan = json.loads(row['action_json']), json.loads(row['plan_json'])
            root_plan = json.loads(row['root_plan'])
            if (not isinstance(action, dict) or not isinstance(plan, dict)
                    or not isinstance(plan.get('objective'), dict) or not isinstance(root_plan, dict)
                    or not isinstance(root_plan.get('continuation_scope'), list)):
                return False
            if (plan['objective']['mode'] != 'none' or action.get('origin') != 'tool_result'
                    or action.get('root_turn_id') != work['root_turn_id']
                    or action.get('parent_turn_id') != work['parent_turn_id']):
                return False
            return any(isinstance(item, dict) and item.get('capability') == row['capability_name']
                       and item.get('arguments') == json.loads(row['arguments_json'])
                       and isinstance(item.get('request_quote'), str) and item['request_quote']
                       and item['request_quote'] in row['transcript']
                       for item in root_plan['continuation_scope'])
        except (ValueError, TypeError, KeyError):
            return False

    @classmethod
    def _valid(cls, connection, work, prepared=False, running=False, settled=False) -> bool:
        from .document_goal_handoff import owns, execution_valid
        if owns(connection, work['root_turn_id']):
            return execution_valid(connection, work, prepared=prepared, running=running, settled=settled)
        target = work["next_turn_id"] if prepared else work["parent_turn_id"]
        try:
            created = datetime.fromisoformat(work["created_at"].replace("Z", "+00:00"))
            created = created.replace(tzinfo=created.tzinfo or timezone.utc)
            if not 0 <= (datetime.now(timezone.utc) - created).total_seconds() <= CONTINUATION_TTL_SECONDS:
                return False
        except (ValueError, TypeError):
            return False
        local = cls.document_followup(connection, work['root_turn_id'])
        if local and (work['parent_turn_id'] != work['root_turn_id'] or not 1 <= work['depth'] <= min(1, MAX_CHAIN_STEPS)
                      or not cls._document_source_valid(connection, work, local)):
            return False
        if local and prepared and not connection.execute('''SELECT 1 FROM agent_plan p JOIN agent_step s USING(plan_id)
                WHERE p.agent_turn_id=? AND p.child_id=? AND p.session_id=? AND s.step_index=0
                AND s.capability_name='conversation.respond' AND s.execution_location='server' AND s.error_code IS NULL''',
                (target, work['child_id'], work['session_id'])).fetchone():
            return False
        preference = connection.execute("SELECT cloud_model_text_allowed FROM privacy_preference WHERE singleton_id=1").fetchone()
        if preference and not preference[0] and not local:
            return False
        neutral = prepared and not local and cls._neutral_origin(connection, work)
        row = connection.execute("""SELECT s.status,s.state,s.snooze_until,o.status AS objective_status,
            o.last_agent_turn_id,t.action_json,p.status AS plan_status,
            (SELECT agent_turn_id FROM agent_turn WHERE session_id=s.session_id ORDER BY rowid DESC LIMIT 1) AS latest
            FROM daily_session s JOIN agent_objective o ON o.objective_id=? AND o.child_id=s.child_id
            JOIN agent_turn t ON t.agent_turn_id=? AND t.session_id=s.session_id AND t.child_id=s.child_id
            JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id
              AND (p.objective_id=o.objective_id OR (? AND p.objective_id IS NULL))
            WHERE s.session_id=? AND s.child_id=?""",
            (work["objective_id"], target, neutral, work["session_id"], work["child_id"])).fetchone()
        if not row or row["status"] != "active" or row["state"] in {"completed", "evolving"}:
            return False
        if (row["objective_status"] != "active" or row["latest"] != target
                or (not local and row["last_agent_turn_id"] != (work['parent_turn_id'] if neutral else target))
                or row["plan_status"] not in ({"completed", "waiting_child"} if settled and local else
                    {"running"} if running else {"planned", "waiting_child"} if prepared else {"completed"})):
            return False
        if row["snooze_until"]:
            try:
                timestamp = datetime.fromisoformat(row["snooze_until"].replace("Z", "+00:00"))
            except (ValueError, TypeError):
                return False
            if timestamp.replace(tzinfo=timestamp.tzinfo or timezone.utc) > datetime.now(timezone.utc):
                return False
        action = json.loads(row["action_json"])
        root = connection.execute("SELECT action_json FROM agent_turn WHERE agent_turn_id=? AND child_id=? AND session_id=?",
                                  (work["root_turn_id"], work["child_id"], work["session_id"])).fetchone()
        if not root:
            return False
        root_action = json.loads(root[0])
        if local:
            return (not prepared or (action.get('document_stop_followup_root') == work['root_turn_id']
                    and action.get('type') == 'none' and action.get('local_only') is True
                    and action.get('cloud_transmission_allowed') is False))
        return (not action.get("local_only") and action.get("cloud_transmission_allowed") is not False
                and not root_action.get("local_only") and root_action.get("cloud_transmission_allowed") is not False)

    def invalidate(self, session_id: str) -> None:
        with self.database.connect() as c:
            self.invalidate_in(c, session_id)
            c.commit()

    @staticmethod
    def invalidate_in(c, session_id: str) -> None:
        c.execute('''UPDATE agent_objective SET status='cancelled',updated_at=?
            WHERE status='active' AND objective_type='execution' AND session_id=?
            AND objective_id IN (SELECT p.objective_id FROM agent_plan p JOIN agent_turn t USING(agent_turn_id)
                WHERE p.session_id=? AND p.child_id=agent_objective.child_id
                AND json_extract(p.context_manifest_json,'$.document_stop_followup_owned_objective')=1
                AND json_extract(t.action_json,'$.type')='control_document_monitor'
                AND json_extract(t.action_json,'$.command')='pause')''',
            (datetime.now(timezone.utc).isoformat(), session_id, session_id))
        # This local response has no device write in flight. A fresh utterance
        # can cancel its running step; external/device steps retain their policy.
        local_running = [row[0] for row in c.execute('''SELECT p.plan_id FROM agent_continuation q
            JOIN agent_turn t ON t.agent_turn_id=q.next_turn_id
            JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id
            JOIN agent_step s USING(plan_id)
            WHERE q.session_id=? AND q.status='completed' AND p.status='running'
            AND json_extract(t.action_json,'$.document_stop_followup_root')=q.root_turn_id
            AND s.step_index=0 AND s.capability_name='conversation.respond' AND s.execution_location='server' ''',
            (session_id,))]
        for plan_id in local_running:
            c.execute("UPDATE agent_plan SET status='cancelled' WHERE plan_id=? AND status='running'", (plan_id,))
            c.execute("UPDATE agent_step SET status='cancelled' WHERE plan_id=? AND status='running'", (plan_id,))
        c.execute("""UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id IN (
            SELECT next_turn_id FROM agent_continuation WHERE session_id=? AND status='completed')
            AND status IN ('planned','waiting_child','failed') AND agent_turn_id IN (
            SELECT agent_turn_id FROM agent_turn WHERE tool_status!='completed')""", (session_id,))
        c.execute("""UPDATE agent_step SET status='cancelled' WHERE status IN ('planned','waiting_child','failed')
            AND plan_id IN (SELECT plan_id FROM agent_plan WHERE session_id=? AND status='cancelled')""", (session_id,))
        c.execute("""UPDATE agent_continuation SET status='superseded',reason='NEW_CHILD_INPUT',updated_at=?
            WHERE session_id=? AND (status IN ('pending','planning') OR next_turn_id IN (
            SELECT agent_turn_id FROM agent_turn WHERE tool_status!='completed'))""",
            (datetime.now(timezone.utc).isoformat(), session_id))

    @classmethod
    def execution_allowed(cls, connection, turn_id, running=False) -> bool:
        table = connection.execute("SELECT 1 FROM sqlite_master WHERE name='agent_continuation'").fetchone()
        if not table:
            return True
        row = connection.execute("SELECT * FROM agent_continuation WHERE next_turn_id=?", (turn_id,)).fetchone()
        if not row:
            turn = connection.execute("SELECT intent FROM agent_turn WHERE agent_turn_id=?", (turn_id,)).fetchone()
            return bool(turn and turn[0] != "runtime_continuation")
        return row["status"] == "completed" and cls._valid(connection, dict(row), prepared=True, running=running)

    @classmethod
    def _receipt(cls, c, work):
        """Read owned execution facts; neither TTL nor this result grants execution."""
        if not work['next_turn_id']:
            return None
        row = c.execute('''SELECT t.intent,t.client_action_id,t.tool_status,t.action_json,
            p.objective_id,p.status AS plan_status,p.context_manifest_json,
            s.status AS step_status,s.result_json,s.capability_name,s.execution_location
            FROM agent_turn t JOIN daily_session ds ON ds.session_id=t.session_id AND ds.child_id=t.child_id
            JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id AND p.child_id=t.child_id AND p.session_id=t.session_id
            JOIN agent_step s ON s.plan_id=p.plan_id AND s.step_index=0
            JOIN agent_objective o ON o.objective_id=? AND o.child_id=t.child_id AND o.session_id=t.session_id
            WHERE t.agent_turn_id=? AND t.child_id=? AND t.session_id=?''',
            (work['objective_id'], work['next_turn_id'], work['child_id'], work['session_id'])).fetchone()
        if not row or row['intent'] != 'runtime_continuation' or row['client_action_id'] != 'continuation:' + work['parent_turn_id']:
            return None
        for original in {work['root_turn_id'], work['parent_turn_id']}:
            if not c.execute('''SELECT 1 FROM agent_turn t JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id
                AND p.child_id=t.child_id AND p.session_id=t.session_id
                WHERE t.agent_turn_id=? AND t.child_id=? AND t.session_id=? AND p.objective_id=?
                AND t.tool_status='completed' ''',
                (original, work['child_id'], work['session_id'], work['objective_id'])).fetchone():
                return None
        if c.execute('''SELECT 1 FROM agent_turn t WHERE t.agent_turn_id IN (?,?,?)
            AND (t.tool_error_code='SOURCE_DELETED' OR EXISTS (SELECT 1 FROM agent_plan p
                JOIN agent_step s USING(plan_id) WHERE p.agent_turn_id=t.agent_turn_id AND s.error_code='SOURCE_DELETED'))''',
            (work['root_turn_id'], work['parent_turn_id'], work['next_turn_id'])).fetchone():
            return None
        try:
            action = json.loads(row['action_json'])
            if not isinstance(action, dict):
                return None
            local = (work['parent_turn_id'] == work['root_turn_id'] and work['depth'] == 1
                and action.get('source') == 'document_stop_followup'
                and action.get('document_stop_followup_root') == work['root_turn_id']
                and action.get('type') == 'none' and action.get('local_only') is True
                and action.get('cloud_transmission_allowed') is False
                and row['capability_name'] == 'conversation.respond' and row['execution_location'] == 'server'
                and json.loads(row['context_manifest_json']).get('document_stop_followup_root') == work['root_turn_id']
                and cls.document_followup(c, work['root_turn_id']))
            if ((action.get('origin') != 'tool_result' and not local)
                    or action.get('root_turn_id') != work['root_turn_id']
                    or action.get('parent_turn_id') != work['parent_turn_id']
                    or (row['objective_id'] != work['objective_id'] and not cls._neutral_origin(c, work))):
                return None
            status, reason = 'prepared', None
            if row['tool_status'] == 'completed' and row['step_status'] == 'completed' and row['plan_status'] in {'completed', 'waiting_child'}:
                from .agent_runtime import AgentRuntimeStore
                result = json.loads(row['result_json']) if row['result_json'] else None
                if not isinstance(result, dict):
                    return None
                status = 'blocked' if AgentRuntimeStore.execution_result_blocked(result) else 'completed'
            elif row['tool_status'] == 'failed' and row['step_status'] == 'failed':
                status = 'failed'
            elif work['execution_admitted_at'] or row['step_status'] == 'running':
                status, reason = 'awaiting_receipt', 'EXECUTION_RESULT_UNCONFIRMED'
        except (ValueError, TypeError, KeyError):
            return None
        return dict(status=status, reason=reason, root_turn_id=work['root_turn_id'],
            parent_turn_id=work['parent_turn_id'], next_turn_id=work['next_turn_id'],
            session_id=work['session_id'], child_id=work['child_id'],
            replay_allowed=False, mastery_update_allowed=False, local_only=True, cloud_transmission_allowed=False)

    def receipt(self, session_id: str, parent_turn_id: str) -> dict:
        with self.database.connect() as c:
            c.execute('BEGIN')
            row = c.execute('SELECT * FROM agent_continuation WHERE session_id=? AND parent_turn_id=?',
                (session_id, parent_turn_id)).fetchone()
            result = self._receipt(c, dict(row)) if row else None
            if result and result['status'] == 'prepared':
                result = {**result, 'status': 'not_confirmed', 'reason': 'NO_EXECUTION_RECEIPT'}
            return result or {'status': 'unavailable', 'replay_allowed': False, 'mastery_update_allowed': False}

    def claim(self, session_id: str, parent_turn_id: str) -> dict:
        with self.database.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM agent_continuation WHERE session_id=? AND parent_turn_id=?",
                            (session_id, parent_turn_id)).fetchone()
            if not row:
                return {"status": "none"}
            work = dict(row)
            from .document_goal_handoff import owns, snapshot
            if owns(c, work['root_turn_id']):
                return snapshot(c, work['root_turn_id'], session_id)
            receipt = self._receipt(c, work)
            if receipt and receipt['status'] != 'prepared':
                return receipt
            if work["status"] in {"blocked", "superseded"}:
                return {"status": work["status"], "reason": work["reason"]}
            if work["status"] == "completed":
                if not receipt:
                    return {"status": "superseded", "reason": "RECEIPT_SCOPE_UNAVAILABLE"}
                next_turn = c.execute("SELECT tool_status FROM agent_turn WHERE agent_turn_id=?", (work["next_turn_id"],)).fetchone()
                current = self._valid(c, work, prepared=True)
                if next_turn and next_turn[0] == "pending" and not current:
                    return {"status": "superseded"}
                return {"status": "ready" if next_turn and next_turn[0] == "pending" else "completed",
                        "next_turn_id": work["next_turn_id"]}
            if not self._valid(c, work):
                c.execute("UPDATE agent_continuation SET status='superseded',reason='CONTEXT_CHANGED' WHERE parent_turn_id=?", (parent_turn_id,))
                c.commit()
                return {"status": "superseded"}
            now = datetime.now(timezone.utc)
            if work["status"] == "planning":
                if work["lease_until"] and datetime.fromisoformat(work["lease_until"]) > now:
                    return {"status": "planning_in_progress"}
                # Only this deterministic local read has no unknown external
                # request or effect. The same row/TTL/authority still applies.
                if not self.document_followup(c, work['root_turn_id']):
                    c.execute("UPDATE agent_continuation SET status='blocked',reason='PLANNING_RESULT_UNKNOWN' WHERE parent_turn_id=?", (parent_turn_id,))
                    c.commit()
                    return {"status": "blocked", "reason": "PLANNING_RESULT_UNKNOWN"}
            work.update(status="planning", lease_token=str(uuid4()), lease_until=(now + timedelta(seconds=LEASE_SECONDS)).isoformat())
            c.execute("UPDATE agent_continuation SET status='planning',lease_token=?,lease_until=?,updated_at=? WHERE parent_turn_id=?",
                      (work["lease_token"], work["lease_until"], now.isoformat(), parent_turn_id))
            root = c.execute("SELECT transcript,action_json FROM agent_turn WHERE agent_turn_id=? AND child_id=?",
                             (work["root_turn_id"], work["child_id"])).fetchone()
            work["original_utterance"] = root["transcript"]
            root_plan = c.execute("SELECT plan_json FROM agent_plan WHERE agent_turn_id=?", (work["root_turn_id"],)).fetchone()
            work["authorized_actions"] = json.loads(root_plan[0]).get("continuation_scope", [])
            parent = c.execute("SELECT capability_name FROM agent_step s JOIN agent_plan p ON p.plan_id=s.plan_id WHERE p.agent_turn_id=? AND s.step_index=0", (parent_turn_id,)).fetchone()
            work["completed_capability"] = parent[0]
            c.commit()
            return work

    def snapshot(self, session_id: str) -> dict:
        with self.database.connect() as c:
            c.execute('BEGIN')
            row = c.execute("SELECT * FROM agent_continuation WHERE session_id=? ORDER BY rowid DESC LIMIT 1", (session_id,)).fetchone()
            if not row:
                return {"status": "none"}
            work = dict(row)
            from .document_goal_handoff import owns, snapshot
            if owns(c, work['root_turn_id']):
                return snapshot(c, work['root_turn_id'], session_id)
            receipt = self._receipt(c, work)
            if receipt and receipt['status'] != 'prepared':
                return receipt
            prepared = work["status"] == "completed"
            if prepared and not receipt:
                return {"status": "inactive", "reason": "RECEIPT_SCOPE_UNAVAILABLE"}
            if work["status"] in {"pending", "planning", "completed"}:
                if not self._valid(c, work, prepared=prepared):
                    return {"status": "inactive", "reason": "CONTEXT_CHANGED"}
                if prepared and work["execution_admitted_at"]:
                    return {"status": "awaiting_receipt", "parent_turn_id": work["parent_turn_id"],
                            "reason": "CLIENT_EXECUTION_RESULT_UNCONFIRMED"}
                return {"status": "ready" if prepared else work["status"],
                        "parent_turn_id": work["parent_turn_id"], "depth": work["depth"],
                        "expires_at": (datetime.fromisoformat(work["created_at"]) + timedelta(seconds=CONTINUATION_TTL_SECONDS)).isoformat()}
            return {"status": work["status"], "reason": work["reason"], "parent_turn_id": work["parent_turn_id"]}

    def admit_client_execution(self, session_id: str, turn_id: str) -> dict:
        with self.database.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM agent_continuation WHERE session_id=? AND next_turn_id=?", (session_id, turn_id)).fetchone()
            if not row or row["status"] != "completed" or not self._valid(c, dict(row), prepared=True):
                return {"status": "superseded"}
            if row["execution_admitted_at"]:
                return {"status": "execution_receipt_required"}
            location = c.execute("SELECT execution_location FROM agent_step s JOIN agent_plan p ON p.plan_id=s.plan_id WHERE p.agent_turn_id=? AND s.step_index=0", (turn_id,)).fetchone()
            if not location or location[0] not in {"client_device_bridge", "local_model"}:
                return {"status": "unsupported"}
            plan = c.execute(
                "SELECT plan_id FROM agent_plan WHERE agent_turn_id=?", (turn_id,)
            ).fetchone()
            from .decision_record import validate_current, StaleDecisionContextError
            try:
                validate_current(c, plan["plan_id"])
            except StaleDecisionContextError:
                return {"status": "superseded", "reason": "child_model_snapshot_stale"}
            c.execute("UPDATE agent_continuation SET execution_admitted_at=? WHERE parent_turn_id=?",
                      (datetime.now(timezone.utc).isoformat(), row["parent_turn_id"]))
            c.commit()
            return {"status": "admitted"}

    def current(self, connection, work) -> bool:
        row = connection.execute("SELECT status,lease_token FROM agent_continuation WHERE parent_turn_id=?", (work["parent_turn_id"],)).fetchone()
        return bool(row and row["status"] == "planning" and row["lease_token"] == work["lease_token"] and self._valid(connection, work))

    def block(self, work, reason: str) -> None:
        with self.database.connect() as c:
            c.execute("UPDATE agent_continuation SET status='blocked',reason=?,updated_at=? WHERE parent_turn_id=? AND status='planning' AND lease_token=?",
                      (reason, datetime.now(timezone.utc).isoformat(), work["parent_turn_id"], work["lease_token"]))
            c.commit()

    @staticmethod
    def finish(connection, work, next_turn_id: str) -> None:
        connection.execute("UPDATE agent_continuation SET status='completed',next_turn_id=?,updated_at=? WHERE parent_turn_id=? AND lease_token=?",
                           (next_turn_id, datetime.now(timezone.utc).isoformat(), work["parent_turn_id"], work["lease_token"]))
