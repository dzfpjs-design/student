"""Project an exact closed-day writing obligation into today's teaching action.

Read-only: the Runtime must admit current voice intent and bind a fresh prompt
transactionally. This projection neither starts a timer nor completes a promise.
"""
import json
from datetime import datetime, timezone

from .learning_outcome_projection import LearningOutcomeProjection
from .local_supply import digest
from .writing_teaching import WritingTeachingService

ARRIVAL_KIND = 'daily_writing_arrival'
COMPLETION_KIND = 'daily_writing_completion_arrival'
LOCAL = {'local_only': True, 'cloud_transmission_allowed': False}
TASK_FIELDS = ('plan_item_id','plan_id','source_type','source_ref','subject','task_type',
               'expected_minutes','position','plan_version','plan_date')


def _require(ok, reason):
    if not ok:
        from .agent_runtime import InvalidAgentPlanError
        raise InvalidAgentPlanError('ANSWER_WRITING_DAILY_' + reason)


def commitment_task_type(c, child_id, payload):
    """Expose the original response modality to local task selection."""
    if not isinstance(payload, dict) or payload.get('origin') != 'daily_learning_closure':
        return 'review'
    original = c.execute('''SELECT i.answer_type,i.expected_answer_json FROM agent_step s
        JOIN diagnostic_attempt a ON a.evidence_id=CASE WHEN json_valid(s.arguments_json)
            THEN json_extract(s.arguments_json,'$.evidence_id') END
        JOIN diagnostic_item i USING(diagnostic_item_id)
        WHERE s.step_id=? AND a.child_id=?''', (payload.get('source_step_id'), child_id)).fetchone()
    if not original or original['answer_type'] not in {'writing','open_response'}:
        return 'review'
    try:
        rubric = json.loads(original['expected_answer_json'])
    except (TypeError, json.JSONDecodeError):
        return 'review'
    return ('writing' if original['answer_type'] == 'writing'
            or (isinstance(rubric, dict) and rubric.get('response_mode') == 'written') else 'review')


def _task(c, child, sid, item_id):
    return c.execute('''SELECT pi.*,dp.version AS plan_version,dp.plan_date FROM plan_item pi
        JOIN daily_plan dp USING(plan_id) WHERE pi.plan_item_id=? AND dp.child_id=?
        AND dp.session_id=? AND dp.status='active' ''',(item_id,child,sid)).fetchone()


def capture_arrival(c, arrival_id):
    from .agent_runtime import AgentRuntimeStore
    _require(c.in_transaction,'TRANSACTION_REQUIRED')
    row = c.execute('SELECT * FROM learning_input_arrival WHERE arrival_id=?',(arrival_id,)).fetchone()
    _require(row,'ARRIVAL_REQUIRED')
    snapshot = json.loads(row['snapshot_json'])
    prior = [entry.get('record') for entry in snapshot if entry.get('kind')==ARRIVAL_KIND]
    if prior:
        return prior[0]
    item = AgentRuntimeStore._next_scheduled_task(c,row['child_id'],row['session_id'])
    record = _capture_scheduled_source(c, row, item)
    snapshot.append({'kind':ARRIVAL_KIND,'record':record,**LOCAL})
    if not any(entry.get('kind')==COMPLETION_KIND for entry in snapshot):
        snapshot.append({'kind':COMPLETION_KIND,'record':_capture_completion(c,row),**LOCAL})
    c.execute('UPDATE learning_input_arrival SET snapshot_json=? WHERE arrival_id=?',
        (json.dumps(snapshot,ensure_ascii=False),arrival_id))
    return record


def _capture_scheduled_source(c, row, item):
    if not item or item['source_type'] != 'agent_retention':
        return None
    unavailable = {'source': {'status': 'blocked', 'reason': 'writing_followup_source_invalid',
                              'action': None, 'binding_version': None, **LOCAL},
                   'task': {key: item[key] for key in ('plan_item_id', 'source_type', 'source_ref')}, **LOCAL}
    # Optional history cannot disable stop/chat. Preserve an unavailable target
    # instead of falling through to another task; admission rejects this record.
    try:
        return _read_scheduled_source(c, row, item, unavailable)
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        return unavailable


def _read_scheduled_source(c, row, item, unavailable):
    if item and item['source_type']=='agent_retention':
        promise = c.execute('SELECT * FROM agent_commitment WHERE commitment_id=? AND child_id=?',
            (item['source_ref'],row['child_id'])).fetchone()
        if not promise:
            return unavailable
        payload = json.loads(promise['payload_json'])
        if not isinstance(payload, dict):
            return unavailable
        if payload.get('origin')=='daily_learning_closure':
            original = c.execute('''SELECT i.* FROM agent_step s JOIN diagnostic_attempt a
                ON a.evidence_id=CASE WHEN json_valid(s.arguments_json)
                    THEN json_extract(s.arguments_json,'$.evidence_id') END
                JOIN diagnostic_item i USING(diagnostic_item_id) WHERE s.step_id=? AND a.child_id=?''',
                (payload.get('source_step_id'),row['child_id'])).fetchone()
            if not original:
                return unavailable
            if original and original['answer_type'] in {'writing','open_response'}:
                rubric = json.loads(original['expected_answer_json'])
                if not isinstance(rubric, dict):
                    return unavailable
                if original['answer_type']=='writing' or rubric.get('response_mode')=='written':
                    actual = _task(c,row['child_id'],row['session_id'],item['plan_item_id'])
                    if not actual:
                        return unavailable
                    from .voice_control_intent import VoiceControlIntent
                    return {'source':resolve_daily_writing_followup(c,child_id=row['child_id'],
                        session_id=row['session_id'],commitment_id=item['source_ref']),
                        'task':{key:actual[key] for key in TASK_FIELDS},
                        'started_at':actual['started_at'], 'source_objective_id':promise['objective_id'],
                        'criteria':rubric.get('task_points'),
                        'consent_version':VoiceControlIntent.text_consent_version(c), **LOCAL}
    return None


