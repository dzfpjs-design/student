"""Exam-owned original-media delivery using the existing source and PCM services.

This module never scores. Frozen exam slots remain the sole scoring owner.
"""
import json
from datetime import datetime, timezone
from hashlib import sha256

VERSION = 'exam_listening_delivery_v1'


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _now():
    return datetime.now(timezone.utc).isoformat()


def _error(reason):
    from .exam_simulation import ExamSimulationError
    raise ExamSimulationError(reason)


def exam_owner(prompt):
    if 'owner_kind' in prompt.keys() and prompt['owner_kind'] == 'exam':
        return True
    try:
        request = json.loads(prompt['request_json'])
        return 'exam_slot' in request or VERSION in request
    except (ValueError, TypeError):
        return False


def require_diagnostic_owner(prompt):
    if exam_owner(prompt):
        from .diagnostic_prompt import DiagnosticPromptError
        raise DiagnosticPromptError('exam_prompt_requires_exam_submit')


def ensure_schema(c):
    c.execute('''CREATE TABLE IF NOT EXISTS exam_listening_delivery_slot(
        run_id TEXT NOT NULL REFERENCES specialized_exam_run(run_id) ON DELETE CASCADE,
        position INTEGER NOT NULL, prompt_id TEXT NOT NULL UNIQUE REFERENCES diagnostic_prompt(prompt_id) ON DELETE CASCADE,
        unit_key TEXT NOT NULL, source_json TEXT NOT NULL,
        PRIMARY KEY(run_id,position),
        FOREIGN KEY(run_id,position) REFERENCES specialized_exam_item(run_id,position) ON DELETE CASCADE)''')
    c.execute('''CREATE TABLE IF NOT EXISTS exam_listening_delivery_unit(
        run_id TEXT NOT NULL REFERENCES specialized_exam_run(run_id) ON DELETE CASCADE,
        unit_key TEXT NOT NULL, prompt_id TEXT NOT NULL REFERENCES diagnostic_prompt(prompt_id) ON DELETE CASCADE,
        declaration_json TEXT NOT NULL, PRIMARY KEY(run_id,unit_key))''')


def migrate_owner(c):
    """Only add ownership to proven legacy originals; never rewrite provenance."""
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='specialized_exam_item'").fetchone():
        return
    columns = {r['name'] for r in c.execute('PRAGMA table_info(specialized_exam_item)')}
    if not {'source_version','authorized_turn_id','answer_text','attempt_id'} <= columns:
        return
    for row in c.execute("SELECT * FROM diagnostic_prompt WHERE owner_kind='diagnostic'").fetchall():
        try:
            request = json.loads(row['request_json'])
            if not isinstance(request, dict):
                continue
            binding = request.get('exam_slot')
            if not isinstance(binding, dict) or set(binding) != {'run_id','position','item_id','source_version'}:
                continue
            slot = c.execute('''SELECT r.child_id,r.session_id,i.* FROM specialized_exam_item i
                JOIN specialized_exam_run r USING(run_id) WHERE i.run_id=? AND i.position=?''',
                (binding['run_id'],binding['position'])).fetchone()
            if (not slot or (row['child_id'],row['origin_session_id'],row['diagnostic_item_id'],row['attempt_id']) !=
                    (slot['child_id'],slot['session_id'],slot['diagnostic_item_id'],slot['attempt_id'])
                    or row['status'] != 'answered' or not slot['attempt_id'] or binding['item_id'] != slot['diagnostic_item_id']
                    or binding['source_version'] != slot['source_version']
                    or row['authorized_turn_id'] != slot['authorized_turn_id']):
                continue
            attempt = c.execute('SELECT * FROM diagnostic_attempt WHERE attempt_id=?',(row['attempt_id'],)).fetchone()
            if not attempt or (attempt['child_id'],attempt['session_id'],attempt['diagnostic_item_id']) != (
                    row['child_id'],row['origin_session_id'],row['diagnostic_item_id']):
                continue
            if not slot['authorized_turn_id'] or attempt['submitted_answer'] != slot['answer_text']:
                continue
            origin = c.execute('''SELECT t.action_json,s.arguments_json FROM agent_turn t JOIN agent_plan p USING(agent_turn_id)
                JOIN agent_step s USING(plan_id) WHERE t.agent_turn_id=? AND t.child_id=? AND t.session_id=?
                AND p.child_id=t.child_id AND p.session_id=t.session_id AND s.step_index=0 AND s.capability_name='exam.submit' ''',
                (slot['authorized_turn_id'],row['child_id'],row['origin_session_id'])).fetchone()
            if (not origin or json.loads(origin['action_json']).get('exam_input_binding') != binding
                    or json.loads(origin['arguments_json']).get('answer') != attempt['submitted_answer']):
                continue
            c.execute("UPDATE diagnostic_prompt SET owner_kind='exam' WHERE prompt_id=?",(row['prompt_id'],))
        except (ValueError, TypeError, KeyError):
            continue


