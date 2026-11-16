"""Source-first baseline publication. No placeholder diagnostic seed is created."""
import json
from uuid import uuid4

from . import curriculum_objective_source as objective
from . import open_response_source as opened
from .curriculum_supply import _evidence, _json, _now, _require, _text, digest
from .local_supply import LocalSupplyError

PREFIX = 'parent_approved_first:'
AUTHOR = 'local:reviewed_source_extractor_v1'
VALIDATOR = 'local:curriculum_first_source_v1'


def _original(content):
    try:
        packet = json.loads(content)
    except (ValueError, TypeError) as error:
        raise LocalSupplyError('CURRICULUM_OBJECTIVE_TYPED_SOURCE_REQUIRED') from error
    if isinstance(packet,dict) and packet.get('schema') in {opened.SCHEMA,opened.MATERIAL_SCHEMA,opened.VISUAL_SCHEMA}:
        return opened.parse(content), opened.VALIDATOR, 'open_response'
    return objective.parse(content), objective.VALIDATOR, 'objective_source'


def _seed_allowed(node,seed,validator):
    return seed['answer_type'] in {'text','open_response'} if validator==opened.VALIDATOR else objective.node_allowed(node,seed)


def _publication(c,child,payload):
    """Exact fields from the current reviewed original; no invented seed or score."""
    open_task = payload['validator']==opened.VALIDATOR
    material = None
    if open_task and payload['open_response']['schema'] in {opened.MATERIAL_SCHEMA,opened.VISUAL_SCHEMA}:
        from .open_response_materials import resolve_source_materials
        material = resolve_source_materials(c,child_id=child,source=payload['open_response'])
    return dict(knowledge_node_id=payload['knowledge_node_id'],prompt=payload['prompt'],
        answer_type='open_response' if open_task else 'text',
        assessment_mode='open_response_rubric' if open_task else 'objective',
        expected_answer_json=_json(opened.rubric(payload['open_response']) if open_task else objective.exact_rubric(payload['objective_source'])),
        explanation=payload['explanation'],difficulty=payload['difficulty'],
        stimulus_text=material['stimulus_text'] if material else None,stimulus_asset=None)


def ensure_schema(c):
    # Borrowed transactions stay owned by the caller, including during restore.
    c.execute('''CREATE TABLE IF NOT EXISTS curriculum_first_source(
        candidate_id TEXT PRIMARY KEY, child_id TEXT NOT NULL REFERENCES child_profile(child_id),
        knowledge_node_id TEXT NOT NULL REFERENCES knowledge_node(knowledge_node_id),
        source_id TEXT NOT NULL REFERENCES knowledge_source(source_id),
        payload_json TEXT NOT NULL, source_snapshot_json TEXT NOT NULL,
        state TEXT NOT NULL, version INTEGER NOT NULL, diagnostic_item_id TEXT,
        created_at TEXT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS curriculum_first_source_event(
        event_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL REFERENCES curriculum_first_source(candidate_id) ON DELETE CASCADE,
        child_id TEXT NOT NULL REFERENCES child_profile(child_id), version INTEGER NOT NULL,
        kind TEXT NOT NULL, actor TEXT NOT NULL, action_id TEXT NOT NULL, request_hash TEXT NOT NULL,
        result_json TEXT NOT NULL, created_at TEXT NOT NULL,
        UNIQUE(child_id,action_id), UNIQUE(candidate_id,version))''')


def options(c, child_id, source_id):
    result = []
    for row in c.execute('''SELECT ke.excerpt_id,ke.content_text,ks.subject,ks.grade_band
        FROM knowledge_excerpt ke JOIN knowledge_source ks USING(source_id)
        WHERE ks.source_id=? AND ks.child_id=? ORDER BY ke.sequence,ke.excerpt_id''', (source_id, child_id)).fetchall():
        try:
            original, validator, _ = _original(row['content_text'])
        except LocalSupplyError:
            continue
        if (original['subject'], original['grade']) != (row['subject'], row['grade_band']):
            continue
        node = c.execute("SELECT * FROM knowledge_node WHERE knowledge_node_id=? AND status='active' AND track='school' AND node_type='skill'",
                         (original['knowledge_node_id'],)).fetchone()
        if (not node or (node['subject'], node['grade_band']) != (original['subject'], original['grade'])
                or not _seed_allowed(node,original,validator)):
            continue
        from .local_supply import validate_item
        has_seed = False
        for item in c.execute("SELECT diagnostic_item_id FROM diagnostic_item WHERE knowledge_node_id=? AND status='active'", (node['knowledge_node_id'],)).fetchall():
            try:
                seed = validate_item(c, child_id, item[0])
                # Use the same modality compatibility as the seeded route.
                if _seed_allowed(node,seed,validator):
                    has_seed = True
                    break
            except LocalSupplyError:
                pass
        if not has_seed:
            result.append(dict(knowledge_node_id=node['knowledge_node_id'], title=node['title'],
                               excerpt_id=row['excerpt_id'], prompt=original['prompt']))
    return result


