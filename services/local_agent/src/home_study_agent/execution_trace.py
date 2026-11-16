"""Local metadata links to existing facts, never a second outcome ledger."""
from contextlib import nullcontext
from datetime import datetime, timezone
import base64
import hashlib
import json
from uuid import UUID, uuid4, uuid5, NAMESPACE_URL


class TraceInputError(ValueError):
    pass


class TraceConflict(RuntimeError):
    pass


EVENT_SOURCES = {
    'request_received': {'agent_turn'}, 'intent_selected': {'agent_turn'},
    'action_scheduled': {'agent_step'}, 'action_result': {'agent_step'},
    'device_result': {'agent_step'}, 'learning_result': {'diagnostic_attempt', 'agent_outcome'},
    'plan_written': {'daily_plan', 'plan_item'}, 'report_written': {'daily_report'},
}
SOURCE_SQL = {
    'agent_turn': 'SELECT * FROM agent_turn WHERE agent_turn_id=? AND child_id=? AND session_id=?',
    'agent_step': '''SELECT s.* FROM agent_step s JOIN agent_plan p USING(plan_id)
        WHERE s.step_id=? AND p.child_id=? AND p.session_id=?''',
    'daily_plan': 'SELECT * FROM daily_plan WHERE plan_id=? AND child_id=? AND session_id=?',
    'plan_item': '''SELECT i.* FROM plan_item i JOIN daily_plan p USING(plan_id)
        WHERE i.plan_item_id=? AND p.child_id=? AND p.session_id=?''',
    'daily_report': 'SELECT * FROM daily_report WHERE report_id=? AND child_id=? AND session_id=?',
    'diagnostic_attempt': 'SELECT * FROM diagnostic_attempt WHERE attempt_id=? AND child_id=? AND session_id=?',
    'agent_outcome': '''SELECT o.* FROM agent_outcome o JOIN agent_objective a USING(objective_id)
        WHERE o.outcome_id=? AND o.child_id=? AND a.session_id=?''',
}
ERROR_CODES = frozenset('MODEL_TIMEOUT MODEL_UNAVAILABLE MODEL_RESPONSE_INVALID APP_RESTARTED '
    'DEVICE_NOT_BOUND DEVICE_COMMAND_UNKNOWN DEVICE_COMMAND_FAILED DEVICE_COMMAND_TIMEOUT '
    'LOCAL_SESSION_REQUIRED PARENT_AUTH_REQUIRED PLAN_INPUT_REQUIRED SOURCE_CHANGED VERSION_CONFLICT '
    'AgentExecutionConflict UnsupportedServerCapabilityError ActivityVoiceError TaskStartBindingError '
    'InputRevisionError ValueError KeyError RuntimeError ModelResponseError'.split())
STATUSES = frozenset('pending planned running active paused waiting_child completed failed unknown cancelled '
    'ready rescheduled accepted recorded withdrawn revoked'.split())


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _source_version(kind, row, *, legacy=False):
    # Finishing an action does not revise the original request/intent.
    if kind == 'agent_turn':
        row = {key: value for key,value in row.items() if key not in {'tool_status','tool_error_code','completed_at','response_text'}}
    if kind == 'agent_step' and not legacy:
        # The final host receipt touches updated_at even when every business
        # field is identical. Keep result, status, error and binding revisions.
        row = {key: value for key,value in row.items() if key != 'updated_at'}
        return 'trace_source_v2:'+_hash(row)
    return 'trace_source_v1:'+_hash(row)


def emit_trace_failure():
    try:
        print('HSC_DIAGNOSTIC '+_json({'event':'trace_record_failed','level':'warn',
            'details':{'error_code':'TRACE_RECORD_FAILED'}}), flush=True)
    except Exception:
        pass