def _run(c, run_id, *, active=False):
    run = c.execute('''SELECT r.*,s.child_id AS session_child,s.status AS session_status,
        cp.status AS child_status FROM specialized_exam_run r JOIN daily_session s USING(session_id)
        JOIN child_profile cp ON cp.child_id=r.child_id WHERE r.run_id=?''',(run_id,)).fetchone()
    if not run or run['child_id'] != run['session_child']:
        _error('EXAM_MEDIA_OWNER_CONFLICT')
    if active and (run['status'] != 'active' or run['session_status'] != 'active' or run['child_status'] != 'active'):
        _error('EXAM_MEDIA_RUN_NOT_ACTIVE')
    return run


def read_source(c, run, slot):
    from .diagnostic_prompt import DiagnosticPromptService, _version
    from .exam_simulation import ExamSimulationService
    from .pet_reviewed_source import published_listening_source
    from .pet_listening_requirement import listening_requirement
    item = DiagnosticPromptService._item(c,run['child_id'],slot['diagnostic_item_id'])
    if not item or ExamSimulationService._source_version(c,run,slot) != slot['source_version']:
        _error('EXAM_MEDIA_SOURCE_CHANGED')
    requirement = listening_requirement(c,run['child_id'],item['diagnostic_item_id'])
    source = published_listening_source(c,run['child_id'],item['diagnostic_item_id'])
    declaration = source.get('delivery_unit') if source else None
    question_key = source.get('question_key') if source else None
    if declaration:
        unit_key = 'reviewed-unit:' + sha256(_json({'version':requirement.get('version'),
            'unit_key':declaration['unit_key']}).encode()).hexdigest()
    else:
        unit_key = 'single:' + str(slot['position'])
        question_key = item['diagnostic_item_id']
        declaration = dict(unit_key=unit_key,member_keys=[question_key],
            original_audio_key=item['source_ref'],required_play_count=requirement.get('required_play_count'))
    return dict(item_version=_version(item),source_version=slot['source_version'],question_key=question_key,
        unit_key=unit_key,declaration=declaration,requirement=requirement)


