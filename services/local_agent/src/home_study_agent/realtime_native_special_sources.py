"""Native concrete choices bind existing local sources; no semantic reclassification.

The witness proves only which current native utterance requested a bounded tool.
It is neither a ModelGateway response nor an assessment or independent-evidence grant.
Existing source/media/assessment writers retain their own transaction validators.
"""
import json
import re
from types import SimpleNamespace

from .agent_runtime import InvalidAgentPlanError
from .privacy_operation_receipt import generation

KEY = 'native_special_source_v1'
CAPABILITIES = frozenset(('native_writing.submission_status', 'native_writing.submit_retained',
    'oral.retry', 'oral.independent_check', 'writing.read_original', 'writing.confirm_original',
    'writing.revise_original', 'writing.independent_check', 'pet.listening_control'))
FIELDS = ('request_quote', 'command', 'native_writing_target', 'oral_followup_binding',
          'writing_ocr_confirmation', 'writing_ocr_binding', 'writing_followup_binding',
          'writing_revision_binding', 'pet_listening_binding')


def fail(reason):
    raise InvalidAgentPlanError('NATIVE_SPECIAL_' + reason)


def _hash(value):
    from .realtime_capability_tools import _hash as digest
    return digest(value)


def _capture(c, voice, runtime, *, current=True):
    from .realtime_native_context import _native_witness
    from .realtime_capability_tools import _immutable_capture
    service = SimpleNamespace(page_source_store=getattr(runtime, 'page_source_store', None))
    capture = _native_witness(service, c, voice)
    if capture is None:
        fail('NATIVE_INPUT_REQUIRED')
    if current:
        owner = c.execute('''SELECT d.status,p.status AS child_status FROM daily_session d
            JOIN child_profile p ON p.child_id=d.child_id WHERE d.session_id=? AND d.child_id=?''',
            (voice['session_id'], voice['child_id'])).fetchone()
        consent = c.execute('SELECT cloud_audio_allowed,cloud_model_text_allowed FROM privacy_preference WHERE singleton_id=1').fetchone()
        if not owner or owner['status'] != 'active' or owner['child_status'] != 'active':
            fail('OWNER_CHANGED')
        if not consent or any(type(consent[k]) is not int or consent[k] != 1
                for k in ('cloud_audio_allowed', 'cloud_model_text_allowed')):
            fail('CONSENT_REQUIRED')
    return capture, _hash(_immutable_capture(capture))


def source_witness(c, turn, action, manifest, capability=None, *, current=True):
    """Validate the separate native witness, including during original persist.

    A pre-persist manifest is supplied only by the native adapter after its Main
    request authorization. Public generic turn APIs cannot supply this manifest.
    Later callers read that same manifest from the saved plan.
    """
    value = manifest.get(KEY)
    if value is None:
        return None
    from .agent_runtime import AgentRuntimeStore
    from .realtime_capability_tools import KEY as PACKET, SOURCE
    if (type(value) is not dict or set(value) != {'version','capability','voice_input_id','storage_generation','transcript_sha256','capture_sha256','arguments_sha256','action_fields_sha256'}
            or type(value.get('version')) is not int or value.get('version') != 1
            or value.get('capability') not in CAPABILITIES
            or capability is not None and capability != value['capability']
            or action.get('source') != SOURCE or action.get(KEY) != _hash(value)
            or action.get('local_only') is not True or action.get('cloud_transmission_allowed') is not False
            or action.get('mastery_update_allowed') is not False or action.get('independent_evidence') is not False):
        fail('WITNESS_CHANGED')
    packet = manifest.get(PACKET)
    if (type(packet) is not dict or packet.get('capability') != value['capability']
            or packet.get('identity', {}).get('voice_input_id') != value.get('voice_input_id')
            or packet.get('storage_generation') != value.get('storage_generation')
            or packet.get('original_transcript_sha256') != value.get('transcript_sha256')
            or packet.get('capture_hash') != value.get('capture_sha256')
            or packet.get('arguments_sha256') != value.get('arguments_sha256')):
        fail('PACKET_CHANGED')
    voice = c.execute('SELECT * FROM voice_input WHERE voice_input_id=? AND child_id=? AND session_id=?',
        (value.get('voice_input_id'), turn['child_id'], turn['session_id'])).fetchone()
    if not voice or voice['transcript'] != turn['transcript']:
        fail('VOICE_CHANGED')
    _, capture_hash = _capture(c, voice, AgentRuntimeStore.__new__(AgentRuntimeStore), current=current)
    if (value.get('storage_generation') != generation(c) or value.get('transcript_sha256') != _hash(turn['transcript'])
            or value.get('capture_sha256') != capture_hash or value.get('action_fields_sha256') != _hash({k: action[k] for k in FIELDS if k in action})
            or action.get('voice_input_id') != voice['voice_input_id']):
        fail('SOURCE_CHANGED')
    return value