def _arrival(c, turn, manifest, *, live):
    from .agent_runtime import AgentRuntimeStore
    from .voice_control_intent import VoiceControlIntent
    row = c.execute('''SELECT * FROM learning_input_arrival WHERE arrival_id=? AND child_id=?
        AND session_id=? AND client_action_id=?''',(manifest.get('learning_input_arrival_id'),
        turn['child_id'],turn['session_id'],turn['client_action_id'])).fetchone()
    _require(row and row['utterance']==turn['transcript'] and turn['intent']!='runtime_continuation','ARRIVAL_CHANGED')
    records = [entry.get('record') for entry in json.loads(row['snapshot_json']) if entry.get('kind')==ARRIVAL_KIND]
    _require(len(records)==1,'ARRIVAL_CHANGED')
    record = records[0]
    if record is None:
        return None
    _require(record.get('source',{}).get('status')=='ready','SOURCE_UNAVAILABLE')
    _require(digest(record['consent_version'])==digest(VoiceControlIntent.text_consent_version(c)),'CONSENT_CHANGED')
    if live:
        latest=c.execute('SELECT arrival_id FROM learning_input_arrival WHERE child_id=? AND session_id=? ORDER BY rowid DESC LIMIT 1',
            (turn['child_id'],turn['session_id'])).fetchone()
        _require(latest and latest[0]==row['arrival_id'],'ARRIVAL_CHANGED')
    action=json.loads(turn['action_json'])
    voice_id=action.get('voice_input_id')
    if voice_id:
        voice=AgentRuntimeStore.resolve_captured_voice(c,session_id=turn['session_id'],child_id=turn['child_id'],
            voice_input_id=voice_id,transcript=turn['transcript'])
        _require(AgentRuntimeStore.voice_capture_snapshot(voice)==action.get('voice_input_binding'),'VOICE_CHANGED')
    return record


def _current(c, turn, record):
    current=resolve_daily_writing_followup(c,child_id=turn['child_id'],session_id=turn['session_id'],
        commitment_id=record['task']['source_ref'],_read_completed=True)
    _require(current==record['source'],'SOURCE_CHANGED')
    item=_task(c,turn['child_id'],turn['session_id'],record['task']['plan_item_id'])
    _require(item and all(item[key]==value for key,value in record['task'].items())
        and item['status'] in {'pending','paused','active','completed'},'TASK_CHANGED')
    return item


def admit_plan(c, turn_id, manifest):
    if not manifest.get('learning_input_arrival_id'):
        return None
    turn=c.execute('SELECT * FROM agent_turn WHERE agent_turn_id=?',(turn_id,)).fetchone()
    record=_arrival(c,turn,manifest,live=True)
    if record is None:
        return None
    item=_current(c,turn,record)
    _require(item['status']=='pending' and item['started_at'] is None and record['started_at'] is None,'TASK_CHANGED')
    active=c.execute("SELECT * FROM agent_objective WHERE child_id=? AND status='active'",(turn['child_id'],)).fetchall()
    parent_session=record['source']['action']['parent_session_id']
    closed=c.execute("SELECT 1 FROM daily_session WHERE session_id=? AND child_id=? AND status='completed'",
        (parent_session,turn['child_id'])).fetchone()
    _require(closed and all(goal['objective_id']==record['source_objective_id']
        and goal['session_id']==parent_session for goal in active),'OTHER_GOAL_ACTIVE')
    criteria=record.get('criteria')
    _require(isinstance(criteria,list) and criteria and all(isinstance(x,str) and x.strip() for x in criteria),'CRITERIA_REQUIRED')
    packet={'action':record['source']['action'],'binding_version':record['source']['teaching_binding_version']}
    manifest.update(daily_writing_binding=record,writing_followup_origin={'objective_id':record['source_objective_id']},
        task_start_binding={'plan_item_id':item['plan_item_id'],'source_type':item['source_type'],
                            'source_ref':item['source_ref'],'operation':'start'})
    action=json.loads(turn['action_json'])
    action.update(writing_followup_binding=packet,request_quote=turn['transcript'],**LOCAL)
    c.execute('UPDATE agent_turn SET action_json=? WHERE agent_turn_id=?',(json.dumps(action,ensure_ascii=False),turn_id))
    return record


