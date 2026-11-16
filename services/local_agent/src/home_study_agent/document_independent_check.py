"""Local source-bound handoff from an original question to a new diagnostic.

The model chooses only the intent. Source identity, target and score authority
come from current host records; an ordinary explanation is not a mistake label.
"""
from contextlib import nullcontext
import json

from . import document_goal_handoff as goal
from .diagnostic_prompt import DiagnosticPromptService, _version
from .learning_obligations import question_identity, qualified_exact_score
from .mistake_book import MistakeBookService

PREFIX = 'document-independent:'
VERSION = 'document_independent_check_v1'


def original_version(state):
    return goal.digest({k: state[k] for k in ('root_fingerprint', 'artifact_hash', 'confirmation_hash', 'tutor_hash')})


def _qualified(c, child, item_id):
    item = DiagnosticPromptService._item(c, child, item_id)
    if not item or item.get('stimulus_text') or item.get('stimulus_asset'):
        return None
    try:
        if item.get('answer_type') == 'open_response':
            from .local_supply import validate_item
            from .open_response_profile import profile_for_item
            owned = validate_item(c, child, item_id)
            if not owned['source_ref'].startswith('parent_approved_specialized:'):
                return None
            node = c.execute('SELECT * FROM knowledge_node WHERE knowledge_node_id=?',
                (owned['knowledge_node_id'],)).fetchone()
            profile_for_item(owned, dict(node))
            return item
        answers = json.loads(item['expected_answer_json']).get('exact_answers')
        if not isinstance(answers, list) or not answers or not isinstance(answers[0], str):
            return None
        proof = qualified_exact_score(c, child, item_id, answers[0])
        return item if proof and proof[1] == 1 else None
    except (ValueError, TypeError, KeyError):
        return None


def plan(c, packet, session, *, exclude_prompt=None):
    from .diagnostic_history_identity import historical_identity, HistoryIdentityUnavailable
    item, payload, root, state, confirmed, teacher = packet
    goal.require(item['status'] in {'pending', 'due'} and state['phase'] == 'scheduled', 'DOCUMENT_CHECK_REVIEW_REVOKED')
    child = item['child_id']
    base = {'version': VERSION, 'commitment_id': item['commitment_id'], 'child_id': child,
        'session_id': session, 'original_version': original_version(state), **goal.LOCAL}
    if c.execute("SELECT 1 FROM diagnostic_prompt WHERE origin_session_id=? AND status IN ('waiting_answer','answer_saved') AND (? IS NULL OR prompt_id!=?)",
                 (session, exclude_prompt, exclude_prompt)).fetchone():
        return {**base, 'status': 'pending_question'}
    from .document_source_review import reviewed_source
    try:
        reviewed, proof = reviewed_source(c, packet)
    except (ValueError, KeyError, TypeError):
        return {**base, 'status':'mapping_required'}
    matches = []
    text = ' '.join(confirmed['confirmed_text'].split())
    for row in c.execute('SELECT diagnostic_item_id,prompt FROM diagnostic_item WHERE status=\'active\''):
        if ' '.join(row['prompt'].split()) == text:
            source = _qualified(c, child, row['diagnostic_item_id'])
            if source and source['subject'] == teacher['subject']:
                matches.append(source)
    if reviewed:
        matches = [reviewed]
        base['mapping_review'] = proof
    elif not matches:
        from .document_target_hypothesis import resolved
        try:
            proposed,hypothesis=resolved(c,packet)
        except (ValueError,TypeError,KeyError,IndexError):
            return {**base,'status':'mapping_required'}
        if proposed:
            matches=[proposed]
            base['mapping_hypothesis']=hypothesis
    nodes = {r['knowledge_node_id'] for r in matches}
    if len(nodes) != 1:
        return {**base, 'status': 'mapping_ambiguous' if nodes else 'mapping_required'}
    source = min(matches, key=lambda row: row['diagnostic_item_id'])
    base.update(mapping_item_id=source['diagnostic_item_id'], mapping_version=_version(source),
        knowledge_node_id=source['knowledge_node_id'])
    identities, seen = MistakeBookService._question_history(c, child)
    original_identity = question_identity(source, connection=c)
    shown = c.execute('SELECT * FROM diagnostic_prompt WHERE child_id=? AND (? IS NULL OR prompt_id!=?)',
        (child, exclude_prompt, exclude_prompt)).fetchall()
    for row in c.execute('SELECT diagnostic_item_id FROM diagnostic_item WHERE knowledge_node_id=? AND status=\'active\' ORDER BY difficulty,diagnostic_item_id',
                         (source['knowledge_node_id'],)):
        candidate = _qualified(c, child, row[0])
        if not candidate:
            continue
        if source['answer_type'] == 'open_response':
            original_rubric = json.loads(source['expected_answer_json'])
            candidate_rubric = json.loads(candidate['expected_answer_json'])
            if candidate['answer_type'] != 'open_response' or any(
                    candidate_rubric.get(key) != original_rubric.get(key) for key in ('task_family','response_mode')):
                continue
        identity = identities.get(row[0])
        if not identity or identity == original_identity or identity in seen:
            continue
        try:
            if any(historical_identity(c, child, previous, identity) == identity for previous in shown):
                continue
        except HistoryIdentityUnavailable:
            return {**base, 'status': 'history_unavailable'}
        return {**base, 'status': 'ready', 'diagnostic_item_id': row[0],
            'item_version': _version(candidate), 'track': candidate['track'], 'subject': candidate['subject']}
    return {**base, 'status': 'new_material_required'}


