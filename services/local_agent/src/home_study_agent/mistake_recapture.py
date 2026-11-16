"""Bind a new device receipt to the original mistake before replacing its source."""
import json
import re

from .local_supply import digest
from .mistake_capture import InvalidMistakeCaptureCandidateError
from .repository import utc_now
from .mistake_source_revision import latest, snapshot


def original(c, mistake_id, child_id):
    source = snapshot(c, mistake_id, child_id)
    if source:
        return source
    row = c.execute('''SELECT knowledge_node_id,diagnostic_item_id,source_evidence_id,prompt_summary,
        submitted_answer,source_type,source_ref FROM mistake_case
        WHERE mistake_id=? AND child_id=? AND status!='excluded' AND source_type='document_camera' ''',
        (mistake_id, child_id)).fetchone()
    return {'case': dict(row), 'source_unavailable': True} if row else None


def target(c, focus):
    if (not focus or not focus.get('mistake_id') or focus['phase'] in {'paused', 'closed'}
            or focus['state'].get('pending_operation') or focus['state'].get('foundation_hold')):
        return None
    source = original(c, focus['mistake_id'], focus['child_id'])
    return {'mistake_id': focus['mistake_id'], 'source_version': digest(source)} if source else None


def receipt_target(c, session_id, child_id, artifact_id):
    query = '''SELECT t.agent_turn_id,t.action_json,t.created_at,t.tool_status,
        s.status,s.result_json FROM agent_turn t JOIN agent_plan p USING(agent_turn_id)
        JOIN agent_step s USING(plan_id) WHERE t.session_id=? AND t.child_id=?
        AND s.capability_name IN ('learning.capture_question','document.retry_capture')'''
    rows = c.execute(query + ''' AND json_extract(t.action_json,'$.mistake_recapture_target') IS NOT NULL
        AND COALESCE(json_extract(s.result_json,'$.execution_receipt.output.capture_artifact_id'),
                     json_extract(s.result_json,'$.client_bridge_receipt.output.capture_artifact_id'))=?
        ORDER BY t.created_at DESC LIMIT 2''', (session_id, child_id, artifact_id)).fetchall()
    if len(rows) > 1:
        raise InvalidMistakeCaptureCandidateError('同一画面关联了多个重拍请求，需要重新核对来源。')
    for row in rows:
        action = json.loads(row['action_json'])
        result = json.loads(row['result_json'] or '{}')
        receipt = result.get('execution_receipt') or result.get('client_bridge_receipt') or {}
        output = receipt.get('output') or {}
        if output.get('capture_artifact_id') == artifact_id and action.get('mistake_recapture_target'):
            cancelled = c.execute('''SELECT 1 FROM mistake_dialogue_event WHERE session_id=? AND child_id=?
                AND json_extract(result_json,'$.state.cancelled_source_repair.recapture_turn_id')=? LIMIT 1''',
                (session_id, child_id, row['agent_turn_id'])).fetchone()
            if cancelled:
                raise InvalidMistakeCaptureCandidateError('这次重拍更正已经取消，没有重新打开旧画面。')
            if row['status'] != 'completed' or row['tool_status'] != 'completed' or receipt.get('status') != 'completed':
                raise InvalidMistakeCaptureCandidateError('重拍结果还未确认完成，原题记录保持不变。')
            later = c.execute('''SELECT 1 FROM agent_turn WHERE session_id=? AND child_id=? AND created_at>?
                AND json_extract(action_json,'$.type') IN ('request_capture','retry_capture','cancel_capture') LIMIT 1''',
                (session_id, child_id, row['created_at'])).fetchone()
            if later:
                raise InvalidMistakeCaptureCandidateError('重拍已被取消或新请求替代，没有使用晚到的旧画面。')
            return {'turn_id': row['agent_turn_id'], 'target': action['mistake_recapture_target']}
    rows = c.execute(query + ' ORDER BY t.created_at DESC LIMIT 1', (session_id, child_id)).fetchall()
    if rows:
        row = rows[0]
        artifact = c.execute('SELECT created_at FROM capture_artifact WHERE artifact_id=? AND child_id=?',
                             (artifact_id, child_id)).fetchone()
        if (json.loads(row['action_json']).get('mistake_recapture_target') and artifact
                and artifact[0] >= row['created_at'] and row['tool_status'] in {'pending', 'running'}):
            raise InvalidMistakeCaptureCandidateError('重拍回执还在保存，保存后会继续核对原题；不会新增错题。')
    return None


