"""Fresh native-only conversation projection. Never the legacy local history.

The HTTP/native host must authorize its current connection for this session.
Historical canonical inputs and corresponding native replies are still unverified.
Only adapter-owned runtime execution facts may be restored; function calls are
never replayed. The separately authorized current formative mistake projection
contains only its selected question, original answer and answer-free practice.
"""
import json
import re
import sqlite3
from datetime import datetime, timezone
from .agent_runtime import AgentRuntimeStore
from .cloud_privacy import sanitize_cloud_text, sensitive_categories
from .realtime_capability_tools import VERSION,KEY,_hash,_fail,_immutable_capture
from .privacy_operation_receipt import generation


NATIVE_PURPOSE='native_cloud_dialogue'
INPUT_ACTOR='native_cloud_input_v1'


def claim_mistake_workspace_input(database, session_id, payload):
    """Mark a locally handled voice input so native reconnect cannot replay it."""
    kinds = ('capture', 'paper', 'voice', 'cancel', 'answer', 'topics', 'topic_explain')
    answer = payload.get('kind') == 'answer'
    fields = {'child_id', 'voice_input_id', 'kind'} | ({'after_ms', 'mistake_id', 'source_revision'} if answer else set())
    if (set(payload) != fields or payload['kind'] not in kinds
            or (answer and (type(payload['after_ms']) is not int or not 0 < payload['after_ms'] < 10**14))
            or not all(isinstance(payload[k], str) and re.fullmatch(r'[A-Za-z0-9_-]{1,160}', payload[k])
                       for k in (('child_id', 'voice_input_id', 'mistake_id') if answer else ('child_id', 'voice_input_id')))
            or (answer and (not isinstance(payload['source_revision'], str)
                            or not re.fullmatch(r'[a-f0-9]{64}', payload['source_revision'])))):
        raise ValueError('mistake workspace voice input invalid')
    child_id, voice_id = payload['child_id'], payload['voice_input_id']
    with database.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute('''SELECT v.* FROM voice_input v JOIN daily_session s USING(session_id)
            JOIN child_profile p ON p.child_id=v.child_id
            WHERE v.voice_input_id=? AND v.session_id=? AND v.child_id=? AND s.child_id=v.child_id
              AND v.status='completed' AND s.status='active' AND p.status='active' ''',
                        (voice_id, session_id, child_id)).fetchone()
        from .realtime_native_safety import native_input_blocked, RISKS
        from .child_safety import classify_child_safety
        if not row:
            raise ValueError('mistake workspace voice owner changed')
        risk = classify_child_safety(row['transcript'])
        if native_input_blocked(c, row) or (risk and risk[2].get('risk_level') in RISKS):
            return {'claimed': False, 'safety': True}
        scope = {'session_id': session_id, 'kind': payload['kind']}
        if answer:
            from .voice_capture_binding import capture_started_at
            from .mistake_learning import _source
            started = capture_started_at(c, voice_id)
            if not started or int(datetime.fromisoformat(started).timestamp() * 1000) < payload['after_ms']:
                return {'claimed': False, 'stale': True}
            mistake_id = payload['mistake_id']
            _, current_revision = _source(c, child_id, mistake_id)
            if current_revision != payload['source_revision']:
                return {'claimed': False, 'stale': True}
            scope.update(mistake_id=mistake_id, source_revision=current_revision)
        audit_id = 'mistake-workspace-voice-' + voice_id
        previous = c.execute('SELECT actor,summary_json FROM audit_event WHERE audit_id=?', (audit_id,)).fetchone()
        if previous and (previous['actor'] != 'mistake_workspace_voice_v1' or json.loads(previous['summary_json']) != scope):
            raise ValueError('mistake workspace voice claim changed')
        if previous:
            c.commit()
            return {'claimed': False, 'already_handled': True}
        c.execute('INSERT INTO audit_event VALUES(?,?,?,?,?,?)',
                  (audit_id, 'mistake_workspace_voice_v1', 'voice_input', child_id,
                   json.dumps(scope, separators=(',', ':')), datetime.now(timezone.utc).isoformat()))
        c.commit()
        return {'claimed': True}