def complete_group_selection(c, child_id, selected, by_domain, *, full):
    """Freeze whole audited groups, preserving each full-blueprint domain quota."""
    from .pet_reviewed_source import published_listening_source
    from .pet_listening_requirement import listening_requirement
    packets = {}
    for domain,items in by_domain.items():
        for item in items:
            try:
                source = published_listening_source(c,child_id,item['diagnostic_item_id'])
                if source and source.get('delivery_unit'):
                    requirement = listening_requirement(c,child_id,item['diagnostic_item_id'])
                    packets[item['diagnostic_item_id']] = (domain,item,source,requirement['version'])
            except (ValueError,TypeError,KeyError):
                continue
    if not packets:
        return selected

    def group_for(item_id, domain):
        packet = packets.get(item_id)
        if not packet:
            return None
        _,_,source,version = packet
        members = [p for p in packets.values() if p[0]==domain and p[3]==version
            and p[2]['delivery_unit']==source['delivery_unit']]
        if {p[2]['question_key'] for p in members} != set(source['delivery_unit']['member_keys']):
            return None
        return sorted((p[1] for p in members),key=lambda i:source['delivery_unit']['member_keys'].index(packets[i['diagnostic_item_id']][2]['question_key']))

    if not full:
        result,seen = [],set()
        for section,domain,item in selected:
            group = group_for(item['diagnostic_item_id'],domain)
            if item['diagnostic_item_id'] in packets and group is None:
                _error('EXAM_MEDIA_SOURCE_GROUP_INCOMPLETE')
            for member in group or [item]:
                if member['diagnostic_item_id'] not in seen:
                    result.append((section,domain,member))
                    seen.add(member['diagnostic_item_id'])
        return result
    categories = {}
    for section,domain,item in selected:
        categories.setdefault((section,domain),[]).append(item)
    result = []
    for (section,domain),original in categories.items():
        if not any(item['diagnostic_item_id'] in packets for item in original):
            result.extend((section,domain,item) for item in original)
            continue
        groups,seen = [],set()
        for item in original + by_domain.get(domain,[]):
            if item['diagnostic_item_id'] in seen:
                continue
            group = group_for(item['diagnostic_item_id'],domain)
            if group is None:
                if item['diagnostic_item_id'] in packets:
                    continue
                group = [item]
            groups.append(group)
            seen.update(member['diagnostic_item_id'] for member in group)
        choices = {0:[]}
        target = len(original)
        for group in groups:
            for count,path in list(choices.items()):
                total = count+len(group)
                if total<=target and total not in choices:
                    choices[total] = path+group
        if target not in choices:
            _error('EXAM_LISTENING_GROUP_BLUEPRINT_QUOTA_UNAVAILABLE')
        result.extend((section,domain,item) for item in choices[target])
    return result


def prepare(c, run):
    from .diagnostic_prompt import DiagnosticPromptService, _ITEM_FIELDS, _version
    from .diagnostic_history_identity import snapshot
    from .exam_simulation import ExamSimulationService
    ordinary = c.execute("""SELECT * FROM diagnostic_prompt WHERE origin_session_id=?
        AND status IN ('waiting_answer','answer_saved')""",(run['session_id'],)).fetchall()
    if any(not exam_owner(row) for row in ordinary):
        _error('EXAM_ORDINARY_DRAFT_PENDING')
    sources, units = [], {}
    for slot in c.execute('SELECT * FROM specialized_exam_item WHERE run_id=? ORDER BY position',(run['run_id'],)).fetchall():
        if not ExamSimulationService._listening_item(c,slot['diagnostic_item_id']):
            continue
        source = read_source(c,run,slot)
        if source['requirement'].get('status')!='known':
            _error('EXAM_LISTENING_ORIGINAL_INSTRUCTION_REQUIRED')
        item = DiagnosticPromptService._item(c,run['child_id'],slot['diagnostic_item_id'])
        prompt_id = f"exam-media:{run['run_id']}:{slot['position']}"
        binding = ExamSimulationService._review_binding(run,slot)
        now = _now()
        request = {'exam_slot':binding,VERSION:{'unit_key':source['unit_key']}}
        c.execute('''INSERT INTO diagnostic_prompt(prompt_id,child_id,origin_session_id,diagnostic_item_id,
            item_version,item_json,request_json,prepare_action_id,status,created_at,updated_at,identity_snapshot_json,owner_kind)
            VALUES(?,?,?,?,?,?,?,?,'waiting_answer',?,?,?,'exam')''',
            (prompt_id,run['child_id'],run['session_id'],item['diagnostic_item_id'],_version(item),
             _json({key:item[key] for key in _ITEM_FIELDS}),_json(request),'exam-media-prepare:'+prompt_id,
             now,now,snapshot(c,child_id=run['child_id'],session_id=run['session_id'],prompt_id=prompt_id,item=item)))
        c.execute('INSERT INTO exam_listening_delivery_slot VALUES(?,?,?,?,?)',
            (run['run_id'],slot['position'],prompt_id,source['unit_key'],_json(source)))
        if source['unit_key'] not in units:
            units[source['unit_key']] = (prompt_id,source['declaration'])
        sources.append(source)
    for unit_key,(prompt_id,declaration) in units.items():
        c.execute('INSERT INTO exam_listening_delivery_unit VALUES(?,?,?,?)',
            (run['run_id'],unit_key,prompt_id,_json(declaration)))
        from .pet_listening_reuse import bind_reviewed_listening_resource
        prompt = c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?',(prompt_id,)).fetchone()
        bind_reviewed_listening_resource(c,run['session_id'],run['child_id'],prompt_id,prompt['item_version'])