def source_question(c, item):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='curriculum_first_source'").fetchone():
        return None
    row = c.execute('SELECT payload_json,child_id FROM curriculum_first_source WHERE candidate_id=? AND diagnostic_item_id=?',
                    (item['source_ref'][len(PREFIX):], item['diagnostic_item_id'])).fetchone()
    if not row:
        return None
    payload = json.loads(row[0])
    material = _publication(c,row['child_id'],payload) if payload['validator']==opened.VALIDATOR else None
    return dict(prompt=payload['prompt'],stimulus_text=material['stimulus_text'] if material else None,stimulus_asset=None)


def reviewed_baseline(c, child_id, item, as_of):
    """Adapter for stage/coverage readers; never declares a transfer stage."""
    from datetime import datetime
    validate_published(c, child_id, item)
    row = c.execute('SELECT * FROM curriculum_first_source WHERE candidate_id=? AND child_id=?',
                    (item['source_ref'][len(PREFIX):],child_id)).fetchone()
    review = c.execute("SELECT created_at FROM curriculum_first_source_event WHERE candidate_id=? AND kind='review' ORDER BY version DESC LIMIT 1",
                       (row['candidate_id'],)).fetchone()
    reviewed_at = datetime.fromisoformat(review[0])
    _require(reviewed_at.tzinfo is not None and reviewed_at <= as_of, 'CURRICULUM_FIRST_REVIEW_DATE_INVALID')
    return dict(source_root_item_id=item['diagnostic_item_id'],stage='baseline',reviewed_at=reviewed_at.isoformat(),
                source_version=digest(dict(row)))


def validate_published(c, child_id, item):
    _require(c.execute("SELECT 1 FROM sqlite_master WHERE name='curriculum_first_source'").fetchone(),
             'CURRICULUM_FIRST_SOURCE_UNAVAILABLE')
    row = c.execute('SELECT * FROM curriculum_first_source WHERE candidate_id=? AND child_id=? AND diagnostic_item_id=?',
                    (item['source_ref'][len(PREFIX):], child_id, item['diagnostic_item_id'])).fetchone()
    _require(row is not None and row['state'] == 'published', 'CURRICULUM_FIRST_SOURCE_UNAVAILABLE')
    payload = json.loads(row['payload_json'])
    current = _evidence(c, child_id, payload, first_node_id=row['knowledge_node_id'])
    _require(current == json.loads(row['source_snapshot_json']), 'CURRICULUM_SOURCE_CHANGED')
    events = c.execute('SELECT kind,actor,result_json FROM curriculum_first_source_event WHERE candidate_id=? ORDER BY version', (row['candidate_id'],)).fetchall()
    _require(any(e['kind'] == 'validate' and e['actor'] == VALIDATOR and json.loads(e['result_json'])['state'] == 'validated' for e in events)
             and any(e['kind'] == 'review' and e['actor'] == 'parent' and json.loads(e['result_json'])['state'] == 'reviewed' for e in events)
             and events[-1]['kind'] == 'publish', 'CURRICULUM_FIRST_REVIEW_UNAVAILABLE')
    expected = _publication(c,child_id,payload)
    _require(all(item[k] == v for k, v in expected.items()),
             'SUPPLY_CONTENT_CHANGED')