def _native_capture(service,c,voice):
    if not voice or voice['status']!='completed' or voice['purpose']!=NATIVE_PURPOSE:return None
    from .realtime_native_safety import native_input_blocked
    if native_input_blocked(c,voice):return None
    capture=AgentRuntimeStore.resolve_captured_voice(c,session_id=voice['session_id'],child_id=voice['child_id'],
        voice_input_id=voice['voice_input_id'],transcript=voice['transcript'],page_source_store=service.page_source_store)
    if capture.get('source_kind')!='realtime_transcript' or capture.get('local_recovery_only') is True:return None
    if not capture.get('speech_started_at'):return None
    return capture


def record_native_input_witness(service,c,session_id,voice_input_id,*,expected_generation):
    """Internal trusted native-ingress completion only, never a model parameter.

    Root verifies Main's input-purpose HMAC before calling. expected_generation
    is read before begin/complete, so a restore cannot promote an old capture.
    """
    if not c.in_transaction or expected_generation!=generation(c):_fail('REALTIME_NATIVE_INPUT_GENERATION_CHANGED')
    voice=c.execute('SELECT * FROM voice_input WHERE voice_input_id=? AND session_id=?',(voice_input_id,session_id)).fetchone()
    capture=_native_capture(service,c,voice)
    if capture is None:_fail('REALTIME_NATIVE_INPUT_SOURCE_INVALID')
    owner=c.execute('''SELECT d.status,p.status AS child_status FROM daily_session d JOIN child_profile p ON p.child_id=d.child_id WHERE d.session_id=? AND d.child_id=?''',(session_id,voice['child_id'])).fetchone()
    consent=c.execute('SELECT cloud_audio_allowed,cloud_model_text_allowed FROM privacy_preference WHERE singleton_id=1').fetchone()
    if not owner or owner['status']!='active' or owner['child_status']!='active' or not consent or any(type(consent[k]) is not int or consent[k]!=1 for k in ('cloud_audio_allowed','cloud_model_text_allowed')):_fail('REALTIME_NATIVE_INPUT_CONSENT_REQUIRED')
    value={'version':VERSION,'voice_input_id':voice_input_id,'session_id':session_id,'child_id':voice['child_id'],
        'client_action_id':voice['client_action_id'],'storage_generation':generation(c),
        'transcript_sha256':_hash(voice['transcript']),'capture_sha256':_hash(_immutable_capture(capture))}
    aid='native-input-'+voice_input_id
    prior=c.execute('SELECT actor,summary_json FROM audit_event WHERE audit_id=?',(aid,)).fetchone()
    if prior and (prior['actor']!=INPUT_ACTOR or json.loads(prior['summary_json'])!=value):_fail('REALTIME_NATIVE_INPUT_WITNESS_CHANGED')
    c.execute('INSERT INTO audit_event VALUES(?,?,?,?,?,?) ON CONFLICT(audit_id) DO NOTHING',
        (aid,INPUT_ACTOR,'native_input_admitted',voice['child_id'],json.dumps(value,ensure_ascii=False,sort_keys=True),voice['created_at']))
    return value


def _native_witness(service,c,voice):
    if voice and voice['purpose']=='household_local_transcript':
        from .realtime_household_input import verify_household_witness
        if not verify_household_witness(c,voice):return None
        return AgentRuntimeStore.resolve_captured_voice(c,session_id=voice['session_id'],child_id=voice['child_id'],voice_input_id=voice['voice_input_id'],transcript=voice['transcript'],page_source_store=service.page_source_store)
    capture=_native_capture(service,c,voice)
    if capture is None:return None
    row=c.execute('SELECT actor,summary_json FROM audit_event WHERE audit_id=?',('native-input-'+voice['voice_input_id'],)).fetchone()
    if not row or row['actor']!=INPUT_ACTOR:return None
    expected={'version':VERSION,'voice_input_id':voice['voice_input_id'],'session_id':voice['session_id'],'child_id':voice['child_id'],
        'client_action_id':voice['client_action_id'],'storage_generation':generation(c),
        'transcript_sha256':_hash(voice['transcript']),'capture_sha256':_hash(_immutable_capture(capture))}
    return capture if json.loads(row['summary_json'])==expected else None