def frozen(c, prompt_id):
    """Historical ownership/source read. It deliberately has no active-session gate."""
    delivery = c.execute('SELECT * FROM exam_listening_delivery_slot WHERE prompt_id=?',(prompt_id,)).fetchone()
    if not delivery:
        _error('EXAM_MEDIA_SLOT_MISSING')
    run = _run(c,delivery['run_id'])
    slot = c.execute('SELECT * FROM specialized_exam_item WHERE run_id=? AND position=?',
        (delivery['run_id'],delivery['position'])).fetchone()
    source = read_source(c,run,slot)
    if _json(source) != delivery['source_json']:
        _error('EXAM_MEDIA_SOURCE_CHANGED')
    unit = c.execute('SELECT * FROM exam_listening_delivery_unit WHERE run_id=? AND unit_key=?',
        (delivery['run_id'],delivery['unit_key'])).fetchone()
    members = c.execute('''SELECT d.*,s.section_id FROM exam_listening_delivery_slot d
        JOIN specialized_exam_item s USING(run_id,position) WHERE d.run_id=? AND d.unit_key=?''',
        (delivery['run_id'],delivery['unit_key'])).fetchall()
    member_sources = [json.loads(row['source_json']) for row in members]
    if (not unit or unit['declaration_json'] != _json(source['declaration'])
            or set(source['declaration']['member_keys']) != {s['question_key'] for s in member_sources}
            or len({row['section_id'] for row in members}) != 1
            or any(s['declaration'] != source['declaration'] or s['requirement'] != source['requirement'] for s in member_sources)):
        _error('EXAM_MEDIA_SOURCE_GROUP_INCOMPLETE')
    # Validate every member against current original source, not merely its frozen JSON.
    for member in members:
        member_slot = c.execute('SELECT * FROM specialized_exam_item WHERE run_id=? AND position=?',
            (run['run_id'],member['position'])).fetchone()
        if _json(read_source(c,run,member_slot)) != member['source_json']:
            _error('EXAM_MEDIA_SOURCE_CHANGED')
    return run,slot,source,unit


def manifest(c, run):
    from .pet_listening_admission import resolve_pet_listening_resource
    result = dict(status='ready',blockers=[],units=[],local_only=True,cloud_transmission_allowed=False)
    for unit in c.execute('SELECT * FROM exam_listening_delivery_unit WHERE run_id=? ORDER BY rowid',(run['run_id'],)).fetchall():
        try:
            _,_,source,_ = frozen(c,unit['prompt_id'])
            if source['requirement'].get('status') != 'known':
                _error('EXAM_MEDIA_REQUIREMENT_UNVERIFIED')
            resource = resolve_pet_listening_resource(c,run['session_id'],run['child_id'],unit['prompt_id'],source['item_version'])
            if resource['status'] != 'ready':
                _error(resource['reason'])
            from .pet_listening_reuse import listening_source_review
            media = c.execute('SELECT * FROM pet_media_binding WHERE media_id=?',(resource['resource']['media_id'],)).fetchone()
            review = listening_source_review(c,media)
            result['units'].append(dict(unit_key=unit['unit_key'],prompt_id=unit['prompt_id'],resource=resource['resource'],
                approval_event_id=review['event_id'] if review else None))
        except ValueError as exc:
            result['blockers'].append(dict(unit_key=unit['unit_key'],prompt_id=unit['prompt_id'],reason=str(exc)))
    if result['blockers']:
        result['status'] = 'preparing'
    return result


def refresh_ready(c, run):
    result = manifest(c,run)
    if run['status']=='paused':
        version = sha256(_json(result['units']).encode()).hexdigest() if result['status']=='ready' else None
        if version and version != run['ready_version']:
            phase = 'ready' if run['delivery_phase'] in {'preparing','ready'} else 'paused'
            c.execute("""UPDATE specialized_exam_run SET delivery_phase=?,ready_at=?,ready_version=?,
                control_revision=control_revision+1 WHERE run_id=?""",(phase,_now(),version,run['run_id']))
        elif not version and run['ready_version']:
            c.execute("""UPDATE specialized_exam_run SET delivery_phase='preparing',ready_at=NULL,
                ready_version=NULL,control_revision=control_revision+1 WHERE run_id=?""",(run['run_id'],))
    return result