def _budget(c, turn, item):
    from .learning_input_replan import LearningInputReplanService
    rule=c.execute('SELECT * FROM family_rule WHERE child_id=?',(turn['child_id'],)).fetchone()
    session=c.execute('SELECT ds.*,cp.grade FROM daily_session ds JOIN child_profile cp USING(child_id) WHERE session_id=?',
        (turn['session_id'],)).fetchone()
    plan=c.execute('SELECT * FROM daily_plan WHERE plan_id=?',(item['plan_id'],)).fetchone()
    context=c.execute('SELECT * FROM session_planning_context WHERE session_id=?',(turn['session_id'],)).fetchone()
    others=[dict(r) for r in c.execute('SELECT * FROM plan_item WHERE plan_id=? AND plan_item_id!=?',
        (item['plan_id'],item['plan_item_id']))]
    _require(rule and session and plan,'BUDGET_UNKNOWN')
    target=json.loads(turn['action_json'])['writing_followup_binding']['action']
    track=c.execute('''SELECT k.track FROM diagnostic_item d JOIN knowledge_node k USING(knowledge_node_id)
        WHERE d.diagnostic_item_id=?''',(target['diagnostic_item_id'],)).fetchone()
    _require(track,'BUDGET_UNKNOWN')
    info,available,reason=LearningInputReplanService(None)._budget(c,turn['child_id'],session,plan,others,rule,
        dict(context) if context else {'energy_level':'medium','fixed_commitment_minutes':0},{},datetime.now(timezone.utc),
        consumes_pet_reserve=track[0]=='pet')
    _require(reason is None and 0<item['expected_minutes']<=info['effective_task_segment_minutes_cap']
        and item['expected_minutes']*60<=available,'BUDGET_INSUFFICIENT')
    return info


def inspect_execution(c, turn_id, *, prompt=None, preparing=False, recovering=False):
    from .agent_runtime import AgentRuntimeStore
    from .task_start_binding import TaskStartBinding
    turn=c.execute('''SELECT t.*,p.objective_id,p.plan_json,p.context_manifest_json,p.status AS plan_status,
        s.status AS step_status,s.capability_name,s.idempotency_key,s.error_code,d.status AS session_status,d.state
        FROM agent_turn t JOIN agent_plan p USING(agent_turn_id) JOIN agent_step s USING(plan_id)
        JOIN daily_session d ON d.session_id=t.session_id AND d.child_id=t.child_id
        WHERE t.agent_turn_id=? AND p.child_id=t.child_id AND p.session_id=t.session_id AND s.step_index=0''',(turn_id,)).fetchone()
    _require(turn and turn['capability_name']=='task.start','COMMAND_CHANGED')
    manifest=json.loads(turn['context_manifest_json'])
    record=_arrival(c,turn,manifest,live=prompt is None)
    _require(record and record==manifest.get('daily_writing_binding'),'BINDING_CHANGED')
    AgentRuntimeStore.assert_turn_source_not_deleted(c,turn_id)
    _require(turn['session_status']=='active' and turn['state']=='study','SESSION_CHANGED')
    if recovering:
        _require(turn['plan_status']==turn['step_status']==turn['tool_status']=='failed'
            and turn['error_code']==turn['tool_error_code']
            and turn['error_code'] in {'OperationalError','IntegrityError','APP_RESTARTED'},'RECOVERY_NOT_AUTHORIZED')
    else:
        _require(turn['plan_status'] in {'running','completed','waiting_child'}
            and turn['step_status'] in {'running','completed'}
            and turn['tool_status'] in {'pending','completed'} and not turn['error_code'] and not turn['tool_error_code'],'COMMAND_CHANGED')
    goal=c.execute("SELECT * FROM agent_objective WHERE objective_id=? AND child_id=? AND session_id=? AND status='active' AND objective_type='learning'",
        (turn['objective_id'],turn['child_id'],turn['session_id'])).fetchone()
    _require(goal and json.loads(goal['success_criteria_json'])==record['criteria'],'GOAL_CHANGED')
    _require(not c.execute("SELECT 1 FROM agent_objective WHERE child_id=? AND status='active' AND objective_id!=?",
        (turn['child_id'],turn['objective_id'])).fetchone(),'GOAL_CHANGED')
    item=_current(c,turn,record)
    packet={'action':record['source']['action'],'binding_version':record['source']['teaching_binding_version']}
    action=json.loads(turn['action_json'])
    _require(action.get('type')=='start_next_task' and action.get('request_quote')==turn['transcript']
        and action.get('writing_followup_binding')==packet,'BINDING_CHANGED')
    _require(manifest.get('task_start_binding')=={'plan_item_id':item['plan_item_id'],
        'source_type':item['source_type'],'source_ref':item['source_ref'],'operation':'start'},'TASK_CHANGED')
    if prompt is None:
        if not recovering:
            TaskStartBinding.authorize(c,turn_id,item['plan_item_id'],'start')
        _require(item['status']=='pending' and item['started_at'] is None,'TASK_CHANGED')
        _budget(c,turn,item)
    else:
        reference=json.loads(prompt['request_json'])
        target=packet['action']
        _require(reference.get('writing_authorized_turn_id')==turn_id
            and prompt['prepare_action_id']==turn['idempotency_key']
            and prompt['child_id']==turn['child_id'] and prompt['origin_session_id']==turn['session_id']
            and prompt['diagnostic_item_id']==target['diagnostic_item_id'] and prompt['item_version']==target['item_version']
            and reference.get('writing_followup')=={**{k:target[k] for k in ('parent_prompt_id','parent_item_version','parent_session_id','kind')},
                'binding_version':packet['binding_version']},'PROMPT_CHANGED')
        if not preparing:
            event=c.execute("SELECT * FROM plan_item_event WHERE client_action_id=? AND plan_item_id=? AND event_type='started'",
                (turn['idempotency_key'],item['plan_item_id'])).fetchone()
            _require(event and item['started_at']==event['occurred_at']
                and item['status'] in {'active','paused','completed'},'START_RECEIPT_CHANGED')
    return {'binding':packet,'record':record,'turn':dict(turn),'item':dict(item),'capability':'task.start','request_quote':turn['transcript']}


