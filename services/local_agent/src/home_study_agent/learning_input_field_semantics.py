"""Current-utterance-only semantics for an exact unconfirmed field question."""
import hashlib
import json
import re

VERSION = 'literal_input_field_semantics_v2'
PROMPT = (
    '只理解孩子对一个待确认字段的本轮回答，不执行任何操作，不生成作业内容。'
    '输入只有本轮transcript和pending_input_field，不提供历史内容，禁止补全或猜测历史。'
    '输出且仅输出field、meaning、quote、literal_quote、minutes、confidence。'
    'field照抄待询问字段。quote必须完整逐字复制本轮原话。confidence是0到1。'
    'ownership字段：明确表示这是自己要做/要交/被布置的作业用meaning=own；'
    '明确别人的或不是自己的用not_mine；仅是/对/确认、含糊、引用或假设用unclear。'
    '不得用某个固定句式代替语义；例如班里布置且自己也得写、今晚自己要交，均可明确归属。'
    'ownership的literal_quote和minutes必须null。'
    'expected_minutes字段：孩子明确给出自己预计用时才用meaning=estimate；'
    'literal_quote必须是本轮可逐字定位的一个带分钟单位的数值片段，minutes是其整数值，不能推算或猜测；'
    '大约/大概等估计措辞仍是孩子的估计，不是完成证据。否定、假设、多个冲突时长或不清楚用unclear。'
    'unclear的literal_quote和minutes必须null。所有其它字段禁止，不返回标题、科目、ID、理由或历史。'
    '若本轮明确是在请求另一个操作或查询而非回答此字段，用meaning=other_request，literal_quote和minutes必须null；'
    '例如询问原稿是否保存、要求继续提交原稿、要求更正识别。此标记不授权任何操作，由原控制通道另行校验。'
    '单说是/确认、给出原题答案、含糊或混合要求仍为unclear，不能作为other_request。'
)


def snapshot(connection, binding):
    from .learning_input_intake import missing
    row = connection.execute('SELECT state,readback_version,proposal_json FROM learning_input_proposal WHERE request_id=?',
                             (binding['request_id'],)).fetchone()
    if not row or row['state'] != 'clarification_required' or row['readback_version'] != binding['readback_version']:
        return None
    proposal = json.loads(row['proposal_json'])
    if not proposal.get('intake') or not proposal.get('target'):
        return None
    field = missing(proposal)
    return {**binding, 'field': field} if field in {'ownership', 'expected_minutes'} else None


def current(connection, child_id, session_id, action_id, binding):
    row = connection.execute('SELECT pending_json FROM learning_input_arrival WHERE child_id=? AND session_id=? AND client_action_id=?',
                             (child_id, session_id, action_id)).fetchone()
    saved = json.loads(row[0] or 'null') if row else None
    return bool(saved and snapshot(connection, saved) == binding)


def parse(content, transcript, field):
    from .learning_input_intake import estimate
    value = json.loads(content)
    if (not isinstance(value, dict) or set(value) != {'field', 'meaning', 'quote', 'literal_quote', 'minutes', 'confidence'}
            or value['field'] != field or value['quote'] != transcript
            or type(value['confidence']) not in (float, int) or not 0 <= value['confidence'] <= 1):
        raise ValueError('INPUT_FIELD_SEMANTICS_INVALID')
    meaning = value['meaning']
    if meaning not in ({'own', 'not_mine', 'unclear', 'other_request'} if field == 'ownership' else {'estimate', 'unclear', 'other_request'}):
        raise ValueError('INPUT_FIELD_MEANING_INVALID')
    if field == 'ownership' or meaning in {'unclear', 'other_request'}:
        if value['literal_quote'] is not None or value['minutes'] is not None:
            raise ValueError('INPUT_FIELD_INVENTED_VALUE')
    else:
        literal = value['literal_quote']
        if (not isinstance(literal, str) or not literal or literal not in transcript
                or type(value['minutes']) is not int or estimate(literal) != value['minutes']):
            raise ValueError('INPUT_FIELD_ESTIMATE_NOT_LITERAL')
    uncertain = (value['confidence'] < .82 or bool(re.search(r'[“”「」『』"]|(?:假如|假设|如果|他说|她说)', transcript)))
    if meaning in {'own', 'other_request'}:
        uncertain = uncertain or bool(re.fullmatch(r'[\s，,。！!]*(?:是|是的|对|确认|好|好的|嗯|yes|yeah|yep|ok|okay)[\s，,。！!]*', transcript, re.I))
    if meaning == 'own':
        uncertain = uncertain or bool(re.search(r'(?:不是我|不属于我|同桌的|别人的)', transcript))
    if meaning == 'estimate':
        uncertain = uncertain or bool(re.search(r'(?:不是|不要|不对|别猜|也许不是|或者|还是)', transcript))
        uncertain = uncertain or bool(re.search(r'[0-9一二两三四五六七八九十百零](?:分钟|分)?(?:到|至|[-~～])[0-9一二两三四五六七八九十百零]', transcript))
    if uncertain:
        value.update(meaning='unclear', literal_quote=None, minutes=None)
    return {'status': 'field_reply', **value}


def recorded(connection, arrival, field, key):
    if not key or key not in {hashlib.sha256((prefix + arrival['client_action_id']).encode()).hexdigest()
                              for prefix in ('voice-control:', 'voice-input-field:')}:
        return None
    row = connection.execute('SELECT * FROM voice_control_intent_request WHERE request_key=? AND child_id=? AND session_id=?',
                             (key, arrival['child_id'], arrival['session_id'])).fetchone()
    fingerprint = hashlib.sha256(json.dumps([arrival['child_id'], arrival['session_id'], arrival['utterance']], ensure_ascii=False).encode()).hexdigest()
    if not row or row['status'] != 'completed' or row['fingerprint'] != fingerprint:
        return None
    pending = json.loads(arrival['pending_json'] or 'null')
    if not pending or json.loads(row['binding_json']).get('input_slot') != {**pending, 'field': field}:
        return None
    decision = json.loads(row['decision_json'])
    if decision.get('status') != 'field_reply':
        return None
    try:
        return parse(json.dumps({k: v for k, v in decision.items() if k != 'status'}), arrival['utterance'], field)
    except (ValueError, TypeError):
        return None


def for_turn(connection, turn_id, arrival, field):
    row = connection.execute('SELECT context_manifest_json FROM agent_plan WHERE agent_turn_id=?', (turn_id,)).fetchone()
    key = json.loads(row[0]).get('input_field_request') if row else None
    if key:
        from .voice_control_intent import VoiceControlIntent
        receipt = connection.execute('SELECT binding_json FROM voice_control_intent_request WHERE request_key=?', (key,)).fetchone()
        consent = VoiceControlIntent.text_consent_version(connection)
        if (not receipt or json.loads(receipt[0]).get('input_field_text_consent') != (list(consent) if consent else None)
                or not VoiceControlIntent.scope_valid(connection, arrival['session_id'], arrival['child_id'], arrival['client_action_id'])):
            return None, key
    return recorded(connection, arrival, field, key), key
