"""Explicit parent-selected, local-only material for one original child session.

Publication never selects or starts learning. Selection is a revocable session
reference, not source permission, mastery evidence, or a model supplied item ID.
Restore rotates privacy storage generation; old selections then fail closed.
"""
from __future__ import annotations
import hashlib
import json
import re
from .local_supply import LocalSupplyError, validate_item
from .privacy_operation_receipt import generation, initialize as initialize_generation
from .repository import utc_now


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _need(ok, code):
    if not ok:
        raise LocalSupplyError(code)


def ensure_schema(c):
    initialize_generation(c)
    c.execute('''CREATE TABLE IF NOT EXISTS paper_learning_selection(
        session_id TEXT PRIMARY KEY REFERENCES daily_session(session_id) ON DELETE CASCADE,
        child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
        version INTEGER NOT NULL, origin_generation TEXT NOT NULL,
        candidates_json TEXT NOT NULL, updated_at TEXT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS paper_learning_selection_event(
        action_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
        child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
        request_hash TEXT NOT NULL, origin_generation TEXT NOT NULL,
        result_json TEXT NOT NULL, created_at TEXT NOT NULL)''')


def _session(c, child, sid):
    row = c.execute('''SELECT ds.*, cp.status AS child_status FROM daily_session ds
        JOIN child_profile cp USING(child_id) WHERE ds.session_id=? AND ds.child_id=?''', (sid, child)).fetchone()
    _need(row and row['status']=='active' and row['child_status']=='active'
          and row['state'] not in {'evolving','completed'}, 'PAPER_SELECTION_SESSION_CHANGED')
    return row


def source_snapshot(c, child, candidate_id):
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    found=[]
    for table, prefix in (('curriculum_first_source','parent_approved_first:'),
                          ('curriculum_supply_package','parent_approved_specialized:')):
        if table not in tables:
            continue
        query=(f'SELECT * FROM {table} WHERE candidate_id=? AND child_id=?' if table=='curriculum_first_source' else
               '''SELECT p.*, s.diagnostic_item_id FROM curriculum_supply_package p
                  JOIN specialized_supply_candidate s ON s.candidate_id=p.candidate_id AND s.child_id=p.child_id
                  WHERE p.candidate_id=? AND p.child_id=?''')
        row=c.execute(query,(candidate_id,child)).fetchone()
        if row:
            found.append((dict(row),prefix))
    _need(len(found)==1, 'PAPER_SELECTION_SOURCE_UNAVAILABLE')
    row,prefix=found[0]
    payload=json.loads(row['payload_json'])
    _need(row['state']=='published' and row['diagnostic_item_id']
          and payload.get('validator')=='curriculum_objective_exact_source_v1'
          and payload.get('stage')=='parallel', 'PAPER_SELECTION_SOURCE_UNAVAILABLE')
    checked=validate_item(c, child, row['diagnostic_item_id'])
    item=c.execute('''SELECT di.*,kn.track,kn.subject FROM diagnostic_item di
        JOIN knowledge_node kn USING(knowledge_node_id) WHERE diagnostic_item_id=?''', (row['diagnostic_item_id'],)).fetchone()
    _need(item and item['source_ref']==prefix+candidate_id and item['track']=='school'
          and checked is not None, 'PAPER_SELECTION_SOURCE_UNAVAILABLE')
    # Exact publication and current source proof. Never serialize rubric or source
    # content into the UI or provider projection.
    return {'candidate_id':candidate_id,'candidate_version':row['version'],
            'diagnostic_item_id':row['diagnostic_item_id'],
            'source_binding':_hash([row['payload_json'],row['source_snapshot_json'],dict(item)])}


def validate_snapshot(c, child, snapshot):
    _need(source_snapshot(c,child,snapshot['candidate_id'])==snapshot, 'PAPER_SELECTION_SOURCE_CHANGED')