def recover_request(c,turn_id):
    """Re-arm only the original failed storage command, before executor CAS."""
    row=c.execute('SELECT context_manifest_json FROM agent_plan WHERE agent_turn_id=?',(turn_id,)).fetchone()
    if not row or 'daily_writing_binding' not in json.loads(row[0]):
        return
    existing=c.execute('''SELECT q.* FROM diagnostic_prompt q JOIN agent_step s ON s.idempotency_key=q.prepare_action_id
        JOIN agent_plan p USING(plan_id) WHERE p.agent_turn_id=? AND s.step_index=0''',(turn_id,)).fetchone()
    proof=inspect_execution(c,turn_id,prompt=existing,recovering=True)
    # A prior prepare/start receipt does not make a new intervening utterance
    # authorize this retry. Recheck the original arrival even for saved prompts.
    _arrival(c,proof['turn'],json.loads(row[0]),live=True)
    c.execute("UPDATE agent_turn SET tool_status='pending',tool_error_code=NULL,completed_at=NULL WHERE agent_turn_id=?",
        (turn_id,))


def bind_prompt(c, turn_id, prompt):
    from .agent_runtime import AgentRuntimeStore
    proof=inspect_execution(c,turn_id,prompt=prompt,preparing=True)
    row=proof['turn']
    manifest=json.loads(row['context_manifest_json'])
    manifest['goal_prompt_binding']={'plan_item_id':proof['item']['plan_item_id'],'prompt_id':prompt['prompt_id'],
        'item_version':prompt['item_version'],'binding':proof['record']['source'], 'stage':False}
    c.execute('UPDATE agent_plan SET context_manifest_json=? WHERE agent_turn_id=?',(json.dumps(manifest,ensure_ascii=False),turn_id))
    _require(AgentRuntimeStore.bind_writing_followup_prompt(c,turn_id,{'prompt':dict(prompt)}),'PROMPT_CHANGED')


def prepare_and_start(database, repository, turn_id):
    from contextlib import nullcontext
    from .diagnostic_prompt import DiagnosticPromptService
    DiagnosticPromptService(database)
    with database.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        existing=c.execute('''SELECT q.* FROM diagnostic_prompt q JOIN agent_step s ON s.idempotency_key=q.prepare_action_id
            JOIN agent_plan p USING(plan_id) WHERE p.agent_turn_id=? AND s.step_index=0''',(turn_id,)).fetchone()
        proof=inspect_execution(c,turn_id,prompt=existing)
        target=proof['binding']['action']
        from .writing_response_origin import writing_item
        _,node=writing_item(c,proof['turn']['child_id'],target['diagnostic_item_id'])
        class TransactionPrompt(DiagnosticPromptService):
            def __init__(self):
                self.database=None
            def _transaction(self):
                return nullcontext(c)
        prompts=TransactionPrompt()
        if existing:
            prepared=prompts.read(proof['turn']['session_id'],prompt_id=existing['prompt_id'])
            prepared={**prepared,'replayed':True}
        else:
            prepared=prompts.prepare(proof['turn']['session_id'],track=node['track'],
                preferred_item_id=target['diagnostic_item_id'],client_action_id=proof['turn']['idempotency_key'],
                _writing_authorized_turn_id=turn_id,writing_followup={**{k:target[k] for k in
                    ('parent_prompt_id','parent_item_version','parent_session_id','kind')},'binding_version':proof['binding']['binding_version']})
            row=c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?',
                ((prepared.get('prompt') or {}).get('prompt_id'),)).fetchone()
            _require(row and prepared['status']=='waiting_answer','PROMPT_UNAVAILABLE')
            proof=inspect_execution(c,turn_id,prompt=row,preparing=True)
            _budget(c,proof['turn'],proof['item'])
        started=repository.start_plan_item(proof['item']['plan_item_id'],proof['turn']['idempotency_key'],
            authorized_turn_id=turn_id,connection=c)
        c.commit()
    return prepared,started


