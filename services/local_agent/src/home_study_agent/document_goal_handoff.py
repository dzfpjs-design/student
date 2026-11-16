"""Purpose-local handoff from a requested capture to its confirmed lesson."""
from datetime import datetime, timezone
import hashlib
import json
from uuid import uuid4

KEY = 'document_goal_handoff_v1'
LOCAL = dict(local_only=True, cloud_transmission_allowed=False, mastery_update_allowed=False)


class DocumentGoalError(ValueError):
    pass


def require(value, reason):
    if not value:
        raise DocumentGoalError(reason)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':')).encode()).hexdigest()


def instant(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    require(parsed.tzinfo is not None, 'DOCUMENT_GOAL_TIME_INVALID')
    return parsed


def root_row(c, root, session=None):
    row = c.execute('''SELECT t.agent_turn_id,t.child_id,t.session_id,t.transcript,t.action_json,
        t.tool_status,t.tool_error_code,t.created_at,p.plan_id,p.objective_id,p.plan_json,
        p.context_manifest_json,p.status AS plan_status,s.step_id,s.capability_name,s.result_json,
        s.status AS step_status,s.error_code FROM agent_turn t JOIN agent_plan p USING(agent_turn_id)
        JOIN agent_step s USING(plan_id) WHERE t.agent_turn_id=? AND s.step_index=0
        AND p.child_id=t.child_id AND p.session_id=t.session_id''', (root,)).fetchone()
    require(row and (session is None or row['session_id'] == session), 'DOCUMENT_GOAL_OWNER_CHANGED')
    return dict(row)


def root_fingerprint(row):
    return digest({key: row[key] for key in ('agent_turn_id', 'child_id', 'session_id', 'transcript',
        'action_json', 'plan_id', 'objective_id', 'plan_json', 'step_id', 'capability_name', 'result_json')})


def artifact(c, row):
    from .agent_runtime import AgentRuntimeStore
    result = json.loads(row['result_json'])
    AgentRuntimeStore.validate_device_receipt(c, row['agent_turn_id'], result)
    receipt = result.get('execution_receipt') or result.get('client_bridge_receipt')
    output = receipt['output']
    identity = output.get('capture_artifact_id') or output.get('artifact_id')
    source = c.execute('SELECT * FROM capture_artifact WHERE artifact_id=?', (identity,)).fetchone()
    require(source and source['child_id'] == row['child_id'] and source['session_id'] == row['session_id'],
        'DOCUMENT_GOAL_SOURCE_CHANGED')
    source = dict(source)
    source_hash = digest({key: source.get(key) for key in ('artifact_id', 'child_id', 'session_id',
        'artifact_type', 'status', 'extracted_text', 'details_json', 'confidence', 'created_at')})
    return source, source_hash


def store(c, row, state):
    c.execute("UPDATE agent_plan SET context_manifest_json=json_set(context_manifest_json,?,json(?)) WHERE plan_id=?",
        ('$.' + KEY, json.dumps(state, ensure_ascii=False), row['plan_id']))


def enqueue(c, turn_id):
    candidate = c.execute('''SELECT 1 FROM agent_plan p JOIN agent_step s USING(plan_id)
        WHERE p.agent_turn_id=? AND s.step_index=0 AND s.capability_name IN
        ('learning.capture_question','document.capture_question','document.retry_capture','document.monitor_control')''', (turn_id,)).fetchone()
    if not candidate:
        return False
    row = root_row(c, turn_id)
    if row['capability_name'] == 'document.monitor_control':
        action = json.loads(row['action_json'])
        if action.get('type') != 'control_document_monitor' or action.get('command') not in {'inspect', 'review'}:
            return False
    plan = json.loads(row['plan_json'])
    scope = plan.get('continuation_scope') or []
    explanations = [item for item in scope if item.get('capability') == 'learning.explain']
    if plan.get('after_execution') != 'replan' or len(explanations) != 1:
        return False
    require(row['tool_status'] == row['step_status'] == row['plan_status'] == 'completed'
        and not row['tool_error_code'] and not row['error_code'], 'DOCUMENT_GOAL_CAPTURE_UNFINISHED')
    require(all(isinstance(item.get('request_quote'), str) and item['request_quote']
        and item['request_quote'] in row['transcript'] for item in scope), 'DOCUMENT_GOAL_SCOPE_CHANGED')
    existing = json.loads(row['context_manifest_json']).get(KEY)
    if existing:
        require(existing['root_fingerprint'] == root_fingerprint(row), 'DOCUMENT_GOAL_ROOT_CHANGED')
        return True
    source, source_hash = artifact(c, row)
    from .voice_scope import text_consent_version
    now = datetime.now(timezone.utc).isoformat()
    state = dict(root_fingerprint=root_fingerprint(row), artifact_id=source['artifact_id'],
        artifact_hash=source_hash, phase='awaiting_confirmation', revision=1, created_at=now,
        consent_version=text_consent_version(c))
    store(c, row, state)
    c.execute("UPDATE agent_continuation SET status='blocked',reason='DOCUMENT_CONFIRMATION_REQUIRED' WHERE parent_turn_id=?",
        (turn_id,))
    return True


def validate(c, root, session, *, revision=None, incoming_action_id=None):
    row = root_row(c, root, session)
    manifest = json.loads(row['context_manifest_json'])
    require(isinstance(manifest, dict), 'DOCUMENT_GOAL_NOT_FOUND')
    state = manifest.get(KEY)
    require(isinstance(state, dict), 'DOCUMENT_GOAL_NOT_FOUND')
    require(revision is None or revision == state['revision'], 'DOCUMENT_GOAL_VERSION_CHANGED')
    require(state['phase'] != 'cancelled', 'DOCUMENT_GOAL_CANCELLED')
    require(state['root_fingerprint'] == root_fingerprint(row)
        and row['tool_status'] == row['step_status'] == row['plan_status'] == 'completed'
        and not row['tool_error_code'] and not row['error_code'], 'DOCUMENT_GOAL_ROOT_CHANGED')
    owner = c.execute('''SELECT s.status,s.state,cp.status AS child_status,o.status AS objective_status,
        o.last_agent_turn_id FROM daily_session s JOIN child_profile cp USING(child_id)
        JOIN agent_objective o ON o.objective_id=? AND o.child_id=s.child_id AND o.session_id=s.session_id
        WHERE s.session_id=? AND s.child_id=?''', (row['objective_id'], session, row['child_id'])).fetchone()
    require(owner and owner['status'] == owner['child_status'] == owner['objective_status'] == 'active'
        and owner['state'] not in {'completed', 'evolving'}, 'DOCUMENT_GOAL_NOT_CURRENT')
    accepted_turns = {root, state.get('teaching_turn_id'), state.get('time_turn_id')}
    voice_confirmation = state.get('confirmation_voice')
    if voice_confirmation:
        accepted_turns.add(voice_confirmation['turn_id'])
        validate_confirmation_voice(c, row, voice_confirmation)
    if state.get('review_correction'):
        validate_confirmation_voice(c, row, state['review_correction'])
    time_voice = state.get('time_voice')
    if time_voice:
        from .document_review_schedule import validate_voice
        validate_voice(c, row, time_voice)
    require(owner['last_agent_turn_id'] in accepted_turns,
        'DOCUMENT_GOAL_NEWER_INPUT')
    current = c.execute('SELECT agent_turn_id FROM agent_turn WHERE session_id=? ORDER BY rowid DESC LIMIT 1',
        (session,)).fetchone()
    require(current and current[0] in accepted_turns,
        'DOCUMENT_GOAL_NEWER_INPUT')
    from .agent_runtime import AgentRuntimeStore
    action = json.loads(row['action_json'])
    if action.get('voice_input_id'):
        voice = AgentRuntimeStore.resolve_captured_voice(c, session_id=session, child_id=row['child_id'],
            voice_input_id=action['voice_input_id'], transcript=row['transcript'])
        require(AgentRuntimeStore.voice_capture_snapshot(voice) == action.get('voice_input_binding'),
            'DOCUMENT_GOAL_VOICE_CHANGED')
    from .voice_scope import scope_valid, text_consent_version
    root_action = c.execute('SELECT client_action_id FROM agent_turn WHERE agent_turn_id=?', (root,)).fetchone()[0]
    current_action = incoming_action_id or (time_voice['client_action_id'] if time_voice else
        voice_confirmation['client_action_id'] if voice_confirmation else root_action)
    require(scope_valid(c, session, row['child_id'], current_action, require_cloud=False),
        'DOCUMENT_GOAL_NOT_CURRENT')
    consent = text_consent_version(c)
    require((list(consent) if consent is not None else None) == state['consent_version'], 'DOCUMENT_GOAL_CONSENT_CHANGED')
    source, source_hash = artifact(c, row)
    require(source['artifact_id'] == state['artifact_id'] and source_hash == state['artifact_hash'],
        'DOCUMENT_GOAL_SOURCE_CHANGED')
    if state.get('confirmation_id'):
        confirmation(c, row, state['confirmation_id'], historical=state['phase'] in {'awaiting_time', 'taught', 'scheduled', 'time_proposal', 'review_cancelled'},
            expected_hash=state['confirmation_hash'])
    if state.get('tutor_turn_id'):
        require(state['teaching_fingerprint'] == teaching_fingerprint(c, state['teaching_turn_id']),
            'DOCUMENT_GOAL_TEACHING_CHANGED')
        tutor = c.execute('SELECT * FROM tutor_turn WHERE turn_id=?', (state['tutor_turn_id'],)).fetchone()
        require(tutor and tutor_proof(tutor) == state['tutor_hash'], 'DOCUMENT_GOAL_TUTOR_CHANGED')
    return row, state


def validate_confirmation_voice(c, root, proof):
    from .agent_runtime import AgentRuntimeStore
    turn = c.execute('SELECT * FROM agent_turn WHERE agent_turn_id=?', (proof['turn_id'],)).fetchone()
    require(turn and turn['child_id'] == root['child_id'] and turn['session_id'] == root['session_id']
        and turn['client_action_id'] == proof['client_action_id'] and turn['transcript'] == proof['transcript']
        and turn['tool_status'] in {'pending', 'running', 'completed'} and not turn['tool_error_code'],
        'DOCUMENT_GOAL_CONFIRMATION_VOICE_CHANGED')
    action = json.loads(turn['action_json'])
    require(isinstance(action, dict) and action.get('type') == proof['action_type']
        and action.get('document_goal_confirmation_root') == root['agent_turn_id']
        and action.get('voice_input_id') == proof['voice_input_id']
        and action.get('corrected_text') == proof.get('corrected_text'), 'DOCUMENT_GOAL_CONFIRMATION_VOICE_CHANGED')
    voice = AgentRuntimeStore.resolve_captured_voice(c, session_id=root['session_id'], child_id=root['child_id'],
        voice_input_id=proof['voice_input_id'], transcript=proof['transcript'])
    require(AgentRuntimeStore.voice_capture_snapshot(voice) == proof['binding'] == action.get('voice_input_binding'),
        'DOCUMENT_GOAL_CONFIRMATION_VOICE_CHANGED')
    return turn


def bind_confirmation_turn(c, session, child, turn_id, key, transcript, action):
    if action.get('type') not in {'confirm_ocr_text', 'correct_ocr_text'} or not action.get('voice_input_id'):
        return
    candidate = c.execute('SELECT root_turn_id FROM agent_continuation WHERE session_id=? AND child_id=? ORDER BY rowid DESC LIMIT 1',
        (session, child)).fetchone()
    if not candidate or not owns(c, candidate[0]):
        return
    try:
        row, state = validate(c, candidate[0], session, incoming_action_id=key)
        require(state['phase'] == 'awaiting_confirmation',
            'DOCUMENT_GOAL_NOT_AWAITING_CONFIRMATION')
    except (ValueError, TypeError, KeyError):
        action['document_goal_confirmation_blocked'] = True
        return
    if state.get('confirmation_voice'):
        state['revision'] += 1
    # Bind before INSERT in the same transaction, while the capture is still the latest turn.
    action['document_goal_confirmation_root'] = row['agent_turn_id']
    action['document_goal_confirmation_revision'] = state['revision']
    state['confirmation_voice'] = dict(turn_id=turn_id, client_action_id=key, transcript=transcript,
        action_type=action['type'], corrected_text=action.get('corrected_text'),
        voice_input_id=action['voice_input_id'], binding=action['voice_input_binding'])
    if action['type'] == 'correct_ocr_text':
        state['review_correction'] = dict(state['confirmation_voice'])
    store(c, row, state)


def confirmation(c, row, confirmation_id, *, historical=False, expected_hash=None):
    confirmed = c.execute('SELECT * FROM tutor_ocr_confirmation WHERE confirmation_id=?', (confirmation_id,)).fetchone()
    state = json.loads(row['context_manifest_json'])[KEY]
    require(confirmed and confirmed['artifact_id'] == state['artifact_id']
        and confirmed['child_id'] == row['child_id'] and confirmed['session_id'] == row['session_id']
        and not confirmed['revoked_at'], 'DOCUMENT_GOAL_CONFIRMATION_CHANGED')
    confirmed = dict(confirmed)
    from .tutor_ocr_confirmation import normalize_confirmation_text
    text = normalize_confirmation_text(confirmed['confirmed_text'])
    require(hashlib.sha256(text.encode()).hexdigest() == confirmed['confirmed_text_hash']
        and instant(confirmed['confirmed_at']) >= instant(row['created_at']), 'DOCUMENT_GOAL_CONFIRMATION_CHANGED')
    require(historical or instant(confirmed['expires_at']) > datetime.now(timezone.utc), 'DOCUMENT_GOAL_CONFIRMATION_EXPIRED')
    require(expected_hash is None or digest(confirmed) == expected_hash, 'DOCUMENT_GOAL_CONFIRMATION_CHANGED')
    if state.get('confirmation_voice'):
        proof = state['confirmation_voice']
        turn = validate_confirmation_voice(c, row, proof)
        require(instant(confirmed['confirmed_at']) >= instant(turn['created_at']), 'DOCUMENT_GOAL_CONFIRMATION_PRECEDES_VOICE')
        if proof['action_type'] == 'correct_ocr_text':
            require(text == normalize_confirmation_text(proof.get('corrected_text') or '')
                and confirmed['confirmation_kind'] == 'manual_correction', 'DOCUMENT_GOAL_CONFIRMATION_CHANGED')
        else:
            corrected = state.get('review_correction')
            if corrected:
                validate_confirmation_voice(c, row, corrected)
                require(text == normalize_confirmation_text(corrected.get('corrected_text') or '')
                    and confirmed['confirmation_kind'] == 'manual_correction', 'DOCUMENT_GOAL_CONFIRMATION_CHANGED')
            else:
                source, _ = artifact(c, row)
                expected = pending_review_value(source, state)['text']
                require(text == normalize_confirmation_text(expected)
                    and confirmed['confirmation_kind'] in {'ocr_verified', 'manual_correction'},
                    'DOCUMENT_GOAL_CONFIRMATION_CHANGED')
    return confirmed


def pending_review_value(source, state):
    details = json.loads(source['details_json'] or '{}')
    require(isinstance(details, dict), 'DOCUMENT_GOAL_SOURCE_CHANGED')
    pipeline, quality = details.get('local_pipeline') or {}, details.get('quality') or {}
    require(isinstance(pipeline, dict) and isinstance(quality, dict), 'DOCUMENT_GOAL_SOURCE_CHANGED')
    candidates = pipeline.get('wrong_question_candidates') or []
    require(isinstance(candidates, list), 'DOCUMENT_GOAL_SOURCE_CHANGED')
    candidates = [item for item in candidates if isinstance(item, dict) and isinstance(item.get('text'), str)
        and item['text'].strip() and item.get('evidence') == 'red_correction_mark_near_question'
        and item.get('requires_child_confirmation') is True]
    marked = candidates[0] if len(candidates) == 1 else None
    corrected = state.get('review_correction')
    text = corrected['corrected_text'] if corrected else marked['text'] if marked else source['extracted_text']
    require(isinstance(text, str), 'DOCUMENT_GOAL_REVIEW_TEXT_UNAVAILABLE')
    text = text.strip()
    require(2 <= len(text) <= 4000, 'DOCUMENT_GOAL_REVIEW_TEXT_UNAVAILABLE')
    confidence = details.get('ocr_confidence', source['confidence'])
    clear = (type(confidence) in (int, float) and confidence >= .75
        and source['status'] == 'completed' and quality.get('passed') is True)
    if marked:
        clear = all(type(marked.get(key)) in (int, float) and marked[key] >= threshold
            for key, threshold in (('confidence', .72), ('ocr_confidence', .7)))
    return dict(artifactId=source['artifact_id'], text=text,
        status='needs_confirmation' if clear and not corrected and len(candidates) <= 1 else 'uncertain',
        createdAt=source['created_at'], source='marked_question' if marked else 'full_page',
        candidateCount=len(candidates))


def review(database, root, session):
    """Read only the original pending material; never recapture or promote a confirmation."""
    with database.connect() as c:
        c.execute('BEGIN')
        row, state = validate(c, root, session)
        require(state['phase'] == 'awaiting_confirmation', 'DOCUMENT_GOAL_NOT_AWAITING_CONFIRMATION')
        source, _ = artifact(c, row)
        return dict(root_turn_id=root, session_id=session, child_id=row['child_id'], revision=state['revision'],
            phase=state['phase'], review=pending_review_value(source, state), **LOCAL)


def owns(c, root):
    row = c.execute('SELECT context_manifest_json FROM agent_plan WHERE agent_turn_id=?', (root,)).fetchone()
    try:
        value = json.loads(row[0]) if row else None
    except (ValueError, TypeError):
        return False
    return isinstance(value, dict) and isinstance(value.get(KEY), dict)


def teaching_arguments(row, confirmed):
    scope = json.loads(row['plan_json'])['continuation_scope']
    candidates = [item for item in scope if item['capability'] == 'learning.explain']
    require(len(candidates) == 1, 'DOCUMENT_GOAL_SCOPE_CHANGED')
    original = candidates[0]['arguments']
    require(set(original) <= {'question', 'subject', 'support_level'}, 'DOCUMENT_GOAL_SCOPE_CHANGED')
    text = confirmed['confirmed_text']
    require(isinstance(original.get('subject'), str) and 0 < len(original['subject']) <= 40,
        'DOCUMENT_GOAL_SUBJECT_REQUIRED')
    require(original.get('question') in (None, text) and 2 <= len(text) <= 4000,
        'DOCUMENT_GOAL_QUESTION_MISMATCH')
    support = original.get('support_level', 1)
    require(type(support) is int and 1 <= support <= 6, 'DOCUMENT_GOAL_SCOPE_CHANGED')
    return dict(subject=original['subject'], question=text, support_level=support)


def prepare(database, runtime, root, session, confirmation_id, revision):
    from .agent_runtime import AgentPlanDecision, ObjectiveDraft, PlannedStep, action_from_plan
    with database.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        row, state = validate(c, root, session)
        if state.get('teaching_turn_id'):
            require(state.get('confirmation_id') == confirmation_id and revision == state['confirmation_revision'],
                'DOCUMENT_GOAL_VERSION_CHANGED')
            return snapshot(c, root, session)
        require(state['revision'] == revision and state['phase'] == 'awaiting_confirmation', 'DOCUMENT_GOAL_VERSION_CHANGED')
        confirmed = confirmation(c, row, confirmation_id)
        args = teaching_arguments(row, confirmed)
        active = c.execute("SELECT * FROM agent_objective WHERE session_id=? AND status='active' ORDER BY updated_at DESC LIMIT 1",
            (session,)).fetchone()
        require(active and active['objective_id'] == row['objective_id'] and active['objective_type'] == 'learning',
            'DOCUMENT_GOAL_OBJECTIVE_CHANGED')
        session_state = c.execute('SELECT state FROM daily_session WHERE session_id=?', (session,)).fetchone()[0]
        from .agent_runtime import CapabilityRegistry
        cap = CapabilityRegistry().require('learning.explain', session_state)
        response = '题干已核对，接着讲这道题；后续请求仍保留。'
        decision = AgentPlanDecision(ObjectiveDraft('continue', 'learning', None, (), 'explain'),
            PlannedStep(cap, args, 'Resolve only the original unknown question from its confirmed capture', cap.expected_evidence),
            response, True, 1)
        turn, key = str(uuid4()), 'document-goal:' + root + ':teach'
        action = action_from_plan(decision, '', dict(active))
        action.update(question=args['question'], source='document_goal_handoff', origin='confirmed_capture',
            root_turn_id=root, parent_turn_id=root, ocr_confirmation_id=confirmation_id,
            tutor_request_action_id='document-goal:' + root + ':tutor',
            document_goal_root_turn_id=root, document_goal_revision=revision+1, **LOCAL)
        now = datetime.now(timezone.utc).isoformat()
        c.execute('''INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,
            action_json,orchestrator_version,created_at,client_action_id)
            VALUES(?,?,?,'','document_goal_handoff',?,?,?,?,?)''',
            (turn, row['child_id'], session, response, json.dumps(action, ensure_ascii=False), KEY, now, key))
        action['agent_runtime'] = runtime.persist_plan(c, child_id=row['child_id'], session_id=session,
            agent_turn_id=turn, client_action_id=key, decision=decision,
            context_manifest={KEY: dict(root_turn_id=root, confirmation_id=confirmation_id,
                root_fingerprint=state['root_fingerprint'], confirmation_hash=digest(confirmed))})
        c.execute('UPDATE agent_turn SET action_json=? WHERE agent_turn_id=?', (json.dumps(action, ensure_ascii=False), turn))
        state.update(phase='ready', revision=revision+1, confirmation_revision=revision,
            confirmation_id=confirmation_id, confirmation_hash=digest(confirmed), teaching_turn_id=turn)
        state['teaching_fingerprint'] = teaching_fingerprint(c, turn)
        store(c, row, state)
        c.execute("UPDATE agent_continuation SET status='completed',next_turn_id=?,reason=NULL,updated_at=? WHERE parent_turn_id=?",
            (turn, now, root))
        value = snapshot(c, root, session)
        require(value['status'] == 'ready', value.get('reason'))
        c.commit()
        return value


def teaching_fingerprint(c, turn):
    row = c.execute('''SELECT t.agent_turn_id,t.child_id,t.session_id,t.transcript,t.action_json,t.client_action_id,
        p.objective_id,p.plan_json,p.context_manifest_json,s.capability_name,s.arguments_json,s.execution_location
        FROM agent_turn t JOIN agent_plan p USING(agent_turn_id) JOIN agent_step s USING(plan_id)
        WHERE t.agent_turn_id=? AND s.step_index=0''', (turn,)).fetchone()
    require(row, 'DOCUMENT_GOAL_TEACHING_CHANGED')
    return digest(dict(row))


def execution_valid(c, work, *, prepared=False, running=False, settled=False):
    try:
        row, state = validate(c, work['root_turn_id'], work['session_id'])
        require(prepared and work['parent_turn_id'] == row['agent_turn_id']
            and work['next_turn_id'] == state.get('teaching_turn_id') and state['phase'] == 'ready',
            'DOCUMENT_GOAL_NOT_READY')
        require(state['teaching_fingerprint'] == teaching_fingerprint(c, state['teaching_turn_id']),
            'DOCUMENT_GOAL_TEACHING_CHANGED')
        target = c.execute('''SELECT t.tool_status,t.tool_error_code,p.status,s.status AS step_status,s.error_code
            FROM agent_turn t JOIN agent_plan p USING(agent_turn_id) JOIN agent_step s USING(plan_id)
            WHERE t.agent_turn_id=? AND s.step_index=0''', (state['teaching_turn_id'],)).fetchone()
        return bool(target and not target['tool_error_code'] and not target['error_code']
            and target['tool_status'] in {'pending', 'running'}
            and target['status'] in {'planned', 'waiting_child', 'running'}
            and target['step_status'] in {'planned', 'waiting_child', 'running'})
    except (ValueError, TypeError, KeyError):
        return False


def tutor_proof(tutor):
    return digest({key: tutor[key] for key in ('turn_id', 'child_id', 'session_id', 'subject', 'question',
        'hint_level', 'answer', 'ocr_confirmation_id', 'client_action_id', 'created_at')})


def complete_teaching(c, turn_id, result):
    teaching = c.execute('SELECT * FROM agent_turn WHERE agent_turn_id=?', (turn_id,)).fetchone()
    if not teaching:
        return
    work = c.execute('SELECT * FROM agent_continuation WHERE next_turn_id=?', (turn_id,)).fetchone()
    owned = work is not None and owns(c, work['root_turn_id'])
    if teaching['intent'] != 'document_goal_handoff' and not owned:
        return
    require(owned and work['parent_turn_id'] == work['root_turn_id']
        and work['session_id'] == teaching['session_id'] and work['execution_admitted_at'],
        'DOCUMENT_GOAL_EXECUTION_NOT_ADMITTED')
    action = json.loads(teaching['action_json'])
    require(isinstance(action, dict), 'DOCUMENT_GOAL_TEACHING_CHANGED')
    root = work['root_turn_id']
    require(action.get('document_goal_root_turn_id') == root, 'DOCUMENT_GOAL_TEACHING_CHANGED')
    row, state = validate(c, root, teaching['session_id'])
    require(state.get('teaching_turn_id') == turn_id
        and state['teaching_fingerprint'] == teaching_fingerprint(c, turn_id), 'DOCUMENT_GOAL_TEACHING_CHANGED')
    receipt = result.get('execution_receipt') or result.get('client_bridge_receipt') or {}
    output = receipt.get('output') or {}
    tutor = c.execute('SELECT * FROM tutor_turn WHERE turn_id=?', (output.get('tutor_turn_id'),)).fetchone()
    require(tutor and tutor['child_id'] == row['child_id'] and tutor['session_id'] == row['session_id']
        and tutor['ocr_confirmation_id'] == state['confirmation_id']
        and tutor['client_action_id'] == action['tutor_request_action_id'], 'DOCUMENT_GOAL_TUTOR_CHANGED')
    args = teaching_arguments(row, confirmation(c, row, state['confirmation_id'],
        historical=bool(state.get('tutor_turn_id')), expected_hash=state['confirmation_hash']))
    require(tutor['subject'] == args['subject'] and tutor['question'] == args['question']
        and tutor['hint_level'] == args['support_level'] and teaching['response_text'] == tutor['answer']
        and instant(tutor['created_at']) >= instant(teaching['created_at']), 'DOCUMENT_GOAL_TUTOR_CHANGED')
    proof = tutor_proof(tutor)
    if state.get('tutor_turn_id'):
        require(state['tutor_turn_id'] == tutor['turn_id'] and state['tutor_hash'] == proof, 'DOCUMENT_GOAL_TUTOR_CHANGED')
        return
    import re
    scopes = json.loads(row['plan_json'])['continuation_scope']
    review = [item for item in scopes if item['capability'] == 'conversation.respond'
              and re.search(r'提醒.{0,8}复习|复习.{0,8}提醒', item['request_quote'])]
    # A teaching receipt is not mastery, and an unspecified reminder has no due time.
    state.update(phase='awaiting_time' if review else 'taught', revision=state['revision'] + 1,
        tutor_turn_id=tutor['turn_id'], tutor_hash=proof,
        review_request_quote=review[0]['request_quote'] if review else None)
    store(c, row, state)


def snapshot(c, root, session):
    try:
        row, state = validate(c, root, session)
    except (ValueError, TypeError, KeyError) as error:
        return dict(status='blocked', reason=str(error), parent_turn_id=root, document_goal={'phase':'blocked'}, **LOCAL)
    messages = {'awaiting_confirmation':'拍题已保存，先核对这次题干；后续请求也保留了。',
        'ready':'这次题干已核对，可以从这份原题继续讲解。',
        'taught':'这次讲解已保存，尚不能据此认定掌握。原题和后续请求都保留了。',
        'awaiting_time':'讲解已经保存。你希望什么时候提醒复习这道题？',
        'scheduled':'这道题的复习提醒已保存。',
        'review_cancelled':'这道题的复习安排已取消，原题和讲解仍保留。'}
    if state['phase'] == 'time_proposal':
        messages['time_proposal'] = state['review_time_proposal']['readback']
    public = {key: state[key] for key in ('phase', 'revision', 'artifact_id')}
    public.update(root_turn_id=root, session_id=session, child_id=row['child_id'],
        objective_id=row['objective_id'], message=messages.get(state['phase'], '原进度已保留。'))
    for key in ('confirmation_id', 'teaching_turn_id', 'tutor_turn_id', 'commitment_id', 'due_at'):
        if state.get(key):
            public[key] = state[key]
    if state['phase'] == 'ready':
        work = c.execute('SELECT * FROM agent_continuation WHERE parent_turn_id=?', (root,)).fetchone()
        if not work or not execution_valid(c, dict(work), prepared=True):
            return dict(status='blocked', reason='DOCUMENT_GOAL_TEACHING_CHANGED', parent_turn_id=root, **LOCAL)
        if work['execution_admitted_at']:
            return dict(status='awaiting_receipt', reason='CLIENT_EXECUTION_RESULT_UNCONFIRMED',
                parent_turn_id=root, document_goal=public, **LOCAL)
        return dict(status='ready', parent_turn_id=root, next_turn_id=state['teaching_turn_id'], document_goal=public, **LOCAL)
    return dict(status='blocked' if state['phase'] != 'scheduled' else 'completed',
        reason='DOCUMENT_GOAL_' + state['phase'].upper(), parent_turn_id=root, document_goal=public, **LOCAL)
