"""Voice-owned review appointments, separate from qualified retention evidence."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from uuid import uuid4
from zoneinfo import ZoneInfo

from . import document_goal_handoff as goal

VERSION = 'document_review_schedule_v1'
ORIGIN = 'document_revisit_v1'
ZONE = 'Asia/Shanghai'
PHASES = {'awaiting_time', 'time_proposal', 'scheduled', 'review_cancelled'}
PROMPT = (
    '只理解本轮复习时间安排，不回答题目，不执行。输入只有当前原话、复习安排阶段、当前时间和时区。'
    '仅输出JSON对象，包含meaning、quote、time、confidence。quote完整逐字复制原话；confidence为0到1。'
    'meaning可以为propose、confirm、cancel、status、other_request、unclear。'
    'propose是明确约定或更改复习时间，先提出候选而非确认。time只能为'
    '{"kind":"relative","minutes":整数}或{"kind":"calendar","date":"YYYY-MM-DD","time":"HH:mm"}。'
    '相对时间从提供的当前时间算起；明天、后天、星期几按提供的当地日期换算，不猜日期。'
    '孩子只说八点但不清楚早晚、只说放学后却没有准确时刻、多个冲突时间，均用unclear。'
    'confirm只用于明确同意刚才读回的待确认时间，必须phase=time_proposal；闲聊中的好、引用或假设不算同意。'
    'cancel只用于明确取消这道题的复习安排，不取消学习任务。status用于询问已保存的安排。'
    'other_request表示另一个请求，由其它能力处理；不要抢答或猜题。'
    '除propose外time必须null。否定一个时间并明确提出另一个可以propose；只否定不猜新时间。'
    '不输出题干、ID、解释、历史、任何额外字段。'
)


def parse(content, transcript, anchor_at, *, native=False):
    value = json.loads(content)
    goal.require(isinstance(value, dict) and set(value) == {'meaning', 'quote', 'time', 'confidence'}
        and value['quote'] == transcript and ((native and value['confidence'] is None)
        or (not native and type(value['confidence']) in (int, float) and 0 <= value['confidence'] <= 1)), 'REVIEW_TIME_SEMANTICS_INVALID')
    meaning = value['meaning']
    goal.require(meaning in {'propose', 'confirm', 'cancel', 'status', 'other_request', 'unclear'}, 'REVIEW_TIME_MEANING_INVALID')
    due = None
    if meaning == 'propose':
        when = value['time']
        goal.require(isinstance(when, dict), 'REVIEW_TIME_REQUIRED')
        anchor = goal.instant(anchor_at)
        if when.get('kind') == 'relative':
            goal.require(set(when) == {'kind', 'minutes'} and type(when['minutes']) is int
                and 1 <= when['minutes'] <= 90 * 24 * 60, 'REVIEW_TIME_INVALID')
            due = anchor + timedelta(minutes=when['minutes'])
        else:
            goal.require(set(when) == {'kind', 'date', 'time'} and when['kind'] == 'calendar'
                and isinstance(when['date'], str) and isinstance(when['time'], str), 'REVIEW_TIME_INVALID')
            date = datetime.strptime(when['date']+' '+when['time'], '%Y-%m-%d %H:%M')
            goal.require(date.strftime('%Y-%m-%d') == when['date'] and date.strftime('%H:%M') == when['time'], 'REVIEW_TIME_INVALID')
            due = date.replace(tzinfo=ZoneInfo(ZONE))
        if not anchor < due <= anchor + timedelta(days=90):
            meaning, due = 'unclear', None
    else:
        goal.require(value['time'] is None, 'REVIEW_TIME_INVENTED')
    if not native and value['confidence'] < .82:
        meaning, due = 'unclear', None
    return {**value, 'status':'document_review', 'meaning':meaning,
        'time':value['time'] if meaning == 'propose' else None,
        'due_at':due.astimezone(timezone.utc).isoformat() if due else None}


def parse_journal(raw, transcript, anchor):
    if raw.get('request_kind')=='native_document_control_v1':
        from .realtime_native_document_controls import parse_native_journal
        return parse_native_journal('learning.review_schedule',raw,transcript,anchor)
    return parse(json.dumps({k:raw[k] for k in ('meaning','quote','time','confidence')}),transcript,anchor)


def focus(c, session, child, action_id):
    candidate = c.execute('SELECT root_turn_id FROM agent_continuation WHERE session_id=? AND child_id=? ORDER BY rowid DESC LIMIT 1',
        (session, child)).fetchone()
    if not candidate or not goal.owns(c, candidate[0]):
        return None
    try:
        root, state = goal.validate(c, candidate[0], session, incoming_action_id=action_id)
        if state['phase'] not in PHASES or not state.get('tutor_turn_id') or not state.get('review_request_quote'):
            return None
        return dict(root_turn_id=root['agent_turn_id'], revision=state['revision'], phase=state['phase'],
            tutor_turn_id=state['tutor_turn_id'], proposal_hash=goal.digest(state.get('review_time_proposal')),
            commitment_id=state.get('commitment_id'))
    except (ValueError, TypeError, KeyError):
        return None


def assert_focus(c, session, child, action_id, binding):
    goal.require(focus(c, session, child, action_id) == binding, 'REVIEW_TIME_FOCUS_CHANGED')


def validate_voice(c, root, proof):
    from .agent_runtime import AgentRuntimeStore
    turn = c.execute('SELECT * FROM agent_turn WHERE agent_turn_id=?', (proof['turn_id'],)).fetchone()
    goal.require(turn and (turn['session_id'], turn['child_id'], turn['client_action_id'], turn['transcript']) ==
        (root['session_id'], root['child_id'], proof['client_action_id'], proof['transcript']), 'REVIEW_TIME_VOICE_CHANGED')
    action = json.loads(turn['action_json'])
    goal.require(isinstance(action, dict) and action.get('type') == 'control_document_review'
        and action.get('document_review_root') == root['agent_turn_id']
        and action.get('document_review_request') == proof['request_key']
        and action.get('voice_input_id') == proof['voice_input_id'], 'REVIEW_TIME_VOICE_CHANGED')
    voice = AgentRuntimeStore.resolve_captured_voice(c, session_id=root['session_id'], child_id=root['child_id'],
        voice_input_id=proof['voice_input_id'], transcript=proof['transcript'])
    goal.require(AgentRuntimeStore.voice_capture_snapshot(voice) == proof['binding'] == action.get('voice_input_binding'),
        'REVIEW_TIME_VOICE_CHANGED')
    return turn


def persist(orchestrator, session, child, session_state, transcript, key, result, voice):
    from .agent_runtime import AgentPlanDecision, ObjectiveDraft, PlannedStep, action_from_plan
    from .repository import utc_now
    binding = result['binding']['document_review']
    cap = orchestrator.capability_registry.require('learning.review_schedule', session_state)
    response = '我先核对这道题的复习安排，保存后再告诉你结果。'
    decision = AgentPlanDecision(ObjectiveDraft('continue', 'learning', None, (), 'explain'),
        PlannedStep(cap, {}, 'Apply only a recorded current-voice review decision', cap.expected_evidence), response, False, 1)
    with orchestrator.database.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        old = c.execute('SELECT * FROM agent_turn WHERE client_action_id=?', (key,)).fetchone()
        if old:
            orchestrator._assert_action_payload(old, session, transcript)
            orchestrator._assert_replayed_voice(old, voice['voice_input_id'])
            return orchestrator._get_replayed(c, old['agent_turn_id'])
        assert_focus(c, session, child, key, binding)
        row, state = goal.validate(c, binding['root_turn_id'], session, incoming_action_id=key)
        goal.require(voice, 'REVIEW_TIME_VOICE_REQUIRED')
        if not result.get('request_key'):
            request_key = hashlib.sha256(('voice-document-review:'+key).encode()).hexdigest()
            fingerprint = hashlib.sha256(json.dumps([child, session, transcript], ensure_ascii=False).encode()).hexdigest()
            c.execute('''INSERT INTO voice_control_intent_request(request_key,child_id,session_id,fingerprint,
                binding_json,status,decision_json,created_at,completed_at) VALUES(?,?,?,?,?,'completed',?,?,?)''',
                (request_key,child,session,fingerprint,json.dumps(result['binding']),
                 json.dumps({'status':'unavailable','reason':result.get('reason','CONTROL_MODEL_FAILED')}),utc_now(),utc_now()))
            result = {**result, 'request_key':request_key}
        turn = str(uuid4())
        action = {**action_from_plan(decision, transcript, None), **goal.LOCAL,
            'document_review_root':row['agent_turn_id'], 'document_review_request':result['request_key']}
        manifest = {'document_review_request':result['request_key'], 'document_review_focus':binding}
        orchestrator._freeze_local_voice(c, session, child, transcript, voice, action, manifest)
        c.execute('''INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,
            action_json,orchestrator_version,created_at,client_action_id) VALUES(?,?,?,?,?,?,?,?,?,?)''',
            (turn, child, session, transcript, VERSION, response, json.dumps(action, ensure_ascii=False), VERSION, utc_now(), key))
        metadata = orchestrator.agent_runtime.persist_plan(c, child_id=child, session_id=session, agent_turn_id=turn,
            client_action_id=key, decision=decision, context_manifest=manifest)
        c.execute("UPDATE agent_turn SET action_json=json_set(action_json,'$.agent_runtime',json(?)) WHERE agent_turn_id=?",
            (json.dumps(metadata), turn))
        state['time_voice'] = dict(turn_id=turn, client_action_id=key, transcript=transcript,
            voice_input_id=voice['voice_input_id'], binding=action['voice_input_binding'], request_key=result['request_key'])
        state['time_turn_id'] = turn
        goal.store(c, row, state)
        c.commit()
        return orchestrator._get(c, turn)


def time_label(value):
    local = goal.instant(value).astimezone(ZoneInfo(ZONE))
    return f'{local.year}年{local.month}月{local.day}日{local.hour}点{local.minute:02d}分（北京时间）'


def execute(database, turn_id):
    from .repository import utc_now
    with database.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        turn = c.execute('SELECT * FROM agent_turn WHERE agent_turn_id=?', (turn_id,)).fetchone()
        goal.require(turn, 'REVIEW_TIME_TURN_NOT_FOUND')
        action = json.loads(turn['action_json'])
        request = c.execute('SELECT * FROM voice_control_intent_request WHERE request_key=? AND child_id=? AND session_id=?',
            (action.get('document_review_request'), turn['child_id'], turn['session_id'])).fetchone()
        from .realtime_native_document_controls import require_request,latest_voice as native_latest_voice
        native=require_request(c,turn,'learning.review_schedule',request)
        goal.require(request and request['status'] == 'completed', 'REVIEW_TIME_REQUEST_UNAVAILABLE')
        fingerprint = hashlib.sha256(json.dumps([turn['child_id'], turn['session_id'], turn['transcript']], ensure_ascii=False).encode()).hexdigest()
        goal.require(request['fingerprint'] == fingerprint, 'REVIEW_TIME_REQUEST_CHANGED')
        if request['execution_receipt_json']:
            saved = json.loads(request['execution_receipt_json'])
            goal.require(saved.get('agent_turn_id') == turn_id, 'REVIEW_TIME_REQUEST_CHANGED')
            return saved
        row, state = goal.validate(c, action.get('document_review_root'), turn['session_id'])
        goal.require(state['time_voice']['turn_id'] == turn_id, 'REVIEW_TIME_FOCUS_CHANGED')
        latest_voice = native_latest_voice(c,turn['session_id'],turn['child_id']) if native else c.execute("""SELECT voice_input_id FROM voice_input
            WHERE session_id=? AND child_id=? AND purpose='agent_command' AND status='completed'
            ORDER BY rowid DESC LIMIT 1""", (turn['session_id'], turn['child_id'])).fetchone()
        goal.require(latest_voice and latest_voice[0] == state['time_voice']['voice_input_id'], 'REVIEW_TIME_NEWER_VOICE')
        binding = json.loads(request['binding_json'])
        assert_focus(c, turn['session_id'], turn['child_id'], turn['client_action_id'], binding['document_review'])
        raw = json.loads(request['decision_json'])
        if raw.get('status') == 'unavailable':
            value = {'meaning':'unclear'}
        else:
            value = parse_journal(raw,turn['transcript'],binding['review_clock'])
            goal.require(value == raw, 'REVIEW_TIME_DECISION_CHANGED')
        now = utc_now()
        if state.get('commitment_id'):
            old = c.execute('SELECT * FROM agent_commitment WHERE commitment_id=?', (state['commitment_id'],)).fetchone()
            payload = json.loads(old['payload_json']) if old else None
            goal.require(old and (old['child_id'], old['objective_id'], old['due_at']) ==
                (row['child_id'], row['objective_id'], state.get('due_at')) and old['commitment_type'] == 'reminder'
                and old['status'] in {'pending','due','cancelled'} and isinstance(payload, dict)
                and payload.get('root_turn_id') == row['agent_turn_id'] and payload.get('origin') == ORIGIN
                and all(payload.get(key) == state.get(key) for key in
                    ('artifact_id','artifact_hash','confirmation_id','tutor_turn_id','tutor_hash'))
                and all(payload.get(key) is value for key, value in goal.LOCAL.items()), 'REVIEW_TIME_COMMITMENT_CHANGED')
        meaning = value['meaning']
        response = '还没有确定具体的复习时间。你想哪一天、上午还是下午几点再看这道题？'
        if raw.get('status') == 'unavailable':
            response = '这次时间理解没有成功，原题和已有安排没有更改。等语音理解恢复后，我们再确认复习时间。'
        if meaning == 'propose' and value['due_at'] and goal.instant(value['due_at']) > goal.instant(now):
            response = f'你希望在{time_label(value["due_at"])}再看这道题，对吗？确认前，原安排不变。'
            state['review_time_proposal'] = dict(due_at=value['due_at'], timezone=ZONE, quote=turn['transcript'],
                turn_id=turn_id, readback=response, created_at=now, voice=state['time_voice'])
            state['phase'] = 'time_proposal'
        elif meaning == 'confirm' and state['phase'] == 'time_proposal':
            proposal = state['review_time_proposal']
            validate_voice(c, row, proposal['voice'])
            proposal_request = c.execute('SELECT decision_json FROM voice_control_intent_request WHERE request_key=?',
                (proposal['voice']['request_key'],)).fetchone()
            goal.require(proposal_request and json.loads(proposal_request[0]).get('due_at') == proposal['due_at'],
                'REVIEW_TIME_PROPOSAL_CHANGED')
            prior = c.execute('''SELECT s.status,s.result_json FROM agent_step s JOIN agent_plan p USING(plan_id)
                WHERE p.agent_turn_id=? AND s.step_index=0 AND s.capability_name='learning.review_schedule' ''', (proposal['turn_id'],)).fetchone()
            goal.require(prior and prior['status'] == 'completed'
                and json.loads(prior['result_json'])['response_text'] == proposal['readback'], 'REVIEW_TIME_READBACK_NOT_SAVED')
            if goal.instant(proposal['due_at']) <= goal.instant(now):
                response = '刚才的候选时间已经过去，没有新增或改动提醒。请告诉我一个新的时间。'
            else:
                cid = state.get('commitment_id') or str(uuid4())
                payload = dict(origin=ORIGIN, root_turn_id=row['agent_turn_id'], artifact_id=state['artifact_id'],
                    artifact_hash=state['artifact_hash'], confirmation_id=state['confirmation_id'], tutor_turn_id=state['tutor_turn_id'],
                    tutor_hash=state['tutor_hash'], timezone=ZONE, scheduling_turn_id=turn_id, **goal.LOCAL)
                c.execute('''INSERT INTO agent_commitment(commitment_id,objective_id,child_id,commitment_type,due_at,status,payload_json,created_at,client_action_id)
                    VALUES(?,?,?,'reminder',?,'pending',?,?,?) ON CONFLICT(commitment_id) DO UPDATE SET
                    due_at=excluded.due_at,status='pending',payload_json=excluded.payload_json,completed_at=NULL''',
                    (cid,row['objective_id'],row['child_id'],proposal['due_at'],json.dumps(payload,ensure_ascii=False),now,ORIGIN+':'+row['agent_turn_id']))
                state.update(commitment_id=cid, due_at=proposal['due_at'], phase='scheduled')
                state.setdefault('review_schedule_history', []).append(dict(operation='schedule', turn_id=turn_id, due_at=proposal['due_at'], at=now))
                state.pop('review_time_proposal', None)
                response = f'已保存：{time_label(state["due_at"])}再看这道题。这是复习安排，不代表已经掌握。'
        elif meaning == 'cancel':
            if state.get('commitment_id'):
                c.execute("UPDATE agent_commitment SET status='cancelled' WHERE commitment_id=? AND child_id=? AND objective_id=?",
                    (state['commitment_id'], row['child_id'], row['objective_id']))
            state.update(phase='review_cancelled')
            state.pop('review_time_proposal', None)
            state.setdefault('review_schedule_history', []).append(dict(operation='cancel',turn_id=turn_id,at=now))
            response = '这道题的复习安排已取消，原题和讲解仍保留，没有结束其它学习任务。'
        elif meaning == 'status':
            response = ('目前保存的复习时间是'+time_label(state['due_at'])+'。' if state['phase']=='scheduled' else
                state['review_time_proposal']['readback'] if state['phase']=='time_proposal' else
                '这道题目前没有已确认的复习时间，原题和讲解都还在。')
        if meaning in {'propose', 'confirm', 'cancel'}:
            # The child's explicit scheduling response closes only the original
            # teaching wait, not another task or the learning objective.
            c.execute("""UPDATE agent_plan SET status='completed',updated_at=?
                WHERE agent_turn_id=? AND child_id=? AND session_id=? AND status='waiting_child'
                AND EXISTS(SELECT 1 FROM agent_step s WHERE s.plan_id=agent_plan.plan_id
                    AND s.step_index=0 AND s.status='completed')""",
                (now,state['teaching_turn_id'],row['child_id'],row['session_id']))
        state['revision'] += 1
        goal.store(c, row, state)
        result = dict(agent_turn_id=turn_id, response_text=response, result=dict(status=state['phase'], root_turn_id=row['agent_turn_id'],
            revision=state['revision'], commitment_id=state.get('commitment_id'), due_at=state.get('due_at'), **goal.LOCAL))
        c.execute('UPDATE voice_control_intent_request SET execution_receipt_json=? WHERE request_key=?',
            (json.dumps(result,ensure_ascii=False),request['request_key']))
        c.commit()
        return result
