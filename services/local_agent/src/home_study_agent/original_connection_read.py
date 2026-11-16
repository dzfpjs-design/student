"""Original admitted SQL read identity, never a publication/transaction capability.

A writable StudyConnection attribute is only a locator. Enrollment and retirement
are confined to the actual Database.connect generator; all identities live here.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import os
import sqlite3
import sys
import weakref

from . import restore_publication as pub

CODE = 'ORIGINAL_CONNECTION_READ_REQUIRED'
_CURSOR=sqlite3.Connection.cursor
_CURSOR_CLASS=sqlite3.Cursor
_PROGRESS=sqlite3.Connection.set_progress_handler
_CONNECTIONS = weakref.WeakKeyDictionary()
_VIEWS = weakref.WeakKeyDictionary()
_SAVED_PDF_READERS = weakref.WeakKeyDictionary()


class _ReadConnection:
    __slots__ = ('__weakref__',)
    def __new__(cls):
        raise pub.RestorePublicationError(CODE)


def _issuer():
    from .database import Database
    pub._need(sys._getframe(2).f_code is Database.connect.__wrapped__.__code__, CODE)


def _enroll(database, connection, admission):
    _issuer()
    from .database import Database, StudyConnection
    pub._need(type(database) is Database and type(connection) is StudyConnection
              and database._original_connections.get(id(connection)) is connection, CODE)
    lease = pub._original_read_lease(admission, database.path)
    # A connection that originally created an absent DB has no prior admitted
    # file identity. It receives no read capability; initialization is unchanged.
    if lease['identity'] is None:
        return None
    value = object.__new__(_ReadConnection)
    # No registry value strongly references its key or original connection/DB.
    _CONNECTIONS[value] = (weakref.ref(database), weakref.ref(connection), weakref.ref(admission),
                           lease['path'], lease['identity'], lease['actor'], os.getpid(),set())
    connection._original_read_locator = value
    return value


def _retire(value, connection):
    _issuer()
    if value is None:
        return
    state = _CONNECTIONS.pop(value, None)
    pub._need(state is not None and state[1]() is connection, CODE)
    from .standard_relocation_provenance import business_snapshot_active
    if business_snapshot_active(connection):
        state[2]().poison(('original_business_scope_unclosed',connection))
    if state[7]:
        state[2]().poison(('original_read_scopes_unclosed',tuple(state[7])))
    if getattr(connection, '_original_read_locator', None) is value:
        del connection._original_read_locator


def _core_state(connection, database=None):
    from .database import Database, StudyConnection
    pub._need(type(connection) is StudyConnection, CODE)
    value = getattr(connection, '_original_read_locator', None)
    pub._need(type(value) is _ReadConnection and value in _CONNECTIONS, CODE)
    state = _CONNECTIONS[value]
    db, original, admission = state[0](), state[1](), state[2]()
    pub._need(type(db) is Database and original is connection and admission is not None
              and (database is None or database is db) and db._original_connections.get(id(connection)) is connection
              and state[6] == os.getpid() and state[5] == pub._actor()
              and pub._path(db.path) == state[3], CODE)
    lease = pub._original_read_lease(admission, state[3])
    pub._need(lease['identity'] == state[4], CODE)
    return state, db, admission


def _state(connection, database=None):
    connection=original_connection(connection)
    state,db,admission=_core_state(connection,database)
    # The original SQLite C cursor is independent of caller-assigned execute,
    # cursor and row_factory attributes. A path only cross-checks the original
    # object; it never locates or creates a Database.
    cursor = _CURSOR(connection, factory=_CURSOR_CLASS)
    try:
        cursor.row_factory = None
        databases = cursor.execute('PRAGMA database_list').fetchall()
        pub._need(len(databases) == 1 and databases[0][1] == 'main'
                  and pub._path(databases[0][2]) == state[3], CODE)
    except BaseException:
        raise
    finally:
        try:
            cursor.close()
        except BaseException:
            admission.poison(('original_read_cursor', cursor))
            raise pub.RestorePublicationError('RESTORE_PUBLICATION_CLOSE_UNCONFIRMED') from None
    return state, db, admission


class _ReadSQL:
    """Fixed internal readers use original C methods; no transaction methods."""
    def __init__(self, connection, resources):
        self.connection = connection
        self.resources = resources
        self.row_factory = sqlite3.Row

    def execute(self, sql, parameters=()):
        pub._need(type(sql) is str and (sql.lstrip().upper().startswith('SELECT ')
                  or sql.lstrip().upper().startswith('PRAGMA TABLE_INFO(')
                  or sql.lstrip().upper().startswith('PRAGMA FOREIGN_KEY_LIST(')
                  or sql.lstrip().upper().startswith('PRAGMA INDEX_LIST(')
                  or sql.lstrip().upper().startswith('PRAGMA INDEX_INFO(')), CODE)
        cursor = _CURSOR(self.connection, factory=_CURSOR_CLASS)
        cursor.row_factory = sqlite3.Row
        self.resources.callback(cursor.close)
        return cursor.execute(sql, parameters)

    def set_progress_handler(self, callback, steps):
        # Only the original finite read validators use this callback.
        return _PROGRESS(self.connection, callback, steps)


def _same_active_read_sql(candidate, expected_raw):
    """Private bool only: a live original trusted_sql facade of this exact raw.

    This never returns a connection or enrolls a view in general read authority.
    Records contain weak references only and are retired by the factory finally.
    """
    if type(candidate) is not _ReadSQL:
        return False
    record = _SAVED_PDF_READERS.get(candidate)
    if record is None:
        return False
    original, resources, actor, pid = record
    raw, held_resources = original(), resources()
    try:
        return (pid == os.getpid() and actor == pub._actor_key(pub._actor())
                and raw is not None and raw is expected_raw and held_resources is not None
                and object.__getattribute__(candidate, 'connection') is raw
                and object.__getattribute__(candidate, 'resources') is held_resources)
    except (AttributeError, TypeError):
        return False


class _ReadResources(ExitStack):
    def __init__(self, admission):
        super().__init__()
        self.admission = admission
        self.original = []
        self.close_records = []
        self.closed_confirmed = False

    def callback(self, callback, *args, **kwargs):
        record = [callback, args, kwargs, False]
        self.close_records.append(record)
        def close_original():
            result = callback(*args, **kwargs)
            record[3] = True
            return result
        return super().callback(close_original)

    def enter_context(self, manager):
        from .registered_asset_snapshot import HeldFile
        from .parent_restore_workflow import _SourceCloseCallbacks
        self.original.append(manager)
        callbacks = _SourceCloseCallbacks(manager) if type(manager) is HeldFile else None
        try:
            return super().enter_context(manager)
        except BaseException:
            # Failed entry may have acquired resources. Only confirmed original
            # HeldFile callbacks can discharge that responsibility.
            if callbacks is None or not callbacks.all_confirmed():
                self.admission.poison(('original_read_enter', self, manager, callbacks))
            raise

    def __exit__(self, *args):
        try:
            result=super().__exit__(*args)
            self.closed_confirmed=True
            return result
        except BaseException:
            self.admission.poison(('original_read_resources', self))
            raise pub.RestorePublicationError('RESTORE_PUBLICATION_CLOSE_UNCONFIRMED') from None


@contextmanager
def _verification(connection, database=None):
    """Borrow the caller's already active snapshot, original resources and actor."""
    from . import standard_relocation_provenance as provenance
    from .knowledge_graph import paper_objective_bridge as paper
    # contextlib.__enter__ is the only intermediary. No caller receives a
    # general read-with-body ticket or can retain/reenter this private scope.
    caller=sys._getframe(2).f_code
    pub._need(caller in{provenance._READ_ROOTS_CODE,provenance._EXPORT_SCOPE_CODE,
                       paper._ASSERT_RELOCATION_READ_CODE},CODE)
    supplied=connection;connection=original_connection(supplied)
    if supplied is not connection:
        if recording_source_view(supplied):raise _UnrecordedSourceRead()
        from .rubric_calibration_transaction import unsupported_dependency
        unsupported_dependency()
    state, db, admission = _state(connection, database)
    pub._need(connection.in_transaction and provenance.business_snapshot_active(connection),CODE)
    changes = connection.total_changes
    resources=_ReadResources(admission);state[7].add(resources)
    try:
        with resources:
            sql = _ReadSQL(connection, resources)
            _SAVED_PDF_READERS[sql] = (weakref.ref(connection), weakref.ref(resources),
                                       pub._actor_key(pub._actor()), os.getpid())
            try:
                yield sql, resources, db
                pub._need(connection.in_transaction and connection.total_changes == changes, CODE)
                pub._need(_state(connection, database)[0] is state
                          and original_connection(supplied)is connection, CODE)
            finally:
                _SAVED_PDF_READERS.pop(sql, None)
    finally:
        if resources.closed_confirmed:state[7].discard(resources)