def candidate_source(c, artifact_id, candidate_id, child_id, session_id):
    artifact = c.execute('SELECT * FROM capture_artifact WHERE artifact_id=? AND child_id=? AND session_id=?',
                         (artifact_id, child_id, session_id)).fetchone()
    candidate = c.execute('SELECT * FROM mistake_capture_candidate WHERE candidate_id=? AND artifact_id=? AND child_id=?',
                          (candidate_id, artifact_id, child_id)).fetchone()
    if (not artifact or artifact['artifact_type'] != 'document_ocr'
            or artifact['status'] not in {'completed', 'quality_warning'} or not candidate
            or candidate['status'] != 'pending' or candidate['mistake_id']):
        raise InvalidMistakeCaptureCandidateError('重拍候选已经变化或不属于当前会话，请重新核对。')
    # Include review decisions: changing formula evidence invalidates a pending readback.
    reviews = [dict(r) for r in c.execute('SELECT * FROM ocr_review_item WHERE artifact_id=? AND child_id=? ORDER BY review_id',
                                        (artifact_id, child_id))]
    value = dict(artifact)
    details = json.loads(value['details_json'])
    details.pop('visual_capture', None)
    value['details_json'] = json.dumps(details, sort_keys=True, ensure_ascii=False)
    return {'artifact': value, 'candidate': dict(candidate), 'reviews': reviews}


def begin(dialogue, focus, binding, artifact_id, candidate_id, action_id, payload):
    from .diagnostic_prompt import _TransactionDatabase
    from .mistake_dialogue import MistakeDialogueService
    if not focus:
        raise InvalidMistakeCaptureCandidateError('原错题已不在当前会话，没有新增另一条错题。')
    with dialogue.database.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        if not c.execute("SELECT 1 FROM daily_session WHERE session_id=? AND child_id=? AND state='study' AND status='active'",
                         (focus['session_id'], focus['child_id'])).fetchone():
            raise InvalidMistakeCaptureCandidateError('当前不在原学习会话，没有更新原错题。')
        borrowed = MistakeDialogueService(_TransactionDatabase(c))
        live = borrowed.state(focus['session_id'])
        result = _begin(borrowed, live, binding, artifact_id, candidate_id, action_id, payload)
        c.commit()
        return result


def _begin(dialogue, focus, binding, artifact_id, candidate_id, action_id, payload):
    pending = (focus or {}).get('state', {}).get('question_confirmation') or {}
    if pending.get('superseded_recapture_turn_id') == binding['turn_id']:
        raise InvalidMistakeCaptureCandidateError('已经改为口述核对原题，不使用之前重拍的晚到画面。')
    with dialogue.database.connect() as c:
        if binding['target'] != target(c, focus):
            raise InvalidMistakeCaptureCandidateError('重拍期间原题或作答已变化，没有覆盖当前记录。请重新发起重拍。')
        source = original(c, focus['mistake_id'], focus['child_id'])
        previous = latest(c, focus['mistake_id'], focus['child_id'])
    items = dialogue.capture.prepare(artifact_id, focus['child_id'], action_id + ':prepare')['items']
    candidates = [item for item in items if item['status'] == 'pending']
    if not candidates:
        raise InvalidMistakeCaptureCandidateError('新画面没有完整候选题，原错题仍保留，请重新拍摄。')
    pending = {'mode': 'source_repair', 'mistake_id': focus['mistake_id'], 'question': '',
        'review_id': '', 'version': digest(source), 'base_revision': previous['revision_id'] if previous else None,
        'offer_id': action_id, 'created_at': utc_now(), 'original_answer': source['case']['submitted_answer'],
        'recapture': {**binding, 'artifact_id': artifact_id}}
    focus['state']['question_confirmation'] = pending
    focus['state']['artifact_id'] = artifact_id
    focus['state']['submitted_answer'] = pending['original_answer']
    focus['state'].pop('teaching_requested', None)
    focus['state'].pop('ocr_confirmation_id', None)
    focus['phase'] = 'remediation'
    if candidate_id or len(candidates) == 1:
        choose(dialogue, focus, candidate_id or candidates[0]['candidate_id'])
    return dialogue._save(focus, prompt(focus), action_id, payload)