def validate(c, turn_id, *, completion=False):
    row = c.execute('''SELECT t.*,p.context_manifest_json,s.capability_name,s.arguments_json
        FROM agent_turn t JOIN agent_plan p USING(agent_turn_id) JOIN agent_step s USING(plan_id)
        WHERE t.agent_turn_id=? AND s.step_index=0 AND p.child_id=t.child_id AND p.session_id=t.session_id''', (turn_id,)).fetchone()
    if row is None:
        return None
    manifest = json.loads(row['context_manifest_json'])
    if KEY not in manifest:
        return None
    action = json.loads(row['action_json'])
    value = source_witness(c, row, action, manifest, row['capability_name'], current=not completion)
    if value['arguments_sha256'] != _hash(json.loads(row['arguments_json'])):
        fail('ARGUMENTS_CHANGED')
    if not completion:
        from .realtime_native_client import require_claim
        require_claim(c, turn_id)
    return True


def semantic_version(c, turn_id, *, capability=None, completion=False):
    """A distinct native-choice version, never a legacy classifier receipt."""
    row = c.execute('''SELECT t.*,p.context_manifest_json,s.capability_name,s.arguments_json
        FROM agent_turn t JOIN agent_plan p USING(agent_turn_id) JOIN agent_step s USING(plan_id)
        WHERE t.agent_turn_id=? AND s.step_index=0''', (turn_id,)).fetchone()
    if row is None or KEY not in json.loads(row['context_manifest_json']):
        return None
    manifest = json.loads(row['context_manifest_json'])
    value = source_witness(c, row, json.loads(row['action_json']), manifest, capability or row['capability_name'], current=not completion)
    if value['arguments_sha256'] != _hash(json.loads(row['arguments_json'])):
        fail('ARGUMENTS_CHANGED')
    if not completion:
        from .realtime_native_client import require_claim
        require_claim(c, turn_id)
    return _hash({'kind': KEY, 'witness': value})


def _ready_confirmation(c, sid, binding, voice):
    from .writing_ocr_authorization import _confirmation
    from .writing_ocr_review import uncertain_confirmation
    row, source, _ = _confirmation(c, sid, binding['confirmation_id'], binding['binding_version'], live=True)
    if uncertain_confirmation(voice['transcript']) or re.search(
            r'[?？]|不是我|别人|他写|她写|不确定|不知道|看不清|改字|识别.*(?:(?<!没)有错|不对|错误)|需要修改|还要修改', voice['transcript']):
        fail('WRITING_CONFIRMATION_UNCERTAIN')
    # Completed actual readback must precede this utterance; a tool label does
    # not prove the child heard the original or that OCR is correct.
    event = c.execute("SELECT kind,created_at FROM writing_ocr_event WHERE confirmation_id=? AND kind IN ('started','ended') ORDER BY rowid DESC LIMIT 1", (row['confirmation_id'],)).fetchone()
    captured = c.execute('SELECT speech_started_at,created_at FROM voice_capture_binding WHERE voice_input_id=?', (voice['voice_input_id'],)).fetchone()
    from datetime import datetime
    if not event or event['kind'] != 'ended' or not captured or datetime.fromisoformat(event['created_at']) > datetime.fromisoformat(captured['speech_started_at'] or captured['created_at']):
        fail('WRITING_READBACK_REQUIRED')


