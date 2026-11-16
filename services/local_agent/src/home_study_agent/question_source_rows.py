"""Bounded storage reads for one synchronous question catalog, not authority."""
import sqlite3

from .rubric_calibration_rows import _Rows
from .rubric_calibration_read import _changes


class QuestionSourceRows:
    def __init__(self, connection, items):
        self.connection, self.rows = connection, {}
        self.changes = getattr(connection, 'total_changes', None)
        self.enabled = (getattr(connection, 'in_transaction', False)
                        and getattr(connection, 'row_factory', None) is sqlite3.Row
                        and type(self.changes) is int)
        if not self.enabled or len(items) > 1024:
            return
        groups = {'variant_candidate': set(), 'specialized_supply_candidate': set()}
        for item in items:
            source = item.get('source_ref') or ''
            if source.startswith(('parent_approved_', 'local_verified_')) and not source.startswith('parent_approved_first:'):
                table = ('variant_candidate' if source.startswith(('parent_approved_variant:', 'local_verified_variant:'))
                         else 'specialized_supply_candidate')
                groups[table].add((source.partition(':')[2], item['diagnostic_item_id']))
        size = 0
        for table, pairs in groups.items():
            if not pairs:
                continue
            exists_sql = 'SELECT 1 FROM sqlite_master WHERE name=?'
            exists = tuple(connection.execute(exists_sql, (table,)).fetchall())
            self.rows[(exists_sql, (table,))] = exists
            if not exists:
                continue
            query = f'SELECT prompt FROM {table} WHERE candidate_id=? AND diagnostic_item_id=?'
            pairs = sorted(pairs)
            for start in range(0, len(pairs), 128):
                chunk = pairs[start:start + 128]
                placeholders = ','.join('(?,?)' for _ in chunk)
                found = {}
                for row in connection.execute(f'''WITH requested(candidate,item) AS (VALUES {placeholders})
                    SELECT requested.candidate,requested.item,s.prompt,s.candidate_id
                    FROM requested LEFT JOIN {table} s
                    ON s.candidate_id=requested.candidate AND s.diagnostic_item_id=requested.item''',
                    tuple(value for pair in chunk for value in pair)):
                    found.setdefault(tuple(row[:2]), []).append(tuple(row[2:]))
                for pair, values in found.items():
                    if len(values) == 1:
                        value = values[0]
                        self.rows[(query, pair)] = ((value[0],),) if value[1] is not None else ()
                        size += len(value[0]) if isinstance(value[0], (str, bytes)) else 8
            if table == 'specialized_supply_candidate':
                package_exists = "SELECT 1 FROM sqlite_master WHERE name='curriculum_supply_package'"
                exists = tuple(connection.execute(package_exists).fetchall())
                self.rows[(package_exists, ())] = exists
                if exists:
                    keys = sorted({pair[0] for pair in pairs})
                    package_sql = 'SELECT payload_json,source_snapshot_json FROM curriculum_supply_package WHERE candidate_id=?'
                    for start in range(0, len(keys), 128):
                        chunk = keys[start:start + 128]
                        placeholders = ','.join('(?)' for _ in chunk)
                        found = {}
                        for row in connection.execute(f'''WITH requested(candidate) AS (VALUES {placeholders})
                            SELECT requested.candidate,p.payload_json,p.source_snapshot_json,p.candidate_id
                            FROM requested LEFT JOIN curriculum_supply_package p ON p.candidate_id=requested.candidate''', chunk):
                            found.setdefault(row[0], []).append(tuple(row[1:]))
                        for key, values in found.items():
                            if len(values) == 1:
                                value = values[0]
                                self.rows[(package_sql, (key,))] = (value[:2],) if value[2] is not None else ()
                                size += sum(len(v) if isinstance(v, (str, bytes)) else 8 for v in value[:2])
            if size > 4_000_000:
                self.rows.clear()
                break

    def current(self):
        if self.rows and (not self.connection.in_transaction or _changes(self.connection) != self.changes):
            raise ValueError('QUESTION_SOURCE_READ_CHANGED')

    def execute(self, sql, parameters=()):
        self.current()
        key = sql, tuple(parameters)
        if key in self.rows:
            return _Rows(self.rows[key])
        return self.connection.execute(sql, parameters)
