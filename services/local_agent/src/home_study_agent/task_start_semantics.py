"""Public intent slots; private task identity is resolved only by the host."""
import hashlib
import json
import re


SUBJECTS = {'math': '数学', 'english': '英语', 'chinese': '语文', 'physics': '物理',
            'chemistry': '化学', 'biology': '生物', 'history': '历史',
            'geography': '地理', 'politics': '道德与法治'}
KINDS = {'review', 'written', 'oral', 'reading', 'listening', 'writing'}
SOURCES = {'school_homework', 'mistake_review'}
NEGATIVE = re.compile(r'不|别|没|稍后|以后|明天|后天|晚点|等会|待会|放一放|放着|之后|暂缓')

PROMPT = (
    'task.start可表示明确开始下一项或指定的待办任务，不限固定口令。'
    '本分支必须额外给start_selector，结构仅包含subject、task_kind、source_type、target_quote。'
    'subject仅math|english|chinese|physics|chemistry|biology|history|geography|politics或null；'
    'task_kind仅review|written|oral|reading|listening|writing或null；'
    'source_type仅school_homework|mistake_review或null。'
    'target_quote是本轮完整原话中现在真正要求执行的连续片段，必须逐字存在且保留完整肯定请求，不能删除片段内的否定。'
    '例如数学先不做，我想先做英语作业，目标片段是我想先做英语作业，对应english、written、school_homework。'
    '只填原话明确表达的维度，不能猜测本地任务、题目、名称或ID。'
    '先做A后做B只表示现在开始A，不创建或重排B；若无法区分现在目标，选mixed/unknown。'
    '否定、引用、介绍功能和未指定对象的那份不是执行授权。其他分支禁止start_selector。'
)

PLAN_PROMPT = (
    'task.start的arguments必须给start_selector对象，且不含其他键。对象必须包含subject、task_kind、source_type、target_quote四个键。'
    'subject枚举math|english|chinese|physics|chemistry|biology|history|geography|politics或null；'
    'task_kind枚举review|written|oral|reading|listening|writing或null；source_type仅school_homework|mistake_review或null。'
    'target_quote逐字复制本轮原话中现在真正要执行的完整肯定请求片段，不能生成、剪掉片段内部的否定或猜本地ID。'
    '明确说开始学习、继续做题或开始下一项时前三项全null，由本机选择待办；仅询问计划或开始方式不算执行。'
    '指明科目/类型时填对应值。先不做数学作业而做英语错题时只选现在的英语错题，不能宣称数学任务已经暂停或重排。'
)


def _verified_plan_call(c, session_id, child_id, call_id):
    row = c.execute('''SELECT * FROM model_call WHERE call_id=? AND child_id=? AND session_id=?
        AND purpose IN ('agent_plan','agent_plan_repair') AND status='completed'
        AND verification_status='verified' AND safety_status='passed' ''',
        (call_id, child_id, session_id)).fetchone()
    if not row or row['purpose'] == 'agent_plan':
        return row
    try:
        manifest = json.loads(row['context_manifest_json'])
        repair = manifest['planner_repair']
        if (not isinstance(repair, dict)
                or set(repair) != {'source_call_id', 'validation_error', 'attempt'}
                or type(repair['attempt']) is not int or repair['attempt'] != 1
                or not isinstance(repair['source_call_id'], str)
                or not isinstance(repair['validation_error'], str)
                or not repair['validation_error'].strip()):
            return None
        source = c.execute('''SELECT * FROM model_call WHERE call_id=? AND child_id=? AND session_id=?
            AND purpose='agent_plan' AND status='failed' AND error_code='MODEL_INVALID_AGENT_PLAN'
            AND verification_status='rejected' AND safety_status='degraded' ''',
            (repair['source_call_id'], child_id, session_id)).fetchone()
        # A format repair is one verified replacement of this exact failed
        # request. Its later local selection receipt is checked separately.
        base = {key: value for key, value in manifest.items()
                if key not in {'planner_repair', 'task_start_selection'}}
        if (not source or row['prompt_version'] != source['prompt_version'] + '.repair1'
                or row['policy_version'] != source['policy_version']
                or base != json.loads(source['context_manifest_json'])):
            return None
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
    return row


def record_plan_selection(c, session_id, child_id, arrival_id, call_id, transcript, selected):
    from .voice_control_intent import VoiceControlIntent
    from .task_start_binding import TaskStartBindingError
    validate(selected, transcript)
    row = c.execute('''SELECT utterance,client_action_id FROM learning_input_arrival
        WHERE arrival_id=? AND session_id=? AND child_id=?''', (arrival_id, session_id, child_id)).fetchone()
    if (not row or row['utterance'] != transcript
            or not VoiceControlIntent.scope_valid(c, session_id, child_id, row['client_action_id'])):
        raise TaskStartBindingError('TASK_START_PLAN_SCOPE_CHANGED')
    if not _verified_plan_call(c, session_id, child_id, call_id):
        raise TaskStartBindingError('TASK_START_PLAN_RECEIPT_CHANGED')
    receipt = {'version': 'task_start_planner_v1', 'arrival_id': arrival_id,
        'utterance_sha256': hashlib.sha256(transcript.encode()).hexdigest(), 'selector': selected,
        'consent_version': list(VoiceControlIntent.text_consent_version(c) or ())}
    updated = c.execute('''UPDATE model_call SET context_manifest_json=json_set(context_manifest_json,
        '$.task_start_selection',json(?)) WHERE call_id=? AND child_id=? AND session_id=?
        AND purpose IN ('agent_plan','agent_plan_repair') AND status='completed' AND verification_status='verified' AND safety_status='passed'
        AND json_extract(context_manifest_json,'$.task_start_selection') IS NULL''',
        (json.dumps(receipt, ensure_ascii=False), call_id, child_id, session_id)).rowcount
    if updated != 1:
        raise TaskStartBindingError('TASK_START_PLAN_RECEIPT_CHANGED')