def compose(c, session, voice, capture, capability, args, runtime, *, arrival_id=None, turn_id=None):
    if capability not in CAPABILITIES:
        return None
    if set(args) - ({'request_quote', 'command'} if capability == 'pet.listening_control' else {'request_quote'}):
        fail('ARGUMENTS_INVALID')
    if not c.in_transaction:
        fail('WRITE_TRANSACTION_REQUIRED')
    if (voice['session_id'], voice['child_id']) != (session['session_id'], session['child_id']):
        fail('OWNER_CHANGED')
    actual, capture_hash = _capture(c, voice, runtime)
    from .realtime_capability_tools import _immutable_capture
    if _hash(_immutable_capture(capture)) != capture_hash:
        fail('CAPTURE_CHANGED')
    if not arrival_id:
        fail('ARRIVAL_REQUIRED')
    arrival = c.execute('SELECT * FROM learning_input_arrival WHERE arrival_id=? AND session_id=? AND child_id=?',
        (arrival_id, session['session_id'], session['child_id'])).fetchone()
    if not arrival or arrival['utterance'] != voice['transcript']:
        fail('ARRIVAL_CHANGED')
    quote = voice['transcript']
    action = {'request_quote': quote}
    arguments = {'request_quote': quote}
    manifest = {'learning_input_arrival_id': arrival_id}
    # An explicit reference cannot be silently replaced. With no answer target,
    # these are controls of the unique local feedback/media, never new answers.
    source_capture = capture if runtime.captured_question(capture) else None
    if capability.startswith('native_writing.'):
        from .native_writing_control import snapshot, validate as validate_target
        target = snapshot(c, session['session_id'], session['child_id'])
        validate_target(c, session['session_id'], session['child_id'], target)
        action['native_writing_target'] = target
    elif capability == 'pet.listening_control':
        if set(args) - {'request_quote', 'command'} or args.get('command') not in {'pause','resume','restart','cancel'}:
            fail('PET_COMMAND_INVALID')
        frozen = runtime.capture_pet_listening_arrival(c, arrival_id)
        target = frozen.get('binding')
        if not target or frozen['status'] in {'cancelled','invalidated','pending_receipt','missing','ambiguous','unavailable'}:
            fail('PET_SOURCE_UNAVAILABLE')
        captured = runtime.captured_question(capture)
        if captured and captured != {'kind':'diagnostic','binding':{k:target[k] for k in ('prompt_id','item_version')}}:
            fail('PET_CAPTURE_TARGET_CHANGED')
        action.update(command=args['command'], pet_listening_binding=target)
        arguments['command'] = args['command']
        manifest['pet_listening_binding'] = target
    elif capability.startswith('oral.'):
        frozen = runtime.capture_oral_followup_arrival(c, arrival_id, source_capture)
        kind = 'retry' if capability == 'oral.retry' else 'independent_check'
        expected = ((frozen or {}).get('packets') or {}).get(kind) or {}
        if expected.get('status') != 'ready':
            fail('ORAL_SOURCE_UNAVAILABLE')
        packet = {k: expected[k] for k in ('action','binding_version')}
        if runtime.revalidate_oral_followup(c, **packet)['status'] != 'ready':
            fail('ORAL_SOURCE_CHANGED')
        action['oral_followup_binding'] = packet
        manifest['oral_native_arrival'] = frozen
    else:
        frozen = runtime.capture_writing_followup_arrival(c, arrival_id, source_capture)
        kind = 'revision' if capability == 'writing.revise_original' else 'independent_check'
        expected = ((frozen or {}).get('packets') or {}).get(kind) or {}
        if capability in {'writing.revise_original','writing.independent_check'} and expected.get('status') == 'ready':
            from .writing_teaching import WritingTeachingService
            packet = {k: expected[k] for k in ('action','binding_version')}
            if WritingTeachingService(None).revalidate_action(session['session_id'], **packet, connection=c)['status'] != 'ready':
                fail('WRITING_SOURCE_CHANGED')
            action['writing_followup_binding'] = packet
            manifest['writing_voice_binding'] = {'request_quote': quote, 'followup': packet}
        elif capability == 'writing.revise_original':
            reference = runtime.writing_reference_arrival(c, session['session_id'], session['child_id'])
            if not reference:
                fail('WRITING_REFERENCE_REQUIRED')
            runtime.validate_writing_reference_execution(c, reference['turn_id'])
            target = {k:reference['reference'][k] for k in ('prompt_id','item_version','source_version')}
            target['reference_turn_id'] = reference['turn_id']
            captured = runtime.captured_question(capture)
            if captured and captured != {'kind':'diagnostic','binding':{k:target[k] for k in ('prompt_id','item_version')}}:
                fail('WRITING_CAPTURE_TARGET_CHANGED')
            action['writing_revision_binding'] = target
            manifest['writing_voice_binding'] = {'request_quote':quote, 'revision':target}
        elif capability == 'writing.independent_check':
            fail('WRITING_INDEPENDENT_SOURCE_UNAVAILABLE')
        else:
            snapshot = runtime.writing_voice_snapshot(c, session['session_id'], session['child_id'], capture)
            if not snapshot:
                fail('WRITING_SOURCE_REQUIRED')
            binding = snapshot.get('confirmation')
            if capability == 'writing.read_original':
                if binding is None:
                    span = runtime.writing_response_span(snapshot, quote)
                    if span is None:
                        fail('WRITING_SCOPE_REQUIRED')
                    from .writing_ocr_authorization import WritingOcrAuthorizationService
                    page = snapshot['page']
                    value = WritingOcrAuthorizationService(runtime.database).prepare_confirmation(session['session_id'],
                        **snapshot['prompt'], artifact_id=page['artifact_id'], revision_id=page['revision_id'],
                        response_start=span[0], response_end=span[1], client_action_id='native-writing-read:'+arrival['client_action_id'], connection=c)
                    binding = {k:value[k] for k in ('confirmation_id','binding_version')}
                else:
                    binding = {k:binding[k] for k in ('confirmation_id','binding_version')}
                action['writing_ocr_confirmation'] = binding
            else:
                if binding is None:
                    fail('WRITING_CONFIRMATION_REQUIRED')
                _ready_confirmation(c, session['session_id'], binding, voice)
                action['writing_ocr_binding'] = binding
            manifest['writing_voice_binding'] = {'request_quote':quote, 'confirmation':binding}
    witness = {'version':1, 'capability':capability, 'voice_input_id':voice['voice_input_id'],
        'storage_generation':generation(c), 'transcript_sha256':_hash(quote), 'capture_sha256':capture_hash,
        'arguments_sha256':_hash(arguments), 'action_fields_sha256':_hash({k:action[k] for k in FIELDS if k in action})}
    manifest[KEY] = witness
    action[KEY] = _hash(witness)
    return {'arguments':arguments, 'action':action, 'manifest':manifest}


