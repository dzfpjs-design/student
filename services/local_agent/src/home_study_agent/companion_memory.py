"""Local, evidence-backed recollections, never mastery or personality evidence.

Call ensure_schema once at startup, handle_command outside the orchestrator's
write connection, and observe_turn AFTER its completion transaction commits.
Only teaching_preferences is an export projection; recall and spoken command
responses are local-only. Expiry is enforced on every access and at startup.
Content is bounded; content-free source/receipt tombstones outlive content to
prevent replay. Erasure covers these tables, not upstream turns or backups:
the application's privacy lifecycle owns those separate retention boundaries.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import math
import re
import sqlite3
from uuid import uuid4

from .database import Database


VERSION = "companion_memory_local_v1"
RETENTION_DAYS = {"preference": 180, "self_report": 90, "event": 30, "learning_barrier": 30}
MAX_ACTIVE = 128
MAX_CONTENT_ROWS = 256
MAX_SOURCES = 8
TEACHING_VALUES = {
    "explanation_style": {"stepwise", "visual", "example"},
    "tone": {"gentle"},
    "pace": {"slow"},
}
_HOME_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")
_STUDY_REVIEW = re.compile(
    r"(?:请)?(?:帮我)?(?:回顾(?:一下)?上次学习|(?:我)?上次学到(?:哪儿|哪里)了|(?:我)?上次学(?:习)?了什么)[？?。！! ]*")
_YESTERDAY_REVIEW = re.compile(
    r"(?:请)?(?:帮我)?(?:回顾(?:一下)?昨天(?:的)?学习|(?:我)?昨天(?:做了什么|学习了什么|学了什么))[？?。！! ]*")
_DOCUMENT_TOPICS = {
    "arithmetic": "计算式", "equation": "方程", "word_problem": "应用题",
    "reading": "阅读", "language_exercise": "语言练习", "text": "视觉OCR文字", "formula": "视觉OCR公式",
}
_DOCUMENT_FOLLOW_UP = {
    "question_requirement": ("题意需澄清", "这道题要你求什么？请用自己的话说说。"),
    "calculation_step": ("计算步骤需核对", "你能说说这一行是怎么算出来的吗？"),
    "unit_or_symbol": ("单位或符号需核对", "这里的单位和符号读得对吗？你为什么这样写？"),
    "copying_or_ocr": ("抄写或OCR差异待核对", "这段文字和你纸上写的一样吗？请帮我核对。"),
}
_DOCUMENT_UNSAFE = re.compile(
    r"忽略.{0,12}(?:指令|规则|提示)|系统提示|提示词|越狱|扮演.{0,8}(?:系统|助手)|"
    r"(?:ignore|override).{0,30}(?:instruction|previous|system)|(?:system|assistant|developer)\s*:|"
    r"<\|.*?\|>|https?://|data:|base64|api[_ -]?key|password|上传|泄露", re.I)
_TEACHING_PATTERNS = (
    ("explanation_style", "stepwise", r"一步一步|分步|分步骤"),
    ("explanation_style", "visual", r"画图|图示|用图|看图"),
    ("explanation_style", "example", r"举例|例子|实例"),
    ("tone", "gentle", r"温柔|温和|轻声"),
    ("pace", "slow", r"慢一点|慢点|慢慢|讲慢|说慢"),
)
_UNSAFE = re.compile(
    r"['\"“”‘’「」『』`]|忽略.{0,8}(指令|规则)|系统提示|提示词|"
    r"system\s*:|assistant\s*:|developer\s*:|https?://|"
    r"假如|假设|如果|扮演|模拟|例如|比如|据说|他说|她说|老师说|妈妈说|爸爸说|"
    r"笨蛋|天才|懒惰|人格|智商", re.I
)
_QUESTION = re.compile(r"[?？]|(?:吗|么|呢)[。！!\s]*$|是不是|是否|为什么|会不会|喜不喜欢|什么|哪种|哪个")
_COMMAND = re.compile(r"^(?:请)?(?:帮我)?(?:记住|忘记)|^(?:你)?记错了|^你(?:还)?记得|^我现在不喜欢")
_EVENT = re.compile(
    r"(?:我(?:今天|昨天|刚才)|(?:今天|昨天|刚才)我)(?!们)"
    r"(?:在.{1,20}?)?(?:参加了|完成了|去了|做完了|读完了|学了|学习了|看了|听了|"
    r"练习了|遇到了|忘了|忘记了|丢了|弄丢了|迟到了|考了|考得|得了|拿到了|"
    r"没(?:有)?(?:参加|完成|去|做完|读完|带)).{1,80}"
)
_EVENT_NONFACT = re.compile(
    r"想|打算|准备|计划|希望|将|明天|后天|下周|以后|等到|之后|然后再|后再|"
    r"请|帮我|告诉我|给我|吧|开玩笑|假的|翻译|复述|重复|解释|改写|怎么说|这句话"
)


def _is_event(text: str) -> bool:
    return bool(_EVENT.fullmatch(text)) and not (_EVENT_NONFACT.search(text) or _QUESTION.search(text) or _UNSAFE.search(text))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (ValueError, TypeError, AttributeError):
        return None


def _compact(text: str) -> str:
    return re.sub(r"[\s，。！？!?、；;：:,.]", "", text).lower()


def _teaching_matches(target: str) -> list[tuple[str, str]]:
    matches = []
    for key, value, pattern in _TEACHING_PATTERNS:
        if not re.search(pattern, target):
            continue
        context = {"explanation_style": r"讲|解释|教我|教学|解题|理解题目",
                   "tone": r"讲|说|语气|语调|声音|对我|跟我",
                   "pace": r"讲|说|语速|节奏|教我"}[key]
        if re.search(context, target):
            matches.append((key, value))
    return matches


@dataclass(frozen=True)
class _Claim:
    content: str
    category: str
    key: str
    value: str
    evidence: str
    teaching_key: str | None = None
    teaching_value: str | None = None


def _claims(text: str) -> list[_Claim]:
    if not text or len(text) > 1000 or _UNSAFE.search(text) or _QUESTION.search(text):
        return []
    result: list[_Claim] = []
    previous_preference = False
    previous_teaching = False
    for raw in re.split(r"[，,。.!！；;\n]+|(?=但(?:是)?(?:讲题|讲解|解释))", text):
        clause = raw.strip(" ：:")
        if not clause or len(clause) > 160:
            continue
        statement = clause
        negative = re.fullmatch(r"(?:但是|但)?(?:我)?(?:讲题|讲解|解释|教我|教学|上课)(?:时)?(?:我)?(?:请)?(?:不要|别|不用|不需要|不喜欢)(.{1,80})", clause)
        if not negative and previous_teaching:
            negative = re.fullmatch(r"(?:但是|但)?(?:请)?(?:不要|别|不用)(.{1,80})", clause)
        if negative:
            for key, value, pattern in _TEACHING_PATTERNS:
                if re.search(pattern, negative[1]):
                    result.append(_Claim(clause, "preference", "teaching:" + key + ":" + value,
                                         "dislike:" + value, clause, key, None))
            previous_preference = False
            continue
        if previous_preference and re.match(r"^(?:但|但是|而是)?(?:更喜欢|喜欢|不喜欢)", statement):
            statement = "我" + re.sub(r"^(?:但是|但|而是)", "", statement)
        preference = re.fullmatch(r"我(?:现在|目前|平时|比较|最)?(不再喜欢|不喜欢|讨厌|更喜欢|喜欢|偏好)(.{1,80})", statement)
        previous_preference = preference is not None
        if preference:
            verb, target = preference.groups()
            if re.search(r"不|没|并非|才怪|才不|喜欢|还是|或者|可能|也许|假的|开玩笑", target):
                continue
            value = "dislike" if verb in {"不喜欢", "不再喜欢", "讨厌"} else "like"
            matches = _teaching_matches(target)
            # Ambiguous teaching preferences stay local, without an enum export.
            teaching = matches[0] if len(matches) == 1 else (None, None)
            previous_teaching = bool(matches)
            key = "teaching:" + teaching[0] + ":" + teaching[1] if teaching[0] else "preference:" + _compact(target)
            result.append(_Claim(statement, "preference", key, value + ":" + (teaching[1] or _compact(target)), clause,
                                 teaching[0], teaching[1] if value == "like" else None))
            continue
        if re.fullmatch(r"我(?:(?:在|做).{1,30}?(?:时)?)?(?:总是|经常|还|暂时|容易|常常)?(?:不懂|不会|没弄懂|看不懂|听不懂|分不清|记不住|跟不上|卡在|弄混|混淆).{1,80}", clause):
            result.append(_Claim(clause, "learning_barrier", "barrier:" + _compact(clause), _compact(clause), clause))
        elif _is_event(clause):
            result.append(_Claim(clause, "event", "event:" + _compact(clause), _compact(clause), clause))
        elif re.fullmatch(r"我(?:觉得|感觉|担心|希望|已经学会了|学会了|已经掌握了|掌握了|还没掌握|还没学会).{1,100}", clause):
            result.append(_Claim(clause, "self_report", "report:" + _compact(clause), _compact(clause), clause))
        elif identity := re.fullmatch(r"(我的生日是|我叫|我住在|我读)(.{1,60})", clause):
            if not re.search(r"不|没|可能|也许", identity[2]):
                result.append(_Claim(clause, "self_report", "report:" + identity[1], _compact(identity[2]), clause))
    negatives = [claim for claim in result if claim.teaching_key and claim.value.startswith("dislike:")]
    positives = [claim for claim in result if claim not in negatives]
    # Explicit teaching refusals win within a turn, even before a positive clause
    # or beyond the normal four-memory extraction budget.
    return positives[:max(0, 4 - len(negatives))] + negatives[:4]


class CompanionMemory:
    """Synchronous SQLite service. Invalid ownership/action reuse raises ValueError."""

    def __init__(self, database: Database):
        self.database = database
        self._schema_ready = False

    def _ready(self) -> None:
        if not self._schema_ready:
            self.ensure_schema()

    def ensure_schema(self) -> None:
        with self.database.connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS companion_memory_config (
                  singleton INTEGER PRIMARY KEY CHECK(singleton = 1), secret TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS companion_memory_source (
                  source_id TEXT PRIMARY KEY,
                  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                  session_id TEXT NOT NULL,
                  action_id TEXT NOT NULL UNIQUE,
                  turn_id TEXT UNIQUE,
                  request_digest TEXT NOT NULL,
                  kind TEXT NOT NULL CHECK(kind IN ('turn', 'remember', 'recall', 'forget', 'correct')),
                  result_code TEXT,
                  observed INTEGER NOT NULL DEFAULT 0,
                  tombstoned INTEGER NOT NULL DEFAULT 0,
                  occurred_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_companion_source_child
                  ON companion_memory_source(child_id);
                CREATE TABLE IF NOT EXISTS companion_memory (
                  memory_id TEXT PRIMARY KEY,
                  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                  category TEXT NOT NULL CHECK(category IN ('preference','self_report','event','learning_barrier')),
                  semantic_key TEXT, value_digest TEXT, content TEXT,
                  status TEXT NOT NULL CHECK(status IN ('active','superseded','revoked','forgotten','expired')),
                  confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
                  importance REAL NOT NULL,
                  occurred_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
                  expires_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                  superseded_by TEXT,
                  teaching_key TEXT, teaching_value TEXT,
                  version TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_companion_active_key
                  ON companion_memory(child_id, semantic_key) WHERE status = 'active';
                CREATE INDEX IF NOT EXISTS idx_companion_child_status
                  ON companion_memory(child_id, status, last_seen_at);
                CREATE TABLE IF NOT EXISTS companion_memory_evidence (
                  memory_id TEXT NOT NULL REFERENCES companion_memory(memory_id) ON DELETE CASCADE,
                  source_id TEXT NOT NULL REFERENCES companion_memory_source(source_id) ON DELETE CASCADE,
                  evidence TEXT,
                  PRIMARY KEY(memory_id, source_id)
                );
                CREATE TABLE IF NOT EXISTS companion_memory_audit (
                  event_id TEXT PRIMARY KEY,
                  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                  memory_id TEXT NOT NULL,
                  operation TEXT NOT NULL,
                  occurred_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS companion_memory_checkpoint (
                  turn_id TEXT PRIMARY KEY,
                  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                  status TEXT NOT NULL CHECK(status IN ('observed','skipped','failed')),
                  attempts INTEGER NOT NULL DEFAULT 1,
                  error_code TEXT,
                  retry_after TEXT,
                  updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS companion_memory_suppression (
                  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                  semantic_digest TEXT NOT NULL,
                  cutoff_at TEXT NOT NULL,
                  PRIMARY KEY(child_id, semantic_digest)
                );
                CREATE TABLE IF NOT EXISTS companion_memory_document_source (
                  source_id TEXT PRIMARY KEY REFERENCES companion_memory_source(source_id) ON DELETE CASCADE,
                  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                  revision_id TEXT NOT NULL UNIQUE,
                  artifact_id TEXT NOT NULL,
                  previous_revision_id TEXT,
                  change_kind TEXT NOT NULL,
                  review_required INTEGER NOT NULL,
                  artifact_status TEXT NOT NULL,
                  ocr_confidence REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS companion_memory_document_checkpoint (
                  revision_id TEXT PRIMARY KEY,
                  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                  status TEXT NOT NULL CHECK(status IN ('observed','skipped','failed')),
                  attempts INTEGER NOT NULL,
                  error_code TEXT,
                  retry_after TEXT,
                  updated_at TEXT NOT NULL
                );
            """)
            if "observation_kind" not in {row["name"] for row in connection.execute("PRAGMA table_info(companion_memory)")}:
                # Preserve existing CHECK constraints and all source foreign keys.
                # Event storage now distinguishes observations from child reports.
                connection.execute("ALTER TABLE companion_memory ADD COLUMN observation_kind TEXT")
            if "observation_json" not in {row["name"] for row in connection.execute("PRAGMA table_info(companion_memory)")}:
                connection.execute("ALTER TABLE companion_memory ADD COLUMN observation_json TEXT")
            document_columns = {row["name"] for row in connection.execute("PRAGMA table_info(companion_memory_document_source)")}
            for column in ("analysis_updated_at", "analysis_status"):
                if column not in document_columns:
                    connection.execute(f"ALTER TABLE companion_memory_document_source ADD COLUMN {column} TEXT")
            connection.execute("INSERT OR IGNORE INTO companion_memory_config VALUES (1, ?)", (uuid4().hex + uuid4().hex,))
            self._prune(connection, _now())
            connection.commit()
        self._schema_ready = True

    @staticmethod
    def is_command(transcript: str) -> bool:
        text = transcript.strip()
        if _STUDY_REVIEW.fullmatch(text) or _YESTERDAY_REVIEW.fullmatch(text):
            return True
        if re.match(r"^你(?:还)?记得", text):
            # Personal recall stays local even when a preference concerns a
            # question. Topic words alone must not route that request to cloud.
            if re.search(r"我.{0,80}(?:喜欢|偏好|习惯|生日|经历|担心|害怕|感觉|觉得)", text):
                return True
            # Current dialogue references and saved question recall belong to
            # their existing conversation/task context, not personal memory.
            if re.search(r"(?:错题|这题|那道题|题目)", text):
                return False
            if re.fullmatch(r"你(?:还)?记得(?:它|这个|那个)(?:吗)?[？?。 ]*", text):
                return False
        return bool(_COMMAND.match(text))

    def is_local_memory_turn(self, turn_id: str) -> bool:
        """Provenance-only history filter, also true after erasure/expiry.

        This deliberately does not search for remembered words in other turns.
        Call outside a caller's write transaction if the schema is not ready yet.
        """
        self._ready()
        with self.database.connect() as connection:
            return connection.execute("""SELECT 1 FROM agent_turn t WHERE t.agent_turn_id = ? AND
                (t.intent = 'memory_command' OR EXISTS (
                    SELECT 1 FROM companion_memory_source s WHERE s.child_id = t.child_id
                    AND s.session_id = t.session_id AND
                    (s.turn_id = t.agent_turn_id OR s.action_id = t.client_action_id)))""", (turn_id,)).fetchone() is not None

    @staticmethod
    def _owner(connection: sqlite3.Connection, child_id: str, session_id: str) -> None:
        row = connection.execute("SELECT child_id FROM daily_session WHERE session_id = ?", (session_id,)).fetchone()
        if not row or row["child_id"] != child_id:
            raise ValueError("companion_memory_session_ownership")

    @staticmethod
    def _digest(connection: sqlite3.Connection, child_id: str, value: str) -> str:
        secret = connection.execute("SELECT secret FROM companion_memory_config WHERE singleton = 1").fetchone()[0]
        return hmac.new(secret.encode(), json.dumps([child_id, value], ensure_ascii=False).encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def _is_local_recovery_turn(turn: sqlite3.Row) -> bool:
        try:
            action = json.loads(turn['action_json'])
        except (TypeError, ValueError):
            return False
        return isinstance(action, dict) and 'local_recovery_control' in action

    @staticmethod
    def _eligible(turn: sqlite3.Row) -> bool:
        if CompanionMemory._is_local_recovery_turn(turn):
            return False
        if turn["tool_status"] != "completed" or not _timestamp(turn["completed_at"]) or turn["tool_error_code"]:
            return False
        try:
            action = json.loads(turn["action_json"])
        except (TypeError, ValueError):
            return False
        if not isinstance(action, dict):
            return False
        allowed = {"none", "ask_tutor", "record_tutor_feedback", "update_reflection", "memory_command"}
        if action.get("type") not in allowed:
            return False
        blocked = r"echo|control|simulat|synthetic|mock|exam|diagnostic|pet_|capture|profile_switch|audio_check|prompt_guard|repetition_confirmation"
        if re.search(blocked, turn["intent"], re.I):
            return False

        def unsafe_metadata(value: object) -> bool:
            if isinstance(value, dict):
                for key, item in value.items():
                    if re.search(r"simulat|synthetic|mock|echo|replay|test_mode", key, re.I) and item:
                        return True
                    if key in {"source", "mode", "provider", "type"} and isinstance(item, str) and re.search(blocked, item, re.I):
                        return True
                    if unsafe_metadata(item):
                        return True
            elif isinstance(value, list):
                return any(unsafe_metadata(item) for item in value)
            return False

        return not unsafe_metadata(action)

    def observe_turn(self, turn_id: str) -> None:
        with self.database.connect() as connection:
            turn = connection.execute('SELECT action_json FROM agent_turn WHERE agent_turn_id=?', (turn_id,)).fetchone()
            if turn and self._is_local_recovery_turn(turn):
                return
        self._ready()
        try:
            self._observe_turn(turn_id)
        except Exception:
            # Never retain exception text: it may contain private SQL parameters.
            with self.database.connect() as connection:
                turn = connection.execute("SELECT child_id FROM agent_turn WHERE agent_turn_id = ?", (turn_id,)).fetchone()
                if turn:
                    self._checkpoint(connection, turn_id, turn[0], "failed", _now())
                    connection.commit()
            raise

    @staticmethod
    def _checkpoint(connection: sqlite3.Connection, turn_id: str, child_id: str, status: str, now: datetime) -> None:
        connection.execute("""INSERT INTO companion_memory_checkpoint
            (turn_id,child_id,status,attempts,error_code,retry_after,updated_at) VALUES (?,?,?,1,?,?,?)
            ON CONFLICT(turn_id) DO UPDATE SET status=excluded.status,
              attempts=companion_memory_checkpoint.attempts+1, error_code=excluded.error_code,
              retry_after=excluded.retry_after, updated_at=excluded.updated_at""", (
            turn_id, child_id, status, "OBSERVATION_FAILED" if status == "failed" else None,
            (now + timedelta(minutes=5)).isoformat() if status == "failed" else None, now.isoformat()))

    def _observe_turn(self, turn_id: str) -> None:
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = _now()
            turn = connection.execute("SELECT * FROM agent_turn WHERE agent_turn_id = ?", (turn_id,)).fetchone()
            if turn and self._is_local_recovery_turn(turn):
                return
            self._prune(connection, now)
            if not turn or turn["tool_status"] != "completed":
                connection.commit()
                return
            checkpoint = connection.execute("SELECT status FROM companion_memory_checkpoint WHERE turn_id = ?", (turn_id,)).fetchone()
            try:
                revisit_turn = json.loads(turn['action_json']).get('type') == 'control_document_revisit'
            except (ValueError, TypeError, AttributeError):
                revisit_turn = False
            if checkpoint and (checkpoint[0] == 'observed' or (checkpoint[0] == 'skipped' and not revisit_turn)):
                connection.commit()
                return
            if revisit_turn and not turn['tool_error_code']:
                from .document_revisit_memory import observe
                observe(self, connection, turn, now)
                self._checkpoint(connection, turn_id, turn['child_id'], 'observed', now)
                self._prune(connection, now)
                connection.commit()
                return
            if not self._eligible(turn):
                self._checkpoint(connection, turn_id, turn["child_id"], "skipped", now)
                connection.commit()
                return
            self._owner(connection, turn["child_id"], turn["session_id"])
            source = connection.execute("SELECT * FROM companion_memory_source WHERE action_id = ?", (turn["client_action_id"],)).fetchone()
            if source:
                self._check_source(connection, source, turn["child_id"], turn["session_id"], turn["transcript"])
                connection.execute("UPDATE companion_memory_source SET turn_id = ?, observed = 1 WHERE source_id = ?",
                                   (turn_id, source["source_id"]))
            elif not self.is_command(turn["transcript"]):
                claims = _claims(turn["transcript"])
                if claims:
                    occurred = _timestamp(turn["created_at"])
                    if occurred is None or occurred > now + timedelta(minutes=5):
                        self._checkpoint(connection, turn_id, turn["child_id"], "skipped", now)
                        connection.commit()
                        return
                    source_id = self._source(connection, turn["child_id"], turn["session_id"], turn["transcript"],
                                             turn["client_action_id"], "turn", occurred, turn_id)
                    self._save(connection, turn["child_id"], claims, source_id, occurred, now, False)
            self._checkpoint(connection, turn_id, turn["child_id"], "observed", now)
            self._prune(connection, now)
            connection.commit()

    def process_pending(self, limit: int = 50) -> dict:
        """Bounded local backfill. Failed rows retry after five minutes, without starvation."""
        self._ready()
        with self.database.connect() as connection:
            now = _now()
            self._prune(connection, now)
            connection.commit()
            cap = min(500, max(0, int(limit)))
            rows = [dict(row) for row in connection.execute("""SELECT t.agent_turn_id AS source_id, 'turn' AS kind, t.created_at FROM agent_turn t
                LEFT JOIN companion_memory_checkpoint c ON c.turn_id = t.agent_turn_id
                WHERE t.tool_status = 'completed' AND
                  CASE WHEN json_valid(t.action_json)
                    THEN json_type(t.action_json,'$.local_recovery_control') IS NULL ELSE 1 END AND
                  (c.turn_id IS NULL OR (c.status = 'failed' AND c.retry_after <= ?)
                   OR (c.status='skipped' AND CASE WHEN json_valid(t.action_json)
                     THEN json_extract(t.action_json,'$.type') END='control_document_revisit'))
                ORDER BY CASE WHEN c.turn_id IS NULL THEN 0 ELSE 1 END, t.created_at, t.agent_turn_id
                LIMIT ?""", (now.isoformat(), cap)).fetchall()]
            if self._has_document_revisions(connection):
                has_analysis = self._has_document_analysis(connection)
                analysis_join = "LEFT JOIN document_learning_analysis l ON l.revision_id=r.revision_id" if has_analysis else ""
                analysis_changed = "OR COALESCE(d.analysis_updated_at,'') != COALESCE(l.updated_at,'') OR COALESCE(d.analysis_status,'') != COALESCE(l.status,'')" if has_analysis else ""
                rows += [dict(row) for row in connection.execute(f"""SELECT r.revision_id AS source_id, 'document' AS kind, r.created_at
                    FROM document_page_revision r JOIN capture_artifact a ON a.artifact_id=r.artifact_id
                    LEFT JOIN companion_memory_document_checkpoint c ON c.revision_id=r.revision_id
                    LEFT JOIN companion_memory_document_source d ON d.revision_id=r.revision_id
                    {analysis_join}
                    WHERE a.status IN ('completed','quality_warning') AND
                      (c.revision_id IS NULL OR (c.status='failed' AND c.retry_after <= ?)
                       OR (c.status='observed' AND d.source_id IS NOT NULL AND
                           (d.review_required != (r.review_required != 0 OR a.status='quality_warning')
                            OR d.artifact_status != a.status OR d.ocr_confidence != MIN(r.ocr_confidence,a.confidence)
                            {analysis_changed})))
                    ORDER BY r.created_at, r.revision_id LIMIT ?""", (now.isoformat(), cap)).fetchall()]
            rows = sorted(rows, key=lambda row: (row["created_at"], row["kind"], row["source_id"]))[:cap]
        result = {"cloud_transmission_allowed": False, "processed": 0, "failed": 0, "failures": []}
        for row in rows:
            try:
                if row["kind"] == "document":
                    self.observe_document_revision(row["source_id"])
                else:
                    self.observe_turn(row["source_id"])
                result["processed"] += 1
            except Exception:
                result["failed"] += 1
                result["failures"].append({"revision_id" if row["kind"] == "document" else "turn_id": row["source_id"], "error_code": "OBSERVATION_FAILED"})
        return result

    @staticmethod
    def _has_document_revisions(connection: sqlite3.Connection) -> bool:
        return connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='document_page_revision'").fetchone() is not None

    @staticmethod
    def _has_document_analysis(connection: sqlite3.Connection) -> bool:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(document_learning_analysis)")}
        return {"revision_id", "child_id", "session_id", "artifact_id", "status", "source", "result_json", "updated_at"} <= columns

    def _document_focus(self, connection: sqlite3.Connection, revision: sqlite3.Row) -> tuple[list[dict], str | None, str | None]:
        """Validate quoted evidence locally; model statements are never copied."""
        if not self._has_document_analysis(connection):
            return [], None, None
        analysis = connection.execute("SELECT * FROM document_learning_analysis WHERE revision_id=?", (revision["revision_id"],)).fetchone()
        if not analysis:
            return [], None, None
        if any(analysis[key] != revision[key] for key in ("child_id", "session_id", "artifact_id")):
            raise ValueError("companion_memory_analysis_ownership")
        marker = (analysis["updated_at"], analysis["status"])
        if analysis["status"] != "completed" or len(analysis["result_json"] or "") > 64000:
            return [], *marker
        try:
            result = json.loads(analysis["result_json"] or "{}")
        except (ValueError, TypeError):
            return [], *marker
        if not isinstance(result, dict) or result.get("status") != "completed":
            return [], *marker
        if any(result.get(key, revision[key]) != revision[key] for key in ("revision_id", "child_id", "session_id", "artifact_id")):
            return [], *marker
        basis = analysis["source"]
        if basis not in {"local_ocr_rules", "cloud_ocr_text", "cloud_page_ocr"} or result.get("source", basis) != basis:
            return [], *marker
        artifact = connection.execute("SELECT substr(extracted_text,1,12001) AS text, substr(details_json,1,100000) AS details,client_action_id FROM capture_artifact WHERE artifact_id=?", (revision["artifact_id"],)).fetchone()
        text = artifact["text"] or ""
        fallback = {}
        if basis == "cloud_page_ocr":
            try:
                fallback = json.loads(artifact["details"] or "{}").get("cloud_vision_fallback", {})
                if fallback.get("status") != "needs_review" or fallback.get("request_id") not in {"monitor:" + artifact["client_action_id"], "artifact:" + revision["artifact_id"]}:
                    return [], *marker
                if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='document_vision_request' AND type='table'").fetchone():
                    return [], *marker
                receipt = connection.execute("SELECT result_json FROM document_vision_request WHERE child_id=? AND request_id=? AND status='succeeded'", (revision["child_id"], fallback["request_id"])).fetchone()
                if not receipt or json.loads(receipt[0]) != fallback:
                    return [], *marker
                blocks = fallback.get("blocks", [])
                if not isinstance(blocks, list) or not 1 <= len(blocks) <= 64:
                    return [], *marker
                text = "\n".join(block["text"] for block in blocks if isinstance(block, dict) and isinstance(block.get("text"), str))
            except (ValueError, TypeError, AttributeError):
                return [], *marker
        elif hashlib.sha256("\n".join(line.strip() for line in text.splitlines() if line.strip()).encode()).hexdigest() != revision["content_sha256"]:
            return [], *marker
        if not text or len(text) > 16000 or _DOCUMENT_UNSAFE.search(text):
            return [], *marker

        def citation(item: object, category: str) -> dict | None:
            if not isinstance(item, dict):
                return None
            kind = item.get("kind")
            allowed = _DOCUMENT_TOPICS if category == "observation" else _DOCUMENT_FOLLOW_UP
            evidence = item.get("evidence")
            if not isinstance(kind, str) or kind not in allowed or not isinstance(evidence, dict):
                return None
            if evidence.get("revision_id") != revision["revision_id"] or evidence.get("artifact_id") != revision["artifact_id"]:
                return None
            quote, start, end = evidence.get("quote"), evidence.get("start"), evidence.get("end")
            if not isinstance(quote, str) or not 2 <= len(quote.strip()) <= 600:
                return None
            visual_ref = {}
            if basis == "cloud_page_ocr":
                index = evidence.get("block_index")
                if type(index) is not int or not 0 <= index < len(blocks) or evidence.get("cloud_vision_request_id") != fallback["request_id"]:
                    return None
                block = blocks[index]
                if not isinstance(block, dict) or block.get("text") != quote or block.get("kind") != kind or kind not in {"text", "formula"}:
                    return None
                bbox, confidence = block.get("bbox"), block.get("confidence")
                if (not isinstance(bbox, list) or len(bbox) != 4 or evidence.get("bbox") != bbox
                        or any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in bbox)
                        or type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence <= .69):
                    return None
                start = 0
                visual_ref = {"cloud_vision_request_id": fallback["request_id"], "block_index": index, "bbox": bbox,
                              "ocr_confidence": confidence, "offset_basis": "block_text"}
            elif type(start) is not int or type(end) is not int or start < 0 or end != start + len(quote) or text[start:end] != quote:
                return None
            return {**visual_ref, "category": category, "kind": kind, "quote": quote[:80], "start": start,
                    "end": start + len(quote[:80]), "revision_id": revision["revision_id"],
                    "artifact_id": revision["artifact_id"], "basis": basis, "status": "needs_verification"}

        groups = []
        for field, category in (("observations", "observation"), ("hypotheses", "hypothesis"), ("questions", "question")):
            entries = result.get(field, [])
            if not isinstance(entries, list):
                entries = []
            valid = [value for entry in entries[:8] if (value := citation(entry, category))]
            if field == "questions":
                # The producer's string questions have no separate citation.
                # Only its fixed question for a validated hypothesis is grounded.
                for hypothesis in groups[1]:
                    if _DOCUMENT_FOLLOW_UP[hypothesis["kind"]][1] in entries:
                        valid.append({**hypothesis, "category": "question"})
            groups.append(valid)
        selected = []
        for index in range(8):
            for group in groups:
                if index < len(group):
                    entry = group[index]
                    if not any((v["category"], v["kind"], v["quote"]) == (entry["category"], entry["kind"], entry["quote"]) for v in selected):
                        selected.append(entry)
                        if len(selected) == 3:
                            return selected, *marker
        return selected, *marker

    @staticmethod
    def _document_checkpoint(connection: sqlite3.Connection, revision_id: str, child_id: str, status: str, now: datetime) -> None:
        connection.execute("""INSERT INTO companion_memory_document_checkpoint VALUES (?,?,?,1,?,?,?)
            ON CONFLICT(revision_id) DO UPDATE SET status=excluded.status, attempts=attempts+1,
              error_code=excluded.error_code, retry_after=excluded.retry_after, updated_at=excluded.updated_at""", (
            revision_id, child_id, status, "DOCUMENT_OBSERVATION_FAILED" if status == "failed" else None,
            (now + timedelta(minutes=5)).isoformat() if status == "failed" else None, now.isoformat()))

    def observe_document_revision(self, revision_id: str) -> None:
        """Project revision metadata and grounded analysis highlights, never images.

        Call outside the producer transaction. Missing source tables are a no-op;
        failed projections have content-free checkpoints and may be retried.
        """
        self._ready()
        try:
            self._observe_document_revision(revision_id)
        except Exception:
            with self.database.connect() as connection:
                if self._has_document_revisions(connection):
                    row = connection.execute("SELECT child_id FROM document_page_revision WHERE revision_id=?", (revision_id,)).fetchone()
                    if row:
                        self._document_checkpoint(connection, revision_id, row[0], "failed", _now())
                        connection.commit()
            raise

    def _observe_document_revision(self, revision_id: str) -> None:
        with self.database.connect() as connection:
            if not self._has_document_revisions(connection):
                return
            connection.execute("BEGIN IMMEDIATE")
            now = _now()
            self._prune(connection, now)
            revision = connection.execute("SELECT * FROM document_page_revision WHERE revision_id=?", (revision_id,)).fetchone()
            if not revision:
                connection.commit()
                return
            child_id, session_id = revision["child_id"], revision["session_id"]
            self._owner(connection, child_id, session_id)
            artifact = connection.execute("""SELECT child_id,session_id,artifact_type,status,confidence,
                substr(extracted_text,1,160) AS excerpt FROM capture_artifact WHERE artifact_id=?""", (revision["artifact_id"],)).fetchone()
            if not artifact or artifact["child_id"] != child_id or artifact["session_id"] != session_id or artifact["artifact_type"] != "document_ocr":
                raise ValueError("companion_memory_document_ownership")
            monitor = connection.execute("SELECT child_id,session_id FROM document_monitor_session WHERE monitor_id=?", (revision["monitor_id"],)).fetchone()
            if not monitor or tuple(monitor) != (child_id, session_id):
                raise ValueError("companion_memory_document_ownership")
            if artifact["status"] not in {"completed", "quality_warning"}:
                connection.commit()
                return
            occurred = _timestamp(revision["created_at"])
            if not occurred or occurred > now + timedelta(minutes=5):
                raise ValueError("companion_memory_document_timestamp")
            confidences = [float(revision["ocr_confidence"]), float(artifact["confidence"])]
            if any(not math.isfinite(value) or not 0 <= value <= 1 for value in confidences):
                raise ValueError("companion_memory_document_confidence")
            confidence = min(confidences)
            previous = None
            if revision["previous_revision_id"]:
                previous = connection.execute("SELECT * FROM document_page_revision WHERE revision_id=?", (revision["previous_revision_id"],)).fetchone()
                if not previous or any(previous[key] != revision[key] for key in ("child_id", "session_id", "monitor_id")):
                    raise ValueError("companion_memory_document_previous_ownership")
            change_kind = "baseline" if previous is None else "text_changed" if previous["content_sha256"] != revision["content_sha256"] else "no_text_change"
            review = bool(revision["review_required"] or artifact["status"] == "quality_warning")
            review_text = "OCR结果仍需复核" if review else "OCR未标记需复核，未表示已经人工确认"
            change_text = {"baseline": "建立页面文字基线", "text_changed": "与前一版本相比，OCR文字发生变化", "no_text_change": "与前一版本相比，未检测到OCR文字变化"}[change_kind]
            excerpt = artifact["excerpt"] or ""
            if _UNSAFE.search(excerpt) or re.search(r"data:|base64|[A-Za-z]:[\\/]", excerpt, re.I):
                excerpt = ""
            content = f"高拍仪学习观察：{change_text}；{review_text}。仅为页面观察，不证明掌握程度、学习习惯或错因。"
            if excerpt:
                content += "OCR文字片段：" + excerpt
            focus, analysis_updated, analysis_status = self._document_focus(connection, revision)
            projection_confidence = min([confidence] + [entry.get("ocr_confidence", confidence) for entry in focus])
            observation_kind = "document_revision"
            evidence = "OCR页面文字（不是孩子自述）：" + excerpt if excerpt else "仅保留页面变化与复核状态；未保存文字片段。"
            if focus:
                observation_kind = "document_learning_analysis"
                points = []
                for entry in focus:
                    category, kind = entry["category"], entry["kind"]
                    label = ("观察候选：" + _DOCUMENT_TOPICS[kind] if category == "observation" else
                             "待验证假设：" + _DOCUMENT_FOLLOW_UP[kind][0] if category == "hypothesis" else
                             "待追问：" + _DOCUMENT_FOLLOW_UP[kind][1])
                    points.append(label + "；逐字引文：" + entry["quote"])
                content = "高拍仪学习重点（OCR引文待复核，不是掌握或错因事实）：" + "；".join(points)
                evidence = "\n".join(dict.fromkeys(entry["quote"] for entry in focus))
            # References and scan timestamps are not semantic changes. Repeated
            # unchanged pages must not create versions or extend retention.
            focus_value = sorted((entry["category"], entry["kind"], entry["quote"], entry["basis"], entry.get("ocr_confidence", confidence)) for entry in focus)
            fingerprint = json.dumps([revision["content_sha256"], review, artifact["status"], confidence, excerpt, focus_value], ensure_ascii=False)
            digest = self._digest(connection, child_id, fingerprint)
            existing = connection.execute("""SELECT s.* FROM companion_memory_source s JOIN companion_memory_document_source d USING(source_id)
                WHERE d.revision_id=? AND d.child_id=?""", (revision_id, child_id)).fetchone()
            if existing:
                connection.execute("UPDATE companion_memory_document_source SET analysis_updated_at=?,analysis_status=? WHERE source_id=?",
                                   (analysis_updated, analysis_status, existing["source_id"]))
            if existing and (existing["tombstoned"] or existing["request_digest"] == digest):
                # Even a later review update cannot revive a forgotten source.
                self._document_checkpoint(connection, revision_id, child_id, "skipped" if existing["tombstoned"] else "observed", now)
                connection.commit()
                return
            if existing:
                source_id = existing["source_id"]
                connection.execute("UPDATE companion_memory_source SET request_digest=? WHERE source_id=?", (digest, source_id))
            else:
                source_id = self._source(connection, child_id, session_id, fingerprint, "document-memory:" + revision_id, "turn", occurred)
            connection.execute("""INSERT INTO companion_memory_document_source
                (source_id,child_id,revision_id,artifact_id,previous_revision_id,change_kind,review_required,artifact_status,ocr_confidence,analysis_updated_at,analysis_status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(source_id) DO UPDATE SET change_kind=excluded.change_kind,
                  review_required=excluded.review_required, artifact_status=excluded.artifact_status, ocr_confidence=excluded.ocr_confidence,
                  analysis_updated_at=excluded.analysis_updated_at,analysis_status=excluded.analysis_status""",
                (source_id, child_id, revision_id, revision["artifact_id"], revision["previous_revision_id"], change_kind, int(review), artifact["status"], confidence, analysis_updated, analysis_status))
            claim = _Claim(content, "event", "document_page:" + revision["content_sha256"], fingerprint, evidence)
            key = self._digest(connection, child_id, claim.key)
            unchanged = connection.execute("SELECT 1 FROM companion_memory WHERE child_id=? AND semantic_key=? AND value_digest=? AND status='active'", (child_id, key, digest)).fetchone()
            if not existing and not focus:
                # A repeated scan whose analysis is still pending cannot replace
                # an already grounded focus with a baseline and then replace it back.
                unchanged = unchanged or connection.execute("""SELECT 1 FROM companion_memory WHERE child_id=?
                    AND semantic_key=? AND status='active' AND observation_kind='document_learning_analysis'""", (child_id, key)).fetchone()
            if not unchanged:
                self._save(connection, child_id, [claim], source_id, occurred, now, False)
                connection.execute("""UPDATE companion_memory SET observation_kind=?,observation_json=?,confidence=?
                    WHERE child_id=? AND semantic_key=? AND value_digest=?""",
                    (observation_kind, json.dumps(focus, ensure_ascii=False), min(projection_confidence, .65 if review or focus else .85), child_id, key, digest))
            self._document_checkpoint(connection, revision_id, child_id, "observed", now)
            self._prune(connection, now)
            connection.commit()

    def _source(self, connection: sqlite3.Connection, child_id: str, session_id: str, transcript: str,
                action_id: str, kind: str, occurred: datetime, turn_id: str | None = None) -> str:
        source_id = str(uuid4())
        connection.execute("""INSERT INTO companion_memory_source
            (source_id,child_id,session_id,action_id,turn_id,request_digest,kind,observed,occurred_at)
            VALUES (?,?,?,?,?,?,?,?,?)""", (source_id, child_id, session_id, action_id, turn_id,
            self._digest(connection, child_id, transcript.strip()), kind, int(turn_id is not None), occurred.isoformat()))
        return source_id

    def _check_source(self, connection: sqlite3.Connection, source: sqlite3.Row,
                      child_id: str, session_id: str, transcript: str) -> None:
        if (source["child_id"] != child_id or source["session_id"] != session_id
                or source["request_digest"] != self._digest(connection, child_id, transcript.strip())):
            raise ValueError("companion_memory_action_conflict")

    @staticmethod
    def _audit(connection: sqlite3.Connection, child_id: str, memory_id: str, operation: str, now: datetime) -> None:
        connection.execute("INSERT INTO companion_memory_audit VALUES (?,?,?,?,?)",
                           (str(uuid4()), child_id, memory_id, operation, now.isoformat()))

    def _erase(self, connection: sqlite3.Connection, row: sqlite3.Row, status: str, now: datetime) -> None:
        if status == "forgotten" and row["semantic_key"]:
            connection.execute("""INSERT INTO companion_memory_suppression VALUES (?,?,?)
                ON CONFLICT(child_id,semantic_digest) DO UPDATE SET cutoff_at = MAX(cutoff_at,excluded.cutoff_at)""",
                (row["child_id"], row["semantic_key"], now.isoformat()))
        connection.execute("""UPDATE companion_memory_source SET tombstoned = 1 WHERE source_id IN
            (SELECT source_id FROM companion_memory_evidence WHERE memory_id = ?)""", (row["memory_id"],))
        connection.execute("UPDATE companion_memory_evidence SET evidence = NULL WHERE memory_id = ?", (row["memory_id"],))
        connection.execute("""UPDATE companion_memory SET content = NULL, semantic_key = NULL, value_digest = NULL,
            teaching_key = NULL, teaching_value = NULL, observation_json = NULL, status = ?, updated_at = ? WHERE memory_id = ?""",
            (status, now.isoformat(), row["memory_id"]))
        self._audit(connection, row["child_id"], row["memory_id"], status, now)

    def _prune(self, connection: sqlite3.Connection, now: datetime) -> None:
        for row in connection.execute("SELECT * FROM companion_memory WHERE teaching_value IS NOT NULL AND content IS NOT NULL").fetchall():
            if not any(claim.teaching_key == row["teaching_key"] and claim.teaching_value == row["teaching_value"]
                       for claim in _claims(row["content"])):
                connection.execute("UPDATE companion_memory SET teaching_key = NULL, teaching_value = NULL WHERE memory_id = ?", (row["memory_id"],))
        # Revoke records from the earlier permissive event rule, including on restart.
        for row in connection.execute("SELECT * FROM companion_memory WHERE category = 'event' AND observation_kind IS NULL AND content IS NOT NULL").fetchall():
            if not _is_event(row["content"]):
                self._erase(connection, row, "revoked", now)
        for row in connection.execute("SELECT * FROM companion_memory WHERE content IS NOT NULL AND expires_at <= ?", (now.isoformat(),)).fetchall():
            self._erase(connection, row, "expired", now)
        children = connection.execute("SELECT DISTINCT child_id FROM companion_memory WHERE content IS NOT NULL").fetchall()
        for child in children:
            for condition, cap in (("status = 'active'", MAX_ACTIVE), ("content IS NOT NULL", MAX_CONTENT_ROWS)):
                rows = connection.execute(f"""SELECT * FROM companion_memory WHERE child_id = ? AND {condition}
                    ORDER BY (status = 'active') DESC, importance DESC, last_seen_at DESC, rowid DESC
                    LIMIT -1 OFFSET ?""", (child[0], cap)).fetchall()
                for row in rows:
                    self._erase(connection, row, "expired", now)

    def _save(self, connection: sqlite3.Connection, child_id: str, claims: list[_Claim], source_id: str,
              occurred: datetime, now: datetime, explicit: bool, fresh_consent: bool = False) -> int:
        saved = 0
        for claim in claims:
            expiry = occurred + timedelta(days=RETENTION_DAYS[claim.category])
            if expiry <= now:
                continue
            key = self._digest(connection, child_id, claim.key)
            value = self._digest(connection, child_id, claim.value)
            if not fresh_consent and connection.execute("""SELECT 1 FROM companion_memory_suppression
                WHERE child_id = ? AND semantic_digest IN (?, '*') AND cutoff_at >= ?""",
                (child_id, key, occurred.isoformat())).fetchone():
                connection.execute("UPDATE companion_memory_source SET tombstoned = 1 WHERE source_id = ?", (source_id,))
                continue
            conflicts = connection.execute("""SELECT * FROM companion_memory WHERE child_id = ? AND status = 'active'
                AND (semantic_key = ? OR (? IS NOT NULL AND teaching_key = ? AND teaching_value IS NOT NULL))
                ORDER BY last_seen_at DESC, memory_id""", (child_id, key, claim.teaching_value, claim.teaching_key)).fetchall()
            old = conflicts[0] if conflicts else None
            if not explicit and connection.execute("""SELECT 1 FROM companion_memory
                WHERE child_id = ? AND semantic_key = ? AND status = 'revoked' AND updated_at >= ?""",
                (child_id, key, occurred.isoformat())).fetchone():
                continue
            memory_id = str(uuid4())
            status = "active"
            successor = None
            if old and old["value_digest"] == value:
                memory_id = old["memory_id"]
                if occurred.isoformat() > old["last_seen_at"]:
                    connection.execute("UPDATE companion_memory SET last_seen_at = ?, expires_at = ?, updated_at = ? WHERE memory_id = ?",
                                       (occurred.isoformat(), expiry.isoformat(), now.isoformat(), memory_id))
            else:
                if old:
                    if occurred.isoformat() < old["last_seen_at"]:
                        status, successor = "superseded", old["memory_id"]
                    else:
                        for conflict in conflicts:
                            connection.execute("UPDATE companion_memory SET status = 'superseded', superseded_by = ?, updated_at = ? WHERE memory_id = ?",
                                               (memory_id, now.isoformat(), conflict["memory_id"]))
                            self._audit(connection, child_id, conflict["memory_id"], "superseded", now)
                connection.execute("""INSERT INTO companion_memory
                    (memory_id,child_id,category,semantic_key,value_digest,content,status,confidence,importance,
                     occurred_at,last_seen_at,expires_at,updated_at,superseded_by,teaching_key,teaching_value,version) VALUES
                    (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    memory_id, child_id, claim.category, key, value, claim.content, status,
                    .9 if explicit else .7, .9 if explicit else (.7 if claim.category == "preference" else .5),
                    occurred.isoformat(), occurred.isoformat(), expiry.isoformat(), now.isoformat(), successor,
                    claim.teaching_key, claim.teaching_value, VERSION,
                ))
                self._audit(connection, child_id, memory_id, "created", now)
            connection.execute("INSERT OR IGNORE INTO companion_memory_evidence VALUES (?,?,?)", (memory_id, source_id, claim.evidence))
            # Keep source identifiers for replay prevention, but bound verbatim evidence.
            connection.execute("""UPDATE companion_memory_evidence SET evidence = NULL WHERE memory_id = ? AND source_id IN (
                SELECT e.source_id FROM companion_memory_evidence e JOIN companion_memory_source s USING(source_id)
                WHERE e.memory_id = ? AND e.evidence IS NOT NULL ORDER BY s.occurred_at DESC, s.source_id LIMIT -1 OFFSET ?)
                """, (memory_id, memory_id, MAX_SOURCES))
            saved += 1
        return saved

    @staticmethod
    def _tokens(query: str) -> set[str]:
        tokens: set[str] = set()
        for word in re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", query.lower()):
            if re.fullmatch(r"[\u4e00-\u9fff]+", word):
                tokens.update(word[index:index + 2] for index in range(max(1, len(word) - 1)))
            else:
                tokens.add(word)
        return tokens

    def recall(self, child_id: str, query: str, limit: int = 6) -> dict:
        """Local-only envelope; confidence describes a self-report, not its truth."""
        with self.database.connect() as connection:
            connection.execute("PRAGMA query_only = ON")
            owned = not connection.in_transaction
            if owned:
                connection.execute('BEGIN')
            try:
                now = _now()
                available = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='companion_memory'").fetchone()
                memories = self._recall(connection, child_id, query, min(MAX_ACTIVE, max(0, int(limit))), now) if available else []
            finally:
                if owned:
                    connection.rollback()
        return {"cloud_transmission_allowed": False, "scope": "local_only", "memories": memories}

    def _recall(self, connection: sqlite3.Connection, child_id: str, query: str, limit: int, now: datetime) -> list[dict]:
        query = re.sub(r"^你(?:还)?记得", "", query.strip()).rstrip("？?。！! ")
        query = re.sub(r"(?:吗|(?<!什)么)$", "", query)
        broad = _compact(query) in {"", "我", "我什么", "我些什么", "我的什么", "关于我的什么"}
        tokens = self._tokens(query)
        ranked = []
        revisit_items = None
        for row in connection.execute("SELECT * FROM companion_memory WHERE child_id = ? AND status = 'active' AND content IS NOT NULL AND expires_at > ?", (child_id, now.isoformat())).fetchall():
            if 'observation_kind' in row.keys() and row['observation_kind'] == 'document_revisit':
                from .document_revisit_memory import report_memory, memory_current
                if revisit_items is None:
                    revisit_items = report_memory(connection, child_id)['items']
                if not memory_current(connection, row, revisit_items):
                    continue
            overlap = len(tokens & self._tokens(row["content"]))
            if not broad and not overlap and _compact(query) not in _compact(row["content"]):
                continue
            age = max(0, (now - _timestamp(row["last_seen_at"])).total_seconds() / 86400)
            score = overlap * 2 + row["importance"] + 1 / (1 + age / 30)
            ranked.append((score, row["last_seen_at"], row["memory_id"], row))
        memories = []
        for _, _, _, row in sorted(ranked, reverse=True)[:limit]:
            sources = [dict(source) for source in connection.execute("""SELECT s.source_id, s.turn_id, s.action_id,
                s.session_id, s.kind, s.occurred_at, e.evidence FROM companion_memory_evidence e
                JOIN companion_memory_source s USING(source_id) WHERE e.memory_id = ? AND e.evidence IS NOT NULL
                ORDER BY s.occurred_at DESC, s.source_id""", (row["memory_id"],)).fetchall()]
            memories.append({key: row[key] for key in ("memory_id", "category", "content", "status", "confidence", "occurred_at", "last_seen_at", "expires_at", "version")})
            memories[-1].update(sources=sources, evidence_basis="child_self_report", verified_fact=False,
                                cloud_transmission_allowed=False, kind=row["category"],
                                source_turn_id=next((source["turn_id"] for source in sources if source["turn_id"]), None),
                                created_at=row["occurred_at"])
            if 'observation_kind' in row.keys() and row['observation_kind'] == 'document_revisit':
                memories[-1].update(kind='learning', category='learning_observation', evidence_basis='original_question_response',
                    observation_kind='document_revisit', verification_status='needs_verification',
                    mastery_update_allowed=False, behavior_inference_allowed=False)
            if "observation_kind" in row.keys() and row["observation_kind"] in {"document_revision", "document_learning_analysis"}:
                citations = json.loads(row["observation_json"] or "[]") if "observation_json" in row.keys() else []
                for source in sources:
                    document = connection.execute("SELECT revision_id,artifact_id,previous_revision_id,change_kind,review_required,artifact_status,ocr_confidence FROM companion_memory_document_source WHERE source_id=? AND child_id=?", (source["source_id"], child_id)).fetchone()
                    if document:
                        source.update(dict(document), kind=row["observation_kind"],
                                      citations=[entry for entry in citations if entry["revision_id"] == document["revision_id"]])
                memories[-1].update(kind="learning", category="learning_observation", evidence_basis="local_document_observation",
                                    observation_kind=row["observation_kind"], citations=citations, verification_status="needs_verification",
                                    mastery_update_allowed=False, behavior_inference_allowed=False, raw_image_stored=False)
        return memories

    def teaching_preferences(self, child_id: str) -> dict[str, str]:
        """Only explicit likes, mapped through a fixed allowlist; no default traits."""
        self._ready()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._prune(connection, _now())
            rows = connection.execute("SELECT teaching_key, teaching_value FROM companion_memory WHERE child_id = ? AND status = 'active' AND category = 'preference'", (child_id,)).fetchall()
            result = {row[0]: row[1] for row in rows if row[0] in TEACHING_VALUES and row[1] in TEACHING_VALUES[row[0]]}
            connection.commit()
            return result

    def handle_command(self, child_id: str, session_id: str, transcript: str, action_id: str) -> str | None:
        """Mutation receipts contain codes/digests, never transcripts or recalled text."""
        text = transcript.strip()
        if not self.is_command(text):
            return None
        self._ready()
        if not action_id or len(action_id) > 200:
            raise ValueError("companion_memory_invalid_action")
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._owner(connection, child_id, session_id)
            now = _now()
            self._prune(connection, now)
            prior = connection.execute("SELECT * FROM companion_memory_source WHERE action_id = ?", (action_id,)).fetchone()
            if prior:
                self._check_source(connection, prior, child_id, session_id, text)
                response = self._response(connection, "erased" if prior["tombstoned"] else prior["result_code"], child_id, text, now)
                connection.commit()
                return response
            study_review = bool(_STUDY_REVIEW.fullmatch(text) or _YESTERDAY_REVIEW.fullmatch(text))
            kind = ("recall" if study_review or re.match(r"^你(?:还)?记得", text) else "forget" if re.match(r"^(?:请)?(?:帮我)?忘记", text)
                    else "correct" if re.match(r"^(?:你)?记错了|^我现在不喜欢", text) else "remember")
            turn = connection.execute("SELECT * FROM agent_turn WHERE client_action_id = ?", (action_id,)).fetchone()
            if turn and (turn["child_id"] != child_id or turn["session_id"] != session_id or turn["transcript"].strip() != text):
                raise ValueError("companion_memory_action_conflict")
            if turn and (turn["tool_status"] == "failed" or (turn["tool_status"] == "completed" and not self._eligible(turn))):
                raise ValueError("companion_memory_ineligible_turn")
            source_id = self._source(connection, child_id, session_id, text, action_id, kind, now,
                                     turn["agent_turn_id"] if turn else None)
            code = "clarify"
            if len(text) <= 1000 and not _UNSAFE.search(text):
                if kind == "recall":
                    if _YESTERDAY_REVIEW.fullmatch(text):
                        # Pin the local date in the content-free receipt so an
                        # action replay after midnight cannot change "yesterday".
                        day = (now.astimezone(_HOME_TIMEZONE).date() - timedelta(days=1)).isoformat()
                        code = "study_review_day:" + day
                    else:
                        code = "study_review_last" if study_review else "recall"
                elif kind == "forget" and not _QUESTION.search(text):
                    target = re.sub(r"^(?:请)?(?:帮我)?忘记", "", text).strip(" ，,。.!！：:")
                    code = self._forget(connection, child_id, target, now)
                else:
                    body = re.sub(r"^(?:请)?(?:帮我)?记住(?:一下)?[，,：: ]*|^(?:你)?记错了[，,：: ]*", "", text)
                    # A change explicitly names both the withdrawn and new preference.
                    change = re.fullmatch(r"我现在不喜欢(.{1,60}?)[，, ]*(?:我(?:现在)?(?:更)?喜欢|(?:更)?喜欢)(.{1,60}?)[。!！]?", body)
                    claims = _claims(body)
                    if change and not _QUESTION.search(body):
                        claims = _claims("我不喜欢" + change[1] + "，我喜欢" + change[2])
                        claims = [_Claim(c.content, c.category, c.key, c.value, body, c.teaching_key, c.teaching_value) for c in claims]
                    correction = re.fullmatch(r"(.{1,100}?)(?:应该是|改成|改为)[，, ]*(.{1,100})", body) if kind == "correct" else None
                    if correction:
                        claims = _claims(correction[2])
                        targets = self._matching(connection, child_id, correction[1])
                        if len(targets) == 1 and claims:
                            connection.execute("UPDATE companion_memory SET status = 'revoked', updated_at = ? WHERE memory_id = ?", (now.isoformat(), targets[0]["memory_id"]))
                            self._audit(connection, child_id, targets[0]["memory_id"], "revoked", now)
                        else:
                            claims = []
                    occurred = _timestamp(turn["created_at"]) if turn else now
                    if claims and occurred and self._save(connection, child_id, claims, source_id, occurred, now, True, fresh_consent=turn is None):
                        code = "corrected" if kind == "correct" else "remembered"
            connection.execute("UPDATE companion_memory_source SET result_code = ? WHERE source_id = ?", (code, source_id))
            self._prune(connection, now)
            response = self._response(connection, code, child_id, text, now)
            connection.commit()
            return response

    @staticmethod
    def _matching(connection: sqlite3.Connection, child_id: str, target: str) -> list[sqlite3.Row]:
        target = _compact(target)
        if not target:
            return []
        return [row for row in connection.execute("SELECT * FROM companion_memory WHERE child_id = ? AND content IS NOT NULL", (child_id,)).fetchall()
                if target in _compact(row["content"])]

    def _forget(self, connection: sqlite3.Connection, child_id: str, target: str, now: datetime) -> str:
        all_memories = _compact(target) in {"全部", "所有", "所有记忆", "关于我的所有记忆", "你记得我的一切"}
        if not target:
            return "clarify"
        if all_memories:
            connection.execute("""INSERT INTO companion_memory_suppression VALUES (?, '*', ?)
                ON CONFLICT(child_id,semantic_digest) DO UPDATE SET cutoff_at=MAX(cutoff_at,excluded.cutoff_at)""",
                (child_id, now.isoformat()))
        rows = (connection.execute("SELECT * FROM companion_memory WHERE child_id = ? AND content IS NOT NULL", (child_id,)).fetchall()
                if all_memories else self._matching(connection, child_id, target))
        ids = {row["memory_id"] for row in rows}
        # A document focus replaces its baseline/review versions. Erase that
        # entire page lineage, including verbatim citations in old versions.
        document_keys = {row["semantic_key"] for row in rows if row["observation_kind"] and row["semantic_key"]}
        for key in document_keys:
            ids.update(row[0] for row in connection.execute(
                "SELECT memory_id FROM companion_memory WHERE child_id=? AND semantic_key=?", (child_id, key)))
        # Erase other fragments sharing verbatim evidence with a forgotten fact.
        # This prevents a compound correction's evidence retaining the old text.
        for row in connection.execute("""SELECT DISTINCT m.* FROM companion_memory m JOIN companion_memory_evidence e USING(memory_id)
            WHERE m.child_id = ? AND e.evidence IS NOT NULL""", (child_id,)).fetchall():
            evidence = connection.execute("SELECT evidence FROM companion_memory_evidence WHERE memory_id = ? AND evidence IS NOT NULL", (row["memory_id"],)).fetchall()
            if all_memories or any(_compact(target) in _compact(item[0]) for item in evidence):
                ids.add(row["memory_id"])
        erased_keys = set()
        for memory_id in ids:
            row = connection.execute("SELECT * FROM companion_memory WHERE memory_id = ? AND child_id = ?", (memory_id, child_id)).fetchone()
            if row["semantic_key"]:
                erased_keys.add(row["semantic_key"])
            self._erase(connection, row, "forgotten", now)
        # Only actual memory candidates become sources. A short deletion query
        # must not taint every unrelated history turn containing the same word.
        for turn in connection.execute("SELECT * FROM agent_turn WHERE child_id = ?", (child_id,)).fetchall():
            matching_keys = []
            for claim in _claims(turn["transcript"]):
                key = self._digest(connection, child_id, claim.key)
                if all_memories or key in erased_keys or _compact(target) in _compact(claim.content):
                    matching_keys.append(key)
            if not matching_keys:
                continue
            for key in matching_keys:
                connection.execute("""INSERT INTO companion_memory_suppression VALUES (?,?,?)
                    ON CONFLICT(child_id,semantic_digest) DO UPDATE SET cutoff_at=MAX(cutoff_at,excluded.cutoff_at)""",
                    (child_id, key, now.isoformat()))
            source = connection.execute("SELECT source_id FROM companion_memory_source WHERE action_id = ?", (turn["client_action_id"],)).fetchone()
            source_id = source[0] if source else self._source(connection, child_id, turn["session_id"], turn["transcript"],
                turn["client_action_id"], "turn", now, turn["agent_turn_id"])
            connection.execute("UPDATE companion_memory_source SET tombstoned = 1 WHERE source_id = ?", (source_id,))
        return "forgotten" if ids else "not_found"

    def forget(self, child_id: str, memory_id: str, action_id: str) -> dict:
        """ID-based erasure for an authenticated parent API; action IDs are global."""
        self._ready()
        if not action_id or len(action_id) > 200:
            raise ValueError("companion_memory_invalid_action")
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM companion_memory WHERE memory_id = ? AND child_id = ?", (memory_id, child_id)).fetchone()
            if not row:
                raise ValueError("companion_memory_unknown_entry")
            prior = connection.execute("SELECT * FROM companion_memory_source WHERE action_id = ?", (action_id,)).fetchone()
            if prior:
                self._check_source(connection, prior, child_id, "", "forget:" + memory_id)
            else:
                now = _now()
                source_id = self._source(connection, child_id, "", "forget:" + memory_id, action_id, "forget", now)
                if row["content"]:
                    self._forget(connection, child_id, row["content"], now)
                if row["status"] != "forgotten":
                    current = connection.execute("SELECT * FROM companion_memory WHERE memory_id = ?", (memory_id,)).fetchone()
                    if current["status"] != "forgotten":
                        self._erase(connection, current, "forgotten", now)
                connection.execute("UPDATE companion_memory_source SET result_code = 'forgotten' WHERE source_id = ?", (source_id,))
            connection.commit()
        return {"cloud_transmission_allowed": False, "memory_id": memory_id, "status": "forgotten"}

    def _study_review_response(self, connection: sqlite3.Connection, child_id: str, day: str | None, now: datetime, selected=None) -> str:
        rows = connection.execute("""SELECT m.*,s.session_id,s.occurred_at AS source_occurred_at,s.rowid AS source_order
            FROM companion_memory m JOIN companion_memory_evidence e USING(memory_id)
            JOIN companion_memory_source s USING(source_id)
            LEFT JOIN companion_memory_document_source d USING(source_id)
            WHERE m.child_id=? AND s.child_id=m.child_id
              AND m.status='active' AND m.content IS NOT NULL AND m.expires_at>?
              AND ((m.observation_kind IN ('document_revision','document_learning_analysis') AND d.child_id=m.child_id)
                   OR m.observation_kind='document_revisit')
              AND e.evidence IS NOT NULL AND s.tombstoned=0""", (child_id, now.isoformat())).fetchall()
        dated = []
        revisit_items = None
        for row in rows:
            if row['observation_kind'] == 'document_revisit':
                from .document_revisit_memory import report_memory, memory_current
                if revisit_items is None:
                    revisit_items = report_memory(connection, child_id)['items']
                if not memory_current(connection, row, revisit_items):
                    continue
            occurred = _timestamp(row["source_occurred_at"])
            if not occurred or occurred > now:
                continue
            local = occurred.astimezone(_HOME_TIMEZONE)
            if day is None or local.date().isoformat() == day:
                dated.append((occurred, row["memory_id"], row, local, row['source_order']))
        dated.sort(key=lambda item: (item[0], item[4], item[1]), reverse=True)
        if not dated:
            return (f"本机暂无北京时间{day}的有效学习观察，不用其他日期的记录代替。这不表示你那天没有学习。" if day else
                    "本机暂无可回顾的有效学习观察，这不表示你没有学习。")
        latest_session = dated[0][2]["session_id"]
        seen, fragments = set(), []
        has_revisit = False
        for _, memory_id, row, local, _ in dated:
            if memory_id in seen or (day is None and row["session_id"] != latest_session):
                continue
            seen.add(memory_id)
            if selected is not None:
                selected.append(dict(row))
            has_revisit = has_revisit or row['observation_kind'] == 'document_revisit'
            fragments.append(local.strftime("%Y-%m-%d %H:%M") + "（北京时间）：" + row["content"])
            if len(fragments) == 3:
                break
        limitation = ("。这些作答、求助和教学建议仍需核对，不表示已掌握或确定错因。" if has_revisit else
            "。这些页面记录仍待复核，不能证明你已掌握、完成作业或确定错因。")
        return ("本机最近一次有记录的学习观察：" if day is None else f"本机{day}的学习观察：") + "；".join(fragments) + limitation

    def _response(self, connection: sqlite3.Connection, code: str | None, child_id: str, text: str, now: datetime) -> str:
        if code == "study_review_last" or (code and code.startswith("study_review_day:")):
            return self._study_review_response(connection, child_id, code.split(":", 1)[1] if ":" in code else None, now)
        if code == "recall":
            memories = self._recall(connection, child_id, text, 6, now)
            if not memories:
                return "我在本机没有找到相关的有效记忆。"
            if any(item["evidence_basis"] == "local_document_observation" for item in memories):
                return "本机记录包括：" + "；".join(item["content"] for item in memories) + "。页面观察不代表你的自述，也不证明掌握程度或人格。"
            return "你曾告诉我：" + "；".join(item["content"] for item in memories) + "。这些只是你的自述，不代表能力或人格结论。"
        return {
            "remembered": "已经保存在本机，作为你告诉我的偏好或经历，不会当作掌握或人格事实。",
            "corrected": "已经按你的说法修正本机记忆，旧说法不再作为当前记忆。",
            "forgotten": "已清除相关本机记忆内容，旧来源重放不会重新记下；原始会话记录和备份由隐私清理单独管理。",
            "not_found": "本机没有找到需要忘记的相关记忆。",
            "erased": "这条旧操作涉及的记忆已经清除，不会重新保存。",
        }.get(code, "请明确说出你自己的偏好、经历，或需要忘记、修正的具体内容；引用和不确定的话不会记成事实。")
