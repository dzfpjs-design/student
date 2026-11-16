"""Local readback and version-bound approval of a corrected mistake question."""
import json
import re
from datetime import datetime

from .paper_formula_review import for_mistake
from .tutor_ocr_confirmation import OcrTutorConfirmationService
from .repository import utc_now

KEY = 'question_confirmation'


def relative_teaching_request(text):
    value = re.sub(r'[，,。！!？?\s]', '', text)
    return bool(re.fullmatch(r'(?:(?:这道题|这题|刚才这题))?(?:我)?(?:还是|还|仍然)?(?:不会|没懂|不明白|没有听懂|没听懂)'
        r'(?:(?:能|可以|请)?(?:再|给我|帮我)*(?:讲|讲解|解释)(?:一下|一遍|这道题|这题)?(?:吗|吧)?)?', value))


def relative_diagnosis_reply(text):
    """An explicit uncertainty about this error answers the current why-question."""
    value = re.sub(r'[，,。！!？?\s]', '', text)
    return bool(re.fullmatch(r'(?:这道题|这题|刚才这题)?(?:我)?(?:还是|还|也)?'
        r'(?:不知道|不清楚|不明白|没弄懂|没弄明白)(?:自己)?'
        r'(?:为什么(?:会)?(?:做)?错(?:了)?|(?:哪里|哪儿|哪一步)错(?:了)?|错(?:在)?(?:哪里|哪儿|哪一步))', value))


def offer(focus, review, action_id, database=None):
    question = review['question']
    if len(question) > 3800:
        focus['state'].pop(KEY, None)
        return '这道题较长，当前还不能完整读回核对。我保留了更正内容，不会截掉题目直接讲解。'
    previous = focus['state'].get(KEY)
    voice_capture_rowid_boundary = None
    if database is not None:
        with database.connect() as connection:
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='voice_capture_binding'"
            ).fetchone()
            if table:
                voice_capture_rowid_boundary = connection.execute(
                    'SELECT COALESCE(MAX(rowid), 0) FROM voice_capture_binding'
                ).fetchone()[0]
    value = {'mistake_id': focus['mistake_id'], 'review_id': review['review_id'],
             'version': review['version'], 'question': question, 'offer_id': action_id, 'created_at': utc_now(),
             'voice_capture_rowid_boundary': voice_capture_rowid_boundary}
    if (previous and previous.get('created_at') and previous.get('offer_id') == action_id
            and all(previous.get(k) == value[k] for k in ('mistake_id', 'review_id', 'version', 'question'))):
        value = previous
    else:
        focus['state'].pop('teaching_turn_id', None)
        focus['state'].pop('ocr_confirmation_id', None)
    focus['state'][KEY] = value
    return f'我先核对更正后的题目：{question}\n这是不是你要讲的那道题？如果读得正确，告诉我，我就接着讲；不对的地方请指出来。'


def accepts(text, semantic=False):
    value = re.sub(r'[，,。！!\s]', '', text)
    if re.search(r'[?？“”"「」]|不|没读对|读错|错了|还要改|别|如果|假如|可能|也许|吗|是不是|是否|好像|应该|举例|(?:妈妈|爸爸|老师|他|她|别人)(?:刚才)?说', value):
        return False
    if semantic and len(value) >= 2:
        return True
    return bool(re.fullmatch(r'(?:嗯)?(?:没错|对|是的|对的)?(?:这次)?(?:你)?(?:读的|题干|题目)(?:是)?(?:对的|正确|没错|准确)(?:(?:就|请|可以|开始|继续|给我)*(?:讲|讲解)(?:这道|这题|这道题)?(?:吧)?)?', value)
        or re.fullmatch(r'(?:没错|是的|对的|确认)(?:就|请|可以|开始|继续|给我)*(?:讲|讲解)(?:这道|这题|这道题)?(?:吧)?', value))


def bind_action(focus, action, transcript, *, revision_semantics=None):
    pending = (focus or {}).get('state', {}).get(KEY)
    from .mistake_source_revision import accepts_joint
    from .mistake_repair_cancel import requested
    from .mistake_revision_semantics import pending_pair, matches, affirmative
    if (pending and pending.get('mode') == 'source_repair' and focus.get('phase') == 'remediation'
            and not focus['state'].get('pending_operation') and not focus['state'].get('foundation_hold')
            and requested(transcript) and action.get('type') in {'none', 'continue_mistake_dialogue', 'ask_tutor'}):
        return {'type': 'continue_mistake_dialogue', 'utterance': transcript,
                'source': 'mistake_question_voice_cancellation', KEY: pending,
                'local_only': True, 'cloud_transmission_allowed': False}
    if pending_pair(focus) and action.get('type') == 'continue_mistake_dialogue' and matches(revision_semantics, pending, transcript):
        confirmed = revision_semantics['decision'] == 'both' and affirmative(transcript)
        return {'type': 'continue_mistake_dialogue', 'utterance': transcript,
                'source': 'mistake_question_voice_confirmation' if confirmed else 'mistake_question_joint_clarification',
                KEY: pending, 'revision_semantics': revision_semantics,
                'local_only': True, 'cloud_transmission_allowed': False}
    if (pending and 'replacement_answer' in pending and focus.get('phase') == 'remediation'
            and action.get('type') in {'confirm_ocr_text', 'continue_mistake_dialogue', 'none', 'ask_tutor'}
            and not accepts_joint(transcript) and accepts(transcript, action.get('type') == 'confirm_ocr_text')):
        return {'type': 'continue_mistake_dialogue', 'utterance': transcript,
                'source': 'mistake_question_joint_clarification', KEY: pending,
                'local_only': True, 'cloud_transmission_allowed': False}
    if (not pending or focus.get('phase') != 'remediation'
            or action.get('source') in {'cloud_planner_unavailable', 'content_free_control_clarification',
                'local_prompt_guard', 'pending_question_conflict', 'pending_answer_clarification', 'playback_overlap_clarification'}
            or action.get('type') not in {'confirm_ocr_text', 'continue_mistake_dialogue', 'none', 'ask_tutor'}
            or not (accepts_joint(transcript) if 'replacement_answer' in pending
                    else accepts(transcript, action.get('type') == 'confirm_ocr_text'))):
        return None
    return {'type': 'continue_mistake_dialogue', 'utterance': transcript,
            'source': 'mistake_question_voice_confirmation', KEY: pending,
            'local_only': True, 'cloud_transmission_allowed': False}