def verify_plan_selection(c, session_id, child_id, action_id, call_id, transcript, selected):
    from .voice_control_intent import VoiceControlIntent
    from .task_start_binding import TaskStartBindingError
    validate(selected, transcript)
    row = _verified_plan_call(c, session_id, child_id, call_id)
    arrival = c.execute('''SELECT arrival_id,utterance FROM learning_input_arrival
        WHERE session_id=? AND child_id=? AND client_action_id=?''', (session_id, child_id, action_id)).fetchone()
    try:
        receipt = json.loads(row['context_manifest_json']).get('task_start_selection') if row else None
    except (ValueError, TypeError, AttributeError):
        raise TaskStartBindingError('TASK_START_PLAN_RECEIPT_CHANGED') from None
    expected = {'version': 'task_start_planner_v1', 'arrival_id': arrival['arrival_id'] if arrival else None,
        'utterance_sha256': hashlib.sha256(transcript.encode()).hexdigest(), 'selector': selected,
        'consent_version': list(VoiceControlIntent.text_consent_version(c) or ())}
    if not arrival or arrival['utterance'] != transcript or receipt != expected:
        raise TaskStartBindingError('TASK_START_PLAN_RECEIPT_CHANGED')
    return receipt


def denied(text):
    # Polite alternatives ask permission; they are not negative imperatives.
    text = re.sub(r'能不能|可不可以|行不行|能否|是否可以', '可以', text)
    return bool(NEGATIVE.search(text))


def validate(selector, transcript):
    if (not isinstance(transcript, str) or not isinstance(selector, dict)
            or set(selector) != {'subject', 'task_kind', 'source_type', 'target_quote'}):
        raise ValueError('CONTROL_START_SELECTOR_INVALID')
    for key, values in (('subject', SUBJECTS), ('task_kind', KINDS), ('source_type', SOURCES)):
        value = selector[key]
        if value is not None and (not isinstance(value, str) or value not in values):
            raise ValueError('CONTROL_START_SELECTOR_INVALID')
    quote = selector['target_quote']
    if (not isinstance(quote, str) or not quote.strip() or quote != quote.strip()
            or len(quote) > 500 or transcript.count(quote) != 1):
        raise ValueError('CONTROL_START_SELECTOR_INVALID')
    quote_in_clause = quote.rstrip('，,。；;！!')
    if not quote_in_clause:
        raise ValueError('CONTROL_START_SELECTOR_INVALID')
    # A substring inside a negative clause cannot manufacture an affirmative.
    clauses = [part.strip() for part in re.split(r'[，,。；;！!]', transcript) if part.strip()]
    containing = [part for part in clauses if quote_in_clause in part]
    if len(containing) != 1 or denied(containing[0]):
        raise ValueError('CONTROL_START_NOT_REQUESTED')
    if re.search(r'他说|她说|老师说|妈妈说|爸爸说|假如|如果|假设|什么意思|是否支持|能否支持|取消|撤回|算了|还没决定', transcript):
        raise ValueError('CONTROL_START_NOT_REQUESTED')
    if re.search(r'[“”「」『』"]', transcript):
        raise ValueError('CONTROL_START_NOT_REQUESTED')
    for part in clauses:
        later = (clauses.index(part) > clauses.index(containing[0])
                 and re.match(r'^(?:然后|之后|再)(?:我|我们)?(?:再)?(?:做|学|练|处理|开始)', part))
        if part != containing[0] and not denied(part) and not later:
            raise ValueError('CONTROL_START_MULTIPLE_TARGETS')
        if part != containing[0]:
            subjects = {code for code, name in SUBJECTS.items() if name in part}
            sources = {code for word, code in (('作业', 'school_homework'), ('错题', 'mistake_review')) if word in part}
            different_subject = bool(subjects and selector['subject'] and selector['subject'] not in subjects)
            different_source = bool(sources and selector['source_type'] and selector['source_type'] not in sources)
            if not different_subject and not different_source:
                raise ValueError('CONTROL_START_NEGATION_SCOPE_UNCLEAR')
    # Empty slots mean "next", not permission to ignore a named alternative.
    mentioned = [name for name in SUBJECTS.values() if name in quote]
    if mentioned and mentioned != [SUBJECTS.get(selector['subject'])]:
        raise ValueError('CONTROL_START_SELECTOR_CONFLICT')
    for word, source in (('错题', 'mistake_review'), ('作业', 'school_homework')):
        if word in quote and selector['source_type'] != source:
            raise ValueError('CONTROL_START_SELECTOR_CONFLICT')
    for word, kind in (('听力', 'listening'), ('阅读', 'reading'), ('口语', 'oral'), ('写作', 'writing')):
        if word in quote and selector['task_kind'] != kind:
            raise ValueError('CONTROL_START_SELECTOR_CONFLICT')
    if not any(selector[key] for key in ('subject', 'task_kind', 'source_type')):
        explicit_generic_start = (re.search(r'(?:开始|继续)(?:今天的|接下来的)?(?:学习|任务|做题|练习)', quote)
                                  and not re.search(r'怎么|如何|为什么|什么意思|吗|呢|[?？]', quote))
        if not re.search(r'下一|接下来', quote) and not explicit_generic_start:
            raise ValueError('CONTROL_START_TARGET_REQUIRED')
    return selector