def replay(c,turn_id):
    from .diagnostic_prompt import DiagnosticPromptService
    row=c.execute('SELECT context_manifest_json FROM agent_plan WHERE agent_turn_id=?',(turn_id,)).fetchone()
    if not row or 'daily_writing_binding' not in json.loads(row[0]):
        return None
    prompt=c.execute('''SELECT q.* FROM diagnostic_prompt q JOIN agent_step s ON s.idempotency_key=q.prepare_action_id
        JOIN agent_plan p USING(plan_id) WHERE p.agent_turn_id=? AND s.step_index=0''',(turn_id,)).fetchone()
    _require(prompt and prompt['status'] in {'waiting_answer','answer_saved','answered'},'PROMPT_CHANGED')
    proof=inspect_execution(c,turn_id,prompt=prompt)
    if prompt['status']=='answered':
        _require(DiagnosticPromptService._receipt(c,prompt),'ANSWER_CHANGED')
        prepared={'status':'answered','prompt':None,'prompt_id':prompt['prompt_id'],'item_version':prompt['item_version']}
    else:
        prepared=DiagnosticPromptService._result(c,prompt)
    return {'capability':'task.start','response_text':'原题和开始记录已经保存，这次只核对记录。',
        'replayed':True,'recorded_receipt':True,'replay_audio_allowed':False,
        'result':{**proof['item'],'diagnostic_prompt':prepared,**LOCAL},'evidence':[],
        'requires_child_reply':False}


def _completion_basis(c, start_turn_id):
    try:
        return _read_completion_basis(c,start_turn_id)
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        _require(False,'COMPLETION_SOURCE_OR_ANSWER_CHANGED')


def _read_completion_basis(c, start_turn_id):
    from .writing_followup_evidence import validate_formal_followup_evaluation
    from .writing_teaching import WritingTeachingService
    prompt=c.execute('''SELECT q.* FROM diagnostic_prompt q WHERE
        CASE WHEN json_valid(q.request_json)
            THEN json_extract(q.request_json,'$.writing_authorized_turn_id') END=?''',(start_turn_id,)).fetchall()
    _require(len(prompt)==1 and prompt[0]['status']=='answered','COMPLETION_ANSWER_REQUIRED')
    prompt=prompt[0]
    original=inspect_execution(c,start_turn_id,prompt=prompt)
    validate_formal_followup_evaluation(c,child_id=prompt['child_id'],session_id=prompt['origin_session_id'],
        prompt_id=prompt['prompt_id'],item_version=prompt['item_version'],attempt_id=prompt['attempt_id'])
    view=WritingTeachingService(None).read(prompt['origin_session_id'],prompt_id=prompt['prompt_id'],
        item_version=prompt['item_version'],connection=c)
    _require(view.get('status')=='feedback_ready','COMPLETION_FEEDBACK_REQUIRED')
    assessment=c.execute('SELECT * FROM rubric_assessment WHERE assessment_id=? AND child_id=?',
        (view['assessment_id'],prompt['child_id'])).fetchone()
    attempt=c.execute('SELECT * FROM diagnostic_attempt WHERE attempt_id=? AND child_id=? AND session_id=?',
        (prompt['attempt_id'],prompt['child_id'],prompt['origin_session_id'])).fetchone()
    evaluated=c.execute('''SELECT s.*,p.objective_id FROM agent_step s JOIN agent_plan p USING(plan_id)
        WHERE s.capability_name='learning.evaluate_response' AND s.status='completed'
        AND p.status IN ('completed','waiting_child') AND p.child_id=? AND p.session_id=? AND p.objective_id=?
        AND CASE WHEN json_valid(s.arguments_json)
            THEN json_extract(s.arguments_json,'$.evidence_id') END=?''',
        (prompt['child_id'],prompt['origin_session_id'],original['turn']['objective_id'],attempt['evidence_id'])).fetchall()
    _require(assessment and len(evaluated)==1,'COMPLETION_FEEDBACK_REQUIRED')
    evaluation=json.loads(evaluated[0]['result_json'])
    evaluation=evaluation.get('result',evaluation)
    evaluation=evaluation.get('evaluation',evaluation)
    _require((evaluation.get('rubric_assessment') or {}).get('assessment_id')==assessment['assessment_id']
        and LearningOutcomeProjection(None).source_available_outcome(c,prompt['child_id'],
            evaluation.get('outcome_id'),original['turn']['objective_id']),'COMPLETION_FEEDBACK_CHANGED')
    return {'start_turn_id':start_turn_id,'task':original['record']['task'],
        'started_at':original['item']['started_at'],'objective_id':original['turn']['objective_id'],
        'prompt_id':prompt['prompt_id'],'item_version':prompt['item_version'],'attempt_id':prompt['attempt_id'],
        'attempt_version':digest(dict(attempt)),'assessment_id':assessment['assessment_id'],
        'assessment_version':digest(dict(assessment)),'evaluation_step_id':evaluated[0]['step_id'],
        'evaluation_version':digest(dict(evaluated[0])),'outcome_id':evaluation['outcome_id'],**LOCAL}


def _capture_completion(c,arrival):
    rows=c.execute('''SELECT p.agent_turn_id,p.context_manifest_json,pi.plan_item_id
        FROM plan_item pi JOIN daily_plan dp USING(plan_id)
        JOIN agent_plan p ON p.child_id=dp.child_id AND p.session_id=dp.session_id
        WHERE pi.status='active' AND pi.source_type='agent_retention' AND dp.status='active'
        AND dp.child_id=? AND dp.session_id=?''',(arrival['child_id'],arrival['session_id'])).fetchall()
    blocked = {'status':'blocked','reason':'COMPLETION_SOURCE_OR_ANSWER_UNAVAILABLE'}
    matched = []
    invalid = False
    # Decode optional, owner-scoped history outside SQLite JSON functions. A bad
    # unrelated record must neither disable voice nor replace a valid exact match.
    for row in rows:
        try:
            manifest = json.loads(row['context_manifest_json'])
            _require(isinstance(manifest, dict), 'COMPLETION_SOURCE_CHANGED')
            binding = manifest.get('daily_writing_binding')
            if binding is None:
                continue
            _require(isinstance(binding, dict) and isinstance(binding.get('task'), dict),
                     'COMPLETION_SOURCE_CHANGED')
            if binding['task'].get('plan_item_id') == row['plan_item_id']:
                matched.append(row)
        except (ValueError, TypeError, KeyError):
            invalid = True
    if not matched:
        return blocked if invalid else None
    try:
        _require(len(matched)==1,'COMPLETION_TASK_AMBIGUOUS')
        return {'status':'ready','basis':_completion_basis(c,matched[0]['agent_turn_id'])}
    except ValueError:
        return blocked