def refresh_reviewed_media_owner(c, media):
    """Persist current readiness with the canonical original audio review."""
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='exam_listening_delivery_unit'").fetchone():
        return
    run = c.execute('''SELECT r.* FROM specialized_exam_run r
        JOIN exam_listening_delivery_unit u USING(run_id)
        WHERE u.prompt_id=? AND r.child_id=? AND r.session_id=?
        AND r.status='paused' AND r.delivery_phase!='ordinary' ''',
        (media['prompt_id'],media['child_id'],media['session_id'])).fetchone()
    if run:
        refresh_ready(c,run)


def control_snapshot(c, run):
    return dict(run_id=run['run_id'],control_revision=run['control_revision'],ready_at=run['ready_at'],
        delivery_phase=run['delivery_phase'],ready_version=run['ready_version'])


def turn_arrival(c, turn_id, run):
    if not turn_id:
        _error('EXAM_LISTENING_CHILD_TURN_REQUIRED')
    row = c.execute('''SELECT a.* FROM agent_turn t JOIN learning_input_arrival a
        ON a.child_id=t.child_id AND a.session_id=t.session_id AND a.client_action_id=t.client_action_id
        WHERE t.agent_turn_id=? AND t.child_id=? AND t.session_id=?''',
        (turn_id,run['child_id'],run['session_id'])).fetchone()
    if not row:
        _error('EXAM_LISTENING_ARRIVAL_REQUIRED')
    return row


def original_arrival(c,turn_id,run,slot,answer):
    authorized = c.execute('SELECT * FROM agent_turn WHERE agent_turn_id=? AND child_id=? AND session_id=?',
        (turn_id,run['child_id'],run['session_id'])).fetchone()
    if not authorized:
        _error('EXAM_LISTENING_CHILD_TURN_REQUIRED')
    action = json.loads(authorized['action_json'])
    original_id = action.get('answer_source_turn_id') or turn_id
    if original_id != turn_id:
        original = c.execute('SELECT * FROM agent_turn WHERE agent_turn_id=? AND child_id=? AND session_id=?',
            (original_id,run['child_id'],run['session_id'])).fetchone()
        candidate = (json.loads(original['action_json']).get('pending_answer_candidate') or {}) if original else {}
        from .exam_simulation import ExamSimulationService
        if (not original or candidate.get('answer') != answer or original['transcript'] != answer
                or (candidate.get('target') or {}).get('binding') != ExamSimulationService._review_binding(run,slot)
                or action.get('local_only') is not True):
            _error('EXAM_LISTENING_ORIGINAL_ANSWER_CONFLICT')
    return turn_arrival(c,original_id,run)


def activate(c, run, turn_id, gate):
    ready = refresh_ready(c,run)
    current = _run(c,run['run_id'])
    if ready['status'] != 'ready':
        _error('EXAM_LISTENING_RESOURCES_NOT_READY')
    arrival = turn_arrival(c,turn_id,run)
    bound = next((r['record'] for r in json.loads(arrival['snapshot_json']) if r.get('kind') == 'exam_listening_control'),None)
    latest = c.execute('SELECT arrival_id FROM learning_input_arrival WHERE session_id=? AND child_id=? ORDER BY rowid DESC LIMIT 1',
        (run['session_id'],run['child_id'])).fetchone()
    if gate is None:
        # The runtime's bounded local exam grammar does not create a classifier
        # receipt. Its real running server step is still a typed grant.
        execution = c.execute('''SELECT t.action_json FROM agent_turn t JOIN agent_plan p USING(agent_turn_id)
            JOIN agent_step s USING(plan_id) WHERE t.agent_turn_id=? AND t.child_id=? AND t.session_id=?
            AND p.child_id=t.child_id AND p.session_id=t.session_id AND p.status='running'
            AND s.step_index=0 AND s.status='running' AND s.execution_location='server'
            AND s.capability_name='exam.resume' ''',(turn_id,run['child_id'],run['session_id'])).fetchone()
        action = json.loads(execution['action_json']) if execution else {}
        if action.get('type')=='resume_exam_simulation' and action.get('exam_run_id')==run['run_id']:
            gate = {'control':'exam.resume','targets':{'exam_run_id':run['run_id']}}
    if (not gate or gate['control'] != 'exam.resume' or gate['targets'].get('exam_run_id') != run['run_id']
            or bound != control_snapshot(c,current) or latest['arrival_id'] != arrival['arrival_id']
            or not current['ready_at'] or not current['ready_at'] <= arrival['created_at'] <= _now()):
        _error('EXAM_LISTENING_START_GRANT_STALE')


