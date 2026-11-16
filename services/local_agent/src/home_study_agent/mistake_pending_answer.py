"""Resuming saved work is a control request, never a second answer."""
import json
import re

from .local_supply import digest


def binding(focus):
    operation = (focus or {}).get('state', {}).get('pending_operation')
    if not focus or not operation:
        return None
    return {'mistake_id': focus['mistake_id'], 'operation_id': operation['action_id'],
            'operation_version': digest(operation)}


def explicit_resume(text):
    return re.sub(r'[，。！？、\s]', '', text) in {'重试', '继续', '继续错题', '继续订正', '继续刚才的题'}


def permits_resume(text):
    # Declining a fresh answer is not declining processing of the saved one.
    # Exempt only a complete re-answer clause, never arbitrary text before a comma:
    # a comma may be ASR hesitation in "不要，继续处理".
    control = re.sub(
        r'(?:不用|不需要|不必|不要|别)(?:再)?(?:(?:让|叫|要求)?我)?(?:再)?'
        r'(?:重答|重新(?:回答|作答)|重复(?:回答|作答)|(?:回?答|作答)(?:一遍|一次))'
        r'(?=$|[\s，,。.!！?？;；])', '', text)
    return not re.search(
        r'[“”「」『』"]|(?:老师|他|她|妈妈|爸爸)说|假如|假设|如果|什么意思|还没决定'
        r'|(?:先别|先不|不要|不用|不想|不需要|不必|暂(?:时)?不|不是要|别再|别'
        r'|不(?=[\s，,。.!！?？;；]*(?:处理|重试|继续|恢复|接着)))'
        r'[\s\S]*?(?:处理|重试|继续|恢复|接着)'
        r'|(?:取消|撤回).{0,8}(?:刚才|处理|回答|答案)'
        r"|\b(?:if|don't|do not|never)\b", control, re.I)


def explicit_withdraw(text):
    value = re.sub(r'[，,。！!\s]', '', text)
    return bool(re.fullmatch(r'(?:请)?(?:撤回|撤销|取消)(?:刚才的|刚才那份|那份|已保存的)(?:回答|答案)', value))


def explicit_replacement(text):
    if not permits_control(text, 'replace'):
        return None
    match = re.fullmatch(
        r'(?:请)?(?:把)?刚才(?:的|那份)?(?:回答|答案)'
        r'(?:改成|改为|换成|替换成|更正为|应该是)[，,:：\s]*(.+?)[。！!\s]*',
        text.strip(), re.DOTALL)
    return match[1].strip() if match and match[1].strip() else None


def replacement_grounded(text, answer):
    if (not isinstance(answer, str) or not answer.strip() or len(answer) > 4000
            or answer != answer.strip() or answer not in text
            or not permits_control(text, 'replace')):
        return False
    markers = re.finditer(r'(?:改成|改为|换成|替换成|更正为|应该是)[，,:：\s]*', text)
    return any(text.startswith(answer, marker.end()) for marker in markers)


def permits_control(text, control):
    if control == 'resume':
        return permits_resume(text)
    if control == 'replace':
        return not re.search(
            r'[“”「」『』"]|(?:老师|他|她|妈妈|爸爸|哥哥|姐姐|弟弟|妹妹|同学|朋友|别人|有人)说'
            r'|假如|假设|如果|什么意思|还没决定|[?？]'
            r'|(?:不要|不用|不需要|不必|不想|不能|不是要|没打算|不应该|先别|暂时不|甭|别)'
            r'[^，,。.!！?？;；]{0,18}(?:改成|改为|换成|替换成|更正为|应该是)'
            r"|\b(?:if|don't|do not|never)\b", text, re.I)
    if control != 'withdraw':
        return False
    return not re.search(
        r'[“”「」『』"]|(?:老师|他|她|妈妈|爸爸|哥哥|姐姐|弟弟|妹妹|同学|朋友|别人|有人)说'
        r'|假如|假设|如果|什么意思|还没决定|[?？]'
        r'|(?:不要|不用|不需要|不必|先别|暂时不|甭|不|别)[^，,。.!！?？;；]*(?:撤回|撤销|取消)'
        r'|(?:先不|先别|不要|不用|甭|不|别)[\s，,。…]*(?:(?:等一下|那个)[\s，,。…]*)?(?:撤回|撤销|取消)'
        r"|\b(?:if|don't|do not|never)\b", text, re.I)