def _completion_arrival(c,turn,manifest,*,live):
    row=c.execute('''SELECT * FROM learning_input_arrival WHERE arrival_id=? AND child_id=?
        AND session_id=? AND client_action_id=?''',(manifest.get('learning_input_arrival_id'),
        turn['child_id'],turn['session_id'],turn['client_action_id'])).fetchone()
    _require(row and row['utterance']==turn['transcript'] and turn['intent']!='runtime_continuation','COMPLETION_ARRIVAL_CHANGED')
    entries=[entry.get('record') for entry in json.loads(row['snapshot_json']) if entry.get('kind')==COMPLETION_KIND]
    _require(len(entries)==1,'COMPLETION_ARRIVAL_CHANGED')
    if live:
        last=c.execute('SELECT arrival_id FROM learning_input_arrival WHERE child_id=? AND session_id=? ORDER BY rowid DESC LIMIT 1',
            (turn['child_id'],turn['session_id'])).fetchone()
        _require(last and last[0]==row['arrival_id'],'COMPLETION_ARRIVAL_CHANGED')
    return entries[0]


def admit_completion(c,turn_id,manifest):
    if not manifest.get('learning_input_arrival_id'):
        return None
    turn=c.execute('SELECT * FROM agent_turn WHERE agent_turn_id=?',(turn_id,)).fetchone()
    record=_completion_arrival(c,turn,manifest,live=True)
    if record is None:
        return None
    _require(record.get('status')=='ready','COMPLETION_ANSWER_REQUIRED')
    basis=record['basis']
    _require(_completion_basis(c,basis['start_turn_id'])==basis,'COMPLETION_SOURCE_CHANGED')
    manifest['daily_writing_completion']=basis
    action=json.loads(turn['action_json'])
    action.update(request_quote=turn['transcript'],daily_writing_completion=basis,**LOCAL)
    c.execute('UPDATE agent_turn SET action_json=? WHERE agent_turn_id=?',(json.dumps(action,ensure_ascii=False),turn_id))
    return basis


def completion_shutdown_receipt(c,turn_id):
    """A shutdown is not a child pause or a new completion request."""
    row=c.execute('''SELECT t.*,p.context_manifest_json,a.snapshot_json,a.created_at AS arrival_at
        FROM agent_turn t JOIN agent_plan p USING(agent_turn_id)
        JOIN learning_input_arrival a ON a.arrival_id=json_extract(p.context_manifest_json,'$.learning_input_arrival_id')
        AND a.child_id=t.child_id AND a.session_id=t.session_id AND a.client_action_id=t.client_action_id
        AND a.utterance=t.transcript WHERE t.agent_turn_id=? AND p.child_id=t.child_id AND p.session_id=t.session_id''',
        (turn_id,)).fetchone()
    basis=json.loads(row['context_manifest_json']).get('daily_writing_completion') if row else None
    if not basis:
        return False
    frozen=[p for entry in json.loads(row['snapshot_json']) for p in entry.get('plans',[])
        if p.get('plan_item_id')==basis['task']['plan_item_id'] and p.get('status')=='active']
    if len(frozen)!=1:
        return False
    before=frozen[0]
    events=c.execute('''SELECT * FROM plan_item_event WHERE plan_item_id=?
        AND julianday(occurred_at)>julianday(?) ORDER BY rowid''',(before['plan_item_id'],row['arrival_at'])).fetchall()
    if len(events)!=1:
        return False
    event=events[0]
    payload=json.loads(event['payload_json'])
    run_id=payload.get('runtime_run_id')
    current=c.execute('SELECT run_id FROM runtime_state WHERE singleton_id=1').fetchone()
    item=_task(c,row['child_id'],row['session_id'],before['plan_item_id'])
    try:
        # runtime_state.started_at is SQLite UTC datetime text on older/current
        # installations; it is not the machine's local wall clock.
        run_started=datetime.fromisoformat(payload['run_started_at'])
        if run_started.tzinfo is None:
            run_started=run_started.replace(tzinfo=timezone.utc)
        return bool(event['event_type']=='paused'
            and set(payload)=={'reason','source','runtime_run_id','run_started_at','active_segment_started_at'}
            and payload['reason']=='runtime_shutdown' and payload['source']=='system'
            and run_id and current and current[0]!=run_id
            and event['client_action_id']==f"runtime-shutdown:{run_id}:{before['plan_item_id']}"
            and payload['active_segment_started_at']==before['active_segment_started_at']
            and run_started<=datetime.fromisoformat(row['arrival_at'])<=datetime.fromisoformat(event['occurred_at'])
            and item and item['status']=='paused' and item['active_segment_started_at'] is None
            and item['started_at']==before['started_at']==basis['started_at']
            and item['active_seconds']>=before['active_seconds']
            and all(item[key]==value for key,value in basis['task'].items()))
    except (ValueError,TypeError,KeyError):
        return False


