from __future__ import annotations

from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from .database import Database
from .controlled_content import (_ControlledBudgetHistoryError, _daily_budget_minutes, _select_approved_query,
    _selection_choice, _attach_selection_offer, _require_selection_binding, _recorded_selection_binding)


class ControlledGameError(ValueError):
    pass


class ControlledGameService:
    """Durable allowlist and time-budget state machine for local Windows games."""

    def __init__(self, database: Database):
        self.database = database
        with self.database.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS controlled_game_rule (
                  child_id TEXT PRIMARY KEY REFERENCES child_profile(child_id) ON DELETE CASCADE,
                  enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0, 1)),
                  max_daily_minutes INTEGER NOT NULL DEFAULT 20,
                  max_item_minutes INTEGER NOT NULL DEFAULT 15,
                  updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS controlled_game_approval (
                  approval_id TEXT PRIMARY KEY,
                  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                  title TEXT NOT NULL,
                  executable_path TEXT NOT NULL,
                  status TEXT NOT NULL CHECK(status IN ('approved', 'revoked')),
                  max_minutes INTEGER NOT NULL,
                  note TEXT,
                  file_size INTEGER NOT NULL,
                  file_mtime_ns INTEGER NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  UNIQUE(child_id, executable_path)
                );
                CREATE TABLE IF NOT EXISTS controlled_game_session (
                  game_session_id TEXT PRIMARY KEY,
                  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                  session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
                  approval_id TEXT NOT NULL REFERENCES controlled_game_approval(approval_id),
                  title TEXT NOT NULL,
                  executable_path TEXT NOT NULL,
                  status TEXT NOT NULL CHECK(status IN (
                    'active', 'completed', 'stopped', 'expired', 'interrupted', 'failed'
                  )),
                  allowed_minutes INTEGER NOT NULL,
                  actual_seconds INTEGER NOT NULL DEFAULT 0,
                  paused_plan_item_id TEXT REFERENCES plan_item(plan_item_id),
                  os_process_id INTEGER,
                  started_at TEXT NOT NULL,
                  ends_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  ended_at TEXT,
                  end_reason TEXT,
                  client_action_id TEXT NOT NULL UNIQUE
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_controlled_game_active_session
                  ON controlled_game_session(session_id) WHERE status = 'active';
                CREATE INDEX IF NOT EXISTS idx_controlled_game_child_started
                  ON controlled_game_session(child_id, started_at DESC);
                """
            )
            connection.commit()

        # Prepare the outcome schema before any caller-owned start transaction.
        from .companion_activity_outcome import CompanionActivityOutcomeService
        self._outcomes = CompanionActivityOutcomeService(self.database)

    @contextmanager
    def _start_transaction(self, connection=None):
        if connection is not None:
            if not isinstance(connection, sqlite3.Connection) or not connection.in_transaction:
                raise ControlledGameError('CONTROLLED_GAME_TRANSACTION_REQUIRED')
            main = next((row[2] for row in connection.execute('PRAGMA database_list') if row[1] == 'main'), None)
            if not main or Path(main).resolve() != self.database.path.resolve():
                raise ControlledGameError('CONTROLLED_GAME_TRANSACTION_OWNER_MISMATCH')
            yield connection
            return
        with self.database.connect() as owned:
            owned.execute('BEGIN IMMEDIATE')
            try:
                yield owned
                owned.commit()
            except BaseException:
                owned.rollback()
                raise

    def recorded_start(self, session_id: str, query: str, client_action_id: str, *, connection, selection_binding=None) -> dict | None:
        """An exact historical reservation is not authority to relaunch its executable."""
        with self._start_transaction(connection) as c:
            row = c.execute('SELECT * FROM controlled_game_session WHERE client_action_id=?',
                            (client_action_id,)).fetchone()
            if row is None:
                return None
            owner = c.execute('SELECT child_id FROM daily_session WHERE session_id=?', (session_id,)).fetchone()
            if row['session_id'] != session_id or owner is None or owner['child_id'] != row['child_id']:
                raise ControlledGameError('CONTROLLED_GAME_START_IDEMPOTENCY_CONFLICT')
            original = c.execute('''SELECT intent FROM companion_activity_outcome
                WHERE source_kind='controlled_game' AND source_id=? AND child_id=? AND session_id=?''',
                (row['game_session_id'], row['child_id'], session_id)).fetchone()
            if original is None or original['intent'] != 'controlled_game:' + query:
                raise ControlledGameError('CONTROLLED_GAME_START_IDEMPOTENCY_CONFLICT')
            saved_binding = _recorded_selection_binding(c, 'controlled_game', row, selection_binding, ControlledGameError)
            return {**_session_dict(row), **saved_binding, 'replayed': True, 'launch_allowed': False,
                    'recorded_receipt': {'game_session_id': row['game_session_id'],
                                         'client_action_id': client_action_id},
                    'local_only': True, 'cloud_transmission_allowed': False}

    def rule(self, child_id: str) -> dict:
        now = _now()
        with self.database.connect() as connection:
            self._require_child(connection, child_id)
            connection.execute(
                """
                INSERT INTO controlled_game_rule(child_id, enabled, max_daily_minutes, max_item_minutes, updated_at)
                VALUES (?, 0, 20, 15, ?) ON CONFLICT(child_id) DO NOTHING
                """,
                (child_id, now),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM controlled_game_rule WHERE child_id=?", (child_id,)
            ).fetchone()
        return _rule_dict(row)

    def update_rule(
        self, child_id: str, *, enabled: bool, max_daily_minutes: int, max_item_minutes: int
    ) -> dict:
        if not 0 <= max_daily_minutes <= 120 or not 1 <= max_item_minutes <= 60:
            raise ControlledGameError("受控游戏时长设置无效。")
        if enabled and max_daily_minutes < 1:
            raise ControlledGameError("启用受控游戏时，每日时长必须大于零。")
        now = _now()
        with self.database.connect() as connection:
            self._require_child(connection, child_id)
            connection.execute(
                """
                INSERT INTO controlled_game_rule(child_id, enabled, max_daily_minutes, max_item_minutes, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(child_id) DO UPDATE SET enabled=excluded.enabled,
                  max_daily_minutes=excluded.max_daily_minutes,
                  max_item_minutes=excluded.max_item_minutes, updated_at=excluded.updated_at
                """,
                (child_id, int(enabled), max_daily_minutes, max_item_minutes, now),
            )
            self._audit(connection, "parent", "controlled_game_rule_updated", child_id, {
                "enabled": enabled,
                "max_daily_minutes": max_daily_minutes,
                "max_item_minutes": max_item_minutes,
            }, now)
            connection.commit()
        return self.rule(child_id)

    def approvals(self, child_id: str, *, include_revoked: bool = True) -> list[dict]:
        with self.database.connect() as connection:
            self._require_child(connection, child_id)
            where = "child_id=?" if include_revoked else "child_id=? AND status='approved'"
            rows = connection.execute(
                f"SELECT * FROM controlled_game_approval WHERE {where} ORDER BY status, title",
                (child_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def approve(
        self, child_id: str, title: str, executable_path: str, max_minutes: int, note: str = ""
    ) -> dict:
        title = title.strip()
        if not title or len(title) > 120:
            raise ControlledGameError("游戏名称必须在1到120个字符之间。")
        if not 1 <= max_minutes <= 60:
            raise ControlledGameError("单次游戏时长必须在1到60分钟之间。")
        path, file_size, file_mtime_ns = _validated_executable(executable_path)
        now = _now()
        with self.database.connect() as connection:
            self._require_child(connection, child_id)
            existing = connection.execute(
                "SELECT approval_id, created_at FROM controlled_game_approval WHERE child_id=? AND executable_path=?",
                (child_id, str(path)),
            ).fetchone()
            approval_id = existing["approval_id"] if existing else str(uuid4())
            created_at = existing["created_at"] if existing else now
            connection.execute(
                """
                INSERT INTO controlled_game_approval(
                  approval_id, child_id, title, executable_path, status, max_minutes, note,
                  file_size, file_mtime_ns, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'approved', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(child_id, executable_path) DO UPDATE SET title=excluded.title,
                  status='approved', max_minutes=excluded.max_minutes, note=excluded.note,
                  file_size=excluded.file_size, file_mtime_ns=excluded.file_mtime_ns,
                  updated_at=excluded.updated_at
                """,
                (
                    approval_id, child_id, title, str(path), max_minutes, note.strip()[:500] or None,
                    file_size, file_mtime_ns, created_at, now,
                ),
            )
            self._audit(connection, "parent", "controlled_game_approved", child_id, {
                "approval_id": approval_id, "title": title, "max_minutes": max_minutes,
            }, now)
            connection.commit()
            row = connection.execute(
                "SELECT * FROM controlled_game_approval WHERE approval_id=?", (approval_id,)
            ).fetchone()
        return dict(row)

    def revoke(self, child_id: str, approval_id: str) -> dict:
        now = _now()
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM controlled_game_approval WHERE approval_id=? AND child_id=?",
                (approval_id, child_id),
            ).fetchone()
            if not row:
                raise ControlledGameError("找不到这项游戏许可。")
            active = connection.execute(
                "SELECT 1 FROM controlled_game_session WHERE approval_id=? AND status='active'",
                (approval_id,),
            ).fetchone()
            if active:
                raise ControlledGameError("游戏正在运行，请先结束本次游戏再撤销许可。")
            connection.execute(
                "UPDATE controlled_game_approval SET status='revoked', updated_at=? WHERE approval_id=?",
                (now, approval_id),
            )
            self._audit(connection, "parent", "controlled_game_revoked", child_id, {
                "approval_id": approval_id, "title": row["title"],
            }, now)
            connection.commit()
        return {"approval_id": approval_id, "status": "revoked"}

    def start(
        self, session_id: str, query: str, client_action_id: str,
        paused_plan_item_id: str | None = None,
        *, connection=None, selection_binding=None,
    ) -> dict:
        now = datetime.now(timezone.utc)
        with self._start_transaction(connection) as connection:

            def register(row):
                self._outcomes.open(row['child_id'], source_kind='controlled_game', source_id=row['game_session_id'],
                    intent='controlled_game:' + query,
                    success_description='记录实际参与和感受；活动结束不代表执行成功或已返回学习。',
                    client_action_id='controlled-game-outcome:' + row['game_session_id'], connection=connection)

            replay = self.recorded_start(session_id, query, client_action_id, connection=connection, selection_binding=selection_binding)
            if replay:
                if replay['session_id'] != session_id or replay['paused_plan_item_id'] != paused_plan_item_id:
                    raise ControlledGameError('CONTROLLED_GAME_START_IDEMPOTENCY_CONFLICT')
                return replay
            session = connection.execute(
                "SELECT child_id FROM daily_session WHERE session_id=? AND status='active'", (session_id,)
            ).fetchone()
            if not session:
                raise ControlledGameError("当前没有可以开始游戏的学习会话。")
            child_id = session["child_id"]
            if connection.execute(
                "SELECT 1 FROM controlled_game_session WHERE session_id=? AND status='active'", (session_id,)
            ).fetchone():
                raise ControlledGameError("已有游戏正在运行，请先说停止游戏。")
            rule = connection.execute(
                "SELECT * FROM controlled_game_rule WHERE child_id=?", (child_id,)
            ).fetchone()
            if not rule or not rule["enabled"]:
                raise ControlledGameError("家长尚未启用受控游戏。")
            try:
                remaining_daily = _daily_budget_minutes(connection, 'controlled_game_session',
                    child_id, now, int(rule['max_daily_minutes']))
            except _ControlledBudgetHistoryError:
                raise ControlledGameError('历史游戏额度暂时无法核对，请家长检查后再开启。') from None
            if remaining_daily <= 0:
                raise ControlledGameError('当前可用游戏额度不足一分钟，先不启动新游戏。')
            approvals = connection.execute(
                """
                SELECT * FROM controlled_game_approval
                WHERE child_id=? AND status='approved'
                ORDER BY CASE WHEN title LIKE ? THEN 0 ELSE 1 END, title
                """,
                (child_id, f"%{query.strip()}%"),
            ).fetchall()
            if not approvals:
                raise ControlledGameError("家长许可目录里还没有可玩的游戏。")
            try:
                selected = _select_approved_query(approvals, query,
                    fields=('title',), label='游戏', error_type=ControlledGameError)
            except ControlledGameError as error:
                _attach_selection_offer(error, 'game', child_id, session_id, _game_selection_choice)
                raise
            if selection_binding is not None:
                _require_selection_binding(selection_binding, _game_selection_choice(selected, child_id, session_id),
                                           query, ControlledGameError)
            path, file_size, file_mtime_ns = _validated_executable(selected["executable_path"])
            if file_size != selected["file_size"] or file_mtime_ns != selected["file_mtime_ns"]:
                raise ControlledGameError("游戏程序已经变化，需要家长重新许可。")
            allowed_minutes = min(
                int(selected["max_minutes"]), int(rule["max_item_minutes"]), remaining_daily
            )
            game_session_id = str(uuid4())
            ends_at = now + timedelta(minutes=allowed_minutes)
            connection.execute(
                """
                INSERT INTO controlled_game_session(
                  game_session_id, child_id, session_id, approval_id, title, executable_path,
                  status, allowed_minutes, paused_plan_item_id, started_at, ends_at, updated_at,
                  client_action_id
                ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)
                """,
                (
                    game_session_id, child_id, session_id, selected["approval_id"], selected["title"],
                    str(path), allowed_minutes, paused_plan_item_id, now.isoformat(), ends_at.isoformat(),
                    now.isoformat(), client_action_id,
                ),
            )
            self._audit(connection, "agent", "controlled_game_started", child_id, {
                "game_session_id": game_session_id,
                "approval_id": selected["approval_id"],
                "title": selected["title"],
                "allowed_minutes": allowed_minutes,
                **({'selection_binding': selection_binding} if selection_binding is not None else {}),
            }, now.isoformat())
            row = connection.execute(
                "SELECT * FROM controlled_game_session WHERE game_session_id=?", (game_session_id,)
            ).fetchone()
            register(row)
        return {**_session_dict(row), **({'selection_binding': selection_binding} if selection_binding is not None else {}),
                'replayed': False, 'launch_allowed': True}

    def bind_process(self, game_session_id: str, os_process_id: int) -> dict:
        if os_process_id < 1:
            raise ControlledGameError("游戏进程编号无效。")
        now = _now()
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM controlled_game_session WHERE game_session_id=? AND status='active'",
                (game_session_id,),
            ).fetchone()
            if not row:
                raise ControlledGameError("这次游戏已经结束，不能再绑定进程。")
            connection.execute(
                "UPDATE controlled_game_session SET os_process_id=?, updated_at=? WHERE game_session_id=?",
                (os_process_id, now, game_session_id),
            )
            self._audit(connection, "client", "controlled_game_process_bound", row["child_id"], {
                "game_session_id": game_session_id,
            }, now)
            connection.commit()
            updated = connection.execute(
                "SELECT * FROM controlled_game_session WHERE game_session_id=?", (game_session_id,)
            ).fetchone()
        return _session_dict(updated)

    def active(self, session_id: str) -> dict | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM controlled_game_session WHERE session_id=? AND status='active'",
                (session_id,),
            ).fetchone()
        return _session_dict(row) if row else None

    def end(self, session_id: str, reason: str, actual_seconds: int = 0,
            *, game_session_id: str | None = None) -> dict:
        now = datetime.now(timezone.utc)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not isinstance(game_session_id, str) or not game_session_id.strip():
                raise ControlledGameError('缺少原游戏标识，不能结束其他正在进行的游戏。')
            row = connection.execute(
                '''SELECT media.* FROM controlled_game_session media
                   JOIN daily_session ds ON ds.session_id=media.session_id AND ds.child_id=media.child_id
                   WHERE media.game_session_id=? AND media.session_id=?''',
                (game_session_id, session_id),
            ).fetchone()
            if not row:
                raise ControlledGameError('原游戏记录不存在或不属于这次学习会话，未结束其他游戏。')
            if row['status'] != 'active':
                connection.commit()
                return {**_session_dict(row), 'replayed': True, 'launch_allowed': False,
                        'recorded_receipt': {'game_session_id': row['game_session_id']},
                        'local_only': True, 'cloud_transmission_allowed': False}
            elapsed = max(0, int((now - datetime.fromisoformat(row["started_at"])).total_seconds()))
            seconds = min(max(actual_seconds, elapsed), int(row["allowed_minutes"]) * 60)
            status = (
                "expired" if reason == "time_limit"
                else "failed" if reason in {"launch_failed", "process_error"}
                else "completed" if reason == "process_exited"
                else "stopped"
            )
            changed = connection.execute(
                """
                UPDATE controlled_game_session SET status=?, actual_seconds=?, ended_at=?,
                  updated_at=?, end_reason=? WHERE game_session_id=? AND session_id=? AND child_id=? AND status='active'
                """,
                (status, seconds, now.isoformat(), now.isoformat(), reason[:120],
                 row['game_session_id'], session_id, row['child_id']),
            )
            if changed.rowcount != 1:
                raise ControlledGameError('原游戏状态已变化，未结束其他游戏。')
            self._audit(connection, "agent", "controlled_game_ended", row["child_id"], {
                "game_session_id": row["game_session_id"], "reason": reason, "actual_seconds": seconds,
            }, now.isoformat())
            updated = connection.execute(
                "SELECT * FROM controlled_game_session WHERE game_session_id=?", (row["game_session_id"],)
            ).fetchone()
            connection.commit()
        return {**_session_dict(updated), 'replayed': False, 'launch_allowed': False,
                'local_only': True, 'cloud_transmission_allowed': False}

    def recover_interrupted(self) -> int:
        now = _now()
        with self.database.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            rows = connection.execute(
                "SELECT * FROM controlled_game_session WHERE status='active'"
            ).fetchall()
            for row in rows:
                elapsed = max(0, int((datetime.fromisoformat(now) - datetime.fromisoformat(row["started_at"])).total_seconds()))
                seconds = min(elapsed, int(row["allowed_minutes"]) * 60)
                connection.execute(
                    """
                    UPDATE controlled_game_session SET status='interrupted', actual_seconds=?, ended_at=?,
                      updated_at=?, end_reason='app_restarted' WHERE game_session_id=?
                    """,
                    (seconds, now, now, row["game_session_id"]),
                )
                self._audit(connection, "system", "controlled_game_interrupted", row["child_id"], {
                    "game_session_id": row["game_session_id"], "actual_seconds": seconds,
                }, now)
            connection.commit()
        return len(rows)

    @staticmethod
    def _require_child(connection, child_id: str):
        row = connection.execute(
            "SELECT child_id FROM child_profile WHERE child_id=? AND status='active'", (child_id,)
        ).fetchone()
        if not row:
            raise ControlledGameError("找不到孩子档案。")
        return row

    @staticmethod
    def _audit(connection, actor: str, action: str, child_id: str, summary: dict, occurred_at: str) -> None:
        connection.execute(
            """
            INSERT INTO audit_event(audit_id, actor, action, child_id, summary_json, occurred_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (str(uuid4()), actor, action, child_id, json.dumps(summary, ensure_ascii=False), occurred_at),
        )


def _game_selection_choice(row, child_id, session_id):
    path, size, modified = _validated_executable(row['executable_path'])
    if size != row['file_size'] or modified != row['file_mtime_ns']:
        raise ControlledGameError('游戏程序已经变化，需要家长重新许可。')
    return _selection_choice(row, child_id, session_id, row['approval_id'], path)


def _validated_executable(value: str) -> tuple[Path, int, int]:
    if not value or len(value) > 4096:
        raise ControlledGameError("游戏程序路径无效。")
    path = Path(value).expanduser().resolve()
    if path.suffix.lower() != ".exe":
        raise ControlledGameError("只允许家长选择 Windows .exe 游戏程序。")
    if not path.is_file():
        raise ControlledGameError("游戏程序不存在或不可用。")
    stat = path.stat()
    return path, stat.st_size, stat.st_mtime_ns


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rule_dict(row) -> dict:
    value = dict(row)
    value["enabled"] = bool(value["enabled"])
    return value


def _session_dict(row) -> dict:
    value = dict(row)
    value["remaining_seconds"] = max(
        0, int((datetime.fromisoformat(value["ends_at"]) - datetime.now(timezone.utc)).total_seconds())
    ) if value["status"] == "active" else 0
    return value