def _uuid(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except (ValueError, AttributeError):
        return False


class ExecutionTraceService:
    def __init__(self, database):
        self.database = database

    def initialize(self):
        """Startup only. Business methods never create tables or commit."""
        with self.database.connect() as c:
            if c.in_transaction:
                raise TraceInputError('TRACE_INITIALIZE_OUTSIDE_TRANSACTION')
            c.execute('BEGIN IMMEDIATE')
            c.execute('''CREATE TABLE IF NOT EXISTS execution_trace_event(
                event_id TEXT PRIMARY KEY,
                child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
                turn_id TEXT NOT NULL REFERENCES agent_turn(agent_turn_id) ON DELETE CASCADE,
                step_id TEXT REFERENCES agent_step(step_id) ON DELETE CASCADE,
                trace_id TEXT NOT NULL, span_id TEXT NOT NULL, parent_span_id TEXT,
                revision INTEGER NOT NULL CHECK(revision>0),
                operation TEXT NOT NULL CHECK(operation IN ('observe','retract')),
                event_type TEXT NOT NULL, source_kind TEXT NOT NULL, source_id TEXT NOT NULL,
                source_version TEXT NOT NULL, source_status TEXT NOT NULL, error_code TEXT,
                elapsed_ms INTEGER NOT NULL CHECK(elapsed_ms BETWEEN 0 AND 86400000),
                action_key_hash TEXT NOT NULL, payload_hash TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(child_id,action_key_hash), UNIQUE(trace_id,span_id,revision))''')
            c.execute('''CREATE TRIGGER IF NOT EXISTS execution_trace_no_update BEFORE UPDATE ON execution_trace_event
                BEGIN SELECT RAISE(ABORT,'TRACE_APPEND_ONLY'); END''')
            c.execute('CREATE INDEX IF NOT EXISTS execution_trace_turn ON execution_trace_event(child_id,turn_id,created_at)')
            c.commit()

    @staticmethod
    def _owned(c, child_id, session_id, turn_id, step_id):
        turn = c.execute('''SELECT t.* FROM agent_turn t JOIN daily_session s USING(session_id)
            WHERE t.agent_turn_id=? AND t.child_id=? AND t.session_id=? AND s.child_id=t.child_id''',
            (turn_id,child_id,session_id)).fetchone()
        if not turn:
            raise KeyError(turn_id)
        if step_id and not c.execute('''SELECT 1 FROM agent_step s JOIN agent_plan p USING(plan_id)
            WHERE s.step_id=? AND p.agent_turn_id=? AND p.child_id=? AND p.session_id=?''',
            (step_id,turn_id,child_id,session_id)).fetchone():
            raise TraceConflict('TRACE_STEP_SCOPE_CONFLICT')

    @classmethod
    def _source(cls, c, child_id, session_id, turn_id, step_id, source_kind, source_id):
        cls._owned(c,child_id,session_id,turn_id,step_id)
        if source_kind not in SOURCE_SQL:
            raise TraceInputError('TRACE_SOURCE_KIND_INVALID')
        if (source_kind=='agent_turn' and source_id!=turn_id) or (source_kind=='agent_step' and source_id!=step_id):
            raise TraceConflict('TRACE_SOURCE_BINDING_CONFLICT')
        row = c.execute(SOURCE_SQL[source_kind], (source_id,child_id,session_id)).fetchone()
        if not row:
            raise TraceConflict('TRACE_SOURCE_SCOPE_CONFLICT')
        return dict(row)

    def source_version(self, *, connection, child_id, session_id, turn_id, step_id, source_kind, source_id):
        row = self._source(connection,child_id,session_id,turn_id,step_id,source_kind,source_id)
        return _source_version(source_kind,row)

    def observe_host_boundary(self, *, connection, turn_id, event_type, step_id=None, trace_id=None, elapsed_ms=0):
        """Best-effort metadata inside a savepoint, never retrying business work."""
        c, opened = connection, False
        try:
            if not c.in_transaction:
                raise TraceInputError('TRACE_TRANSACTION_REQUIRED')
            c.execute('SAVEPOINT execution_trace_metadata')
            opened = True
            owner = c.execute('SELECT child_id,session_id FROM agent_turn WHERE agent_turn_id=?',(turn_id,)).fetchone()
            if not owner:
                raise KeyError(turn_id)
            kind = 'agent_step' if step_id else 'agent_turn'
            identifier = step_id or turn_id
            root = c.execute('SELECT * FROM execution_trace_event WHERE turn_id=? AND child_id=? ORDER BY rowid LIMIT 1',
                (turn_id,owner['child_id'])).fetchone()
            if root:
                trace_id = root['trace_id']
            elif not _uuid(trace_id) or c.execute('SELECT 1 FROM execution_trace_event WHERE trace_id=?',(trace_id,)).fetchone():
                trace_id = str(uuid4())
            span_id = str(uuid5(NAMESPACE_URL, f'hsc-trace:{turn_id}:{identifier}:{event_type}'))
            prior = c.execute('SELECT * FROM execution_trace_event WHERE trace_id=? AND span_id=? ORDER BY revision DESC LIMIT 1',
                (trace_id,span_id)).fetchone()
            ref = dict(child_id=owner['child_id'],session_id=owner['session_id'],turn_id=turn_id,
                step_id=step_id,source_kind=kind,source_id=identifier)
            version = self.source_version(connection=c,**ref)
            # Transport replay keeps the first measurement; it is not a new act.
            if prior and (prior['source_version']==version or event_type in {'request_received','intent_selected'}):
                event = self._public(prior)
            else:
                event = self.record_link(connection=c,**ref,trace_id=trace_id,span_id=span_id,
                    parent_span_id=root['span_id'] if root and root['span_id']!=span_id else None,
                    event_type=event_type,source_version=version,elapsed_ms=elapsed_ms,
                    expected_revision=prior['revision'] if prior else 0,
                    client_action_id='host-trace:'+_hash([turn_id,identifier,event_type,version]))
            c.execute('RELEASE SAVEPOINT execution_trace_metadata')
            return {'status':'recorded','trace_id':trace_id,'event_id':event['event_id']}
        except Exception:
            if opened:
                try:
                    c.execute('ROLLBACK TO SAVEPOINT execution_trace_metadata')
                    c.execute('RELEASE SAVEPOINT execution_trace_metadata')
                except Exception:
                    pass
            # Do not print the exception: it may contain a SQL value or path.
            emit_trace_failure()
            return {'status':'unavailable','error_code':'TRACE_RECORD_FAILED'}

    def record_link(self, *, connection, child_id, session_id, turn_id, step_id, trace_id, span_id,
                    parent_span_id, event_type, source_kind, source_id, source_version, elapsed_ms,
                    expected_revision, client_action_id, operation='observe'):
        c = connection
        if not c.in_transaction:
            raise TraceInputError('TRACE_TRANSACTION_REQUIRED')
        if (not _uuid(trace_id) or not _uuid(span_id) or (parent_span_id is not None and not _uuid(parent_span_id))
                or type(elapsed_ms) is not int or not 0 <= elapsed_ms <= 86400000
                or type(expected_revision) is not int or expected_revision < 0
                or operation not in {'observe','retract'} or source_kind not in EVENT_SOURCES.get(event_type,set())
                or not isinstance(client_action_id,str) or not 1 <= len(client_action_id) <= 200):
            raise TraceInputError('TRACE_METADATA_INVALID')
        self._owned(c,child_id,session_id,turn_id,step_id)
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_trace_event'").fetchone():
            raise TraceInputError('TRACE_SCHEMA_NOT_INITIALIZED')
        payload = dict(child_id=child_id,session_id=session_id,turn_id=turn_id,step_id=step_id,trace_id=trace_id,
            span_id=span_id,parent_span_id=parent_span_id,event_type=event_type,source_kind=source_kind,source_id=source_id,
            source_version=source_version,elapsed_ms=elapsed_ms,expected_revision=expected_revision,operation=operation)
        digest, key = _hash(payload), _hash(client_action_id)
        replay = c.execute('SELECT * FROM execution_trace_event WHERE child_id=? AND action_key_hash=?', (child_id,key)).fetchone()
        if replay:
            if replay['payload_hash'] != digest:
                raise TraceConflict('TRACE_IDEMPOTENCY_CONFLICT')
            return self._public(replay)
        root = c.execute('SELECT * FROM execution_trace_event WHERE trace_id=? ORDER BY rowid LIMIT 1', (trace_id,)).fetchone()
        if root and any(root[k] != payload[k] for k in ('child_id','session_id','turn_id')):
            raise TraceConflict('TRACE_ROOT_SCOPE_CONFLICT')
        prior = c.execute('SELECT * FROM execution_trace_event WHERE trace_id=? AND span_id=? ORDER BY revision DESC LIMIT 1',
            (trace_id,span_id)).fetchone()
        if (prior['revision'] if prior else 0) != expected_revision or (operation=='retract' and not prior):
            raise TraceConflict('TRACE_REVISION_CONFLICT')
        if prior and (prior['operation']=='retract' or any(prior[k]!=payload[k] for k in
                ('child_id','session_id','turn_id','step_id','parent_span_id','event_type','source_kind','source_id'))):
            raise TraceConflict('TRACE_BINDING_FROZEN')
        if parent_span_id:
            parent = c.execute('SELECT * FROM execution_trace_event WHERE trace_id=? AND span_id=? ORDER BY revision DESC LIMIT 1',
                (trace_id,parent_span_id)).fetchone()
            if not parent or parent['operation']=='retract' or parent_span_id==span_id:
                raise TraceConflict('TRACE_PARENT_CONFLICT')
        elif root and root['span_id'] != span_id:
            raise TraceConflict('TRACE_PARENT_REQUIRED')
        row = (self._source(c,child_id,session_id,turn_id,step_id,source_kind,source_id)
            if operation=='observe' else dict(prior))
        expected_source = _source_version(source_kind,row) if operation=='observe' else prior['source_version']
        if source_version != expected_source:
            raise TraceConflict('TRACE_SOURCE_VERSION_CONFLICT')
        if operation=='observe' and event_type=='device_result' and row.get('execution_location') != 'client_device_bridge':
            raise TraceConflict('TRACE_DEVICE_SOURCE_REQUIRED')
        status = row.get('status',row.get('tool_status',row.get('source_status','recorded')))
        error = row.get('error_code',row.get('tool_error_code'))
        event_id = str(uuid4())
        c.execute('INSERT INTO execution_trace_event VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (event_id,child_id,session_id,turn_id,step_id,trace_id,span_id,parent_span_id,expected_revision+1,operation,
             event_type,source_kind,source_id,source_version,status if status in STATUSES else 'recorded',
             error if error in ERROR_CODES else None,elapsed_ms,key,digest,datetime.now(timezone.utc).isoformat()))
        return self._public(c.execute('SELECT * FROM execution_trace_event WHERE event_id=?',(event_id,)).fetchone())

    @staticmethod
    def _public(row):
        result = {key: row[key] for key in row.keys() if key not in {'action_key_hash','payload_hash'}}
        result.update(summary='本地业务记录的元数据关联；不表示学习成效。',
            recovery_action='check_receipt_before_retry' if row['source_status'] in {'unknown','failed','running'} else 'inspect_local_status',
            causal_link_verified=False, learning_effect_verified=False,
            source_version_scope='record_snapshot_not_learning_qualification', qualification_status='not_evaluated')
        return result

    def read(self, child_id, turn_id, *, connection=None):
        with nullcontext(connection) if connection is not None else self.database.connect() as c:
            if not c.in_transaction:
                c.execute('BEGIN')
            turn = c.execute('SELECT session_id FROM agent_turn WHERE agent_turn_id=? AND child_id=?',(turn_id,child_id)).fetchone()
            if not turn:
                raise KeyError(turn_id)
            self._owned(c,child_id,turn[0],turn_id,None)
            rows = c.execute('SELECT * FROM execution_trace_event WHERE child_id=? AND turn_id=? ORDER BY rowid',
                (child_id,turn_id)).fetchall() if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_trace_event'").fetchone() else []
            latest = {(row['trace_id'],row['span_id']):row for row in rows}
            spans = []
            for row in latest.values():
                if row['operation']=='retract':
                    continue
                try:
                    source = self._source(c,**{k:row[k] for k in
                        ('child_id','session_id','turn_id','step_id','source_kind','source_id')})
                    # Old hashes did not preserve the timestamp separately;
                    # never reinterpret them as proof under the new policy.
                    current = _source_version(row['source_kind'],source,
                        legacy=row['source_version'].startswith('trace_source_v1:')) == row['source_version']
                except (KeyError,TraceConflict):
                    current = False
                parent = latest.get((row['trace_id'],row['parent_span_id'])) if row['parent_span_id'] else None
                spans.append({**self._public(row),'source_current':current,
                    'parent_link_current':row['parent_span_id'] is None or bool(parent and parent['operation']!='retract')})
            steps = c.execute('''SELECT s.step_id,s.status,s.error_code FROM agent_step s JOIN agent_plan p USING(plan_id)
                WHERE p.agent_turn_id=? AND p.child_id=? ORDER BY s.step_index''',(turn_id,child_id)).fetchall()
            return {'version':'execution_trace_v1','child_id':child_id,'turn_id':turn_id,
                'spans':spans,'history':[self._public(row) for row in rows],
                'existing_steps':[{'step_id':s['step_id'],'status':s['status'] if s['status'] in STATUSES else 'unknown',
                    'error_code':s['error_code'] if s['error_code'] in ERROR_CODES else None} for s in steps],
                'missing_stages':sorted(set(EVENT_SOURCES)-{row['event_type'] for row in rows if row['operation']=='observe'}),
                'changed_stages':sorted({s['event_type'] for s in spans if not s['source_current']} |
                    {row['event_type'] for row in latest.values() if row['operation']=='retract'}),
                'outcome_evidence_created':False,'local_only':True,'cloud_transmission_allowed':False}

    def list_turns(self, child_id, limit=20, before=None, *, connection=None):
        """A cursor locates an owned row; it is never an authorization token."""
        from .agent_runtime import CapabilityRegistry
        if type(limit) is not int or not 1 <= limit <= 100:
            raise TraceInputError('TRACE_LIMIT_INVALID')
        with nullcontext(connection) if connection is not None else self.database.connect() as c:
            if not c.in_transaction:
                c.execute('BEGIN')
            if not c.execute('SELECT 1 FROM child_profile WHERE child_id=?',(child_id,)).fetchone():
                raise KeyError(child_id)
            anchor = None
            if before is not None:
                try:
                    if not isinstance(before,str) or not 1 <= len(before) <= 1024:
                        raise ValueError()
                    cursor = json.loads(base64.b64decode(before+'='*(-len(before)%4),altchars=b'-_',validate=True))
                    if (not isinstance(cursor,list) or len(cursor)!=3 or type(cursor[0]) is not int or cursor[0]!=1
                            or not isinstance(cursor[1],str) or not isinstance(cursor[2],str)):
                        raise ValueError()
                except (ValueError,UnicodeError):
                    raise TraceInputError('TRACE_CURSOR_INVALID') from None
                if cursor[1] != _hash(child_id):
                    raise KeyError(child_id)
                anchor = c.execute('SELECT created_at,agent_turn_id FROM agent_turn WHERE child_id=? AND agent_turn_id=?',
                    (child_id,cursor[2])).fetchone()
                if not anchor:
                    raise TraceInputError('TRACE_CURSOR_EXPIRED_REFRESH_REQUIRED')
            has_trace = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_trace_event'").fetchone()
            count = '(SELECT COUNT(*) FROM execution_trace_event e WHERE e.child_id=t.child_id AND e.turn_id=t.agent_turn_id)' if has_trace else '0'
            predicate, args = '', [child_id]
            if anchor:
                predicate = 'AND (t.created_at < ? OR (t.created_at=? AND t.agent_turn_id<?))'
                args.extend((anchor['created_at'],anchor['created_at'],anchor['agent_turn_id']))
            rows = c.execute(f'''SELECT t.agent_turn_id,t.created_at,COALESCE(s.status,t.tool_status) AS status,
                COALESCE(s.error_code,t.tool_error_code) AS error_code,s.capability_name,{count} AS trace_event_count
                FROM agent_turn t JOIN daily_session ds ON ds.session_id=t.session_id AND ds.child_id=t.child_id
                LEFT JOIN agent_step s ON s.step_id=(SELECT original.step_id FROM agent_step original
                    JOIN agent_plan p ON p.plan_id=original.plan_id WHERE p.agent_turn_id=t.agent_turn_id
                        AND p.child_id=t.child_id AND p.session_id=t.session_id
                    ORDER BY original.step_index,original.step_id LIMIT 1)
                WHERE t.child_id=? {predicate} ORDER BY t.created_at DESC,t.agent_turn_id DESC LIMIT ?''',
                (*args,limit+1)).fetchall()
            known = {item['name'] for item in CapabilityRegistry().catalog()}
            items = []
            for row in rows[:limit]:
                try:
                    datetime.fromisoformat(row['created_at'])
                    created_at = row['created_at']
                except (TypeError,ValueError):
                    created_at = ''
                items.append({'turn_id':row['agent_turn_id'],'created_at':created_at,
                    'status':row['status'] if row['status'] in STATUSES else 'unknown',
                    'error_code':row['error_code'] if row['error_code'] in ERROR_CODES else None,
                    'capability_name':row['capability_name'] if row['capability_name'] in known else None,
                    'trace_event_count':row['trace_event_count']})
            cursor = base64.urlsafe_b64encode(_json([1,_hash(child_id),items[-1]['turn_id']]).encode()).decode().rstrip('=') if len(rows)>limit else None
            return {'items':items,'next_cursor':cursor}
