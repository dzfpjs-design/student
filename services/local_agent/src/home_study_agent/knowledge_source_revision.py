"""Explicit, local source repair. Historical excerpts are never overwritten."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from uuid import uuid4

from .repository import utc_now


class SourceRevisionConflict(ValueError):
    pass


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def migrate(connection):
    """Called immediately after SCHEMA, outside any transaction/worker startup.

    Rebuild the referenced table without renaming the old one (SQLite would
    rewrite its incoming FKs). Restore the caller's FK setting even on failure.
    """
    if connection.in_transaction:
        raise RuntimeError('SOURCE_REVISION_MIGRATION_REQUIRES_IDLE_CONNECTION')
    columns = [r['name'] for r in connection.execute('PRAGMA table_info(knowledge_source)')]
    if 'current_version' not in columns:
        # Only the project's known V8 TEXT schema is supported. PRAGMA does not
        # encode these constraints: reject rather than silently discarding them.
        original_sql = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='knowledge_source'").fetchone()[0]
        if (re.search(r'\b(CHECK|COLLATE|DEFERRABLE|GENERATED|STRICT)\b|\bWITHOUT\s+ROWID\b|\bON\s+CONFLICT\b', original_sql, re.I)
                or len(connection.execute('PRAGMA table_xinfo(knowledge_source)').fetchall()) != len(columns)):
            raise RuntimeError('SOURCE_REVISION_UNSUPPORTED_SCHEMA_CONSTRAINT')
        def quoted(name):
            return '"' + name.replace('"', '""') + '"'
        info = connection.execute('PRAGMA table_info(knowledge_source)').fetchall()
        known = {'source_id','child_id','source_type','title','subject','grade_band','publisher',
                 'version_label','source_ref','effective_from','effective_to','license_status',
                 'review_status','content_hash','imported_at'}
        if (set(columns) - known or any(r['dflt_value'] is not None for r in info)
                or [r['name'] for r in info if r['pk']] != ['source_id']):
            raise RuntimeError('SOURCE_REVISION_UNSUPPORTED_SCHEMA')
        indices = connection.execute('PRAGMA index_list(knowledge_source)').fetchall()
        unique = [tuple(r['name'] for r in connection.execute('PRAGMA index_info(' + quoted(i['name']) + ')'))
                  for i in indices if i['unique'] and i['origin'] == 'u']
        if ('child_id', 'content_hash') not in unique or any(r['type'].upper() != 'TEXT' for r in info):
            raise RuntimeError('SOURCE_REVISION_UNSUPPORTED_SCHEMA')
        definitions = []
        for column in info:
            definition = quoted(column['name']) + ' TEXT'
            if column['notnull']:
                definition += ' NOT NULL'
            if column['dflt_value'] is not None:
                definition += ' DEFAULT ' + column['dflt_value']
            definitions.append(definition)
        definitions.extend(['current_version INTEGER NOT NULL DEFAULT 1 CHECK(current_version IN (0,1))', 'predecessor_id TEXT'])
        primary = sorted((r for r in info if r['pk']), key=lambda r: r['pk'])
        definitions.append('PRIMARY KEY (' + ','.join(quoted(r['name']) for r in primary) + ')')
        for names in unique:
            if names != ('child_id', 'content_hash'):
                definitions.append('UNIQUE (' + ','.join(quoted(name) for name in names) + ')')
        foreign_keys = {}
        for fk in connection.execute('PRAGMA foreign_key_list(knowledge_source)').fetchall():
            foreign_keys.setdefault(fk['id'], []).append(fk)
        for group in foreign_keys.values():
            group.sort(key=lambda r: r['seq'])
            fk = group[0]
            definitions.append('FOREIGN KEY (' + ','.join(quoted(r['from']) for r in group)
                + ') REFERENCES ' + quoted(fk['table']) + '(' + ','.join(quoted(r['to']) for r in group)
                + ') ON DELETE ' + fk['on_delete'] + ' ON UPDATE ' + fk['on_update'])
        revised = 'CREATE TABLE knowledge_source_rebuilt (' + ','.join(definitions) + ')'
        ancillary = [r[0] for r in connection.execute("SELECT sql FROM sqlite_master WHERE tbl_name='knowledge_source' AND type IN ('index','trigger') AND sql IS NOT NULL")]
        fk = connection.execute('PRAGMA foreign_keys').fetchone()[0]
        connection.execute('PRAGMA foreign_keys=OFF')
        try:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute(revised)
            names = ','.join('"' + name + '"' for name in columns)
            connection.execute(f'INSERT INTO knowledge_source_rebuilt({names}) SELECT {names} FROM knowledge_source')
            connection.execute('DROP TABLE knowledge_source')
            connection.execute('ALTER TABLE knowledge_source_rebuilt RENAME TO knowledge_source')
            for statement in ancillary:
                connection.execute(statement)
            ensure_schema(connection)
            if connection.execute('PRAGMA foreign_key_check').fetchone():
                raise sqlite3.IntegrityError('SOURCE_REVISION_MIGRATION_FOREIGN_KEY_FAILED')
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.execute(f'PRAGMA foreign_keys={int(fk)}')
    else:
        ensure_schema(connection)


def ensure_schema(c):
    c.execute('''CREATE UNIQUE INDEX IF NOT EXISTS knowledge_source_current_hash
                 ON knowledge_source(child_id,content_hash) WHERE current_version=1''')
    c.execute('''CREATE TABLE IF NOT EXISTS knowledge_source_revision_event(
        event_id TEXT PRIMARY KEY, child_id TEXT NOT NULL REFERENCES child_profile(child_id),
        old_source_id TEXT REFERENCES knowledge_source(source_id) ON DELETE SET NULL,
        new_source_id TEXT NOT NULL REFERENCES knowledge_source(source_id) ON DELETE CASCADE,
        action TEXT NOT NULL CHECK(action IN ('revise','review')),
        client_action_id TEXT NOT NULL, request_hash TEXT NOT NULL,
        previous_version TEXT NOT NULL, actor TEXT NOT NULL, occurred_at TEXT NOT NULL,
        UNIQUE(child_id,client_action_id))''')
    columns = {r['name'] for r in c.execute('PRAGMA table_info(knowledge_source_revision_event)')}
    for column in ('previous_review_status', 'new_excerpts_hash'):
        if column not in columns:
            c.execute(f'ALTER TABLE knowledge_source_revision_event ADD COLUMN {column} TEXT')
    c.execute("INSERT OR IGNORE INTO schema_migration(version,applied_at) VALUES (71,datetime('now'))")


def source_state(c, child_id, source_id):
    row = c.execute('SELECT * FROM knowledge_source WHERE child_id=? AND source_id=?', (child_id, source_id)).fetchone()
    if not row:
        raise KeyError(source_id)
    excerpts = [dict(r) for r in c.execute('SELECT * FROM knowledge_excerpt WHERE source_id=? ORDER BY sequence,excerpt_id', (source_id,)).fetchall()]
    return dict(row), excerpts, _hash([dict(row), excerpts])


def complete_excerpts(excerpts, lines):
    return (len(excerpts) == len(lines) and all(
        row['sequence'] == n and row['content_text'] == line
        and row['content_hash'] == hashlib.sha256(line.encode()).hexdigest()
        for n, (row, line) in enumerate(zip(excerpts, lines), 1)))


class KnowledgeSourceRevisionService:
    def __init__(self, database, *, replay=None, reconcile=None):
        self.database = database
        self.replay = replay
        self.reconcile = reconcile

    def _preview(self, c, child_id, source_id, content_text):
        from .knowledge_base import source_lines
        source, excerpts, version = source_state(c, child_id, source_id)
        content, lines = source_lines(content_text)
        if hashlib.sha256(content.encode()).hexdigest() != source['content_hash']:
            raise SourceRevisionConflict('SOURCE_REVISION_ORIGINAL_HASH_MISMATCH')
        if not source['current_version']:
            raise SourceRevisionConflict('SOURCE_REVISION_SUPERSEDED')
        complete = complete_excerpts(excerpts, lines)
        return {'source': source, 'source_version': version, 'status': 'complete' if complete else 'incomplete',
                'stored_excerpts': [{'text': r['content_text'], 'sequence': r['sequence']} for r in excerpts],
                'replacement_text': content, 'replacement_length': len(content),
                'stored_length': sum(len(r['content_text']) for r in excerpts),
                'review_required': True, 'purpose_permission_inherited': False}, lines

    def preview(self, child_id, source_id, content_text):
        with self.database.connect() as c:
            c.execute('BEGIN')
            return self._preview(c, child_id, source_id, content_text)[0]

    def get(self, child_id, source_id):
        with self.database.connect() as c:
            c.execute('BEGIN')
            source, excerpts, version = source_state(c, child_id, source_id)
            history = [dict(r) for r in c.execute('''SELECT event_id,old_source_id,new_source_id,action,actor,occurred_at
                FROM knowledge_source_revision_event WHERE child_id=? AND (old_source_id=? OR new_source_id=?)
                ORDER BY occurred_at,event_id''', (child_id, source_id, source_id)).fetchall()]
            return {'current': source, 'source_version': version,
                    'stored_excerpts': [{'text': r['content_text'], 'sequence': r['sequence']} for r in excerpts],
                    'history': history, 'purpose_permission_inherited': False}

    def _receipt(self, c, child_id, key, request_hash):
        row = c.execute('SELECT * FROM knowledge_source_revision_event WHERE child_id=? AND client_action_id=?', (child_id, key)).fetchone()
        if not row:
            return None
        if row['request_hash'] != request_hash:
            raise SourceRevisionConflict('SOURCE_REVISION_KEY_CONFLICT')
        source, _, version = source_state(c, child_id, row['new_source_id'])
        return {'receipt': dict(row), 'current': source, 'source_version': version,
                'purpose_permission_inherited': False}

    def revise(self, child_id, source_id, *, content_text, expected_version, client_action_id):
        request_hash = _hash(['revise', source_id, content_text, expected_version])
        with self.database.external_effect_guard(), self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            source_state(c, child_id, source_id)
            prior = self._receipt(c, child_id, client_action_id, request_hash)
            if prior:
                return prior
            preview, lines = self._preview(c, child_id, source_id, content_text)
            if preview['source_version'] != expected_version:
                raise SourceRevisionConflict('SOURCE_REVISION_CHANGED')
            if preview['status'] != 'incomplete':
                raise SourceRevisionConflict('SOURCE_REVISION_ALREADY_COMPLETE')
            new_id, now = str(uuid4()), utc_now()
            c.execute("UPDATE knowledge_source SET current_version=0,review_status='superseded' WHERE source_id=?", (source_id,))
            source = {**preview['source'], 'source_id': new_id, 'current_version': 1,
                      'review_status': 'quarantined', 'predecessor_id': source_id, 'imported_at': now}
            columns = ','.join(source)
            c.execute(f"INSERT INTO knowledge_source({columns}) VALUES ({','.join('?' for _ in source)})", tuple(source.values()))
            for sequence, text in enumerate(lines, 1):
                c.execute('''INSERT INTO knowledge_excerpt(excerpt_id,source_id,sequence,content_text,content_hash)
                             VALUES (?,?,?,?,?)''', (str(uuid4()), new_id, sequence, text, hashlib.sha256(text.encode()).hexdigest()))
            _, new_excerpts, _ = source_state(c, child_id, new_id)
            c.execute('''INSERT INTO knowledge_source_revision_event VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                      (str(uuid4()), child_id, source_id, new_id, 'revise', client_action_id, request_hash,
                       expected_version, 'parent', now, preview['source']['review_status'], _hash(new_excerpts)))
            # Shared read qualification observes superseded before caches are replayed.
            if self.replay:
                self.replay(child_id, connection=c, reason='source_revised')
            if self.reconcile:
                self.reconcile(child_id, connection=c)
            result = self._receipt(c, child_id, client_action_id, request_hash)
            c.commit()
            return result

    def review(self, child_id, source_id, *, expected_version, client_action_id):
        request_hash = _hash(['review', source_id, expected_version])
        with self.database.external_effect_guard(), self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            source, excerpts, version = source_state(c, child_id, source_id)
            prior = self._receipt(c, child_id, client_action_id, request_hash)
            if prior:
                return prior
            if version != expected_version or not source['current_version'] or not source['predecessor_id']:
                raise SourceRevisionConflict('SOURCE_REVISION_CHANGED')
            if source['review_status'] != 'quarantined' or source['license_status'] == 'unknown':
                raise SourceRevisionConflict('SOURCE_REVISION_REVIEW_NOT_ALLOWED')
            revision = c.execute("SELECT new_excerpts_hash FROM knowledge_source_revision_event WHERE new_source_id=? AND action='revise'", (source_id,)).fetchone()
            if not revision or revision[0] != _hash(excerpts):
                raise SourceRevisionConflict('SOURCE_REVISION_EXCERPTS_CHANGED')
            c.execute("UPDATE knowledge_source SET review_status='approved' WHERE source_id=?", (source_id,))
            c.execute('INSERT INTO knowledge_source_revision_event VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                      (str(uuid4()), child_id, None, source_id, 'review', client_action_id, request_hash,
                       version, 'parent', utc_now(), source['review_status'], _hash(excerpts)))
            result = self._receipt(c, child_id, client_action_id, request_hash)
            c.commit()
            return result
