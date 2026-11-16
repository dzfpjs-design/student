"""Request-local calibration dependencies, not a durable authorization cache.

Only owned connections and audited native/self-contained sources participate.
New transactions replay every SQL dependency. A writer's own mutations replay
affected dependencies, including triggers; unknown mutations replay everything.
Signed authority, expiry and policy remain live on every reuse. Managed oral
recordings are reread and hashed on reuse; no path/mtime or plaintext cache.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import copy, deepcopy
from functools import wraps
from datetime import datetime, timezone
import inspect
import sqlite3

from .rubric_calibration_evidence import digest, require


_SESSION = ContextVar('calibration_validation_session', default=None)
_CAPTURE = ContextVar('calibration_dependency_capture', default=None)


def _same_rows(actual, expected):
    return len(actual) == len(expected) and all(
        left.keys() == right.keys() and all(type(a) is type(b) and a == b for a, b in zip(left, right))
        for left, right in zip(actual, expected))


@contextmanager
def validation_session():
    if _SESSION.get() is not None:
        yield
        return
    token = _SESSION.set({})
    try:
        yield
    finally:
        _SESSION.get().clear()
        _SESSION.reset(token)


def scoped_validation(function):
    if inspect.iscoroutinefunction(function):
        @wraps(function)
        async def asynchronous(*args, **kwargs):
            with validation_session():
                return await function(*args, **kwargs)
        return asynchronous
    @wraps(function)
    def synchronous(*args, **kwargs):
        with validation_session():
            return function(*args, **kwargs)
    return synchronous


def unsupported_dependency():
    capture = _CAPTURE.get()
    if capture is not None:
        capture.supported = False


def recording_checked(connection, path, expected_hash, byte_count):
    """Private dependency receipt from the managed original-recording reader."""
    from .database import local_media_scope
    capture = _CAPTURE.get()
    if capture is None:
        return
    scope = local_media_scope(connection)
    value = (expected_hash, byte_count, scope)
    previous = capture.recordings.get(path)
    if (not scope or type(byte_count) is not int or byte_count <= 0
            or previous is not None and previous != value):
        capture.supported = False
        return
    if previous is None:
        capture.recording_bytes += byte_count
    # Only small private descriptors persist for this request, never audio.
    if len(capture.recordings) >= 4096 or capture.recording_bytes > 512 * 1024 * 1024:
        capture.supported = False
        return
    capture.recordings[path] = value


def scope_finished(scope):
    capture = _CAPTURE.get()
    if capture is not None:
        names = ('sources', 'index', 'invalid', 'malformed', 'used', 'verified', 'policy')
        capture.scope = {name: deepcopy(getattr(scope, name)) for name in names if hasattr(scope, name)}
        capture.math = set(scope.signatures.checked)
        capture.math_size = scope.signatures.size


class _Transcript:
    def __init__(self, connection):
        self.connection = connection
        self.reads, self.dependencies = {}, {}
        self.active_sql, self.scope = None, None
        self.supported, self.size = True, 0
        self.functions = set()
        self.recordings, self.recording_bytes = {}, 0

    def __getattr__(self, name):
        value = getattr(self.connection, name)
        if callable(value) and name not in {'row_factory', 'text_factory'}:
            self.supported = False
        return value

    def observe(self, action, first, second, database, trigger):
        if self.active_sql is not None:
            dependencies = self.dependencies.setdefault(self.active_sql, set())
            if action == sqlite3.SQLITE_READ:
                dependencies.add((database, first))
        if action == sqlite3.SQLITE_FUNCTION:
            self.functions.add(second)
        return (sqlite3.SQLITE_OK if action in {
            sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
            sqlite3.SQLITE_RECURSIVE} else sqlite3.SQLITE_DENY)

    def execute(self, sql, parameters=()):
        from .rubric_calibration_rows import _Rows
        self.active_sql = sql
        try:
            cursor = self.connection.execute(sql, parameters)
            positional = isinstance(parameters, (tuple, list)) and all(
                type(value) in (type(None), str, bytes, int, float) for value in parameters)
            if not self.supported or not positional or len(self.reads) >= 12000 or self.size >= 16_000_000:
                self.supported = False
                return cursor
            rows = tuple(cursor.fetchall())
        finally:
            self.active_sql = None
        key = sql, tuple((type(value), value) for value in parameters)
        if key not in self.reads:
            self.reads[key] = rows
            self.size += sum(len(v) if isinstance(v, (str, bytes)) else 8 for r in rows for v in r)
            self.supported = self.size <= 16_000_000
        else:
            require(_same_rows(self.reads[key], rows), 'READ_SCOPE_CHANGED')
        return _Rows(rows)

    def recordings_current(self, connection):
        from .database import local_media_scope
        from .school_spoken_score import recording_bytes_checked
        scope = local_media_scope(connection)
        for path, (expected_hash, byte_count, original_scope) in self.recordings.items():
            if scope != original_scope:
                return False
            try:
                if recording_bytes_checked(connection, path, expected_hash) != byte_count:
                    return False
            except ValueError:
                return False
        return True

    def compare(self, connection):
        if not self.recordings_current(connection):
            return False
        if connection.total_changes != connection.known_changes:
            connection.dirty = None
            connection.known_changes = connection.total_changes
        started = (connection.epoch, connection.mutation_generation, connection.total_changes)
        changed = connection.changed_tables(self)
        if changed == set():
            return started == (connection.epoch, connection.mutation_generation, connection.total_changes)
        for (sql, parameters), expected in self.reads.items():
            dependencies = self.dependencies.get(sql)
            if changed is not None and dependencies is not None and not dependencies.intersection(changed):
                continue
            actual = tuple(connection.execute(sql, tuple(value for kind, value in parameters)).fetchall())
            if not _same_rows(actual, expected):
                return False
        return started == (connection.epoch, connection.mutation_generation, connection.total_changes)


class _OwnedCursor:
    """Keep native cursor behavior, but mark exposure of its connection."""
    __slots__ = ('_cursor', '_owner')

    def __init__(self, cursor, owner):
        self._cursor, self._owner = cursor, owner

    def __getattr__(self, name):
        return getattr(self._cursor, name)

    def __setattr__(self, name, value):
        if name in self.__slots__:
            object.__setattr__(self, name, value)
        else:
            if name == 'row_factory':
                self._owner.reusable, self._owner.dirty = False, None
            setattr(self._cursor, name, value)

    @property
    def connection(self):
        return self._owner.connection

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._cursor)

    def execute(self, *args, **kwargs):
        self._owner.dirty = None
        self._cursor.execute(*args, **kwargs)
        return self

    def executemany(self, *args, **kwargs):
        self._owner.dirty = None
        self._cursor.executemany(*args, **kwargs)
        return self

    def executescript(self, *args, **kwargs):
        self._owner.dirty = None
        self._cursor.executescript(*args, **kwargs)
        return self


class _OwnedConnection:
    """Own authorizer/trace multiplexing only on a newly opened connection."""
    def __init__(self, connection):
        self._connection = connection
        self.reusable = True
        self.epoch, self.depth = 0, 0
        self.mutation_generation = self.unknown_generation = 0
        self.table_generations = {}
        self.dirty = None
        self.known_changes = connection.total_changes
        self.external_trace, self.observer = None, None
        self.statement = None
        self.statements = {}
        connection.set_authorizer(self._authorize)
        connection.set_trace_callback(self._trace)

    @property
    def dirty(self):
        return self._dirty

    @dirty.setter
    def dirty(self, value):
        self._dirty = value
        if value is None:
            # Unknown effects invalidate every older ticket, not just the last
            # ticket verified on this connection.
            self.mutation_generation += 1
            self.unknown_generation = self.mutation_generation

    def __getattr__(self, name):
        value = getattr(self._connection, name)
        if callable(value) and name not in {'commit', 'rollback', 'row_factory', 'text_factory'}:
            self.reusable, self.dirty = False, None
        return value

    @property
    def connection(self):
        self.reusable, self.dirty = False, None
        return self._connection

    @property
    def row_factory(self):
        return self._connection.row_factory

    @row_factory.setter
    def row_factory(self, value):
        self.connection.row_factory = value

    @property
    def text_factory(self):
        return self._connection.text_factory

    @text_factory.setter
    def text_factory(self, value):
        self.connection.text_factory = value

    @property
    def isolation_level(self):
        return self._connection.isolation_level

    @isolation_level.setter
    def isolation_level(self, value):
        self.connection.isolation_level = value

    def cursor(self, *args, **kwargs):
        if args or kwargs:
            self.reusable, self.dirty = False, None
        return _OwnedCursor(self._connection.cursor(*args, **kwargs), self)

    def executemany(self, *args, **kwargs):
        return self.cursor().executemany(*args, **kwargs)

    def executescript(self, *args, **kwargs):
        return self.cursor().executescript(*args, **kwargs)

    def set_trace_callback(self, callback):
        self.external_trace = callback

    def set_authorizer(self, callback):
        self.connection.set_authorizer(callback)

    def create_function(self, *args, **kwargs):
        return self.connection.create_function(*args, **kwargs)

    def create_aggregate(self, *args, **kwargs):
        return self.connection.create_aggregate(*args, **kwargs)

    def create_collation(self, *args, **kwargs):
        return self.connection.create_collation(*args, **kwargs)

    def _trace(self, sql):
        operation = sql.lstrip().split(None, 1)[0].upper()
        if operation in {'BEGIN', 'COMMIT', 'END', 'ROLLBACK', 'SAVEPOINT', 'RELEASE'}:
            self.epoch += 1
            self.dirty = None
        elif not self.depth and operation not in {'SELECT', 'WITH', 'PRAGMA'}:
            self.dirty = None
        if self.external_trace is not None:
            self.external_trace(sql)

    def _authorize(self, action, first, second, database, trigger):
        statement = self.statement
        if statement is not None:
            if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}:
                statement['tables'].add((database, first))
            elif action in {sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT}:
                statement['transaction'] = True
            elif action == sqlite3.SQLITE_FUNCTION:
                if second not in {'json_extract', 'json_valid', 'coalesce'}:
                    statement['functions'] = True
            elif action not in {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ,
                                sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_RECURSIVE}:
                statement['unknown'] = True
        return self.observer(action, first, second, database, trigger) if self.observer else sqlite3.SQLITE_OK

    @contextmanager
    def recording(self, transcript):
        require(self.observer is None, 'READ_SCOPE_CHANGED')
        from .original_connection_read import (original_connection_view,registered_view,
            original_connection,poison_original,CODE)
        from contextlib import nullcontext
        registered = registered_view(self)
        scope=original_connection_view(transcript,self)if registered else nullcontext()
        with scope:
            original = original_connection(self) if registered else self._connection
            authorizer = self._authorize
            def restore_original_authorizer():
                if registered:
                    sqlite3.Connection.set_authorizer(original,authorizer)
                else:
                    original.set_authorizer(authorizer)
            self.observer = transcript.observe
            body_error = None
            installed = False
            try:
                restore_original_authorizer()
                installed = True
                yield
            except BaseException as error:
                body_error = error
                if registered and not installed:
                    poison_original(original,('recording_authorizer_setup_unconfirmed',self,error))
                raise
            finally:
                self.observer = None
                try:
                    if registered:
                        try:
                            require(original_connection(self)is original,CODE)
                        except BaseException as error:
                            poison_original(original,('recording_original_chain',self,body_error,error))
                            if body_error is None:raise
                finally:
                    try:
                        restore_original_authorizer()
                    except BaseException as error:
                        if registered:
                            poison_original(original,('recording_authorizer_unconfirmed',self,body_error,error))
                        if body_error is None:raise

    def execute(self, sql, parameters=()):
        before = self._connection.total_changes
        if before != self.known_changes:
            self.dirty = None
        metadata = self.statements.setdefault(sql, {
            'tables': set(), 'unknown': False, 'functions': False, 'transaction': False})
        self.statement = metadata
        self.depth += 1
        try:
            return _OwnedCursor(self._connection.execute(sql, parameters), self)
        finally:
            self.depth -= 1
            self.statement = None
            after = self._connection.total_changes
            if metadata['transaction']:
                self.epoch += 1
                self.dirty = None
            if (metadata['unknown'] or (metadata['functions'] and metadata['tables'])
                    or (after != before and not metadata['tables'])):
                self.dirty = None
                self.statements.clear()
                if self.reusable:
                    self._connection.set_authorizer(self._authorize)
            elif metadata['tables']:
                self.mutation_generation += 1
                for table in metadata['tables']:
                    self.table_generations[table] = self.mutation_generation
                if self.dirty is not None:
                    self.dirty.update(metadata['tables'])
                if len(self.table_generations) > 4096:
                    self.table_generations.clear()
                    self.dirty = None
            self.known_changes = after
            if len(self.statements) > 4096:
                self.statements.clear()
                self.dirty = None
                if self.reusable:
                    self._connection.set_authorizer(self._authorize)

    def changed_tables(self, ticket):
        if (not self.reusable or getattr(ticket, 'verified_connection', None) is not self or
                getattr(ticket, 'verified_epoch', None) != self.epoch or
                self._connection.total_changes != self.known_changes or
                getattr(ticket, 'verified_generation', -1) < self.unknown_generation):
            return None
        return {table for table, generation in self.table_generations.items()
                if generation > ticket.verified_generation}

    def verified(self, ticket):
        if self._connection.total_changes != self.known_changes:
            self.dirty = None
        ticket.verified_connection, ticket.verified_epoch = self, self.epoch
        ticket.verified_generation = self.mutation_generation
        self.known_changes = self._connection.total_changes


@contextmanager
def calibration_connection(database):
    with database.connect() as c:
        if _SESSION.get() is None:yield c
        else:
            from .original_connection_read import original_connection_view
            owned=_OwnedConnection(c)
            with original_connection_view(owned,c):yield owned


def _warm_score_protocols(c, database, child_id):
    from .rubric_calibration import RubricCalibrationService
    tables = {row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'rubric_assessment', 'rubric_review_event', 'rubric_calibration_protocol',
            'rubric_calibration_event'} <= tables:
        return False
    # This is a cost hint, never admission: the original caller still validates
    # the current assessment, original source, files, trust and effective time.
    protocols = c.execute('''SELECT DISTINCT p.* FROM rubric_assessment a
        JOIN rubric_calibration_protocol p ON p.child_id=a.child_id AND p.protocol_id=
          json_extract(CASE WHEN json_valid(a.payload_json) THEN a.payload_json ELSE '{}' END,
                       '$.calibration_binding.protocol_id')
        WHERE a.child_id=? AND EXISTS(
          SELECT 1 FROM rubric_calibration_event e
          WHERE e.protocol_id=p.protocol_id AND e.operation='qualify') AND NOT EXISTS(
          SELECT 1 FROM rubric_review_event r WHERE r.assessment_id=a.assessment_id)''', (child_id,)).fetchall()
    service = RubricCalibrationService(database)
    for row in protocols:
        try:
            service._read(c, row, datetime.now(timezone.utc))
        except (ValueError, TypeError, KeyError, IndexError):
            # Bad optional hints must neither block unrelated work nor approve
            # this score. A failed capture yields no reusable proof.
            continue
    return any(ticket.verified_connection is c and ticket.verified_epoch == c.epoch
               for ticket in (_SESSION.get() or {}).values())


class CalibrationWriterBusyError(RuntimeError):
    """No caller writes ran; retry the same request after bounded contention."""


def _upgrade_busy(error):
    code = getattr(error, 'sqlite_errorcode', None)
    if type(code) is int:
        return code & 255 == 5
    # Python 3.9 has no sqlite_errorcode. Limit its fallback to SQLite's exact
    # BUSY message at this one pre-yield upgrade, never catch IO/LOCKED errors.
    return str(error) == 'database is locked'


@contextmanager
def calibrated_write(database, child_id):
    """Own a new connection; never wrap or take ownership of a borrowed adapter."""
    from .database import Database
    if type(database) is not Database:
        with database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            yield c
        return
    for attempt in range(2):
        token = _SESSION.set({})
        try:
            with calibration_connection(database) as c:
                c.execute('BEGIN')
                prepared = _warm_score_protocols(c, database, child_id)
                if not prepared:
                    # No authorization can be reused. Discard the optional read
                    # snapshot and retain serialized writer/revision semantics.
                    c.rollback()
                    c.execute('BEGIN IMMEDIATE')
                else:
                    try:
                        c.execute('UPDATE child_profile SET child_id=child_id WHERE 0')
                    except sqlite3.OperationalError as error:
                        if not _upgrade_busy(error):
                            raise
                        if attempt:
                            raise CalibrationWriterBusyError('CALIBRATION_WRITER_BUSY') from error
                        continue
                # Caller exceptions are outside the retry boundary. A retry
                # above closes the connection and discards its entire proof.
                yield c
                return
        finally:
            _SESSION.get().clear()
            _SESSION.reset(token)


def has_prepared(binding):
    session = _SESSION.get()
    return bool(session and binding and any(t.result['protocol_id'] == binding.get('protocol_id')
                                           for t in session.values()))


def begin_validated_write(connection, binding):
    if not binding or binding.get('status') != 'bound':
        connection.execute('BEGIN IMMEDIATE')
        return
    from .rubric_calibration_host import revalidate
    connection.execute('BEGIN')
    revalidate(connection, binding, phase='dispatch')
    # Upgrade this exact validated snapshot, without firing any row trigger.
    # A concurrent commit makes SQLite reject the upgrade, never reuse stale
    # evidence. The caller obtains its acceptance time only after this returns.
    connection.execute('UPDATE learning_evaluation_execution SET state=state WHERE 0')


def assert_prepared_dependencies(connection, binding):
    """A commit cannot repair proof dependencies changed by its own writes.

    This is an additional rejection only. The caller still runs the original
    live source/trust/expiry/assessment guard after this database comparison.
    """
    session = _SESSION.get()
    if not session or not isinstance(connection, _OwnedConnection) or not connection.reusable:
        return
    for ticket in session.values():
        if (ticket.result['protocol_id'] == binding.get('protocol_id')
                and getattr(ticket, 'verified_connection', None) is connection
                and getattr(ticket, 'verified_epoch', None) == connection.epoch):
            require(ticket.compare(connection), 'PREPARED_EVIDENCE_CHANGED')
            connection.verified(ticket)


def read_current(service, c, row, now, read_full):
    from .family_clock import family_today
    from .rubric_calibration_read import ReadScope
    session = _SESSION.get()
    if (session is None or not isinstance(c, _OwnedConnection) or not c.reusable
            or c.row_factory is not sqlite3.Row or c.text_factory is not str
            or service.authority_resolver is not None):
        return read_full(c)
    key = digest(dict(row))
    ticket = session.get(key)
    def current_day_read():
        day = family_today()
        value = read_full(c)
        require(day == family_today(), 'READ_SCOPE_CHANGED')
        return value
    if ticket is not None and now >= ticket.now and family_today() == ticket.day:
        if ticket.compare(c):
            scope = ReadScope(c, row['child_id'], None)
            for name, value in ticket.scope.items():
                # Nested entries are private captured values, read-only in _live
                # and finish. resolve() still deep-copies every returned proof.
                setattr(scope, name, copy(value))
            scope.signatures.checked = set(ticket.math)
            scope.signatures.size = ticket.math_size
            if service._live(c, row, now, _scope=scope):
                scope.signatures.clear()
                session.pop(key, None)
                return read_full(c)
            scope.finish()
            # Trust checks may invoke local host code; recheck external media
            # before returning a reused result from that boundary.
            if not ticket.recordings_current(c):
                session.pop(key, None)
                return read_full(c)
            if family_today() != ticket.day:
                session.pop(key, None)
                return current_day_read()
            c.verified(ticket)
            return deepcopy(ticket.result)
        session.pop(key, None)
    transcript = _Transcript(c)
    started_day = family_today()
    token = _CAPTURE.set(transcript)
    outside_recording = False
    try:
        from .original_connection_read import _UnrecordedSourceRead
        try:
            with c.recording(transcript):
                result = read_full(transcript)
        except _UnrecordedSourceRead:
            from .rubric_calibration import RubricCalibrationService
            from types import CodeType, FunctionType
            producer = RubricCalibrationService._read.__code__
            require(inspect.currentframe().f_back.f_code is producer
                and type(read_full) is FunctionType
                and any(type(code)is CodeType and code is read_full.__code__
                    and code.co_name == 'read_full' for code in producer.co_consts), 'READ_SCOPE_CHANGED')
            # A fixed C-level source proof cannot masquerade as a complete
            # dependency transcript. Keep full/live validation, no cache hint.
            unsupported_dependency()
            outside_recording = True
    finally:
        _CAPTURE.reset(token)
    if outside_recording:
        # The original observer and capture scope have both retired before the
        # complete, read-only source validation is repeated under its marker.
        from .standard_relocation_provenance import business_snapshot
        with business_snapshot(c):
            result = read_full(c)
    if started_day != family_today():
        session.pop(key, None)
        return current_day_read()
    supported = (c.reusable and transcript.supported and transcript.scope and transcript.scope['sources']
        and transcript.functions <= {'json_extract'} and
        all(s['modality'] in {'original_written_text', 'original_spoken_text'}
            for s in transcript.scope['sources'].values()) and
        (all(s['modality'] == 'original_written_text' for s in transcript.scope['sources'].values())
            or bool(transcript.recordings)))
    if result['score_qualified'] and supported:
        require(transcript.recordings_current(c), 'READ_SCOPE_CHANGED')
        transcript.now, transcript.day, transcript.result = now, started_day, deepcopy(result)
        session.clear()
        session[key] = transcript
        c.verified(transcript)
    return result