def inspect_completion(c,turn_id,*,replay=False,recovering=False):
    from .agent_runtime import AgentRuntimeStore
    from .agent_continuation import ContinuationStore
    row=c.execute('''SELECT t.*,p.context_manifest_json,p.status AS plan_status,s.status AS step_status,
        s.idempotency_key,s.capability_name,s.error_code FROM agent_turn t JOIN agent_plan p USING(agent_turn_id)
        JOIN agent_step s USING(plan_id) WHERE t.agent_turn_id=? AND p.child_id=t.child_id
        AND p.session_id=t.session_id AND s.step_index=0''',(turn_id,)).fetchone()
    manifest=json.loads(row['context_manifest_json']) if row else {}
    basis=manifest.get('daily_writing_completion')
    action=json.loads(row['action_json']) if row else {}
    if basis is None:
        _require(not action.get('daily_writing_completion'),'COMPLETION_BINDING_CHANGED')
        return None
    _require(c.in_transaction and row['capability_name']=='task.complete','COMPLETION_COMMAND_CHANGED')
    _require(action.get('type')=='complete_active_task' and action.get('request_quote')==row['transcript']
        and action.get('daily_writing_completion')==basis,'COMPLETION_COMMAND_CHANGED')
    frozen=_completion_arrival(c,row,manifest,live=not replay)
    _require(frozen=={'status':'ready','basis':basis},'COMPLETION_ARRIVAL_CHANGED')
    if recovering:
        _require(row['plan_status']==row['step_status']==row['tool_status']=='failed'
            and row['error_code']==row['tool_error_code']
            and row['error_code'] in {'IntegrityError','OperationalError','APP_RESTARTED'},'COMPLETION_RECOVERY_FORBIDDEN')
    else:
        _require(row['plan_status'] not in {'cancelled','failed'} and row['step_status'] not in {'cancelled','failed'}
            and row['tool_status'] not in {'cancelled','failed'},'COMPLETION_COMMAND_CHANGED')
    if not replay and not recovering:
        _require(ContinuationStore.execution_allowed(c,turn_id,running=True),'COMPLETION_COMMAND_CHANGED')
    AgentRuntimeStore.assert_turn_source_not_deleted(c,turn_id)
    from .voice_control_intent import VoiceControlIntent
    control=manifest.get('voice_control_binding')
    if control:
        journal=c.execute('SELECT status FROM voice_control_intent_request WHERE request_key=?',(control['request_key'],)).fetchone()
        _require(journal and journal[0]=='completed' and VoiceControlIntent.scope_valid(c,row['session_id'],row['child_id'],row['client_action_id']),
            'COMPLETION_CONTROL_CHANGED')
    AgentRuntimeStore.inspect_voice_control_execution(c,turn_id,allow_planned=recovering)
    if action.get('voice_input_id'):
        captured=AgentRuntimeStore.resolve_captured_voice(c,session_id=row['session_id'],child_id=row['child_id'],
            voice_input_id=action['voice_input_id'],transcript=row['transcript'])
        _require(AgentRuntimeStore.voice_capture_snapshot(captured)==action.get('voice_input_binding'),'COMPLETION_VOICE_CHANGED')
    _require(_completion_basis(c,basis['start_turn_id'])==basis,'COMPLETION_SOURCE_CHANGED')
    item=_task(c,row['child_id'],row['session_id'],basis['task']['plan_item_id'])
    event=c.execute("SELECT * FROM plan_item_event WHERE client_action_id=? AND plan_item_id=? AND event_type='completed'",
        (row['idempotency_key'],item['plan_item_id'])).fetchone()
    _require(item['status']==('completed' if event else 'active')
        or (not event and completion_shutdown_receipt(c,turn_id)),'COMPLETION_TASK_CHANGED')
    if replay:
        _require(event,'COMPLETION_RECEIPT_REQUIRED')
    return {'basis':basis,'item':dict(item),'turn':dict(row),'event':dict(event) if event else None}


def recover_completion(c,turn_id):
    if inspect_completion(c,turn_id,recovering=True):
        c.execute("UPDATE agent_turn SET tool_status='pending',tool_error_code=NULL,completed_at=NULL WHERE agent_turn_id=?",(turn_id,))


def _settled_promise(c,promise):
    """Only our actual completion receipt explains status/date changes in history."""
    events=c.execute('''SELECT e.*,p.context_manifest_json,s.idempotency_key FROM plan_item_event e
        JOIN plan_item pi USING(plan_item_id) JOIN daily_plan dp USING(plan_id)
        JOIN agent_step s ON s.idempotency_key=e.client_action_id AND s.capability_name='task.complete'
        JOIN agent_plan p ON p.plan_id=s.plan_id
        WHERE pi.source_type='agent_retention' AND pi.source_ref=? AND e.event_type='completed'
        AND dp.child_id=? AND p.child_id=dp.child_id AND p.session_id=dp.session_id''',
        (promise['commitment_id'],promise['child_id'])).fetchall()
    for event in events:
        receipt=json.loads(event['payload_json']).get('daily_writing_completion')
        frozen=json.loads(event['context_manifest_json']).get('daily_writing_completion')
        if not receipt or receipt.get('basis')!=frozen or not frozen or frozen['task']['plan_item_id']!=event['plan_item_id']:
            continue
        before={**dict(promise),'status':receipt.get('previous_status'),'completed_at':None}
        if (promise['completed_at']==event['occurred_at'] and before['status'] in {'pending','due'}
                and digest(before)==receipt.get('commitment_version')):
            return before
    return None