def record(c, call_id, pending, text, control='resume', replacement_answer=None):
    if control not in {'resume', 'withdraw', 'replace'}:
        raise ValueError('unknown pending answer control')
    if control == 'replace' and not replacement_grounded(text, replacement_answer):
        raise ValueError('pending answer replacement is not grounded')
    path = '$.mistake_pending_' + control
    receipt_input = ([pending, text, control, replacement_answer]
                     if control == 'replace' else [pending, text, control])
    receipt = {'version': 1, 'binding_hash': digest(receipt_input)}
    updated = c.execute('''UPDATE model_call SET context_manifest_json=json_set(context_manifest_json,
        ?,json(?)) WHERE call_id=? AND purpose='agent_plan'
        AND status='completed' AND verification_status='verified' AND safety_status='passed'
        AND json_extract(context_manifest_json,?) IS NULL''',
        (path, json.dumps(receipt), call_id, path)).rowcount
    if updated != 1:
        raise ValueError('pending answer model receipt unavailable')


def authorized(database, focus, text, turn_id, *, control='resume', replacement_answer=None):
    if (not permits_control(text, control)
            or control == 'replace' and not replacement_grounded(text, replacement_answer)):
        return False
    if not turn_id:
        return ((control == 'resume' and explicit_resume(text))
                or (control == 'replace' and explicit_replacement(text) == replacement_answer))
    from .agent_runtime import InvalidAgentPlanError, AgentRuntimeStore
    with database.connect() as c:
        row = c.execute('''SELECT t.transcript,t.action_json,p.context_manifest_json,s.arguments_json
            FROM agent_turn t JOIN agent_plan p USING(agent_turn_id) JOIN agent_step s USING(plan_id)
            WHERE t.agent_turn_id=? AND t.child_id=? AND t.session_id=? AND p.status='running'
            AND s.status='running' AND s.capability_name='mistake.dialogue' ''',
            (turn_id, focus['child_id'], focus['session_id'])).fetchall()
        if len(row) != 1:
            return False
        row = row[0]
        action, manifest, arguments = (json.loads(row[k]) for k in ('action_json', 'context_manifest_json', 'arguments_json'))
        if row['transcript'] != text or arguments.get('utterance') != text:
            return False
        if action.get('pending_answer_operation') != binding(focus):
            raise InvalidAgentPlanError('PENDING_MISTAKE_ANSWER_CHANGED')
        if action.get('voice_input_id'):
            captured = AgentRuntimeStore.resolve_captured_voice(c, session_id=focus['session_id'], child_id=focus['child_id'],
                voice_input_id=action['voice_input_id'], transcript=text)
            if AgentRuntimeStore.voice_capture_snapshot(captured) != action.get('voice_input_binding'):
                raise InvalidAgentPlanError('PENDING_MISTAKE_VOICE_CHANGED')
        explicit = (explicit_resume(text) if control == 'resume' else
                    explicit_withdraw(text) if control == 'withdraw' else
                    explicit_replacement(text) == replacement_answer)
        if explicit:
            return True
        expected_arguments = {'utterance', 'pending_answer_control'}
        if control == 'replace':
            expected_arguments.add('replacement_answer')
        if (set(arguments) != expected_arguments or arguments['pending_answer_control'] != control
                or control == 'replace' and arguments.get('replacement_answer') != replacement_answer
                or manifest.get('pending_answer_operation') != binding(focus)):
            return False
        model = c.execute('''SELECT context_manifest_json FROM model_call WHERE call_id=? AND child_id=? AND session_id=?
            AND purpose='agent_plan' AND status='completed' AND verification_status='verified' AND safety_status='passed' ''',
            (manifest.get('pending_answer_control_model_call_id'), focus['child_id'], focus['session_id'])).fetchone()
        try:
            receipt = json.loads(model[0]).get('mistake_pending_' + control) if model else None
        except (TypeError, ValueError, AttributeError):
            return False
        receipt_input = ([binding(focus), text, control, replacement_answer]
                         if control == 'replace' else [binding(focus), text, control])
        return receipt == {'version': 1, 'binding_hash': digest(receipt_input)}
