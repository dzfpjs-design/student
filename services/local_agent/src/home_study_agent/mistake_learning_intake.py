"""Local question intake for cloud formative learning, without a graph prerequisite.

These are children's supplied questions, not verified errors or formal scores.
Legacy mistake IDs are reused when the exact question and original answer exist.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from uuid import uuid4


class MistakeIntakeError(ValueError):
    pass


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _text(value, limit, *, required=False):
    if not isinstance(value, str) or len(value) > limit or required and not value.strip():
        raise MistakeIntakeError('MISTAKE_INTAKE_INPUT_INVALID')
    return value.strip()


def _table(c, name):
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def read_intake_card(connection, child_id, mistake_id):
    if not _table(connection, 'mistake_learning_card'):
        return None
    row = connection.execute('SELECT * FROM mistake_learning_card WHERE mistake_id=? AND child_id=? AND status=?',
                             (mistake_id, child_id, 'active')).fetchone()
    return dict(row) if row else None


def list_intake_cards(connection, child_id):
    if not _table(connection, 'mistake_learning_card'):
        return []
    return [dict(row) for row in connection.execute(
        "SELECT * FROM mistake_learning_card WHERE child_id=? AND status='active' ORDER BY updated_at DESC,mistake_id",
        (child_id,)).fetchall()]


def _effective_question(c, row):
    question = row['prompt_summary']
    if _table(c, 'mistake_source_revision'):
        revised = c.execute('''SELECT question FROM mistake_source_revision
            WHERE mistake_id=? AND child_id=? ORDER BY version DESC LIMIT 1''',
                            (row['mistake_id'], row['child_id'])).fetchone()
        if revised:
            question = revised['question']
    return question


def _fingerprint(question, answer):
    return _digest([' '.join(question.split()), ' '.join(answer.split())])


class MistakeLearningIntake:
    def __init__(self, database):
        self.database = database
        with database.connect() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS mistake_learning_card (
              mistake_id TEXT PRIMARY KEY,
              child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
              session_id TEXT REFERENCES daily_session(session_id) ON DELETE SET NULL,
              subject TEXT NOT NULL,title TEXT NOT NULL,prompt_summary TEXT NOT NULL,
              submitted_answer TEXT NOT NULL,source_type TEXT NOT NULL,source_ref TEXT,
              revision INTEGER NOT NULL,source_revision TEXT NOT NULL,
              status TEXT NOT NULL CHECK(status IN ('active','deleted')),
              fingerprint TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
              UNIQUE(child_id,fingerprint)
            );
            CREATE TABLE IF NOT EXISTS mistake_learning_intake_action (
              client_action_id TEXT PRIMARY KEY,
              child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
              request_hash TEXT NOT NULL,mistake_id TEXT NOT NULL,created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS mistake_learning_card_revision (
              mistake_id TEXT NOT NULL REFERENCES mistake_learning_card(mistake_id) ON DELETE CASCADE,
              revision INTEGER NOT NULL,source_json TEXT NOT NULL,created_at TEXT NOT NULL,
              PRIMARY KEY(mistake_id,revision)
            );
            ''')

    @staticmethod
    def _owner(c, child_id, session_id, guard):
        if not c.execute("SELECT 1 FROM child_profile WHERE child_id=? AND status='active'", (child_id,)).fetchone():
            raise MistakeIntakeError('MISTAKE_INTAKE_CHILD_NOT_FOUND')
        if session_id and not c.execute("SELECT 1 FROM daily_session WHERE session_id=? AND child_id=? AND status='active'",
                                       (session_id, child_id)).fetchone():
            raise MistakeIntakeError('MISTAKE_INTAKE_SESSION_CHANGED')
        if guard is not None and guard(c) is not True:
            raise MistakeIntakeError('MISTAKE_INTAKE_OWNER_CHANGED')

    @staticmethod
    def _existing(c, child_id, mid):
        card = read_intake_card(c, child_id, mid)
        if card:
            return card
        if _table(c, 'mistake_case'):
            row = c.execute("SELECT * FROM mistake_case WHERE mistake_id=? AND child_id=? AND status!='excluded'",
                            (mid, child_id)).fetchone()
            if row:
                return {**dict(row), 'prompt_summary': _effective_question(c, row)}
        raise MistakeIntakeError('MISTAKE_INTAKE_SOURCE_CHANGED')

    def create(self, *, child_id, question, submitted_answer='', subject='', session_id=None,
               client_action_id, source_type='typed', guard=None):
        question = _text(question, 12000, required=True)
        answer = _text(submitted_answer, 8000)
        subject = _text(subject, 40)
        action = _text(client_action_id, 200, required=True)
        if source_type not in ('typed', 'spoken'):
            raise MistakeIntakeError('MISTAKE_INTAKE_SOURCE_INVALID')
        request = dict(child_id=child_id, session_id=session_id, question=question,
                       submitted_answer=answer, subject=subject, source_type=source_type)
        fingerprint = _fingerprint(question, answer)
        request_hash = _digest(request)
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            self._owner(c, child_id, session_id, guard)
            previous = c.execute('SELECT * FROM mistake_learning_intake_action WHERE client_action_id=?', (action,)).fetchone()
            if previous:
                if previous['child_id'] != child_id or previous['request_hash'] != request_hash:
                    raise MistakeIntakeError('MISTAKE_INTAKE_ACTION_CONFLICT')
                existing = self._existing(c, child_id, previous['mistake_id'])
                if _fingerprint(existing['prompt_summary'], existing['submitted_answer']) != fingerprint:
                    raise MistakeIntakeError('MISTAKE_INTAKE_SOURCE_CHANGED')
                return existing
            mid = None
            if _table(c, 'mistake_case'):
                for row in c.execute("SELECT mistake_id,child_id,prompt_summary,submitted_answer FROM mistake_case WHERE child_id=? AND status!='excluded'",
                                     (child_id,)):
                    if _fingerprint(_effective_question(c, row), row['submitted_answer']) == fingerprint:
                        mid = row['mistake_id']
                        break
            if not mid:
                row = c.execute('SELECT mistake_id FROM mistake_learning_card WHERE child_id=? AND fingerprint=? AND status=?',
                                (child_id, fingerprint, 'active')).fetchone()
                mid = row['mistake_id'] if row else None
            now = _now()
            if not mid:
                mid = 'mlc-' + str(uuid4())
                revision = _digest([question, answer, subject, 1])
                c.execute('''INSERT INTO mistake_learning_card(
                    mistake_id,child_id,session_id,subject,title,prompt_summary,submitted_answer,
                    source_type,source_ref,revision,source_revision,status,fingerprint,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                          (mid, child_id, session_id, subject, question[:80], question, answer, source_type,
                           None, 1, revision, 'active', fingerprint, now, now))
                source = {'question': question, 'original_answer': answer, 'subject': subject, 'source_type': source_type}
                c.execute('INSERT INTO mistake_learning_card_revision VALUES(?,?,?,?)',
                          (mid, 1, json.dumps(source, ensure_ascii=False), now))
            c.execute('INSERT INTO mistake_learning_intake_action VALUES(?,?,?,?,?)',
                      (action, child_id, request_hash, mid, now))
            self._owner(c, child_id, session_id, guard)
            result = self._existing(c, child_id, mid)
            c.commit()
            return result