def poison_original(connection, reason):
    """Retain an unknown original read/rollback resource; never recover it here."""
    connection=original_connection(connection)
    value = getattr(connection, '_original_read_locator', None)
    pub._need(type(value) is _ReadConnection and value in _CONNECTIONS, CODE)
    state = _CONNECTIONS[value]
    admission = state[2]()
    pub._need(state[1]() is connection and admission is not None and state[5] == pub._actor(), CODE)
    admission.poison(reason)


@contextmanager
def trusted_sql(connection):
    """Original SQLite reads for the pre-existing healthy binding path.

    This accepts a real raw SQLite connection as before; it confers no admitted
    generation/file capability. Caller-replaced SQL methods are never trusted.
    """
    from .database import StudyConnection
    supplied=connection;connection=original_connection(supplied)
    if supplied is not connection:
        if recording_source_view(supplied):raise _UnrecordedSourceRead()
        from .rubric_calibration_transaction import unsupported_dependency
        unsupported_dependency()
    pub._need(type(connection) in {sqlite3.Connection, StudyConnection}, CODE)
    with ExitStack() as resources:
        sql = _ReadSQL(connection, resources)
        sql.local_media_runtime = getattr(connection, 'local_media_runtime', None)
        if type(connection) is StudyConnection:
            _SAVED_PDF_READERS[sql] = (weakref.ref(connection), weakref.ref(resources),
                                       pub._actor_key(pub._actor()), os.getpid())
        try:
            yield sql
        finally:
            _SAVED_PDF_READERS.pop(sql, None)