class CurriculumFirstSourceService:
    def __init__(self, database):
        self.database = database

    def initialize(self):
        with self.database.connect() as c:
            ensure_schema(c)
            c.commit()

    @staticmethod
    def row(c, child, cid):
        row = c.execute('SELECT * FROM curriculum_first_source WHERE child_id=? AND candidate_id=?', (child, cid)).fetchone()
        if row is None:
            raise KeyError(cid)
        return row

    @staticmethod
    def event(c, row, kind, actor, action, request_hash, result):
        c.execute('INSERT INTO curriculum_first_source_event VALUES(?,?,?,?,?,?,?,?,?,?)',
                  (str(uuid4()), row['candidate_id'], row['child_id'], row['version']+1, kind, actor, action, request_hash, _json(result), _now()))
        c.execute('UPDATE curriculum_first_source SET state=?,version=version+1 WHERE candidate_id=?',
                  (result['state'], row['candidate_id']))

    def generate(self, child, spec, *, action_id, expected_scope_version):
        self.initialize()
        _require(set(spec) == {'knowledge_node_id','excerpt_id','requirement_id','standard_page','standard_quote','explanation','difficulty'}, 'CURRICULUM_INPUT')
        action_id = _text(action_id, 120)
        request_hash = digest(['first', spec, expected_scope_version])
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            prior = c.execute('SELECT * FROM curriculum_first_source_event WHERE child_id=? AND action_id=?', (child, action_id)).fetchone()
            if prior:
                _require(prior['request_hash'] == request_hash, 'CURRICULUM_ACTION_CONFLICT')
                return self.get_in(c, child, prior['candidate_id'])
            raw = c.execute('''SELECT ke.content_text,ks.source_id FROM knowledge_excerpt ke JOIN knowledge_source ks USING(source_id)
                WHERE ke.excerpt_id=? AND ks.child_id=?''', (spec['excerpt_id'], child)).fetchone()
            if not raw:
                raise KeyError(spec['excerpt_id'])
            original, validator, payload_key = _original(raw['content_text'])
            _require(original['knowledge_node_id'] == spec['knowledge_node_id'], 'CURRICULUM_SCOPE_MISMATCH')
            _require(any(o['knowledge_node_id'] == spec['knowledge_node_id'] and o['excerpt_id'] == spec['excerpt_id']
                         for o in options(c, child, raw['source_id'])), 'CURRICULUM_FIRST_SOURCE_CONFLICT')
            payload = dict(spec,seed_item_id=None,stage='parallel',validator=validator,
                **{payload_key:original},prompt=original['prompt'],expected_answer=None)
            payload['explanation'] = _text(payload['explanation'], 1200)
            _require(type(payload['difficulty']) is int and 1 <= payload['difficulty'] <= 5, 'CURRICULUM_INPUT')
            proof = _evidence(c, child, payload, first_node_id=spec['knowledge_node_id'])
            _require(type(expected_scope_version) is int and proof['selection']['version'] == expected_scope_version,
                     'CURRICULUM_SCOPE_VERSION_CONFLICT')
            _require(not c.execute("SELECT 1 FROM curriculum_first_source WHERE child_id=? AND knowledge_node_id=? AND state NOT IN ('withdrawn','rejected')",
                                   (child, spec['knowledge_node_id'])).fetchone(), 'CURRICULUM_FIRST_SOURCE_CONFLICT')
            cid = 'first-'+str(uuid4())
            c.execute('INSERT INTO curriculum_first_source VALUES(?,?,?,?,?,?,?,0,NULL,?)',
                      (cid, child, spec['knowledge_node_id'], raw['source_id'], _json(payload), _json(proof), 'draft', _now()))
            self.event(c, self.row(c, child, cid), 'draft', AUTHOR, action_id, request_hash, dict(state='draft', blockers=[]))
            result = self.get_in(c, child, cid)
            c.commit()
            return result

    def transition(self, child, cid, kind, *, actor, action_id, expected_version, values=None):
        if kind == 'withdraw':
            # Disclosure checks and source withdrawal must own the same guard
            # before either opens its SQLite write transaction.
            with self.database.external_effect_guard(timeout=2):
                return self._transition(child, cid, kind, actor=actor, action_id=action_id,
                                        expected_version=expected_version, values=values)
        return self._transition(child, cid, kind, actor=actor, action_id=action_id,
                                expected_version=expected_version, values=values)

    def _transition(self, child, cid, kind, *, actor, action_id, expected_version, values=None):
        self.initialize()
        values = values or {}
        _require(kind in {'validate','review','publish','withdraw'}, 'CURRICULUM_INPUT')
        _require(actor == (VALIDATOR if kind == 'validate' else 'parent'), 'CURRICULUM_INDEPENDENT_REVIEW_REQUIRED')
        request_hash = digest([cid, kind, actor, expected_version, values])
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = self.row(c, child, cid)
            prior = c.execute('SELECT * FROM curriculum_first_source_event WHERE child_id=? AND action_id=?', (child, action_id)).fetchone()
            if prior:
                _require(prior['request_hash'] == request_hash, 'CURRICULUM_ACTION_CONFLICT')
                return self.get_in(c, child, cid)
            _require(type(expected_version) is int and row['version'] == expected_version, 'CURRICULUM_VERSION_CONFLICT')
            allowed = {'validate': {'draft','blocked'}, 'review': {'validated'}, 'publish': {'reviewed'},
                       'withdraw': {'draft','blocked','validated','reviewed','published','rejected'}}
            _require(row['state'] in allowed[kind], 'CURRICULUM_STATE_CONFLICT')
            _require(not values.get('stage_review'), 'CURRICULUM_FIRST_BASELINE_ONLY')
            payload = json.loads(row['payload_json'])
            result = dict(values, blockers=[], state={'validate':'validated','review':'reviewed','publish':'published','withdraw':'withdrawn'}[kind])
            if kind == 'review':
                _require(values.get('decision') in {'approve','reject'} and _text(values.get('note'), 500), 'CURRICULUM_INPUT')
                if values['decision'] == 'reject':
                    result['state'] = 'rejected'
            if result['state'] not in {'withdrawn','rejected'}:
                try:
                    proof = _evidence(c, child, payload, first_node_id=row['knowledge_node_id'])
                    _require(proof == json.loads(row['source_snapshot_json']), 'CURRICULUM_SOURCE_CHANGED')
                    if payload['validator']==opened.VALIDATOR:
                        checked = opened.check(payload,None,node=proof['open_response_node'],connection=c,child_id=child)
                        result['check'] = {k:v for k,v in checked.items() if k!='visual_materials'}
                    else:
                        result['check'] = dict(validator=objective.VALIDATOR,
                            computed_answer=payload['objective_source']['expected_answer']['exact_answers'][0],
                            question_hash=digest(payload['prompt']), payload_hash=digest(payload),
                            qualification='original_reference_checked_not_computed_or_skill_certified',
                            expected_answer=objective.exact_rubric(payload['objective_source']))
                except LocalSupplyError as error:
                    result.update(state='blocked', blockers=[str(error)])
            if result['state'] == 'published':
                item_id = 'diag-'+cid
                fields = _publication(c,child,payload)
                if row['diagnostic_item_id'] is not None:
                    prior_item = c.execute('SELECT * FROM diagnostic_item WHERE diagnostic_item_id=?', (item_id,)).fetchone()
                    _require(prior_item is not None and prior_item['source_ref'] == PREFIX+cid
                             and prior_item['status']=='retired' and all(prior_item[k]==v for k,v in fields.items()),
                             'CURRICULUM_FIRST_ITEM_CONFLICT')
                    c.execute("UPDATE diagnostic_item SET status='active' WHERE diagnostic_item_id=?", (item_id,))
                else:
                    c.execute('''INSERT INTO diagnostic_item(diagnostic_item_id,knowledge_node_id,prompt,answer_type,
                        expected_answer_json,explanation,assessment_mode,difficulty,source_ref,status,stimulus_text,stimulus_asset)
                        VALUES(?,?,?,?,?,?,?,?,?,'active',?,?)''',
                        (item_id,fields['knowledge_node_id'],fields['prompt'],fields['answer_type'],fields['expected_answer_json'],
                         fields['explanation'],fields['assessment_mode'],fields['difficulty'],PREFIX+cid,fields['stimulus_text'],fields['stimulus_asset']))
                c.execute('UPDATE curriculum_first_source SET diagnostic_item_id=? WHERE candidate_id=?', (item_id,cid))
            if result['state'] in {'withdrawn','rejected','blocked'} and row['diagnostic_item_id']:
                c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?", (row['diagnostic_item_id'],))
            self.event(c,row,kind,actor,_text(action_id,120),request_hash,result)
            current = self.get_in(c,child,cid)
            c.commit()
            return current

    def get(self, child, cid):
        self.initialize()
        with self.database.connect() as c:
            c.execute('BEGIN')
            return self.get_in(c,child,cid)

    def get_in(self,c,child,cid):
        row = self.row(c,child,cid)
        payload, proof = json.loads(row['payload_json']), json.loads(row['source_snapshot_json'])
        latest = c.execute('SELECT result_json FROM curriculum_first_source_event WHERE candidate_id=? ORDER BY version DESC LIMIT 1', (cid,)).fetchone()
        blockers = list(json.loads(latest[0]).get('blockers', [])) if latest and row['state'] == 'blocked' else []
        try:
            _require(_evidence(c,child,payload,first_node_id=row['knowledge_node_id']) == proof, 'CURRICULUM_SOURCE_CHANGED')
            if row['state'] == 'published':
                from .local_supply import validate_item
                validate_item(c,child,row['diagnostic_item_id'])
        except LocalSupplyError as error:
            blockers = list(dict.fromkeys([*blockers, str(error)]))
        packet = dict(fingerprint=digest(proof),scope=json.loads(proof['selection']['scope_json']),
            textbook={k:proof['source'][k] for k in ('source_id','title','source_ref','version_label','review_status','excerpt_id','content_text')},
            standard=dict(source_id=proof['standard']['source_id'],official_url=proof['standard']['official_url'],
                page_number=proof['page']['page_number'],requirement_text=proof['requirement']['requirement_text'],quote=payload['standard_quote']),
            review_scope='source_first_baseline_only_not_transfer_or_mastery')
        if payload['validator']==opened.VALIDATOR:
            packet['open_response'] = payload['open_response']
            if payload['open_response']['schema'] in {opened.MATERIAL_SCHEMA,opened.VISUAL_SCHEMA}:
                from .open_response_materials import parent_source_materials
                packet['current_materials'] = parent_source_materials(c,child_id=child,source=payload['open_response'],candidate_id=cid)
        events = [dict(version=e['version'],kind=e['kind'],actor=e['actor'],created_at=e['created_at'],result=json.loads(e['result_json']))
                  for e in c.execute('SELECT * FROM curriculum_first_source_event WHERE candidate_id=? ORDER BY version',(cid,))]
        return dict(candidate_id=cid,child_id=child,version=row['version'],state=row['state'],
            eligible=row['state']=='published' and not blockers,blockers=blockers,supersedes=None,
            diagnostic_item_id=row['diagnostic_item_id'],payload=payload,review_packet=packet,stage_review_context=None,events=events,
            next_action='revise_sources_or_question' if blockers else {'draft':'independent_validate','validated':'review',
                'reviewed':'publish','published':'plan_selection','withdrawn':'new_version','rejected':'new_version','blocked':'revise_sources_or_question'}[row['state']],
            local_only=True,cloud_transmission_allowed=False)