def approve(database, focus, text, action_id, authorized_turn_id):
    pending = focus['state'].get(KEY)
    if pending and pending.get('mode') == 'source_repair':
        from .mistake_source_revision import approve as approve_repair
        return approve_repair(database, focus, text, action_id, authorized_turn_id)
    if not pending or not authorized_turn_id:
        return False
    with database.connect() as c:
        turn = c.execute('SELECT * FROM agent_turn WHERE agent_turn_id=? AND session_id=? AND child_id=?',
            (authorized_turn_id, focus['session_id'], focus['child_id'])).fetchone()
        action = json.loads(turn['action_json']) if turn else {}
        live = c.execute('SELECT mistake_id,state_json FROM mistake_dialogue WHERE session_id=? AND child_id=?',
                         (focus['session_id'], focus['child_id'])).fetchone()
        current = for_mistake(c, focus['mistake_id'], focus['child_id'])
        running = c.execute('''SELECT s.arguments_json FROM agent_step s JOIN agent_plan p USING(plan_id)
            WHERE p.agent_turn_id=? AND p.child_id=? AND p.session_id=? AND p.status='running'
            AND s.status='running' AND s.capability_name='mistake.dialogue' ''',
            (authorized_turn_id, focus['child_id'], focus['session_id'])).fetchall()
        saved_confirmation = c.execute('SELECT formula_review_json FROM tutor_ocr_confirmation WHERE client_action_id=? AND revoked_at IS NULL',
            ('mistake-voice-confirm:' + authorized_turn_id,)).fetchone()
        recovery = bool(turn and turn['tool_status'] == 'failed' and current and saved_confirmation
            and json.loads(saved_confirmation[0] or 'null') == {k: current.get(k) for k in ('review_id', 'version')})
        approval_checks = {
            'turn': bool(turn),
            'transcript': bool(turn and turn['transcript'] == text),
            'tool_error': bool(turn and (not turn['tool_error_code'] or recovery)),
            'tool_status': bool(turn and (turn['tool_status'] in {'pending', 'running', 'completed'} or recovery)),
            'running': len(running) == 1,
            'running_utterance': bool(running and json.loads(running[0][0]).get('utterance') == text),
            'source': action.get('source') == 'mistake_question_voice_confirmation',
            'pending': action.get(KEY) == pending,
            'utterance': action.get('utterance') == text,
            'live': bool(live and live['mistake_id'] == focus['mistake_id']),
            'live_pending': bool(live and json.loads(live['state_json']).get(KEY) == pending),
            'current': bool(current and current['status'] == 'confirmed'),
            'current_binding': bool(current and not any(
                current.get(k) != pending.get(k) for k in ('review_id', 'version', 'question'))),
        }
        if not all(approval_checks.values()):
            return False
        if action.get('voice_input_id'):
            from .agent_runtime import AgentRuntimeStore
            captured = AgentRuntimeStore.resolve_captured_voice(c, session_id=focus['session_id'], child_id=focus['child_id'],
                voice_input_id=action['voice_input_id'], transcript=text)
            recorded = c.execute('SELECT rowid,created_at FROM voice_capture_binding WHERE voice_input_id=?',
                                 (action['voice_input_id'],)).fetchone()
            boundary = pending.get('voice_capture_rowid_boundary')
            from .voice_capture_binding import capture_started_at
            onset = capture_started_at(c, action['voice_input_id'])
            follows_offer = bool(recorded and onset and pending.get('created_at')
                and datetime.fromisoformat(onset) >= datetime.fromisoformat(pending['created_at'])
                and (boundary is None or int(recorded['rowid']) > int(boundary)))
            if (AgentRuntimeStore.voice_capture_snapshot(captured) != action.get('voice_input_binding')
                    or not follows_offer):
                return False
        source = c.execute('SELECT source_ref FROM mistake_case WHERE mistake_id=? AND child_id=?',
                           (focus['mistake_id'], focus['child_id'])).fetchone()[0]
    confirmation = OcrTutorConfirmationService(database).confirm(focus['session_id'], source.partition('#')[0],
        current['question'], 'manual_correction', 'mistake-voice-confirm:' + authorized_turn_id,
        expected_formula_binding={k: current[k] for k in ('review_id', 'version')})
    focus['state'].pop(KEY, None)
    focus['state']['teaching_requested'] = True
    focus['state']['question_review_status'] = 'confirmed'
    focus['state']['question_text'] = current['question']
    focus['state']['ocr_confirmation_id'] = confirmation['confirmation_id']
    return True