def read_in(c, child, sid):
    _session(c,child,sid)
    row=c.execute('SELECT * FROM paper_learning_selection WHERE session_id=? AND child_id=?',(sid,child)).fetchone()
    if not row:
        return {'session_id':sid,'child_id':child,'version':0,'items':[], 'status':'empty'}
    items=json.loads(row['candidates_json'])
    reason=None
    if row['origin_generation']!=generation(c):
        reason='PAPER_SELECTION_RESTORE_RESELECT_REQUIRED'
    else:
        try:
            for item in items:validate_snapshot(c,child,item)
        except LocalSupplyError:
            reason='PAPER_SELECTION_SOURCE_CHANGED'
    titles=[]
    for item in items:
        prompt=c.execute('SELECT prompt FROM diagnostic_item WHERE diagnostic_item_id=?',(item['diagnostic_item_id'],)).fetchone()
        titles.append({'candidate_id':item['candidate_id'],'candidate_version':item['candidate_version'],
                       'title':prompt[0][:200] if prompt else '原题待核对'})
    return {'session_id':sid,'child_id':child,'version':row['version'],'items':titles,
            'status':'blocked' if reason else 'selected' if items else 'empty','reason':reason}


def arrival_binding(c,child,sid):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='paper_learning_selection'").fetchone():
        return None
    row=c.execute('SELECT * FROM paper_learning_selection WHERE session_id=? AND child_id=?',(sid,child)).fetchone()
    return {'child_id':child,'session_id':sid,'origin_generation':generation(c),
            'version':row['version'] if row else 0,
            'selection_hash':_hash(dict(row)) if row else None}


def inspect_voice(c, session, turn_id):
    """Use the actual current runtime command and arrival, never request body text."""
    row=c.execute('''SELECT t.*,p.context_manifest_json,p.status AS plan_status,
        p.child_id AS plan_child,p.session_id AS plan_session,s.status AS step_status,s.arguments_json
        FROM agent_turn t JOIN agent_plan p USING(agent_turn_id) JOIN agent_step s USING(plan_id)
        WHERE t.agent_turn_id=? AND s.step_index=0 AND s.capability_name='diagnostic.start' ''',(turn_id,)).fetchone()
    _need(row and row['child_id']==session['child_id'] and row['session_id']==session['session_id']
          and row['plan_child']==row['child_id'] and row['plan_session']==row['session_id']
          and row['intent']!='runtime_continuation' and row['tool_status']=='pending'
          and row['plan_status']==row['step_status']=='running', 'PAPER_SELECTION_VOICE_NOT_EXECUTABLE')
    latest=c.execute('SELECT agent_turn_id FROM agent_turn WHERE session_id=? ORDER BY rowid DESC LIMIT 1',(session['session_id'],)).fetchone()
    arrival=c.execute('SELECT * FROM learning_input_arrival WHERE session_id=? ORDER BY rowid DESC LIMIT 1',(session['session_id'],)).fetchone()
    manifest=json.loads(row['context_manifest_json'])
    _need(latest and latest[0]==turn_id and arrival and arrival['child_id']==session['child_id']
          and arrival['arrival_id']==manifest.get('learning_input_arrival_id')
          and arrival['utterance']==row['transcript'] and arrival['client_action_id']==row['client_action_id'],
          'PAPER_SELECTION_VOICE_SUPERSEDED')
    action=json.loads(row['action_json'])
    args=json.loads(row['arguments_json'])
    _need(action.get('type')=='start_diagnostic' and not any(k in args or k in action for k in
          ('preferred_item_id','paper_candidate_id','paper_selection_id','diagnostic_item_id')),
          'PAPER_SELECTION_VOICE_BINDING_CHANGED')
    text=re.sub(r'[\s，。！？、,!.?]', '', row['transcript'])
    if re.fullmatch(r'(?:请|帮我|我想|我要|我想要|请帮我)?(?:开始|做|练|学习|做一下|开始做)?(?:这张|这份)(?:试卷|卷子|卷|材料)(?:的题|上的题|第一题)?(?:吧)?',text):
        intent='paper'
    elif re.fullmatch(r'(?:请|帮我)?(?:来|做|继续|开始)?(?:下一题|下一道题)(?:吧)?',text):
        intent='next'
    elif re.search(r'(?:这张|这份).{0,6}(?:卷|材料)|试卷|卷子',text):
        intent='ambiguous'
    else:
        return None
    bound=[v.get('binding') for v in json.loads(arrival['snapshot_json']) if v.get('kind')=='paper_learning_selection_arrival']
    _need(len(bound)==1 and bound[0]==arrival_binding(c,session['child_id'],session['session_id']),
          'PAPER_SELECTION_ARRIVAL_CHANGED')
    return intent


