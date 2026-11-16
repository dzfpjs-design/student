from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


ACTIVE_STATUSES = ("listening", "awaiting_confirmation")
MAX_QUESTION_CHARACTERS = 1800


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _compact(text: str) -> str:
    return re.sub(r"[\s，。！？!?\u3001；;：:,.]", "", text).lower()


class SpokenQuestionIntake:
    """Durable, local dialogue state for a child dictating one question.

    The realtime model supplies speech recognition only. This state machine owns
    the multi-turn meaning of "listen while I read the question", so each ASR
    final is not independently re-planned as a camera command.
    """

    def __init__(self, database: Any):
        self.database = database

    def initialize(self) -> None:
        with self.database.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS spoken_question_intake (
                  intake_id TEXT PRIMARY KEY,
                  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                  session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
                  status TEXT NOT NULL CHECK(status IN (
                    'listening','awaiting_confirmation','consumed','cancelled'
                  )),
                  subject TEXT,
                  grade_label TEXT,
                  question_text TEXT NOT NULL DEFAULT '',
                  camera_allowed INTEGER NOT NULL DEFAULT 0 CHECK(camera_allowed IN (0,1)),
                  revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
                  started_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  confirmed_at TEXT,
                  consumed_at TEXT,
                  cancelled_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_spoken_question_one_active
                ON spoken_question_intake(session_id, child_id)
                WHERE status IN ('listening','awaiting_confirmation');
                CREATE INDEX IF NOT EXISTS idx_spoken_question_history
                ON spoken_question_intake(child_id, updated_at DESC);
                CREATE TABLE IF NOT EXISTS spoken_question_intake_event (
                  event_id TEXT PRIMARY KEY,
                  intake_id TEXT NOT NULL REFERENCES spoken_question_intake(intake_id) ON DELETE CASCADE,
                  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                  session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
                  client_action_id TEXT NOT NULL UNIQUE,
                  event_type TEXT NOT NULL,
                  transcript TEXT NOT NULL,
                  result_json TEXT NOT NULL,
                  agent_turn_id TEXT REFERENCES agent_turn(agent_turn_id) ON DELETE SET NULL,
                  created_at TEXT NOT NULL
                );
                """
            )
            connection.commit()

    @staticmethod
    def requests_spoken_intake(text: str) -> bool:
        compact = _compact(text)
        if not compact or any(marker in compact for marker in (
            "不要听我说题", "别听我说题", "不用听我说题",
        )):
            return False
        if ("为什么" in compact or "怎么" in compact) and "听我" in compact:
            return False
        if any(marker in compact for marker in ("刚才没听我", "之前没听我", "没有听我")):
            return False
        patterns = (
            r"我(?:来|要|想)?口述(?:一下)?(?:这道|这个|一道|一个|新)?(?:数学|语文|英语|物理|化学|生物|地理|历史|道德与法治)?题(?:目)?",
            r"听我口述(?:一下)?(?:这道|这个|一道|一个|新)?(?:数学|语文|英语|物理|化学|生物|地理|历史|道德与法治)?题(?:目)?",
            r"听我(?:把)?(?:这道|这个|一道|一个|新)?题(?:目)?(?:说|读|念|讲)?",
            r"听我(?:说|读|念)(?:一下)?(?:这道|这个|一道|一个|新)?题",
            r"我(?:来|要|想)?(?:说|读|念)(?:一下)?(?:这道|这个|一道|一个|新)?题",
            r"你听我把.{0,12}题(?:目)?(?:说|读|念)(?:一下)?",
        )
        return any(re.search(pattern, compact) for pattern in patterns)

    @staticmethod
    def requests_missing_context_repair(text: str) -> bool:
        compact = _compact(text)
        correction = any(marker in compact for marker in (
            "你没有理解", "你没理解", "你理解错", "你听错",
            "题目是让你", "不是让你", "我说的是",
        ))
        missing_reference = any(marker in compact for marker in (
            "前面的", "上面的", "题目前面", "图里的", "图中的",
            "根据前面", "根据上面", "这些等式", "上述条件",
        ))
        return correction and missing_reference

    @staticmethod
    def _active(connection: Any, child_id: str, session_id: str) -> dict[str, Any] | None:
        row = connection.execute(
            """SELECT * FROM spoken_question_intake
               WHERE child_id=? AND session_id=? AND status IN ('listening','awaiting_confirmation')
               ORDER BY rowid DESC LIMIT 1""",
            (child_id, session_id),
        ).fetchone()
        return dict(row) if row else None

    def should_route(self, connection: Any, child_id: str, session_id: str, text: str) -> bool:
        return bool(
            self._active(connection, child_id, session_id)
            or self.requests_spoken_intake(text)
            or self.requests_missing_context_repair(text)
        )

    @staticmethod
    def _metadata(text: str) -> tuple[str | None, str | None]:
        compact = _compact(text)
        subject = next((item for item in (
            "数学", "语文", "英语", "物理", "化学", "生物", "地理", "历史", "道德与法治",
        ) if item in compact), None)
        grade_match = re.search(
            r"(?:小学)?[一二三四五六]年级|(?:初中)?[七八九]年级|初[一二三]",
            compact,
        )
        return subject, grade_match.group(0) if grade_match else None

    @classmethod
    def _metadata_only(cls, text: str) -> bool:
        subject, grade = cls._metadata(text)
        if not subject and not grade:
            return False
        residual = _compact(text)
        for marker in (subject, grade, "这道题", "这个题", "是个", "是一道", "的", "小学", "初中"):
            if marker:
                residual = residual.replace(marker, "")
        return len(residual) <= 4

    @staticmethod
    def _cancel_requested(text: str) -> bool:
        compact = _compact(text)
        return bool(re.fullmatch(
            r"(?:先)?(?:不说了|不读了|不念了|取消(?:这道)?题|这道题不讲了|算了)(?:先这样)?",
            compact,
        ))

    @staticmethod
    def _restart_requested(text: str) -> bool:
        compact = _compact(text)
        return bool(re.fullmatch(
            r"(?:不对)?(?:我)?(?:重新说|重新读|重新念|重来|我重说|从头来)(?:一遍)?",
            compact,
        ))

    @staticmethod
    def _not_finished(text: str) -> bool:
        compact = _compact(text)
        return any(marker in compact for marker in (
            "还没说完", "我还没说完", "还没读完", "还没念完", "后面还有", "等一下",
        ))

    @staticmethod
    def _confirmation(text: str) -> bool:
        compact = _compact(text)
        return bool(re.fullmatch(
            r"(?:对|对的|是|是的|没错|就是|完整了|说完了|读完了|念完了|可以讲了|对请讲|对帮我讲一下|对帮我解一下)",
            compact,
        ))

    @staticmethod
    def _finished_marker(text: str) -> bool:
        compact = _compact(text)
        return any(marker in compact for marker in (
            "题目说完了", "题目读完了", "题目念完了", "我说完了", "我读完了", "我念完了",
        ))

    @staticmethod
    def _looks_complete(text: str) -> bool:
        compact = _compact(text)
        return bool(
            text.rstrip().endswith(("？", "?"))
            or re.search(r"(?:是多少|等于多少|有几个|是什么|为什么|怎么做|怎样解|求.{0,20}(?:值|长度|面积|角度))$", compact)
        )

    @staticmethod
    def _start_remainder(text: str) -> str:
        match = re.search(r"(?:题目|这道题|题)(?:是|：|:)", text)
        if match and match.end() < len(text):
            return text[match.end():].strip(" ，。：:")
        return ""

    @staticmethod
    def _readback(question: str) -> str:
        if len(question) <= 390:
            return f"我听到的完整题目是：{question}。我听对了吗？"
        return (
            f"我已完整记下这道较长的题，共{len(question)}个字。"
            f"开头是“{question[:160]}”，结尾是“{question[-100:]}”。我听对了吗？"
        )

    @staticmethod
    def _result(intake: dict[str, Any], event_type: str, response: str, *, explain: bool = False) -> dict[str, Any]:
        summary = {
            "intake_id": intake["intake_id"],
            "status": intake["status"],
            "revision": intake["revision"],
            "subject": intake.get("subject"),
            "grade_label": intake.get("grade_label"),
            "question_character_count": len(intake.get("question_text") or ""),
            "camera_allowed": False,
        }
        action: dict[str, Any] = {
            "type": "ask_tutor" if explain else "none",
            "source": "spoken_question_intake",
            "spoken_question_intake": summary,
        }
        if explain:
            action.update(
                question=intake["question_text"],
                subject=intake.get("subject") or "未知学科",
                hint_level=6,
                cloud_transmission_allowed=True,
            )
        else:
            action.update(local_only=True, cloud_transmission_allowed=False)
        return {
            "event_type": event_type,
            "intent": "learning.explain" if explain else "spoken_question_intake",
            "response": response,
            "action": action,
            "intake": summary,
            "requires_child_reply": not explain,
        }

    @staticmethod
    def _insert_event(
        connection: Any,
        *,
        intake: dict[str, Any],
        client_action_id: str,
        transcript: str,
        result: dict[str, Any],
    ) -> None:
        connection.execute(
            """INSERT INTO spoken_question_intake_event(
                 event_id,intake_id,child_id,session_id,client_action_id,event_type,
                 transcript,result_json,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                str(uuid4()), intake["intake_id"], intake["child_id"], intake["session_id"],
                client_action_id, result["event_type"], transcript,
                json.dumps(result, ensure_ascii=False), _now(),
            ),
        )

    def handle(
        self,
        connection: Any,
        *,
        child_id: str,
        session_id: str,
        transcript: str,
        client_action_id: str,
    ) -> dict[str, Any] | None:
        replay = connection.execute(
            "SELECT result_json FROM spoken_question_intake_event WHERE client_action_id=?",
            (client_action_id,),
        ).fetchone()
        if replay:
            return json.loads(replay[0])

        active = self._active(connection, child_id, session_id)
        missing_context_repair = self.requests_missing_context_repair(transcript)
        start = self.requests_spoken_intake(transcript) or missing_context_repair
        if not active and not start:
            return None

        now = _now()
        if start:
            if active:
                connection.execute(
                    "UPDATE spoken_question_intake SET status='cancelled',cancelled_at=?,updated_at=?,revision=revision+1 WHERE intake_id=?",
                    (now, now, active["intake_id"]),
                )
            intake_id = str(uuid4())
            subject, grade = self._metadata(transcript)
            remainder = "" if missing_context_repair else self._start_remainder(transcript)
            connection.execute(
                """INSERT INTO spoken_question_intake(
                     intake_id,child_id,session_id,status,subject,grade_label,question_text,
                     camera_allowed,revision,started_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,0,1,?,?)""",
                (intake_id, child_id, session_id, "listening", subject, grade, remainder, now, now),
            )
            active = dict(connection.execute(
                "SELECT * FROM spoken_question_intake WHERE intake_id=?", (intake_id,),
            ).fetchone())
            if remainder and self._looks_complete(remainder):
                connection.execute(
                    "UPDATE spoken_question_intake SET status='awaiting_confirmation',updated_at=?,revision=revision+1 WHERE intake_id=?",
                    (now, intake_id),
                )
                active = dict(connection.execute("SELECT * FROM spoken_question_intake WHERE intake_id=?", (intake_id,)).fetchone())
                result = self._result(active, "question_candidate_completed", self._readback(remainder))
            elif missing_context_repair:
                result = self._result(
                    active, "missing_context_repair_started",
                    "我先停下刚才的讲解。你提到了前面的等式或图中条件，但我当前记到的题干里没有这些内容。请从第一条条件开始完整口述；说完后我会复述确认，确认前不会继续解题，也不会启动高拍仪。",
                )
            else:
                result = self._result(
                    active, "intake_started",
                    "好，我现在只听你口述，不会启动高拍仪。你可以分段说，说完后我会复述整道题请你确认。",
                )
            self._insert_event(connection, intake=active, client_action_id=client_action_id, transcript=transcript, result=result)
            return result

        assert active is not None
        if self._cancel_requested(transcript):
            connection.execute(
                "UPDATE spoken_question_intake SET status='cancelled',cancelled_at=?,updated_at=?,revision=revision+1 WHERE intake_id=?",
                (now, now, active["intake_id"]),
            )
            active = dict(connection.execute("SELECT * FROM spoken_question_intake WHERE intake_id=?", (active["intake_id"],)).fetchone())
            result = self._result(active, "intake_cancelled", "好，这次口述题目已取消，没有启动高拍仪，也没有开始讲解。")
        elif self._restart_requested(transcript):
            connection.execute(
                """UPDATE spoken_question_intake SET status='listening',question_text='',
                   updated_at=?,revision=revision+1 WHERE intake_id=?""",
                (now, active["intake_id"]),
            )
            active = dict(connection.execute("SELECT * FROM spoken_question_intake WHERE intake_id=?", (active["intake_id"],)).fetchone())
            result = self._result(active, "intake_restarted", "好，刚才的题干不作数。你从头重新说，年级和学科信息我先保留。")
        elif active["status"] == "awaiting_confirmation":
            if self._confirmation(transcript):
                connection.execute(
                    """UPDATE spoken_question_intake SET status='consumed',confirmed_at=?,consumed_at=?,
                       updated_at=?,revision=revision+1 WHERE intake_id=?""",
                    (now, now, now, active["intake_id"]),
                )
                active = dict(connection.execute("SELECT * FROM spoken_question_intake WHERE intake_id=?", (active["intake_id"],)).fetchone())
                result = self._result(active, "question_confirmed", "好，题干已确认。我现在按这道题讲，同步把关键步骤写到黑板上。", explain=True)
            elif self._not_finished(transcript):
                connection.execute(
                    "UPDATE spoken_question_intake SET status='listening',updated_at=?,revision=revision+1 WHERE intake_id=?",
                    (now, active["intake_id"]),
                )
                active = dict(connection.execute("SELECT * FROM spoken_question_intake WHERE intake_id=?", (active["intake_id"],)).fetchone())
                result = self._result(active, "intake_continued", "好，我还在听，刚才的内容已保留。你继续说后面的题干。")
            else:
                result = self._result(active, "confirmation_unclear", "我还没有改题干。如果我听对了，说“对”；如果还没说完，说“还没说完”；要重来就说“重新说”。")
        else:
            subject, grade = self._metadata(transcript)
            if self._metadata_only(transcript):
                connection.execute(
                    """UPDATE spoken_question_intake SET subject=COALESCE(?,subject),
                       grade_label=COALESCE(?,grade_label),updated_at=?,revision=revision+1
                       WHERE intake_id=?""",
                    (subject, grade, now, active["intake_id"]),
                )
                active = dict(connection.execute("SELECT * FROM spoken_question_intake WHERE intake_id=?", (active["intake_id"],)).fetchone())
                details = "、".join(item for item in (active.get("grade_label"), active.get("subject")) if item)
                result = self._result(active, "metadata_recorded", f"记下了，这是{details}题。你继续说题干，我还在听。")
            elif self._finished_marker(transcript):
                if not active["question_text"].strip():
                    result = self._result(active, "empty_question", "我还没记到题干内容。请从题目第一句开始说，我会继续听。")
                else:
                    connection.execute(
                        "UPDATE spoken_question_intake SET status='awaiting_confirmation',updated_at=?,revision=revision+1 WHERE intake_id=?",
                        (now, active["intake_id"]),
                    )
                    active = dict(connection.execute("SELECT * FROM spoken_question_intake WHERE intake_id=?", (active["intake_id"],)).fetchone())
                    result = self._result(active, "question_candidate_completed", self._readback(active["question_text"]))
            else:
                old = active["question_text"].strip()
                combined = f"{old}{'' if not old or old.endswith(('，', '。', '；', '：', '？')) else '，'}{transcript.strip()}"
                if len(combined) > MAX_QUESTION_CHARACTERS:
                    result = self._result(
                        active, "question_too_long",
                        f"这道题已记了{len(old)}个字，再加这段会超过当前可靠处理范围。我没有丢掉前面内容。请说“重新说”后精简题干，或改用高拍仪。",
                    )
                else:
                    next_status = "awaiting_confirmation" if self._looks_complete(transcript) else "listening"
                    connection.execute(
                        """UPDATE spoken_question_intake SET question_text=?,subject=COALESCE(?,subject),
                           grade_label=COALESCE(?,grade_label),status=?,updated_at=?,revision=revision+1
                           WHERE intake_id=?""",
                        (combined, subject, grade, next_status, now, active["intake_id"]),
                    )
                    active = dict(connection.execute("SELECT * FROM spoken_question_intake WHERE intake_id=?", (active["intake_id"],)).fetchone())
                    if next_status == "awaiting_confirmation":
                        result = self._result(active, "question_candidate_completed", self._readback(combined))
                    else:
                        result = self._result(active, "question_chunk_recorded", "这一段已记下，你继续说。说完可以说“题目说完了”。")

        self._insert_event(connection, intake=active, client_action_id=client_action_id, transcript=transcript, result=result)
        return result

    @staticmethod
    def bind_agent_turn(connection: Any, client_action_id: str, agent_turn_id: str) -> None:
        connection.execute(
            "UPDATE spoken_question_intake_event SET agent_turn_id=? WHERE client_action_id=? AND agent_turn_id IS NULL",
            (agent_turn_id, client_action_id),
        )
