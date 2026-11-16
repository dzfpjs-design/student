"""Revocable, child/item/purpose permission; publication is never cloud consent."""
import json
from datetime import datetime, timezone
from urllib.parse import urlsplit
from uuid import uuid4

from .diagnostic_prompt import DiagnosticPromptService, _version
from .local_supply import LocalSupplyError, digest, validate_item
from .repository import utc_now

VERSION = 'writing_question_formative_text_v1'
MATERIAL_VERSION = 'writing_question_material_formative_text_v2'
TABLE = 'writing_question_authorization_event'
FLAGS = {'local_only':True, 'cloud_transmission_allowed':False}
LICENSE_OPTIONS = ('CC0-1.0','original_author','explicit_cloud_processing_permission')


def require(value, reason):
    if not value:
        raise ValueError(reason if reason.startswith('WRITING_QUESTION_') else 'WRITING_QUESTION_'+reason)


def _text(value, limit):
    require(isinstance(value,str) and 0<len(value.strip())<=limit,'INPUT_INVALID')
    return value


def _consent(c):
    preference = c.execute('SELECT * FROM privacy_preference WHERE singleton_id=1').fetchone()
    consent = c.execute("SELECT * FROM consent_record WHERE scope='cloud_model_text_allowed' ORDER BY rowid DESC LIMIT 1").fetchone()
    require(preference and preference['cloud_model_text_allowed']==1 and consent and consent['allowed']==1
        and consent['source']=='parent_ui' and consent['policy_version']=='privacy_v1','PARENT_TEXT_CONSENT_REQUIRED')
    try:
        at = datetime.fromisoformat(consent['occurred_at'])
    except (ValueError,TypeError):
        raise ValueError('WRITING_QUESTION_CONSENT_TIME_INVALID') from None
    require(at.tzinfo and at<=datetime.now(timezone.utc),'CONSENT_TIME_INVALID')
    return digest(dict(consent))


def _source_scope(c, child_id, item_id):
    """Use the existing publication validator, never infer rights from source_ref."""
    item = validate_item(c,child_id,item_id)
    open_response = item['answer_type']=='open_response' and item['assessment_mode']=='open_response_rubric'
    first_open = open_response and item['source_ref'].startswith('parent_approved_first:')
    require((open_response or item['answer_type']=='writing' and item['assessment_mode']=='pet_reference_rubric')
        and (first_open or item['source_ref'].startswith('parent_approved_specialized:')),'REVIEWED_WRITING_REQUIRED')
    require(not item['stimulus_asset'] and (not item['stimulus_text'] or open_response),'LINKED_MATERIAL_FORBIDDEN')
    cid = item['source_ref'].split(':',1)[1]
    table = 'curriculum_first_source' if first_open else 'curriculum_supply_package'
    package = c.execute(f'SELECT * FROM {table} WHERE candidate_id=? AND child_id=?', (cid,child_id)).fetchone()
    require(package and package['state']=='published','PUBLICATION_REQUIRED')
    proof = json.loads(package['source_snapshot_json'])
    source = proof['source']
    # A full source snapshot equality was already checked by validate_item.
    require(source['child_id']==child_id and source['license_status']=='public'
        and source['source_type'] in {'school_material','curriculum'},'SOURCE_NOT_AUTHORIZABLE')
    require(not c.execute("SELECT 1 FROM sqlite_master WHERE name='privacy_source_erasure'").fetchone()
        or not c.execute('SELECT 1 FROM privacy_source_erasure WHERE child_id=? AND source_id=?',
            (child_id,source['source_id'])).fetchone(),'SOURCE_ERASED')
    if open_response:
        from .open_response_source import parse, VALIDATOR
        from .open_response_profile import profile_for_item
        node = c.execute('SELECT * FROM knowledge_node WHERE knowledge_node_id=?',(item['knowledge_node_id'],)).fetchone()
        profile_for_item(item,dict(node))
        payload_key,node_key='open_response','open_response_node'
    else:
        from .pet_writing_source import parse, VALIDATOR
        payload_key,node_key='pet_writing','writing_node'
    payload = json.loads(package['payload_json'])
    require(payload.get('validator')==VALIDATOR,'REVIEWED_WRITING_REQUIRED')
    original = parse(source['content_text'])
    require(original==payload.get(payload_key) and original['prompt']==item['prompt']
        and original['task_points']==json.loads(item['expected_answer_json'])['task_points'],'SOURCE_CHANGED')
    candidate = package if first_open else c.execute('SELECT * FROM specialized_supply_candidate WHERE candidate_id=?',(cid,)).fetchone()
    event_table = 'curriculum_first_source_event' if first_open else 'curriculum_supply_event'
    events = [dict(r) for r in c.execute(f'SELECT * FROM {event_table} WHERE candidate_id=? AND child_id=? ORDER BY version',(cid,child_id))]
    scope = {'item_version':_version(DiagnosticPromptService._item(c,child_id,item_id)),
        'source_version':digest({'item':item,'source':source,'candidate':dict(candidate),'package':dict(package),'events':events}),
        'curriculum_version':digest({key:proof[key] for key in ('selection','requirement','standard','page','seed',node_key)}),
        'source_id':source['source_id'],'candidate_id':cid,'purpose_version':VERSION,
        'preview':{'question':original['prompt'],'task_points':original['task_points']}}
    if open_response and original['context_requirement'] == 'external_material':
        from .open_response_materials import resolve_open_response_materials
        resolved = resolve_open_response_materials(c, child_id=child_id,
            diagnostic_item_id=item_id, expected_item_version=scope['item_version'])
        require(resolved['status'] == 'ready', 'MATERIAL_SOURCE_UNAVAILABLE')
        require(resolved['stimulus_text'] == item['stimulus_text'], 'MATERIAL_SOURCE_CHANGED')
        for material in resolved['materials']:
            basis = material['source_basis']
            require(basis['child_id'] == child_id and basis['license_status'] == 'public'
                and basis['source_type'] in {'school_material', 'curriculum'}, 'MATERIAL_SOURCE_NOT_AUTHORIZABLE')
        scope.update(purpose_version=MATERIAL_VERSION,
            material_binding_version=resolved['binding_version'],
            material_bindings=[material['binding'] for material in resolved['materials']])
        scope['source_version'] = digest({'question': scope['source_version'],
            'material_binding_version': resolved['binding_version'], 'material_bindings': scope['material_bindings']})
        scope['preview'] = {'question': question_text(item), 'task_points': original['task_points'],
            'materials': [{'material_key': material['material_key'], 'text': material['text'],
                'title': material['source_basis']['title'], 'license_status': material['source_basis']['license_status'],
                'source_version': material['binding']['source_version'], 'text_hash': material['binding']['text_hash']}
                for material in resolved['materials']]}
    else:
        require(not item['stimulus_text'], 'LINKED_MATERIAL_FORBIDDEN')
    return scope