def resolve_daily_writing_followup(c, *, child_id, session_id, commitment_id, as_of=None, _read_completed=False):
    if not c.in_transaction:
        raise ValueError('WRITING_DAILY_READ_TRANSACTION_REQUIRED')
    blocked = {'status': 'blocked', 'action': None, 'binding_version': None,
               'local_only': True, 'cloud_transmission_allowed': False}

    def reject(reason):
        return {**blocked, 'reason': reason}

    current = c.execute('''SELECT ds.* FROM daily_session ds JOIN child_profile cp USING(child_id)
        WHERE ds.session_id=? AND ds.child_id=? AND cp.status='active' ''', (session_id, child_id)).fetchone()
    if not current or current['status'] != 'active' or current['state'] in {'evolving', 'completed'}:
        return reject('writing_session_not_active')
    promise = c.execute('''SELECT ac.* FROM agent_commitment ac JOIN agent_objective ao
        ON ao.objective_id=ac.objective_id AND ao.child_id=ac.child_id
        WHERE ac.commitment_id=? AND ac.child_id=? AND ao.status IN ('active','paused')
          AND ac.commitment_type='follow_up' ''',
        (commitment_id, child_id)).fetchone()
    if not promise:
        return reject('writing_followup_obligation_unavailable')
    if promise['status']=='completed' and _read_completed:
        promise=_settled_promise(c,promise)
    if not promise or promise['status'] not in {'pending','due'}:
        return reject('writing_followup_obligation_unavailable')
    try:
        payload = json.loads(promise['payload_json'])
        now = as_of if as_of is not None else datetime.now(timezone.utc)
        due = datetime.fromisoformat(promise['due_at'])
        if not now.tzinfo or not due.tzinfo:
            return reject('writing_followup_time_invalid')
        if now < due:
            return reject('writing_followup_not_due')
        if payload.get('origin') != 'daily_learning_closure' or not payload.get('source_outcome_id'):
            return reject('writing_followup_source_required')
        step = c.execute('''SELECT s.*,p.session_id,p.objective_id FROM agent_step s
            JOIN agent_plan p USING(plan_id) WHERE s.step_id=? AND p.child_id=?
            AND p.objective_id=? AND s.capability_name='learning.evaluate_response' AND s.status='completed' ''',
            (payload.get('source_step_id'), child_id, promise['objective_id'])).fetchone()
        if not step:
            return reject('writing_followup_evaluation_missing')
        evaluation = json.loads(step['result_json'])
        if isinstance(evaluation.get('result'), dict):
            evaluation = evaluation['result']
        if isinstance(evaluation.get('evaluation'), dict):
            evaluation = evaluation['evaluation']
        if evaluation.get('outcome_id') != payload['source_outcome_id']:
            return reject('writing_followup_outcome_changed')
        if not LearningOutcomeProjection(None).commitment_available(c, promise):
            return reject('writing_followup_source_unavailable')
        arguments = json.loads(step['arguments_json'])
        prompts = c.execute('''SELECT dp.* FROM diagnostic_prompt dp JOIN diagnostic_attempt da
            ON da.attempt_id=dp.attempt_id AND da.child_id=dp.child_id AND da.session_id=dp.origin_session_id
            WHERE da.evidence_id=? AND dp.child_id=? AND dp.origin_session_id=? AND dp.status='answered' ''',
            (arguments.get('evidence_id'), child_id, step['session_id'])).fetchall()
        if len(prompts) != 1 or step['session_id'] == session_id:
            return reject('writing_followup_original_missing')
        original = prompts[0]
        resolved = WritingTeachingService(None).resolve_next_action(step['session_id'],
            prompt_id=original['prompt_id'], item_version=original['item_version'], kind='revision',
            target_session_id=session_id, connection=c)
        if resolved['status'] != 'ready':
            return resolved
        assessment = evaluation.get('rubric_assessment') or {}
        if resolved['action']['assessment_id'] != assessment.get('assessment_id'):
            return reject('writing_followup_assessment_changed')
        action = resolved['action']
        scope = {'version': 'daily_writing_followup_v1', 'commitment_id': commitment_id,
                 'commitment_version': digest(dict(promise)), 'source_outcome_id': payload['source_outcome_id'],
                 'source_step_id': step['step_id'], 'evaluation_version': digest(dict(step)),
                 'action': action, 'teaching_binding_version': resolved['binding_version']}
        return {**scope, 'status': 'ready', 'binding_version': digest(scope),
                'local_only': True, 'cloud_transmission_allowed': False}
    except (ValueError, TypeError, KeyError, AttributeError):
        return reject('writing_followup_source_invalid')
