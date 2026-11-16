"""Ephemeral prior-history index; no score, source or review is cached."""
from bisect import bisect_left, bisect_right
from datetime import timedelta


class _EarlierAtTime:
    """Range minimum rowid over exact timestamps, including out-of-order history."""
    def __init__(self, entries):
        entries.sort()
        self.times = [at for at, _ in entries]
        self.size = len(entries)
        self.tree = [float('inf')] * self.size + [rowid for _, rowid in entries]
        for index in range(self.size - 1, 0, -1):
            self.tree[index] = min(self.tree[index * 2], self.tree[index * 2 + 1])

    def before(self, rowid, occurred):
        left = bisect_right(self.times, occurred - timedelta(hours=24)) + self.size
        right = bisect_right(self.times, occurred) + self.size
        earliest = float('inf')
        while left < right:
            if left % 2:
                earliest = min(earliest, self.tree[left])
                left += 1
            if right % 2:
                right -= 1
                earliest = min(earliest, self.tree[right])
            left //= 2
            right //= 2
        return earliest < rowid


class RubricPriorIndex:
    """Use within one synchronous borrowed read transaction and child.

    The caller must not end its transaction inside this context. SQLite's
    connection counters are change guards, not a transaction epoch detector.
    """
    def __init__(self, c, child_id):
        if not c.in_transaction:
            raise RuntimeError('RUBRIC_PRIOR_READ_TRANSACTION_REQUIRED')
        self.c, self.child_id = c, child_id
        self.changes = c.total_changes
        self.data_version = c.execute('PRAGMA data_version').fetchone()[0]
        self.schema_version = c.execute('PRAGMA schema_version').fetchone()[0]
        self.active = self.entered = False
        self.rows = None

    def __enter__(self):
        if self.entered:
            raise RuntimeError('RUBRIC_PRIOR_READ_SCOPE_CHANGED')
        self.active = self.entered = True
        try:
            self._check(self.c, self.child_id)
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *_):
        self.active = False
        self.rows = self.questions = self.bad_rows = self.bad_limits = None

    def _check(self, c, child_id):
        if (not self.active or c is not self.c or child_id != self.child_id or not c.in_transaction
                or c.total_changes != self.changes
                or c.execute('PRAGMA data_version').fetchone()[0] != self.data_version
                or c.execute('PRAGMA schema_version').fetchone()[0] != self.schema_version):
            raise RuntimeError('RUBRIC_PRIOR_READ_SCOPE_CHANGED')

    def _load(self):
        from .learning_rubric import _prior_question_time, _prior_answer_safe_after
        rows = self.c.execute('SELECT rowid AS _rowid,assessment_id,step_id,payload_json FROM rubric_assessment WHERE child_id=? ORDER BY rowid',
            (self.child_id,)).fetchall()
        self.rows, groups, self.bad_rows, self.bad_limits = {}, {}, [], []
        unknown, latest_safe = False, None
        for row in rows:
            rowid = row['_rowid']
            self.rows[row['assessment_id']] = rowid
            try:
                identity, at = _prior_question_time(row['payload_json'], connection=self.c)
            except (ValueError, TypeError, AttributeError, KeyError):
                safe_after = _prior_answer_safe_after(self.c, self.child_id, row['step_id'])
                unknown = unknown or safe_after is None
                if safe_after is not None:
                    latest_safe = max(latest_safe, safe_after) if latest_safe is not None else safe_after
                self.bad_rows.append(rowid)
                self.bad_limits.append((unknown, latest_safe))
            else:
                groups.setdefault(identity, []).append((at, rowid))
        self.questions = {identity: _EarlierAtTime(entries) for identity, entries in groups.items()}

    def assisted(self, c, child_id, assessment_id, identity, occurred):
        self._check(c, child_id)
        if self.rows is None:
            self._load()
        rowid = self.rows.get(assessment_id)
        if rowid is None:
            raise RuntimeError('RUBRIC_PRIOR_ASSESSMENT_NOT_IN_SNAPSHOT')
        earlier_bad = bisect_left(self.bad_rows, rowid) - 1
        if earlier_bad >= 0:
            unknown, safe_after = self.bad_limits[earlier_bad]
            if unknown or safe_after is not None and occurred < safe_after:
                return True
        questions = self.questions.get(identity)
        return bool(questions and questions.before(rowid, occurred))
