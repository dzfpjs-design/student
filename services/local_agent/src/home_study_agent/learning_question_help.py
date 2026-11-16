"""Current question identity for a voice-requested lesson, never a new topic."""
import json
import hashlib
from datetime import datetime

from .teaching_question_target import resolve_question_target


def snapshot(c, session_id, child_id, *, _historical_prompt=None):
    if not c.in_transaction:
        raise ValueError('LEARNING_HELP_TRANSACTION_REQUIRED')
    try:
        return _snapshot(c,session_id,child_id,_historical_prompt=_historical_prompt)
    except (ValueError,KeyError,TypeError):
        return None


def _snapshot(c, session_id, child_id, *, _historical_prompt=None):
    session = c.execute('''SELECT state,status FROM daily_session WHERE session_id=? AND child_id=?''',
        (session_id,child_id)).fetchone()
    if not session or tuple(session)!=('study','active'):
        return None
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='diagnostic_prompt'").fetchone():
        return None
    prompt = c.execute('''SELECT * FROM diagnostic_prompt WHERE origin_session_id=? AND child_id=?
        AND (? IS NULL OR prompt_id=?) ORDER BY rowid DESC LIMIT 1''',
        (session_id,child_id,_historical_prompt,_historical_prompt)).fetchone()
    if not prompt or prompt['status'] not in {'waiting_answer','answered'}:
        return None
    item = json.loads(prompt['item_json'])
    target = resolve_question_target(c,session_id,subject=item['subject'],question=item['prompt'],
        prompt_id=prompt['prompt_id'],item_version=prompt['item_version'])
    if target['status']!='resolved':
        return None
    roots = c.execute('''SELECT DISTINCT root.* FROM agent_plan origin JOIN agent_plan root
        ON root.objective_id=origin.objective_id AND root.child_id=origin.child_id AND root.session_id=origin.session_id
        WHERE origin.child_id=? AND origin.session_id=?
        AND json_extract(origin.plan_json,'$.local_learning_binding.prompt_id')=?
        AND json_type(root.context_manifest_json,'$.scheduled_writing_goal')='object' ''',
        (child_id,session_id,prompt['prompt_id'])).fetchall()
    task_state=[]
    for root in roots:
        from .scheduled_writing_goal import validate_origin
        root_id = json.loads(root['plan_json']).get('local_learning_binding',{}).get('prompt_id')
        original = c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=? AND child_id=? AND origin_session_id=?',
            (root_id,child_id,session_id)).fetchone()
        try:
            if not original:
                return None
            validate_origin(c,root,original)
            marker=json.loads(root['context_manifest_json'])['scheduled_writing_goal']
            live=c.execute('''SELECT pi.status,pi.active_segment_started_at,pi.active_seconds,
                (SELECT max(rowid) FROM plan_item_event e WHERE e.plan_item_id=pi.plan_item_id),
                o.status AS objective_status FROM plan_item pi JOIN agent_objective o
                ON o.objective_id=? WHERE pi.plan_item_id=? AND o.child_id=? AND o.session_id=?''',
                (root['objective_id'],marker['plan_item_id'],child_id,session_id)).fetchone()
            if not live or (_historical_prompt is None and
                    (live['status'] not in {'active','paused'} or live['objective_status']!='active')):
                return None
            task_state.append([marker['plan_item_id'],*tuple(live)])
        except ValueError:
            return None
    return dict(prompt_id=prompt['prompt_id'],item_version=prompt['item_version'],
        question=item['prompt'],subject=item['subject'],source_fingerprint=target['fingerprint'],
        task_state=sorted(task_state))


def validate(c, session_id, child_id, target):
    current = snapshot(c,session_id,child_id)
    if not isinstance(target,dict) or not current or current!=target:
        raise ValueError('LEARNING_HELP_QUESTION_CHANGED')
    return current


def _require(valid):
    if not valid:
        raise ValueError('LEARNING_HELP_EVIDENCE_CHANGED')


