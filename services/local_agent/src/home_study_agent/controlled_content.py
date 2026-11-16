from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from contextlib import contextmanager
import json
import hashlib
import math
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

from .database import Database
from .family_clock import family_time, family_today


class ControlledContentError(ValueError):
    pass


class ControlledContentService:
    """Parent-approved, local-only educational media with durable time limits."""

    def __init__(self, database: Database):
        self.database = database
        with self.database.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS controlled_content_rule (
                  child_id TEXT PRIMARY KEY REFERENCES child_profile(child_id) ON DELETE CASCADE,
                  enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0, 1)),
                  max_daily_minutes INTEGER NOT NULL DEFAULT 20,
                  max_item_minutes INTEGER NOT NULL DEFAULT 15,
                  updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS controlled_content_approval (
                  approval_id TEXT PRIMARY KEY,
                  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                  curriculum_row_id INTEGER NOT NULL REFERENCES curriculum_resource(row_id) ON DELETE CASCADE,
                  status TEXT NOT NULL CHECK(status IN ('approved', 'revoked')),
                  max_minutes INTEGER NOT NULL,
                  note TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  UNIQUE(child_id, curriculum_row_id)
                );
                CREATE TABLE IF NOT EXISTS controlled_content_session (
                  content_session_id TEXT PRIMARY KEY,
                  child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                  session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
                  curriculum_row_id INTEGER NOT NULL REFERENCES curriculum_resource(row_id),
                  title TEXT NOT NULL,
                  local_path TEXT NOT NULL,
                  status TEXT NOT NULL CHECK(status IN (
                    'active', 'completed', 'stopped', 'expired', 'interrupted', 'failed'
                  )),
                  allowed_minutes INTEGER NOT NULL,
                  actual_seconds INTEGER NOT NULL DEFAULT 0,
                  paused_plan_item_id TEXT REFERENCES plan_item(plan_item_id),
                  started_at TEXT NOT NULL,
                  ends_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  ended_at TEXT,
                  end_reason TEXT,
                  client_action_id TEXT NOT NULL UNIQUE
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_controlled_content_active_session
                  ON controlled_content_session(session_id) WHERE status = 'active';
                CREATE INDEX IF NOT EXISTS idx_controlled_content_child_started
                  ON controlled_content_session(child_id, started_at DESC);
                """
            )
            connection.commit()

        # Schema preparation must finish before a caller opens the start transaction.
        from .companion_activity_outcome import CompanionActivityOutcomeService
        self._outcomes = CompanionActivityOutcomeService(self.database)

    @contextmanager
    def _start_transaction(self, connection=None):
        if connection is not None:
            if not isinstance(connection, sqlite3.Connection) or not connection.in_transaction:
                raise ControlledContentError('CONTROLLED_CONTENT_TRANSACTION_REQUIRED')
            main = next((row[2] for row in connection.execute('PRAGMA database_list') if row[1] == 'main'), None)
            if not main or Path(main).resolve() != self.database.path.resolve():
                raise ControlledContentError('CONTROLLED_CONTENT_TRANSACTION_OWNER_MISMATCH')
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
        """Read an exact historical start; this never authorizes launching media again."""
        with self._start_transaction(connection) as c:
            row = c.execute('SELECT * FROM controlled_content_session WHERE client_action_id=?',
                            (client_action_id,)).fetchone()
            if row is None:
                return None
            owner = c.execute('SELECT child_id FROM daily_session WHERE session_id=?', (session_id,)).fetchone()
            if row['session_id'] != session_id or owner is None or owner['child_id'] != row['child_id']:
                raise ControlledContentError('CONTROLLED_CONTENT_START_IDEMPOTENCY_CONFLICT')
            original = c.execute('''SELECT intent FROM companion_activity_outcome
                WHERE source_kind='controlled_content' AND source_id=? AND child_id=? AND session_id=?''',
                (row['content_session_id'], row['child_id'], session_id)).fetchone()
            if original is None or original['intent'] != 'controlled_content:' + query:
                raise ControlledContentError('CONTROLLED_CONTENT_START_IDEMPOTENCY_CONFLICT')
            saved_binding = _recorded_selection_binding(c, 'controlled_content', row, selection_binding, ControlledContentError)
            return {**_playback_dict(row), **saved_binding, 'replayed': True, 'launch_allowed': False,
                    'recorded_receipt': {'content_session_id': row['content_session_id'],
                                         'client_action_id': client_action_id},
                    'local_only': True, 'cloud_transmission_allowed': False}

    def rule(self, child_id: str) -> dict:
        now = _now()
        with self.database.connect() as connection:
            self._require_child(connection, child_id)
            connection.execute(
                """
                INSERT INTO controlled_content_rule(child_id, enabled, max_daily_minutes, max_item_minutes, updated_at)
                VALUES (?, 0, 20, 15, ?)
                ON CONFLICT(child_id) DO NOTHING
                """,
                (child_id, now),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM controlled_content_rule WHERE child_id = ?", (child_id,)
            ).fetchone()
        return _rule_dict(row)

    def update_rule(
        self, child_id: str, *, enabled: bool, max_daily_minutes: int, max_item_minutes: int
    ) -> dict:
        if not 0 <= max_daily_minutes <= 120 or not 1 <= max_item_minutes <= 60:
            raise ControlledContentError("受控内容时长设置无效。")
        if enabled and max_daily_minutes < 1:
            raise ControlledContentError("启用受控内容时，每日时长必须大于零。")
        now = _now()
        with self.database.connect() as connection:
            self._require_child(connection, child_id)
            connection.execute(
                """
                INSERT INTO controlled_content_rule(child_id, enabled, max_daily_minutes, max_item_minutes, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(child_id) DO UPDATE SET enabled=excluded.enabled,
                  max_daily_minutes=excluded.max_daily_minutes,
                  max_item_minutes=excluded.max_item_minutes, updated_at=excluded.updated_at
                """,
                (child_id, int(enabled), max_daily_minutes, max_item_minutes, now),
            )
            self._audit(
                connection, "parent", "controlled_content_rule_updated", child_id,
                {"enabled": enabled, "max_daily_minutes": max_daily_minutes, "max_item_minutes": max_item_minutes},
                now,
            )
            connection.commit()
        return self.rule(child_id)

    def candidates(self, child_id: str, query: str = "", subject: str | None = None, limit: int = 30) -> list[dict]:
        query = query.strip()
        with self.database.connect() as connection:
            child = self._require_child(connection, child_id)
            clauses = [
                "cc.status = 'ready'", "cr.local_video_available = 1", "cr.grade_title = ?"
            ]
            params: list[object] = [child["grade"]]
            if subject:
                clauses.append("cr.subject_title LIKE ?")
                params.append(f"%{subject.strip()}%")
            if query:
                clauses.append("(cr.title LIKE ? OR cr.description LIKE ? OR cr.unit_path LIKE ?)")
                term = f"%{query}%"
                params.extend([term, term, term])
            rows = connection.execute(
                f"""
                SELECT cr.row_id, cr.title, cr.subject_title, cr.grade_title, cr.unit_path,
                       cr.video_duration, cr.local_video_available,
                       COALESCE(a.status, 'not_approved') AS approval_status,
                       a.max_minutes, a.note
                FROM curriculum_resource cr
                JOIN curriculum_corpus cc ON cc.corpus_id = cr.corpus_id
                LEFT JOIN controlled_content_approval a
                  ON a.curriculum_row_id = cr.row_id AND a.child_id = ?
                WHERE {' AND '.join(clauses)}
                ORDER BY CASE WHEN a.status = 'approved' THEN 0 ELSE 1 END, cr.subject_title, cr.title
                LIMIT ?
                """,
                (child_id, *params, min(max(limit, 1), 100)),
            ).fetchall()
        return [dict(row) for row in rows]

    def approve(
        self, child_id: str, curriculum_row_id: int, max_minutes: int, note: str = ""
    ) -> dict:
        if not 1 <= max_minutes <= 60:
            raise ControlledContentError("单个内容时长必须在1到60分钟之间。")
        now = _now()
        with self.database.connect() as connection:
            self._require_child(connection, child_id)
            resource = self._resource(connection, curriculum_row_id)
            resource_path = Path(resource["video_root_path"]) / resource["video_relative_path"]
            if not resource["local_video_available"] or not resource_path.is_file():
                raise ControlledContentError("本地视频文件不可用，不能加入许可目录。")
            existing = connection.execute(
                "SELECT approval_id FROM controlled_content_approval WHERE child_id=? AND curriculum_row_id=?",
                (child_id, curriculum_row_id),
            ).fetchone()
            approval_id = existing["approval_id"] if existing else str(uuid4())
            connection.execute(
                """
                INSERT INTO controlled_content_approval(
                  approval_id, child_id, curriculum_row_id, status, max_minutes, note, created_at, updated_at
                ) VALUES (?, ?, ?, 'approved', ?, ?, ?, ?)
                ON CONFLICT(child_id, curriculum_row_id) DO UPDATE SET status='approved',
                  max_minutes=excluded.max_minutes, note=excluded.note, updated_at=excluded.updated_at
                """,
                (approval_id, child_id, curriculum_row_id, max_minutes, note.strip()[:500] or None, now, now),
            )
            self._audit(
                connection, "parent", "controlled_content_approved", child_id,
                {"row_id": curriculum_row_id, "title": resource["title"], "max_minutes": max_minutes}, now,
            )
            connection.commit()
        return next(item for item in self.candidates(child_id, limit=100) if item["row_id"] == curriculum_row_id)

    def revoke(self, child_id: str, curriculum_row_id: int) -> dict:
        now = _now()
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT approval_id FROM controlled_content_approval WHERE child_id=? AND curriculum_row_id=?",
                (child_id, curriculum_row_id),
            ).fetchone()
            if not row:
                raise ControlledContentError("这项内容不在许可目录中。")
            connection.execute(
                "UPDATE controlled_content_approval SET status='revoked', updated_at=? WHERE approval_id=?",
                (now, row["approval_id"]),
            )
            self._audit(connection, "parent", "controlled_content_revoked", child_id, {"row_id": curriculum_row_id}, now)
            connection.commit()
        return {"row_id": curriculum_row_id, "status": "revoked"}

    def approved(self, child_id: str) -> list[dict]:
        return [item for item in self.candidates(child_id, limit=100) if item["approval_status"] == "approved"]

    def start(
        self, session_id: str, query: str, client_action_id: str,
        paused_plan_item_id: str | None = None,
        *, connection=None, selection_binding=None,
    ) -> dict:
        now = datetime.now(timezone.utc)
        with self._start_transaction(connection) as connection:

            def register(row):
                self._outcomes.open(row['child_id'], source_kind='controlled_content', source_id=row['content_session_id'],
                    intent='controlled_content:' + query,
                    success_description='记录实际参与和感受；活动结束不代表执行成功或已返回学习。',
                    client_action_id='controlled-content-outcome:' + row['content_session_id'], connection=connection)

            replay = self.recorded_start(session_id, query, client_action_id, connection=connection, selection_binding=selection_binding)
            if replay:
                if replay['session_id'] != session_id or replay['paused_plan_item_id'] != paused_plan_item_id:
                    raise ControlledContentError('CONTROLLED_CONTENT_START_IDEMPOTENCY_CONFLICT')
                return replay
            session = connection.execute(
                "SELECT child_id FROM daily_session WHERE session_id=? AND status='active'", (session_id,)
            ).fetchone()
            if not session:
                raise ControlledContentError("当前没有可播放内容的学习会话。")
            child_id = session["child_id"]
            active = connection.execute(
                "SELECT content_session_id FROM controlled_content_session WHERE session_id=? AND status='active'",
                (session_id,),
            ).fetchone()
            if active:
                raise ControlledContentError("已有内容正在播放，请先说停止视频。")
            rule = connection.execute(
                "SELECT * FROM controlled_content_rule WHERE child_id=?", (child_id,)
            ).fetchone()
            if not rule or not rule["enabled"]:
                raise ControlledContentError("家长尚未启用受控内容陪伴。")
            try:
                remaining_daily = _daily_budget_minutes(connection, 'controlled_content_session',
                    child_id, now, int(rule['max_daily_minutes']))
            except _ControlledBudgetHistoryError:
                raise ControlledContentError('历史视频额度暂时无法核对，请家长检查后再开启。') from None
            if remaining_daily <= 0:
                raise ControlledContentError('当前可用视频额度不足一分钟，先不启动新的视频。')
            approvals = connection.execute(
                """
                SELECT a.max_minutes, a.updated_at AS approval_updated_at, a.approval_id,
                       cr.*, cc.video_root_path, cc.status AS corpus_status
                FROM controlled_content_approval a
                JOIN curriculum_resource cr ON cr.row_id=a.curriculum_row_id
                JOIN curriculum_corpus cc ON cc.corpus_id=cr.corpus_id
                WHERE a.child_id=? AND a.status='approved' AND cr.local_video_available=1
                ORDER BY CASE WHEN cr.title LIKE ? OR cr.unit_path LIKE ? THEN 0 ELSE 1 END,
                         cr.subject_title, cr.title
                """,
                (child_id, f"%{query.strip()}%", f"%{query.strip()}%"),
            ).fetchall()
            if not approvals:
                raise ControlledContentError("许可目录里没有可播放的本地内容。")
            try:
                selected = _select_approved_query(approvals, query,
                    fields=('title', 'unit_path', 'subject_title'), label='视频', error_type=ControlledContentError)
            except ControlledContentError as error:
                _attach_selection_offer(error, 'video', child_id, session_id, _video_selection_choice)
                raise
            if selection_binding is not None:
                _require_selection_binding(selection_binding, _video_selection_choice(selected, child_id, session_id),
                                           query, ControlledContentError)
            local_path = str((Path(selected["video_root_path"]) / selected["video_relative_path"]).resolve())
            root = Path(selected["video_root_path"]).resolve()
            if not _is_within(Path(local_path), root) or not Path(local_path).is_file():
                raise ControlledContentError("获准内容的本地文件已经移动或不可用。")
            allowed_minutes = min(
                int(selected["max_minutes"]), int(rule["max_item_minutes"]), remaining_daily
            )
            content_session_id = str(uuid4())
            ends_at = now + timedelta(minutes=allowed_minutes)
            connection.execute(
                """
                INSERT INTO controlled_content_session(
                  content_session_id, child_id, session_id, curriculum_row_id, title, local_path,
                  status, allowed_minutes, paused_plan_item_id, started_at, ends_at, updated_at,
                  client_action_id
                ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)
                """,
                (
                    content_session_id, child_id, session_id, selected["row_id"], selected["title"],
                    local_path, allowed_minutes, paused_plan_item_id, now.isoformat(), ends_at.isoformat(),
                    now.isoformat(), client_action_id,
                ),
            )
            self._audit(
                connection, "agent", "controlled_content_started", child_id,
                {"content_session_id": content_session_id, "row_id": selected["row_id"], "allowed_minutes": allowed_minutes,
                 **({'selection_binding': selection_binding} if selection_binding is not None else {})},
                now.isoformat(),
            )
            row = connection.execute(
                "SELECT * FROM controlled_content_session WHERE content_session_id=?", (content_session_id,)
            ).fetchone()
            register(row)
        return {**_playback_dict(row), **({'selection_binding': selection_binding} if selection_binding is not None else {}),
                'replayed': False, 'launch_allowed': True}

    def active(self, session_id: str) -> dict | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM controlled_content_session WHERE session_id=? AND status='active'", (session_id,)
            ).fetchone()
        return _playback_dict(row) if row else None

    def end(self, session_id: str, reason: str, actual_seconds: int = 0,
            *, content_session_id: str | None = None) -> dict:
        now = datetime.now(timezone.utc)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not isinstance(content_session_id, str) or not content_session_id.strip():
                raise ControlledContentError('缺少原视频标识，不能结束其他正在播放的内容。')
            row = connection.execute(
                '''SELECT media.* FROM controlled_content_session media
                   JOIN daily_session ds ON ds.session_id=media.session_id AND ds.child_id=media.child_id
                   WHERE media.content_session_id=? AND media.session_id=?''',
                (content_session_id, session_id),
            ).fetchone()
            if not row:
                raise ControlledContentError('原视频记录不存在或不属于这次学习会话，未结束其他内容。')
            if row['status'] != 'active':
                connection.commit()
                return {**_playback_dict(row), 'replayed': True, 'launch_allowed': False,
                        'recorded_receipt': {'content_session_id': row['content_session_id']},
                        'local_only': True, 'cloud_transmission_allowed': False}
            elapsed = max(0, int((now - datetime.fromisoformat(row["started_at"])).total_seconds()))
            seconds = min(max(actual_seconds, elapsed), int(row["allowed_minutes"]) * 60)
            status = "expired" if reason == "time_limit" else "completed" if reason == "media_ended" else "stopped"
            changed = connection.execute(
                """
                UPDATE controlled_content_session SET status=?, actual_seconds=?, ended_at=?,
                  updated_at=?, end_reason=? WHERE content_session_id=? AND session_id=? AND child_id=? AND status='active'
                """,
                (status, seconds, now.isoformat(), now.isoformat(), reason[:120],
                 row['content_session_id'], session_id, row['child_id']),
            )
            if changed.rowcount != 1:
                raise ControlledContentError('原视频状态已变化，未结束其他内容。')
            self._audit(
                connection, "agent", "controlled_content_ended", row["child_id"],
                {"content_session_id": row["content_session_id"], "reason": reason, "actual_seconds": seconds},
                now.isoformat(),
            )
            updated = connection.execute(
                "SELECT * FROM controlled_content_session WHERE content_session_id=?", (row["content_session_id"],)
            ).fetchone()
            connection.commit()
        return {**_playback_dict(updated), 'replayed': False, 'launch_allowed': False,
                'local_only': True, 'cloud_transmission_allowed': False}

    def recover_interrupted(self) -> int:
        now = _now()
        with self.database.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            rows = connection.execute(
                "SELECT * FROM controlled_content_session WHERE status='active'"
            ).fetchall()
            for row in rows:
                elapsed = max(
                    0,
                    int((datetime.fromisoformat(now) - datetime.fromisoformat(row["started_at"])).total_seconds()),
                )
                actual_seconds = min(elapsed, int(row["allowed_minutes"]) * 60)
                connection.execute(
                    """
                    UPDATE controlled_content_session SET status='interrupted', actual_seconds=?, ended_at=?, updated_at=?,
                      end_reason='app_restarted' WHERE content_session_id=?
                    """,
                    (actual_seconds, now, now, row["content_session_id"]),
                )
                self._audit(
                    connection, "system", "controlled_content_interrupted", row["child_id"],
                    {
                        "content_session_id": row["content_session_id"],
                        "reason": "app_restarted",
                        "actual_seconds": actual_seconds,
                    },
                    now,
                )
            connection.commit()
        return len(rows)

    @staticmethod
    def _require_child(connection, child_id: str):
        row = connection.execute(
            "SELECT child_id, grade FROM child_profile WHERE child_id=? AND status='active'", (child_id,)
        ).fetchone()
        if not row:
            raise ControlledContentError("找不到孩子档案。")
        return row

    @staticmethod
    def _resource(connection, row_id: int):
        row = connection.execute(
            """
            SELECT cr.*, cc.video_root_path
            FROM curriculum_resource cr JOIN curriculum_corpus cc ON cc.corpus_id=cr.corpus_id
            WHERE cr.row_id=? AND cc.status='ready'
            """,
            (row_id,),
        ).fetchone()
        if not row:
            raise ControlledContentError("找不到这项本地课程内容。")
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


def _select_approved_query(approvals, query, *, fields, label, error_type):
    text = query.strip().casefold()
    if not text:
        # Empty query is the existing explicit generic start contract, not a
        # failed attempt to resolve a particular requested topic.
        return approvals[0]
    exact = [row for row in approvals if text == row['title'].strip().casefold()]
    matches = exact or [row for row in approvals
                        if text in ' '.join(str(row[field] or '') for field in fields).casefold()]
    if len(matches) == 1:
        return matches[0]
    choices = matches or approvals
    titles = list(dict.fromkeys(' '.join(row['title'].split()) for row in choices))
    names = '、'.join('「' + title[:120] + '」' for title in titles[:5])
    def clarification(message):
        error = error_type(message)
        error.selection_status = 'ambiguous' if matches else 'unmatched'
        error.selection_choices = choices
        return error
    if not matches:
        raise clarification(f'没有找到符合这个主题的已许可{label}，没有启动。可选名称包括{names}，请说想选的完整名称。')
    if len(titles) < len(choices):
        raise clarification(f'找到多个符合的{label}，其中有同名内容{names}，没有启动。请家长先区分名称或许可范围，再选择。')
    raise clarification(f'找到多个符合的{label}，没有启动。可选名称包括{names}，请说想选的完整名称。')


def _selection_choice(row, child_id, session_id, resource_id, path):
    stat = path.stat()
    basis = {'child_id': child_id, 'session_id': session_id, 'source': dict(row),
             'file_size': stat.st_size, 'file_mtime_ns': stat.st_mtime_ns, 'path': str(path)}
    version = hashlib.sha256(json.dumps(basis, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {'resource_id': str(resource_id), 'title': row['title'], 'source_version': version}


def _video_selection_choice(row, child_id, session_id):
    root = Path(row['video_root_path']).resolve()
    path = (root / row['video_relative_path']).resolve()
    if row['corpus_status'] != 'ready' or not _is_within(path, root) or not path.is_file():
        raise ControlledContentError('原视频来源已变化，请家长核对后再选择。')
    return _selection_choice(row, child_id, session_id, row['row_id'], path)


def _attach_selection_offer(error, kind, child_id, session_id, choice):
    status = getattr(error, 'selection_status', None)
    if status not in {'unmatched', 'ambiguous'}:
        return
    choices = []
    unavailable_count = 0
    unselectable_title_count = 0
    for row in error.selection_choices[:100]:
        # Keep the exact title; start.query accepts at most 200 characters.
        # Never advertise a truncated title as a different executable choice.
        if len(row['title']) > 200:
            unselectable_title_count += 1
            continue
        try:
            choices.append(choice(row, child_id, session_id))
        except (type(error), OSError):
            # A single unavailable approved file must not hide other choices.
            # This does not catch database failures or auto-select a survivor.
            unavailable_count += 1
    if not choices:
        unavailable = type(error)('这些内容暂时不能选择，没有启动活动。'
            + ('有些名称过长，请家长缩短名称后再试。' if unselectable_title_count else '')
            + ('请家长检查原文件和许可后再选择。' if unavailable_count else ''))
        unavailable.selection_code = 'CONTROLLED_SELECTION_SOURCE_UNAVAILABLE'
        raise unavailable
    if unselectable_title_count:
        error.args = (str(error) + ' 有些名称过长，未列入可选项，请家长缩短名称后再试。',)
    error.selection_code = 'CONTROLLED_CONTENT_SELECTION_REQUIRED' if kind == 'video' else 'CONTROLLED_GAME_SELECTION_REQUIRED'
    error.selection_details = {'selection': {'version': 1, 'kind': kind, 'child_id': child_id, 'session_id': session_id,
        'status': status, 'choices': choices, 'unavailable_count': unavailable_count,
        'unselectable_title_count': unselectable_title_count,
        'local_only': True, 'cloud_transmission_allowed': False}}


def _require_selection_binding(binding, choice, query, error_type):
    expected = {key: choice[key] for key in ('resource_id', 'source_version')}
    if not query.strip() or binding != expected:
        error = error_type('刚才选择的原资源或许可版本已变化，没有打开其他内容。')
        error.selection_code = 'CONTROLLED_SELECTION_SOURCE_CHANGED'
        raise error


def _recorded_selection_binding(connection, kind, row, binding, error_type):
    if binding is None:
        return {}
    identity = row['content_session_id' if kind == 'controlled_content' else 'game_session_id']
    records = connection.execute('''SELECT summary_json FROM audit_event
        WHERE action=? AND child_id=? AND instr(summary_json,?)>0''',
        (kind + '_started', row['child_id'], identity)).fetchall()
    for record in records:
        try:
            value = json.loads(record['summary_json'])
        except (TypeError, ValueError):
            continue
        if (isinstance(value, dict) and value.get('content_session_id' if kind == 'controlled_content' else 'game_session_id') == identity
                and value.get('selection_binding') == binding):
            return {'selection_binding': binding}
    raise error_type('原选择回执的身份或版本无法核对，没有重新打开内容。')


class _ControlledBudgetHistoryError(ValueError):
    pass


def _budget_time(value):
    try:
        instant = datetime.fromisoformat(value.replace('Z', '+00:00'))
        # This exact legacy SQLite storage format is UTC, not host-local time.
        if instant.tzinfo is None and re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}', value):
            instant = instant.replace(tzinfo=timezone.utc)
        return family_time(instant).astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError, OverflowError):
        raise _ControlledBudgetHistoryError('controlled budget timestamp unavailable') from None


def _daily_budget_minutes(connection, table, child_id, now, daily_minutes):
    """Budget reservations are not physical playback or closure evidence."""
    if table not in {'controlled_content_session', 'controlled_game_session'}:
        raise ValueError('Unsupported controlled budget table')
    local = family_time(now)
    midnight = datetime.combine(family_today(now), time.min, tzinfo=local.tzinfo)
    day_start = midnight.astimezone(timezone.utc)
    day_end = (midnight + timedelta(days=1)).astimezone(timezone.utc)
    next_end = (midnight + timedelta(days=2)).astimezone(timezone.utc)
    cap = max(0, daily_minutes * 60)
    may_cross = now + timedelta(seconds=min(cap, 3600)) > day_end
    horizon = next_end if may_cross else day_end
    # A one-second query margin avoids SQLite Julian-date precision dropping
    # a subsecond overlap. The actual interval intersection below is exact.
    rows = connection.execute(f'''SELECT started_at,ends_at,ended_at,status,allowed_minutes,actual_seconds
        FROM {table} WHERE child_id=? AND (
          julianday(started_at) IS NULL OR julianday(ends_at) IS NULL OR
          (julianday(ends_at)>=julianday(?) AND julianday(started_at)<julianday(?)))''',
        (child_id, (day_start - timedelta(seconds=1)).isoformat(),
         (horizon + timedelta(seconds=1)).isoformat())).fetchall()
    intervals = []
    for row in rows:
        started, expires = _budget_time(row['started_at']), _budget_time(row['ends_at'])
        minutes, reported = row['allowed_minutes'], row['actual_seconds']
        if (type(minutes) is not int or not 1 <= minutes <= 60 or type(reported) is not int
                or not 0 <= reported <= minutes * 60 or expires - started != timedelta(minutes=minutes)):
            raise _ControlledBudgetHistoryError('controlled budget duration unavailable')
        if row['status'] in {'active', 'interrupted', 'failed', 'expired'}:
            # Unconfirmed launches retain the original grant, including future
            # portions. A restart's elapsed field is not a confirmed refund.
            end = expires
        elif row['status'] in {'completed', 'stopped'}:
            ended = _budget_time(row['ended_at'])
            if ended < started:
                raise _ControlledBudgetHistoryError('controlled budget order unavailable')
            duration = min(minutes * 60, max(reported, (ended - started).total_seconds()))
            end = started + timedelta(seconds=duration)
        else:
            raise _ControlledBudgetHistoryError('controlled budget status unavailable')
        intervals.append((started, end))

    def available(begin, end):
        used = sum(max(0.0, (min(finish, end) - max(start, begin)).total_seconds())
                   for start, finish in intervals)
        return max(0, cap - math.ceil(used))

    remaining = available(day_start, day_end)
    if may_cross:
        remaining = min(remaining, max(0.0, (day_end - now).total_seconds()) + available(day_end, next_end))
    # The durable/renderer contract allocates whole minutes. Never round up
    # a partial remaining minute into a larger executable grant.
    return int(remaining // 60)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rule_dict(row) -> dict:
    value = dict(row)
    value["enabled"] = bool(value["enabled"])
    return value


def _playback_dict(row) -> dict:
    value = dict(row)
    value["remaining_seconds"] = max(
        0, int((datetime.fromisoformat(value["ends_at"]) - datetime.now(timezone.utc)).total_seconds())
    ) if value["status"] == "active" else 0
    return value