def question_text(item):
    return item['prompt'] + ('\n\n' + item['stimulus_text'] if item.get('stimulus_text') else '')


def _scope(c, child_id, item_id):
    try:
        return _source_scope(c,child_id,item_id)
    except LocalSupplyError as error:
        raise ValueError('WRITING_QUESTION_SOURCE_INVALID:'+str(error)) from error
    except (KeyError,TypeError,json.JSONDecodeError) as error:
        raise ValueError('WRITING_QUESTION_SOURCE_PROOF_INVALID') from error


def _license(value):
    require(isinstance(value,dict) and set(value)=={'license_id','reference','evidence'},'LICENSE_EVIDENCE_REQUIRED')
    # Other licenses need their own attribution/terms implementation, not a label override.
    require(value['license_id'] in LICENSE_OPTIONS,'LICENSE_UNSUPPORTED')
    try:
        reference = urlsplit(_text(value['reference'],1024))
    except ValueError:
        raise ValueError('WRITING_QUESTION_LICENSE_REFERENCE_INVALID') from None
    web_reference = reference.scheme=='https' and reference.hostname and not reference.username and not reference.password and not reference.fragment
    declaration_reference = value['license_id']!='CC0-1.0' and not reference.scheme and not value['reference'].startswith(('/','\\'))
    require(web_reference or declaration_reference,'LICENSE_REFERENCE_INVALID')
    _text(value['evidence'],2000)
    require(len(value['evidence'].strip())>=20,'LICENSE_EVIDENCE_REQUIRED')
    return dict(value)


def _latest(c, child_id, item_id):
    if not c.execute('SELECT 1 FROM sqlite_master WHERE name=?',(TABLE,)).fetchone():
        return None
    return c.execute('SELECT * FROM '+TABLE+' WHERE child_id=? AND diagnostic_item_id=? ORDER BY revision DESC LIMIT 1',
        (child_id,item_id)).fetchone()