def prompt_source(c, prompt):
    if exam_owner(prompt) and VERSION in json.loads(prompt['request_json']):
        run,slot,_,_ = frozen(c,prompt['prompt_id'])
        if (prompt['child_id'],prompt['origin_session_id'],prompt['diagnostic_item_id']) != (
                run['child_id'],run['session_id'],slot['diagnostic_item_id']):
            _error('EXAM_MEDIA_OWNER_CONFLICT')


def playback_permission(c, row, *, starting=False, state=None):
    prompt = c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?',(row['prompt_id'],)).fetchone()
    if not prompt or not exam_owner(prompt):
        return None
    run,slot,source,unit = frozen(c,prompt['prompt_id'])
    _run(c,run['run_id'],active=True)
    current = c.execute('SELECT * FROM specialized_exam_item WHERE run_id=? AND position=?',
        (run['run_id'],run['current_position'])).fetchone()
    member = c.execute('SELECT unit_key FROM exam_listening_delivery_slot WHERE run_id=? AND position=?',
        (run['run_id'],run['current_position'])).fetchone()
    from .exam_simulation import ExamSimulationService
    if not current or not member or member['unit_key'] != source['unit_key'] or unit['prompt_id'] != prompt['prompt_id']:
        _error('EXAM_MEDIA_UNIT_NOT_CURRENT')
    ExamSimulationService._open_slot(c,run,current)
    from .pet_listening_admission import resolve_pet_listening_resource
    from .pet_listening_reuse import listening_source_review
    resource = resolve_pet_listening_resource(c,run['session_id'],run['child_id'],unit['prompt_id'],source['item_version'])
    if resource['status']!='ready' or resource['resource']['media_id']!=row['media_id']:
        _error('EXAM_MEDIA_RESOURCE_NOT_READY')
    review = listening_source_review(c,row)
    lease = dict(version=VERSION,run_id=run['run_id'],unit_key=source['unit_key'],
        control_revision=run['control_revision'],source_version=row['source_version'],
        audio_sha256=json.loads(row['audio_json'])['sha256'],approval_event_id=review['event_id'],
        requirement_version=source['requirement']['version'])
    if not starting and state is not None and state.get('exam_lease') != lease:
        _error('EXAM_MEDIA_LEASE_STALE')
    return lease


def invalidate(c, run_id, *, terminal=False, preserve_pending=False):
    c.execute('UPDATE specialized_exam_run SET control_revision=control_revision+1 WHERE run_id=?',(run_id,))
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='pet_media_binding'").fetchone():
        return
    for row in c.execute('''SELECT m.* FROM pet_media_binding m JOIN exam_listening_delivery_unit u
        ON u.prompt_id=m.prompt_id WHERE u.run_id=? AND m.kind='listening' ''',(run_id,)).fetchall():
        state = json.loads(row['state_json'])
        pending = state.get('pending_end')
        lease = state.get('exam_lease')
        if preserve_pending and state.get('status')=='pending_receipt' and pending and lease:
            revision = c.execute('SELECT control_revision FROM specialized_exam_run WHERE run_id=?',(run_id,)).fetchone()[0]
            state.update(revision=state['revision']+1,runtime_epoch=None,
                exam_pending_recovery=dict(control_revision=revision,
                    client_action_id=pending['request']['client_action_id'],lease=lease))
        else:
            # Cancelling remaining work preserves accepted original playback and
            # saved answers. Run ownership, CAS and the erased lease stop new audio.
            state.update(generation=state['generation']+1,revision=state['revision']+1,runtime_epoch=None,
                status='paused',started=None,pending_end=None,frame_position=0)
            state.pop('exam_pending_recovery',None)
        state.pop('exam_lease',None)
        c.execute('UPDATE pet_media_binding SET state_json=? WHERE media_id=?',(_json(state),row['media_id']))