def _view_kind(view):
    from .rubric_calibration_transaction import _OwnedConnection,_Transcript,calibration_connection
    from .diagnostic_prompt import _BorrowedConnection,_TransactionDatabase
    from .exam_simulation import _ExamConnection,_ExamDatabase
    from .rubric_calibration_rows import ReadRows
    from .agent_runtime import _RuntimeTransactionDatabase
    choices={_OwnedConnection:('_connection',calibration_connection.__wrapped__.__code__),
        _Transcript:('connection',_OwnedConnection.recording.__wrapped__.__code__),
        _BorrowedConnection:('connection',_TransactionDatabase.connect.__wrapped__.__code__),
        _ExamConnection:('connection',_ExamDatabase.connect.__wrapped__.__code__),
        ReadRows:('connection',ReadRows.__enter__.__code__),
        _RuntimeTransactionDatabase:('connection',_RuntimeTransactionDatabase.connect.__wrapped__.__code__)}
    pub._need(type(view)in choices,CODE);return choices[type(view)]


def original_connection(connection):
    """Resolve only live factory-issued views; never discover a DB by a path."""
    from .database import StudyConnection
    current=connection;seen=set()
    for _ in range(8):
        if type(current)in{sqlite3.Connection,StudyConnection}:return current
        pub._need(id(current)not in seen and current in _VIEWS,CODE);seen.add(id(current))
        target,raw,actor,pid,proof=_VIEWS[current];field,_=_view_kind(current)
        pub._need(actor==pub._actor()and pid==os.getpid()and target()is not None
            and object.__getattribute__(current,field)is target(),CODE)
        state,_,_=_core_state(raw());pub._need(state is proof,CODE)
        current=target()
    raise pub.RestorePublicationError(CODE)


@contextmanager
def original_connection_view(view,target):
    field,issuer=_view_kind(view)
    pub._need(sys._getframe(2).f_code is issuer and view not in _VIEWS
        and object.__getattribute__(view,field)is target,CODE)
    raw=original_connection(target)
    from .rubric_calibration_rows import ReadRows
    # This nested read view enters under the already-issued transcript's
    # SELECT-only authorizer. Its original lease/database-list proof is live;
    # do not issue a forbidden PRAGMA or replace the original authorizer.
    if type(view)is ReadRows and registered_view(target) and recording_source_view(target):
        state,_,admission=_core_state(raw)
    else:
        state,_,admission=_state(raw)
    _VIEWS[view]=(weakref.ref(target),weakref.ref(raw),pub._actor(),os.getpid(),state)
    body_error=None
    try:
        yield view
    except BaseException as error:
        body_error=error
        raise
    finally:
        try:pub._need(original_connection(view)is raw,CODE)
        except BaseException as view_error:
            admission.poison(('original_read_view_changed',view,raw,body_error,view_error))
            # A failed marker/unknown resource may already poison this original
            # lease. Preserve that first error; no failed scope returns a proof.
            if body_error is None:raise
        finally:_VIEWS.pop(view,None)


def registered_view(connection):return connection in _VIEWS


def registered_original(connection):
    from .database import StudyConnection
    if type(connection)is not StudyConnection:return False
    value=getattr(connection,'_original_read_locator',None)
    state=_CONNECTIONS.get(value) if type(value)is _ReadConnection else None
    return state is not None and state[1]()is connection


class _UnrecordedSourceRead(RuntimeError):
    pass


def recording_source_view(connection):
    raw=original_connection(connection)
    state,_,_=_core_state(raw)
    return _readonly_view(raw,state)


def _readonly_view(connection,state):
    # The original recording factory checked database_list before installing its
    # SELECT-only authorizer. That same live view cannot ATTACH/change the DB.
    from .rubric_calibration_transaction import _Transcript,_OwnedConnection
    for view,entry in tuple(_VIEWS.items()):
        if type(view)is not _Transcript or entry[1]()is not connection or entry[4]is not state:continue
        owner=object.__getattribute__(view,'connection')
        if type(owner)is not _OwnedConnection or owner not in _VIEWS:continue
        observer=object.__getattribute__(owner,'observer')
        if(getattr(observer,'__self__',None)is view and getattr(observer,'__func__',None)is _Transcript.observe
            and original_connection(view)is connection):return True
    return False