def control_receipt(c, turn_id, *, allow_planned=False):
    """Only retained native manuscript controls; no generic control bypass."""
    row = c.execute("""SELECT t.*,p.context_manifest_json,p.status AS plan_status,s.status AS step_status,
        s.capability_name FROM agent_turn t JOIN agent_plan p USING(agent_turn_id)
        JOIN agent_step s USING(plan_id) WHERE t.agent_turn_id=? AND s.step_index=0""", (turn_id,)).fetchone()
    if row is None or row['capability_name'] not in {'native_writing.submission_status','native_writing.submit_retained'}:
        return None
    manifest = json.loads(row['context_manifest_json'])
    if KEY not in manifest:
        return None
    action = json.loads(row['action_json'])
    source_witness(c, row, action, manifest, row['capability_name'])
    from .realtime_native_client import require_claim
    require_claim(c, turn_id, allow_planned=allow_planned)
    allowed = {'planned','running'} if allow_planned else {'running'}
    if row['plan_status'] not in allowed or row['step_status'] not in allowed:
        fail('COMMAND_NOT_EXECUTABLE')
    from .native_writing_control import validate as validate_target
    target = action.get('native_writing_target')
    validate_target(c, row['session_id'], row['child_id'], target)
    return {'control':row['capability_name'], 'targets':{'native_writing':target},
        'request_key':row['client_action_id'], 'source_kind':KEY}