def pending_receipt_permission(c, row, state, event):
    """Only an exact already-persisted ended request can settle after restart."""
    recovery = state.get('exam_pending_recovery')
    if not recovery or recovery.get('client_action_id') != event['client_action_id']:
        return False
    run,slot,source,unit = frozen(c,row['prompt_id'])
    current = c.execute('SELECT unit_key FROM exam_listening_delivery_slot WHERE run_id=? AND position=?',
        (run['run_id'],run['current_position'])).fetchone()
    session = c.execute('''SELECT s.status,cp.status AS child_status FROM daily_session s
        JOIN child_profile cp ON cp.child_id=s.child_id WHERE s.session_id=? AND s.child_id=?''',
        (run['session_id'],run['child_id'])).fetchone()
    if (run['status']!='paused' or run['delivery_phase']!='paused' or not session
            or session['status']!='active' or session['child_status']!='active'
            or recovery.get('control_revision')!=run['control_revision'] or not current
            or current['unit_key']!=unit['unit_key'] or unit['prompt_id']!=row['prompt_id']
            or event['operation']!='playback' or state['status'] not in {'pending_receipt','playback_finished'}):
        return False
    from .pet_listening_admission import resolve_pet_listening_resource
    from .pet_listening_reuse import listening_source_review
    resource=resolve_pet_listening_resource(c,run['session_id'],run['child_id'],row['prompt_id'],source['item_version'])
    review=listening_source_review(c,row)
    lease=recovery.get('lease') or {}
    payload=json.loads(event['payload_json'])
    pending=state.get('pending_end')
    return bool(resource['status']=='ready' and resource['resource']['media_id']==row['media_id'] and review
        and lease.get('run_id')==run['run_id'] and lease.get('unit_key')==unit['unit_key']
        and lease.get('source_version')==row['source_version']
        and lease.get('audio_sha256')==json.loads(row['audio_json'])['sha256']
        and lease.get('approval_event_id')==review['event_id']
        and lease.get('requirement_version')==source['requirement']['version']
        and payload.get('event')=='ended' and payload.get('receipt_state')=='pending'
        and (not pending or pending['request']['client_action_id']==event['client_action_id']))


def scoring_scope(c, run, slot, *, answer=None, turn_id=None, historical=False):
    from .database import local_media_scope
    from .pet_listening_scope import delivered_before
    from .pet_media_evidence import _date
    prompt_id = f"exam-media:{run['run_id']}:{slot['position']}"
    _,_,source,unit = frozen(c,prompt_id)
    prompt = c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?',(prompt_id,)).fetchone()
    canonical = c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?',(unit['prompt_id'],)).fetchone()
    scope = local_media_scope(c)
    if not scope:
        _error('EXAM_MEDIA_RUNTIME_UNAVAILABLE')
    arrival = original_arrival(c,turn_id or slot['authorized_turn_id'],run,slot,answer or slot['answer_text'])
    cutoff = arrival['created_at']
    if not historical and not slot['answer_action_id']:
        captured = next((r['record'] for r in json.loads(arrival['snapshot_json']) if r.get('kind')=='exam_listening_control'),None)
        if captured != control_snapshot(c,run):
            _error('EXAM_LISTENING_ANSWER_LEASE_STALE')
    if not run['started_at'] <= cutoff <= _now():
        _error('EXAM_LISTENING_ARRIVAL_TIME_CONFLICT')
    if slot['answer_arrived_at'] and cutoff != slot['answer_arrived_at']:
        _error('EXAM_LISTENING_ARRIVAL_TIME_CONFLICT')
    if answer is not None and slot['answer_text'] and slot['answer_text'] != answer:
        _error('EXAM_LISTENING_ORIGINAL_ANSWER_CONFLICT')
    delivered = delivered_before(c,dict(canonical),source['requirement'],cutoff=_date(cutoff),**scope)
    if not delivered:
        _error('EXAM_LISTENING_ORIGINAL_PLAYBACK_BEFORE_ANSWER_REQUIRED')
    return dict(scope,session_id=run['session_id'],prompt_id=prompt_id,item_version=prompt['item_version'])