def _licenses_for_scope(value, scope):
    if scope['purpose_version'] == VERSION:
        return _license(value)
    require(scope['purpose_version'] == MATERIAL_VERSION, 'PURPOSE_UNSUPPORTED')
    require(isinstance(value, dict) and set(value) == {'question', 'materials'}, 'MATERIAL_LICENSES_REQUIRED')
    question = _license(value['question'])
    materials = value['materials']
    bindings = scope.get('material_bindings')
    require(isinstance(bindings, list) and bindings and isinstance(materials, list)
            and len(materials) == len(bindings), 'MATERIAL_LICENSES_REQUIRED')
    checked = []
    for material, binding in zip(materials, bindings):
        require(isinstance(material, dict) and set(material) == {
            'material_key', 'source_version', 'text_hash', 'license_basis'}, 'MATERIAL_LICENSES_INVALID')
        require(all(material[key] == binding[key] for key in ('material_key', 'source_version', 'text_hash')),
                'MATERIAL_LICENSE_VERSION_CHANGED')
        checked.append({**material, 'license_basis': _license(material['license_basis'])})
    return {'question': question, 'materials': checked}


def _binding(scope, consent):
    return {**{key:value for key,value in scope.items() if key!='preview'},'consent_version':consent}


def _read(c, child_id, item_id):
    latest = _latest(c,child_id,item_id)
    base = dict(child_id=child_id,diagnostic_item_id=item_id,revision=latest['revision'] if latest else 0,
        status='authorization_required',reason=None,item_version=None,source_version=None,curriculum_version=None,
        preview=None,authorization_id=None,purpose_version=None,license_options=list(LICENSE_OPTIONS),
        rights_review_authority='parent_declaration_not_legal_or_network_verification',**FLAGS)
    try:
        scope = _scope(c,child_id,item_id)
        base.update({key:scope[key] for key in ('item_version','source_version','curriculum_version','purpose_version','preview')})
        consent = _consent(c)
        if latest:
            require(latest['command']=='grant','REVOKED')
            require(json.loads(latest['binding_json'])==_binding(scope,consent)
                and all(latest[key]==scope[key] for key in ('item_version','source_version','curriculum_version','source_id','purpose_version')),
                'AUTHORIZATION_VERSION_CHANGED')
            _licenses_for_scope(json.loads(latest['license_json']), scope)
            base.update(status='authorized',authorization_id=latest['event_id'])
    except (ValueError, LocalSupplyError) as error:
        if isinstance(error,ValueError) and not isinstance(error,LocalSupplyError) and not str(error).startswith('WRITING_QUESTION_'):
            raise
        base.update(status='invalidated' if latest else 'blocked',reason=str(error))
    if latest and latest['command']=='revoke':
        base.update(status='revoked',authorization_id=None,reason='WRITING_QUESTION_'+
            ('RESTORE_REAUTHORIZATION_REQUIRED' if latest['actor_id']=='system:restore' else 'REVOKED'))
    return base


def authorized_question_projection(c, *, child_id, diagnostic_item_id):
    """Local read-only DTO. Only its preview is eligible for outer safe projection."""
    view = _read(c,child_id,diagnostic_item_id)
    require(view['status']=='authorized',view['reason'] or 'AUTHORIZATION_REQUIRED')
    event = _latest(c,child_id,diagnostic_item_id)
    return {'question':view['preview']['question'],'task_points':view['preview']['task_points'],
        'local_audit':{key:view[key] for key in ('child_id','diagnostic_item_id','item_version','source_version','curriculum_version','authorization_id')},
        'authorization_version':digest(dict(event)), 'purpose_version':view['purpose_version'],**FLAGS}


def quarantine_after_restore(c, restore_id):
    """Append revocations in the staged restore transaction. No commit or DDL."""
    require(c.in_transaction,'TRANSACTION_REQUIRED')
    _text(restore_id,200)
    if not c.execute('SELECT 1 FROM sqlite_master WHERE name=?',(TABLE,)).fetchone():
        return 0
    latest = c.execute('''SELECT e.* FROM writing_question_authorization_event e
        WHERE e.command='grant' AND NOT EXISTS(SELECT 1 FROM writing_question_authorization_event later
            WHERE later.child_id=e.child_id AND later.diagnostic_item_id=e.diagnostic_item_id AND later.revision>e.revision)''').fetchall()
    count = 0
    for row in latest:
        key = 'restore:'+digest([restore_id,row['child_id'],row['diagnostic_item_id']])
        if c.execute('SELECT 1 FROM '+TABLE+' WHERE child_id=? AND client_action_id=?',(row['child_id'],key)).fetchone():
            continue
        c.execute('INSERT INTO '+TABLE+' VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (str(uuid4()),row['child_id'],row['diagnostic_item_id'],row['source_id'],row['revision']+1,'revoke',
             row['item_version'],row['source_version'],row['curriculum_version'],row['purpose_version'],'system:restore',
             digest({'restore_id':restore_id,'prior_event_id':row['event_id'],'purpose':'restore_quarantine'}),
             row['binding_json'],row['license_json'],key,utc_now()))
        count += 1
    return count