def load_native_context(service, session_id, *, authorize=None, max_turns=12):
    if type(max_turns) is not int or not 1<=max_turns<=24: _fail('REALTIME_NATIVE_CONTEXT_LIMIT_INVALID')
    with service.database.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        owner=c.execute('''SELECT d.*,p.status AS child_status FROM daily_session d
            JOIN child_profile p ON p.child_id=d.child_id WHERE d.session_id=?''', (session_id,)).fetchone()
        if not owner or owner['status']!='active' or owner['child_status']!='active': _fail('REALTIME_NATIVE_CONTEXT_OWNER_CHANGED')
        consent=c.execute('SELECT cloud_audio_allowed,cloud_model_text_allowed FROM privacy_preference WHERE singleton_id=1').fetchone()
        if not consent or any(type(consent[k]) is not int or consent[k]!=1 for k in ('cloud_audio_allowed','cloud_model_text_allowed')):
            _fail('REALTIME_NATIVE_CONTEXT_CONSENT_REQUIRED')
        try: allowed=authorize is not None and authorize(c,session_id,owner['child_id']) is True
        except Exception: allowed=False
        if not allowed: _fail('REALTIME_NATIVE_CONTEXT_HOST_UNCONFIRMED')
        names=tuple(r[0] for r in c.execute('SELECT nickname FROM child_profile WHERE status=?',('active',)))
        rows=c.execute('''SELECT * FROM agent_turn WHERE session_id=? AND child_id=?
            AND intent='qwen_native_dialogue_unverified' ORDER BY rowid DESC LIMIT ?''',
            (session_id,owner['child_id'],max_turns)).fetchall()
        events=[];seen=set()
        for row in reversed(rows):
            try:
                action=json.loads(row['action_json']);identity=action['native_identity']
                if (action.get('source')!='qwen_native_dialogue_unverified' or action.get('mastery_update_allowed') is not False
                    or action.get('independent_evidence') is not False or action.get('local_only') is not True
                    or action.get('cloud_transmission_allowed') is not False):continue
                body={'version':VERSION,**identity,'text':row['response_text'],'status':'unverified','terminal':action['terminal']}
                if action.get('input_source') is not None:body['input_source']=action['input_source']
                if action.get('dialogue_fingerprint')!=_hash(body):continue
                voice=c.execute('SELECT * FROM voice_input WHERE voice_input_id=? AND child_id=? AND session_id=?',
                    (identity['voice_input_id'],owner['child_id'],session_id)).fetchone()
                if not voice or voice['status']!='completed' or voice['transcript']!=row['transcript']:continue
                capture=_native_witness(service,c,voice)
                if capture is None:continue
                # Raw audio authorization does not turn family-sensitive prose
                # into a general historical text export; keep existing redaction.
                if sensitive_categories(voice['transcript']) or sensitive_categories(row['response_text']):continue
                user=sanitize_cloud_text(voice['transcript'],names).text
                assistant=sanitize_cloud_text(row['response_text'],names).text
                if identity['voice_input_id'] not in seen:
                    events.append((voice['created_at'],0,{'role':'user','text':user,'status':'unverified'}));seen.add(identity['voice_input_id'])
                events.append((row['created_at'],1,{'role':'assistant','text':assistant,'status':'unverified'}))
            except (ValueError,TypeError,KeyError):continue
        tools=c.execute('''SELECT t.*,s.status AS step_status,s.error_code AS step_error
            FROM agent_turn t JOIN agent_plan p USING(agent_turn_id) JOIN agent_step s USING(plan_id)
            WHERE t.session_id=? AND t.child_id=? AND json_extract(p.context_manifest_json,?) IS NOT NULL
            ORDER BY t.rowid DESC LIMIT ?''', (session_id,owner['child_id'],'$.'+KEY,max_turns)).fetchall()
        facts=[];pending=[]
        for row in reversed(tools):
            try:
                packet=service._packet(c,row)
                if not packet or packet['storage_generation']!=generation(c):continue
                voice=c.execute('SELECT * FROM voice_input WHERE voice_input_id=? AND child_id=? AND session_id=?',
                    (packet['identity']['voice_input_id'],owner['child_id'],session_id)).fetchone()
                if not voice or voice['status']!='completed' or _hash(voice['transcript'])!=packet['original_transcript_sha256']:continue
                capture=_native_witness(service,c,voice)
                if capture is None or _hash(_immutable_capture(capture))!=packet['capture_hash']:continue
                if packet['identity']['voice_input_id'] not in seen and not sensitive_categories(voice['transcript']):
                    events.append((voice['created_at'],0,{'role':'user','text':sanitize_cloud_text(voice['transcript'],names).text,'status':'unverified'}))
                    seen.add(packet['identity']['voice_input_id'])
                # A native-execution fact is a saved record, not a new dispatch.
                # Current permission/owner projection is recomputed by _receipt.
                value=service._receipt(c,row,replayed=True)
                if value['execution']['status']=='completed' and value['public_projection']['eligible']:
                    facts.append(value['public_result'])
                elif value['execution']['status'] in {'pending','unknown'}:
                    # Host-only recovery metadata. Never send these identities
                    # to the model or replay a function_call in a new session.
                    pending.append({'call_id':packet['identity']['call_id'],'action_id':row['client_action_id'],
                        'identity':{'version':VERSION,**packet['identity']},'status':value['execution']['status']})
            except (ValueError,TypeError,KeyError):continue
        # Only this native purpose + original admitted source/current-generation
        # witness can recover orphan input. A purpose string alone is no grant.
        orphan_rows=c.execute("""SELECT v.* FROM voice_input v WHERE v.session_id=? AND v.child_id=?
            AND v.status='completed' AND v.purpose IN (?,?) AND NOT EXISTS(
                SELECT 1 FROM audit_event a WHERE a.audit_id='mistake-workspace-voice-'||v.voice_input_id
                  AND a.actor='mistake_workspace_voice_v1' AND a.child_id=v.child_id)
            AND NOT EXISTS(
                SELECT 1 FROM agent_turn t WHERE t.session_id=v.session_id AND t.child_id=v.child_id
                  AND json_extract(t.action_json,'$.voice_input_id')=v.voice_input_id
                  AND (t.intent='qwen_native_dialogue_unverified' OR t.intent='qwen_realtime_capability'))
            ORDER BY v.rowid DESC LIMIT ?""",(session_id,owner['child_id'],NATIVE_PURPOSE,'household_local_transcript',max_turns)).fetchall()
        orphans=[]
        for voice in orphan_rows:
            try:
                if voice['voice_input_id'] in seen or _native_witness(service,c,voice) is None or sensitive_categories(voice['transcript']):continue
                text=sanitize_cloud_text(voice['transcript'],names).text
                event={'role':'user','text':text,'status':'unverified'}
                events.append((voice['created_at'],0,event));seen.add(voice['voice_input_id'])
                orphans.append((event,{'voice_input_id':voice['voice_input_id'],'client_action_id':voice['client_action_id'],
                    'created_at':voice['created_at'],'text':text,**({'source_kind':'household_local_transcript'} if voice['purpose']=='household_local_transcript' else {})}))
            except (ValueError,TypeError,KeyError):continue
        selected=[];budget=12000
        # Reserve the bounded window for the newest context first, then restore
        # chronological message order. Old turns cannot crowd out “刚才那个”.
        for event in sorted(events,key=lambda x:(x[0],x[1]),reverse=True):
            if len(selected)>=24:break
            if len(event[2]['text'])>budget:continue
            selected.append(event);budget-=len(event[2]['text'])
        dialogue=[event[2] for event in sorted(selected,key=lambda x:(x[0],x[1]))]
        selected_ids={id(event) for event in dialogue}
        pending_inputs=[value for event,value in orphans if id(event) in selected_ids]
        pending_inputs.sort(key=lambda x:x['created_at'])
        result={'version':VERSION,'recent_dialogue':dialogue,'runtime_facts':{'version':VERSION,'items':facts,'current_session_state':owner['state']},'pending_calls':pending,'pending_inputs':pending_inputs,
                'pending_completions':service.pending_completions(c,session_id,owner['child_id'])}
        from .realtime_native_mistake_learning import context_projection
        current_graph_version = None
        graph_version_reader = getattr(service, 'current_graph_version', None)
        if graph_version_reader:
            try:
                current_graph_version = graph_version_reader(connection=c)
            except (OSError, sqlite3.Error, ValueError):
                pass
        mistake=context_projection(c,session_id,names=names,current_graph_version=current_graph_version)
        if mistake is not None:result['runtime_facts']['mistake_learning']=mistake
        from .realtime_native_mistake_learning import review_context_projection
        due=review_context_projection(c,session_id,names=names,current_graph_version=current_graph_version or '')
        if due is not None:result['runtime_facts']['mistake_review_today']=due
        result['revision']=_hash(result)
        c.commit();return result