def _origin(c, turn_id):
    row = c.execute('''SELECT t.*,p.context_manifest_json,p.status AS plan_status,
        s.status AS step_status,s.arguments_json,s.result_json,s.capability_name
        FROM agent_turn t JOIN agent_plan p USING(agent_turn_id) JOIN agent_step s USING(plan_id)
        WHERE t.agent_turn_id=? AND p.child_id=t.child_id AND p.session_id=t.session_id AND s.step_index=0''',
        (turn_id,)).fetchone()
    if not row:
        return None
    frozen = json.loads(row['context_manifest_json']).get('voice_control_binding') or {}
    if frozen.get('control')!='learning.help_current':
        return None
    receipt = c.execute('SELECT * FROM voice_control_intent_request WHERE request_key=?',
        (frozen.get('request_key'),)).fetchone()
    fingerprint = hashlib.sha256(json.dumps([row['child_id'],row['session_id'],row['transcript']],ensure_ascii=False).encode()).hexdigest()
    _require(receipt and receipt['status']=='completed' and receipt['fingerprint']==fingerprint
        and receipt['child_id']==row['child_id'] and receipt['session_id']==row['session_id']
        and json.loads(receipt['binding_json'])==frozen.get('targets'))
    decision = json.loads(receipt['decision_json'])
    _require(decision.get('status')=='control' and decision.get('control')=='learning.help_current')
    target = frozen['targets']['learning_help']
    action,arguments = json.loads(row['action_json']),json.loads(row['arguments_json'])
    expected = dict(question=target['question'],subject=target['subject'],hint_level=1,
        diagnostic_prompt_id=target['prompt_id'],diagnostic_item_version=target['item_version'],
        tutor_request_action_id='learning-help:'+frozen['request_key'])
    _require(row['capability_name']=='learning.explain' and action.get('type')=='ask_tutor'
        and all(action.get(k)==v and arguments.get(k)==v for k,v in expected.items()))
    return row,target,action


def validate_dispatch(c, action_id, session_id, child_id, *, request_target=None, _completing=False):
    if not action_id.startswith('learning-help:'):
        return
    row = c.execute('''SELECT t.agent_turn_id FROM agent_turn t JOIN agent_plan p USING(agent_turn_id)
        WHERE t.child_id=? AND t.session_id=?
        AND json_extract(p.context_manifest_json,'$.voice_control_binding.request_key')=?''',
        (child_id,session_id,action_id[len('learning-help:'):])).fetchone()
    _require(row)
    from .voice_control_intent import VoiceControlIntent
    proof = VoiceControlIntent.inspect_execution(c,row['agent_turn_id'],allow_planned=True,_completing_help=_completing)
    _require(proof and proof['control']=='learning.help_current')
    if not _completing:
        target=proof['targets']['learning_help']
        _require(request_target=={key:target[key] for key in ('question','subject','prompt_id','item_version')})


def teaching_receipt(c, turn_id, result=None, *, historical=False):
    origin = _origin(c,turn_id)
    if origin is None:
        return None
    row,target,action = origin
    if historical:
        _require(row['tool_status']==row['step_status']=='completed'
            and row['plan_status'] in {'completed','waiting_child','awaiting_child'})
        result = json.loads(row['result_json'])
        current=snapshot(c,row['session_id'],row['child_id'],_historical_prompt=target['prompt_id'])
        _require(current and {k:v for k,v in current.items() if k!='task_state'}==
            {k:v for k,v in target.items() if k!='task_state'})
        _require([task[0] for task in current['task_state']]==[task[0] for task in target['task_state']])
    else:
        validate_dispatch(c,action['tutor_request_action_id'],row['session_id'],row['child_id'],_completing=True)
    _require(isinstance(result,dict))
    receipt = result.get('execution_receipt') or {}
    output = receipt.get('output') or {}
    _require(receipt.get('capability')=='learning.explain' and receipt.get('action_type')=='ask_tutor'
        and receipt.get('execution_location')=='local_model' and receipt.get('status')=='completed')
    tutor = c.execute('SELECT * FROM tutor_turn WHERE turn_id=? AND child_id=? AND session_id=?',
        (output.get('tutor_turn_id'),row['child_id'],row['session_id'])).fetchone()
    _require(tutor and tutor['client_action_id']==action['tutor_request_action_id']
        and tutor['subject']==target['subject'] and tutor['question']==target['question']
        and tutor['hint_level']==1 and tutor['status']=='completed' and bool(tutor['answer'])
        and tutor['safety_status'] in {'passed','answer_leak_replaced'}
        and row['response_text']==tutor['answer']
        and datetime.fromisoformat(tutor['created_at'])>=datetime.fromisoformat(row['created_at']))
    question_target = json.loads(tutor['output_envelope_json'])['local_teaching_control']['question_target']
    _require(question_target.get('fingerprint')==target['source_fingerprint']
        and question_target.get('binding',{}).get('prompt_id')==target['prompt_id'])
    proof = dict(version='learning_help_receipt_v1',prompt_id=target['prompt_id'],
        tutor_turn_id=tutor['turn_id'],tutor_hash=hashlib.sha256(json.dumps(dict(tutor),sort_keys=True,ensure_ascii=False).encode()).hexdigest())
    if historical:
        _require(result.get('learning_help_receipt')==proof)
    else:
        result['learning_help_receipt']=proof
    return proof