def purge_sources(c, child_id, *, source_ids=None, affected_scopes):
    """Before source DELETE: exact first-root descendants use existing native erasure.

    Host owns transaction, authorization, exports, and final mastery replay. This
    returns only counts; child/node scope stays in the transaction-local collector.
    """
    _require(c.in_transaction, 'CURRICULUM_TRANSACTION_REQUIRED')
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='curriculum_first_source'").fetchone():
        return {}
    rows = c.execute('SELECT * FROM curriculum_first_source WHERE child_id=?', (child_id,)).fetchall()
    if source_ids is not None:
        rows = [row for row in rows if row['source_id'] in source_ids]
    counts = {'curriculum_first_source':0,'curriculum_first_source_event':0}
    from .privacy_native_sources import purge_native_sources
    for row in rows:
        if row['diagnostic_item_id']:
            removed = purge_native_sources(c,child_id,{PREFIX+row['candidate_id']},affected_scopes=affected_scopes)
            for key,value in removed.items():
                counts[key] = counts.get(key,0)+value
        counts['curriculum_first_source_event'] += c.execute('DELETE FROM curriculum_first_source_event WHERE candidate_id=? AND child_id=?',
                                                            (row['candidate_id'],child_id)).rowcount
        counts['curriculum_first_source'] += c.execute('DELETE FROM curriculum_first_source WHERE candidate_id=? AND child_id=?',
                                                      (row['candidate_id'],child_id)).rowcount
    return counts


def quarantine_after_restore(c, restore_id):
    """Borrow staging transaction; no old approval automatically reactivates a root."""
    _require(c.in_transaction, 'CURRICULUM_TRANSACTION_REQUIRED')
    ensure_schema(c)
    count = 0
    for row in c.execute("SELECT * FROM curriculum_first_source WHERE state IN ('validated','reviewed','published')").fetchall():
        action = 'restore:'+_text(restore_id,100)+':'+row['candidate_id']
        if c.execute('SELECT 1 FROM curriculum_first_source_event WHERE child_id=? AND action_id=?',(row['child_id'],action)).fetchone():
            continue
        if row['diagnostic_item_id']:
            c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?",(row['diagnostic_item_id'],))
        CurriculumFirstSourceService.event(c,row,'restore_quarantine','system',action,digest([restore_id,row['candidate_id']]),
            dict(state='blocked',blockers=['CURRICULUM_FIRST_REVIEW_AFTER_RESTORE'],note='备份恢复后需重新校验和审核，旧操作只供核对。'))
        count += 1
    return count