def prompt(focus):
    pending = focus['state']['question_confirmation']
    if not pending['question']:
        return '重拍看到多道题。刚才那道错题是第几题？我会读回题干和原答案，确认后才更新原错题。'
    return (f"重拍读到的题干是：{pending['question']}。原答案仍是“{pending['original_answer']}”。"
            '这是原来同一道错题，题干准确且原答案没变，对吗？没有确认前不会替换原记录。')


def choose(dialogue, focus, candidate_id):
    from uuid import uuid4
    pending = focus['state']['question_confirmation']
    with dialogue.database.connect() as c:
        source = candidate_source(c, pending['recapture']['artifact_id'], candidate_id,
                                  focus['child_id'], focus['session_id'])
    question = source['candidate']['question_text']
    if not 2 <= len(question) <= 2800:
        raise InvalidMistakeCaptureCandidateError('这道题无法完整读回，请把目标题单独放到画面中重拍。')
    pending.update(question=question, review_id=str(uuid4()), created_at=utc_now())
    focus['state']['question_text'] = question
    pending['recapture'].update(candidate_id=candidate_id, candidate_version=digest(source))


def select_reply(dialogue, focus, text, action_id):
    pending = focus['state'].get('question_confirmation') or {}
    if not pending.get('recapture') or focus['phase'] in {'paused', 'closed'}:
        return None
    # The caller owns spoken corrections; do not trap them behind a missing
    # question-number selection or bypass an answer that is still being saved.
    if (focus.get('mistake_id') and not focus['state'].get('pending_operation')
            and re.match(r'(?:错题题干改成|题干改成|题目是|题目应该是)[，,:：\s]*(.+)', text, re.DOTALL)):
        return None
    compact = re.sub(r'[，。！？、\s]', '', text)
    selected = re.fullmatch(r'(?:我要看|看|选|是)?第?([0-9]+|[一二两三四五六七八九十]{1,3})题', compact)
    if selected:
        from .mistake_dialogue import spoken_question_number
        items = dialogue.capture.list(pending['recapture']['artifact_id'], focus['child_id'])['items']
        number = spoken_question_number(selected[1])
        matches = [x for x in items if x['status'] == 'pending' and re.match(
            rf'^\s*(?:[（(][A-Za-z ]*[)）]\s*)?{number}[.．、)）]', x['question_text'])]
        if len(matches) != 1:
            return dialogue._save(focus, '还不能唯一确定这道题，请把原错题单独放到画面中央再重拍。', action_id, text)
        choose(dialogue, focus, matches[0]['candidate_id'])
        return dialogue._save(focus, prompt(focus), action_id, text)
    if not pending['question'] and not re.search(r'暂停|先停|先不做|等一下|关闭|以后再做|跳过', text):
        return dialogue._save(focus, prompt(focus), action_id, text)
    return None


def verify_pending(c, focus, pending):
    recapture = pending['recapture']
    binding = receipt_target(c, focus['session_id'], focus['child_id'], recapture['artifact_id'])
    if (not binding or binding['turn_id'] != recapture['turn_id'] or binding['target'] != recapture['target']
            or target(c, focus) != recapture['target']):
        raise InvalidMistakeCaptureCandidateError('原题重拍请求已经变化，没有修改原记录。')
    source = candidate_source(c, recapture['artifact_id'], recapture['candidate_id'], focus['child_id'], focus['session_id'])
    if digest(source) != recapture['candidate_version'] or source['candidate']['question_text'] != pending['question']:
        raise InvalidMistakeCaptureCandidateError('重拍题干在读回后发生变化，请重新选题核对。')
    return source
