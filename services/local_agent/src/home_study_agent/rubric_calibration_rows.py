"""Bounded raw SQLite rows for one synchronous calibration read.

Only enumerated, deterministic table lookups are reused. Validators still run,
including external file, authorization, trust and current-time checks.
"""
import sqlite3

from .rubric_calibration_evidence import require
from .rubric_calibration_read import _changes


QUERIES = frozenset({
    "SELECT grade FROM child_profile WHERE child_id=? AND status='active'",
    'SELECT grade FROM child_profile WHERE child_id=?',
    'SELECT * FROM knowledge_node WHERE knowledge_node_id=?',
    'SELECT * FROM diagnostic_item WHERE diagnostic_item_id=?',
    "SELECT 1 FROM sqlite_master WHERE name='rubric_assessment'",
    'SELECT 1 FROM sqlite_master WHERE name=?',
    'SELECT * FROM learning_evidence WHERE evidence_id=? AND child_id=?',
    'SELECT * FROM rubric_assessment WHERE child_id=? AND assessment_id=?',
    'SELECT * FROM rubric_assessment WHERE child_id=? AND model_call_id=?',
    'SELECT * FROM rubric_review_event WHERE assessment_id=? ORDER BY revision DESC LIMIT 1',
    'SELECT * FROM rubric_calibration_protocol WHERE child_id=? AND protocol_id=?',
    'SELECT * FROM curriculum_supply_package WHERE candidate_id=? AND child_id=?',
    'SELECT * FROM curriculum_supply_event WHERE candidate_id=? ORDER BY version',
    'SELECT * FROM curriculum_standard_requirement WHERE requirement_id=?',
    'SELECT * FROM curriculum_standard_source WHERE source_id=?',
    'SELECT * FROM curriculum_standard_page WHERE source_id=? AND page_number=?',
    'SELECT * FROM specialized_supply_job WHERE job_id=? AND child_id=?',
    'SELECT * FROM variant_generation_job WHERE job_id=? AND child_id=?',
    "SELECT * FROM specialized_supply_candidate WHERE candidate_id=? AND child_id=? AND diagnostic_item_id=? AND status='promoted'",
    "SELECT * FROM variant_candidate WHERE candidate_id=? AND child_id=? AND diagnostic_item_id=? AND status='promoted'",
    "SELECT di.*,kn.grade_band FROM diagnostic_item di JOIN knowledge_node kn USING(knowledge_node_id)\n        WHERE di.diagnostic_item_id=? AND di.status='active' AND kn.status='active'",
    'SELECT * FROM curriculum_supply_selection\n        WHERE child_id=? AND subject=? ORDER BY version DESC LIMIT 1',
})


class _Rows:
    def __init__(self, rows):
        self.rows, self.position = rows, 0

    def fetchone(self):
        if self.position == len(self.rows):
            return None
        row = self.rows[self.position]
        self.position += 1
        return row

    def fetchall(self):
        rows = list(self.rows[self.position:])
        self.position = len(self.rows)
        return rows

    def __iter__(self):
        while self.position < len(self.rows):
            yield self.fetchone()


class ReadRows:
    def __init__(self, connection, *, max_entries=4096):
        require(connection.in_transaction, 'READ_TRANSACTION_REQUIRED')
        self.connection = connection
        self.changes = _changes(connection)
        self.rows = {}
        self.max_entries = max_entries
        self.bytes = 0
        self.closed = False

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def _current(self):
        require(not self.closed and self.connection.in_transaction
                and _changes(self.connection) == self.changes, 'READ_SCOPE_CHANGED')

    def execute(self, sql, parameters=()):
        self._current()
        if sql not in QUERIES or getattr(self.connection, 'row_factory', None) is not sqlite3.Row:
            return self.connection.execute(sql, parameters)
        key = sql, tuple(parameters)
        if key in self.rows:
            return _Rows(self.rows[key])
        cursor = self.connection.execute(sql, parameters)
        if len(self.rows) >= self.max_entries or self.bytes >= 8_000_000:
            return cursor
        rows = tuple(cursor.fetchall())
        size = sum(len(v) if isinstance(v, (str, bytes)) else 8 for row in rows for v in row)
        if self.bytes + size <= 8_000_000:
            self.rows[key] = rows
            self.bytes += size
        return _Rows(rows)

    def commit(self):
        require(False, 'READ_SCOPE_CHANGED')

    rollback = commit

    def __enter__(self):
        from .original_connection_read import original_connection_view,registered_view,registered_original
        self._original_source_view_scope = None
        if registered_view(self.connection) or registered_original(self.connection):
            scope = original_connection_view(self,self.connection)
            scope.__enter__()
            self._original_source_view_scope = scope
        return self

    def __exit__(self, *error):
        try:
            scope = self._original_source_view_scope
            if scope is not None:
                return scope.__exit__(*error)
        finally:
            self.closed = True
            self.rows.clear()