def begin(c, turn, packet, binding):
    goal.require(plan(c, packet, turn['session_id']) == binding, 'DOCUMENT_CHECK_SOURCE_CHANGED')
    if binding['status']=='mapping_required':
        from .document_target_hypothesis import offer
        proposal_message=offer(c,packet)
        if proposal_message:
            return None,proposal_message
    if binding['status']=='new_material_required':
        from .document_check_supply import deliver
        c.execute('SAVEPOINT document_check_supply')
        supplied=deliver(c,turn['child_id'],binding['mapping_item_id'],PREFIX+'supply:'+turn['agent_turn_id'])
        refreshed=plan(c,packet,turn['session_id']) if supplied else None
        if refreshed and refreshed['status']=='ready' and refreshed['diagnostic_item_id']==supplied:
            binding.clear()
            binding.update(refreshed)
            c.execute("UPDATE agent_turn SET action_json=json_set(action_json,'$.document_independent_check',json(?)) WHERE agent_turn_id=?",
                (json.dumps(binding),turn['agent_turn_id']))
        else:
            c.execute('ROLLBACK TO document_check_supply')
        c.execute('RELEASE document_check_supply')
    messages = {
        'mapping_required': '我保留了换新题的请求。还需要核对原题对应的知识点与可靠题源，暂不随便换题，也不把原题标成错题。',
        'mapping_ambiguous': '这道原题对应了不止一个学习目标，需要先核对目标，不能随便选一个来判断掌握。',
        'pending_question': '还有一道题正在等待作答或核对结果，我不会用新题覆盖它。',
        'history_unavailable': '有一条旧题记录还不能核对清楚，暂时不能确认新题是否做过。原题和你的请求已经保留。',
        'new_material_required': '这个学习目标暂时没有经过验证、又没做过或听过讲解的新题。换题请求已保留，不用重复原题冒充掌握。',
    }
    if binding['status'] != 'ready':
        return None, messages[binding['status']]

    class TransactionPrompt(DiagnosticPromptService):
        def __init__(self):
            self.database = None

        def _transaction(self):
            return nullcontext(c)

    result = TransactionPrompt().prepare(turn['session_id'], track=binding['track'], subject=binding['subject'],
        preferred_item_id=binding['diagnostic_item_id'], client_action_id=PREFIX + turn['agent_turn_id'])
    goal.require(result['status'] == 'waiting_answer' and result['prompt']['item_version'] == binding['item_version'],
        'DOCUMENT_CHECK_PROMPT_CHANGED')
    ref = {'turn_id': turn['agent_turn_id'], 'binding': binding}
    c.execute("UPDATE diagnostic_prompt SET request_json=json_set(request_json,'$.document_independent',json(?)) WHERE prompt_id=?",
        (json.dumps(ref), result['prompt']['prompt_id']))
    from .document_spoken_supplement import bind_independent_teaching_context
    teaching=bind_independent_teaching_context(c,packet,result['prompt']['prompt_id'])
    if teaching:
        result['teaching_context']=teaching
    if result['prompt']['item']['answer_type'] == 'open_response':
        from .document_open_check import bind_before_answer
        bind_before_answer(c, turn, result['prompt'])
    introduction=('原题目标尚未最终确认，先用这道新题检查你选择的这一项。' if binding.get('mapping_hypothesis')
        else '我们换一道题，检查能否自己运用刚才的知识。')
    return result, introduction + result['prompt']['item']['prompt'] + '请独立回答，不确定也可以说。'