def mirror_answer(c, run, slot, answer, action, turn_id):
    scope = scoring_scope(c,run,slot,answer=answer,turn_id=turn_id)
    arrival = original_arrival(c,turn_id,run,slot,answer)
    c.execute('''UPDATE diagnostic_prompt SET status='answer_saved',answer_json=?,answer_action_id=?,
        authorized_turn_id=?,updated_at=? WHERE prompt_id=? AND status='waiting_answer' ''',
        (_json({'answer':answer,'hint_level':0}),'exam-media-answer:'+action,turn_id,arrival['created_at'],scope['prompt_id']))
    c.execute('UPDATE specialized_exam_item SET answer_arrived_at=? WHERE run_id=? AND position=?',
        (arrival['created_at'],run['run_id'],slot['position']))
    return scope


def attach_attempt(c, run, slot, attempt):
    c.execute("UPDATE diagnostic_prompt SET status='answered',attempt_id=? WHERE prompt_id=? AND status='answer_saved'",
        (attempt['attempt_id'],f"exam-media:{run['run_id']}:{slot['position']}"))


def public_delivery(c, run, slot):
    if not slot:
        return None
    delivery = c.execute('SELECT * FROM exam_listening_delivery_slot WHERE run_id=? AND position=?',
        (run['run_id'],slot['position'])).fetchone()
    if not delivery:
        return None
    unit = c.execute('SELECT * FROM exam_listening_delivery_unit WHERE run_id=? AND unit_key=?',
        (run['run_id'],delivery['unit_key'])).fetchone()
    canonical = c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?',(unit['prompt_id'],)).fetchone()
    resource = None
    try:
        from .pet_listening_admission import resolve_pet_listening_resource
        from .database import local_media_scope
        from .pet_media_evidence import project_pet_media_evidence
        admitted = resolve_pet_listening_resource(c,run['session_id'],run['child_id'],unit['prompt_id'],canonical['item_version'])
        if admitted['status']=='ready':
            media = project_pet_media_evidence(c,run['session_id'],media_id=admitted['resource']['media_id'],**local_media_scope(c))
            resource = dict(admitted['resource'],generation=media['generation'],revision=media['revision'],
                status=media['status'],completed_play_count=media['qualification']['task_complete_play_count'])
    except (ValueError,TypeError,KeyError):
        pass
    from .diagnostic_prompt import _ITEM_FIELDS
    saved = json.loads(canonical['item_json'])
    public_listening_item(saved)
    saved['stimulus_text'] = None
    saved['stimulus_asset'] = None
    saved['pet_audio_modality'] = 'listening'
    media_prompt = dict(prompt_id=canonical['prompt_id'],item_version=canonical['item_version'],
        child_id=canonical['child_id'],origin_session_id=canonical['origin_session_id'],
        diagnostic_item_id=canonical['diagnostic_item_id'],owner_kind='exam',item=saved,pet_audio_modality='listening')
    return dict(version=VERSION,run_id=run['run_id'],position=slot['position'],unit_key=unit['unit_key'],
        child_id=run['child_id'],session_id=run['session_id'],item_id=slot['diagnostic_item_id'],
        prompt_id=unit['prompt_id'],item_version=canonical['item_version'],control_revision=run['control_revision'],
        playback_allowed=run['status']=='active' and resource is not None,resource=resource,media_prompt=media_prompt,
        media_item_id=canonical['diagnostic_item_id'],
        local_only=True,cloud_transmission_allowed=False)


def public_listening_item(item):
    """Keep legacy text-containing questions from publishing the original script."""
    script = (item.get('stimulus_text') or '').strip()
    if script and script.casefold() in (item.get('prompt') or '').casefold():
        import re
        item['prompt'] = re.sub(re.escape(script),'【请听原音】',item['prompt'],flags=re.IGNORECASE)
    item['stimulus_text'] = None
    item['stimulus_asset'] = None
    return item