class WritingQuestionAuthorizationService:
    """actor_id is supplied by the host's authenticated parent dependency only."""
    def __init__(self,database):
        self.database = database

    def initialize(self):
        with self.database.connect() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS writing_question_authorization_event(
                event_id TEXT PRIMARY KEY,
                child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                diagnostic_item_id TEXT NOT NULL REFERENCES diagnostic_item(diagnostic_item_id) ON DELETE CASCADE,
                source_id TEXT NOT NULL REFERENCES knowledge_source(source_id) ON DELETE CASCADE,
                revision INTEGER NOT NULL CHECK(revision>0), command TEXT NOT NULL CHECK(command IN ('grant','revoke')),
                item_version TEXT NOT NULL,source_version TEXT NOT NULL,curriculum_version TEXT NOT NULL,
                purpose_version TEXT NOT NULL,actor_id TEXT NOT NULL,request_hash TEXT NOT NULL,
                binding_json TEXT NOT NULL,license_json TEXT NOT NULL,client_action_id TEXT NOT NULL,created_at TEXT NOT NULL,
                UNIQUE(child_id,diagnostic_item_id,revision),UNIQUE(child_id,client_action_id))''')
            c.execute('''CREATE TRIGGER IF NOT EXISTS writing_question_authorization_no_update
                BEFORE UPDATE ON writing_question_authorization_event
                BEGIN SELECT RAISE(ABORT,'immutable writing purpose authorization'); END''')
            c.commit()

    def read(self,child_id,*,diagnostic_item_id):
        with self.database.connect() as c:
            c.execute('BEGIN')
            return _read(c,child_id,diagnostic_item_id)

    def grant(self,child_id,*,diagnostic_item_id,item_version,source_version,curriculum_version,
              license_basis,expected_revision,client_action_id,actor_id,material_license_bases=None):
        data = {'item_version':item_version,'source_version':source_version,'curriculum_version':curriculum_version,
                'license_basis':_license(license_basis)}
        if material_license_bases is not None:
            require(isinstance(material_license_bases, list), 'MATERIAL_LICENSES_INVALID')
            data['material_license_bases'] = material_license_bases
        return self._command(child_id,diagnostic_item_id,'grant',expected_revision,client_action_id,actor_id,
            data)

    def revoke(self,child_id,*,diagnostic_item_id,expected_revision,client_action_id,actor_id):
        return self._command(child_id,diagnostic_item_id,'revoke',expected_revision,client_action_id,actor_id,{})

    def _command(self,child_id,item_id,command,expected_revision,key,actor,data):
        _text(actor,200)
        _text(key,200)
        require(type(expected_revision) is int and expected_revision>=0,'REVISION_CONFLICT')
        request_hash = digest(dict(child_id=child_id,diagnostic_item_id=item_id,command=command,
            expected_revision=expected_revision,actor_id=actor,data=data,purpose_version=VERSION))
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            require(c.execute("SELECT 1 FROM child_profile WHERE child_id=? AND status='active'",(child_id,)).fetchone(),'CHILD_UNAVAILABLE')
            existing = c.execute('SELECT * FROM '+TABLE+' WHERE child_id=? AND client_action_id=?',(child_id,key)).fetchone()
            if existing:
                require(existing['request_hash']==request_hash,'REQUEST_CONFLICT')
                return {'receipt':{key:existing[key] for key in ('event_id','revision','command')},'current':_read(c,child_id,item_id)}
            latest = _latest(c,child_id,item_id)
            require((latest['revision'] if latest else 0)==expected_revision,'REVISION_CONFLICT')
            if command=='grant':
                scope = _scope(c,child_id,item_id)
                require(all(data[key]==scope[key] for key in ('item_version','source_version','curriculum_version')),'AUTHORIZATION_VERSION_CHANGED')
                requested_license = ({'question': data['license_basis'], 'materials': data['material_license_bases']}
                    if 'material_license_bases' in data else data['license_basis'])
                binding,license_basis = _binding(scope,_consent(c)),_licenses_for_scope(requested_license, scope)
            else:
                require(latest is not None,'AUTHORIZATION_REQUIRED')
                binding,license_basis = json.loads(latest['binding_json']),json.loads(latest['license_json'])
            event_id = str(uuid4())
            c.execute('INSERT INTO '+TABLE+' VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (event_id,child_id,item_id,binding['source_id'],expected_revision+1,command,
                 binding['item_version'],binding['source_version'],binding['curriculum_version'],binding['purpose_version'],actor,request_hash,
                 json.dumps(binding,ensure_ascii=False),json.dumps(license_basis,ensure_ascii=False),key,utc_now()))
            current = _read(c,child_id,item_id)
            c.commit()
            return {'receipt':{'event_id':event_id,'revision':expected_revision+1,'command':command},'current':current}