def replayed(saved):
    """Project a historical receipt without reactivating its old question."""
    result = saved.get('result') or {}
    if not result.get('diagnostic_prompt'):
        if result.get('spoken_conversation'):
            return {**saved, 'replayed':True, 'recorded_receipt':True, 'replay_audio_allowed':False, 'evidence':[]}
        return saved
    result = {k: v for k, v in result.items() if k != 'diagnostic_prompt'}
    flags = {'replayed': True, 'recorded_receipt': True, 'replay_audio_allowed': False}
    return {**saved, **flags, 'evidence': [],
        'response_text': ('同题重答记录已经保存，不重复开启重答，请继续当前作答。' if result.get('practice_kind')=='spoken_retry'
            else '换题记录已经保存，请核对原题目的当前作答状态，不重新出题。'),
        'result': {**result, **flags, 'status': 'independent_check_recorded', **goal.LOCAL}}


def source_reason(c, row):
    """Check the original at read and scoring admission, never silently retarget."""
    from . import document_revisit as revisit
    from .agent_runtime import AgentRuntimeStore
    try:
        request = json.loads(row['request_json'])
        goal.require(isinstance(request, dict), 'DOCUMENT_CHECK_REQUEST_INVALID')
        ref = request.get('document_independent')
        if not row['prepare_action_id'].startswith(PREFIX) and ref is None:
            return None
        goal.require(isinstance(ref, dict) and set(ref) == {'turn_id', 'binding'}, 'DOCUMENT_CHECK_BINDING_MISSING')
        turn = c.execute('SELECT * FROM agent_turn WHERE agent_turn_id=?', (ref['turn_id'],)).fetchone()
        goal.require(turn and (turn['session_id'], turn['child_id']) ==
            (row['origin_session_id'], row['child_id']) and row['prepare_action_id'] == PREFIX + turn['agent_turn_id'],
            'DOCUMENT_CHECK_OWNER_CHANGED')
        # The prompt and journal commit together, before the outer step receipt.
        # A failed acknowledgement must not discard this already durable work.
        step = c.execute('''SELECT s.status,p.status AS plan_status FROM agent_plan p
            JOIN agent_step s USING(plan_id) WHERE p.agent_turn_id=?
            AND s.step_index=0 AND s.capability_name='learning.review_revisit' ''', (turn['agent_turn_id'],)).fetchone()
        goal.require(step and step['status'] in {'running', 'failed', 'completed'}
            and step['plan_status'] in {'running', 'failed', 'completed', 'waiting_child'}, 'DOCUMENT_CHECK_STEP_CHANGED')
        action = json.loads(turn['action_json'])
        goal.require(action.get('document_independent_check') == ref['binding'], 'DOCUMENT_CHECK_BINDING_CHANGED')
        voice = AgentRuntimeStore.resolve_captured_voice(c, session_id=turn['session_id'], child_id=turn['child_id'],
            voice_input_id=action.get('voice_input_id'), transcript=turn['transcript'])
        goal.require(action.get('voice_input_binding') == AgentRuntimeStore.voice_capture_snapshot(voice), 'DOCUMENT_CHECK_VOICE_CHANGED')
        journal = c.execute('SELECT execution_receipt_json FROM voice_control_intent_request WHERE request_key=?',
            (action.get('document_revisit_request'),)).fetchone()
        receipt = json.loads(journal[0]) if journal and journal[0] else {}
        prompt = receipt.get('result', {}).get('diagnostic_prompt', {}).get('prompt', {})
        goal.require(receipt.get('agent_turn_id') == turn['agent_turn_id'] and prompt.get('prompt_id') == row['prompt_id']
            and prompt.get('item_version') == row['item_version'], 'DOCUMENT_CHECK_RECEIPT_CHANGED')
        revisit.current_session(c, row['origin_session_id'], row['child_id'])
        packet = revisit.retained(c, ref['binding']['commitment_id'], row['child_id'])
        if ref['binding'].get('spoken_retry'):
            from .document_spoken_retry import validate
            validate(c, row, packet, ref['binding'])
        if row['status'] == 'answered':
            # A saved answer has legitimately consumed the unseen question.
            # Validate its receipt, not whether we would select it again today.
            from .document_check_followup import current
            from .document_spoken_retry import parent_packet
            recorded, _, _ = current(c, parent_packet(packet,row['prompt_id']))
            goal.require(recorded['prompt_id'] == row['prompt_id'], 'DOCUMENT_CHECK_RESULT_CHANGED')
        elif not ref['binding'].get('spoken_retry'):
            goal.require(plan(c, packet, row['origin_session_id'], exclude_prompt=row['prompt_id']) == ref['binding'],
                'DOCUMENT_CHECK_SOURCE_CHANGED')
        return None
    except (ValueError, TypeError, KeyError):
        return 'document_independent_source_changed'