def selected_for_voice(c, session, turn_id):
    intent=inspect_voice(c,session,turn_id)
    if intent is None:return None
    _need(intent!='ambiguous','PAPER_SELECTION_INTENT_UNCLEAR')
    row=c.execute('SELECT * FROM paper_learning_selection WHERE session_id=? AND child_id=?',
                  (session['session_id'],session['child_id'])).fetchone()
    if not row or not json.loads(row['candidates_json']):
        _need(intent!='paper','PAPER_SELECTION_REQUIRED')
        return None
    _need(row['origin_generation']==generation(c),'PAPER_SELECTION_RESTORE_RESELECT_REQUIRED')
    items=json.loads(row['candidates_json'])
    for item in items:validate_snapshot(c,session['child_id'],item)
    return {'version':row['version'],'origin_generation':row['origin_generation'],'items':items}


class PaperLearningSelectionService:
    def __init__(self,database):
        self.database=database
        with database.connect() as c:
            ensure_schema(c);c.commit()

    def options(self,child):
        with self.database.connect() as c:
            c.execute('BEGIN')
            sessions=c.execute('''SELECT session_id,started_at,state FROM daily_session
                WHERE child_id=? AND status='active' AND state NOT IN ('evolving','completed')
                ORDER BY started_at DESC''',(child,)).fetchall()
            return {'child_id':child,'origin_generation':generation(c),'sessions':[{'session_id':r['session_id'],'started_at':r['started_at'],
                    'state':r['state'],'selection':read_in(c,child,r['session_id'])} for r in sessions],
                    'local_only':True,'cloud_transmission_allowed':False}

    def change(self,child,sid,*,candidate_id=None,candidate_version=None,expected_version,expected_generation,action_id,operation):
        _need(operation in {'replace','append','clear'} and type(expected_version) is int and expected_version>=0
              and isinstance(action_id,str) and 1<=len(action_id)<=120,'PAPER_SELECTION_INPUT')
        _need((operation=='clear' and candidate_id is None and candidate_version is None)
              or (operation!='clear' and isinstance(candidate_id,str) and type(candidate_version) is int), 'PAPER_SELECTION_INPUT')
        digest=_hash([child,sid,candidate_id,candidate_version,expected_version,expected_generation,operation])
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE');_session(c,child,sid)
            current_generation=generation(c)
            _need(expected_generation==current_generation,'PAPER_SELECTION_RESTORE_RESELECT_REQUIRED')
            prior=c.execute('SELECT * FROM paper_learning_selection_event WHERE action_id=?',(action_id,)).fetchone()
            if prior:
                _need(prior['child_id']==child and prior['session_id']==sid and prior['request_hash']==digest
                      and prior['origin_generation']==current_generation,'PAPER_SELECTION_ACTION_CONFLICT')
                saved=json.loads(prior['result_json'])
                current=read_in(c,child,sid)
                _need(current['version']==saved['version'],'PAPER_SELECTION_SUPERSEDED')
                return current
            row=c.execute('SELECT * FROM paper_learning_selection WHERE session_id=?',(sid,)).fetchone()
            _need(not row or row['child_id']==child,'PAPER_SELECTION_OWNERSHIP_CONFLICT')
            _need((row['version'] if row else 0)==expected_version,'PAPER_SELECTION_VERSION_CONFLICT')
            items=[]
            if operation=='append' and row:
                _need(row['origin_generation']==current_generation,'PAPER_SELECTION_RESTORE_RESELECT_REQUIRED')
                items=json.loads(row['candidates_json'])
                for item in items:validate_snapshot(c,child,item)
            if operation!='clear':
                item=source_snapshot(c,child,candidate_id)
                _need(item['candidate_version']==candidate_version,'PAPER_SELECTION_SOURCE_CHANGED')
                _need(not any(v['candidate_id']==candidate_id for v in items),'PAPER_SELECTION_DUPLICATE')
                items.append(item)
            _need(len(items)<=24,'PAPER_SELECTION_LIMIT')
            now=utc_now()
            c.execute('''INSERT INTO paper_learning_selection VALUES(?,?,?,?,?,?) ON CONFLICT(session_id)
                DO UPDATE SET version=excluded.version,origin_generation=excluded.origin_generation,
                candidates_json=excluded.candidates_json,updated_at=excluded.updated_at''',
                (sid,child,expected_version+1,current_generation,_json(items),now))
            result=read_in(c,child,sid)
            c.execute('INSERT INTO paper_learning_selection_event VALUES(?,?,?,?,?,?,?)',
                      (action_id,sid,child,digest,current_generation,_json(result),now))
            c.commit();return result
