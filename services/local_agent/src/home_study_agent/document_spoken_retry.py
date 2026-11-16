"""Same-task spoken revisions retain their parent feedback and original voice."""
from contextlib import nullcontext
from copy import deepcopy
import json

from . import document_goal_handoff as goal


def parent_packet(packet, prompt_id):
    item, payload, *rest = packet
    payload = deepcopy(payload)
    work = payload['practice']
    results = work.get('independent_results', [])
    matches = [i for i, r in enumerate(results) if r['prompt_id'] == prompt_id]
    goal.require(len(matches) == 1, 'DOCUMENT_RETRY_PARENT_MISSING')
    work.update(phase='check_feedback', independent_prompt_id=prompt_id,
        independent_results=results[:matches[0]+1])
    return (item, payload, *rest)


def plan(c, packet, session, *, parent_id=None):
    from .document_check_followup import current
    from .document_open_feedback import read
    from .document_revisit import current_session
    current_session(c, session, packet[0]['child_id'])
    goal.require(packet[0]['status'] in {'pending','due'} and packet[1]['practice']['session_id'] == session,
        'DOCUMENT_RETRY_SESSION_CHANGED')
    if parent_id:
        packet = parent_packet(packet, parent_id)
    parent, source, attempt = current(c, packet)
    feedback = read(c, packet)
    assistance = c.execute('SELECT hint_level FROM learning_evidence WHERE evidence_id=? AND child_id=?',
        (attempt['evidence_id'], parent['child_id'])).fetchone()
    goal.require(assistance and type(assistance[0]) is int and 0 <= assistance[0] <= 5,
        'DOCUMENT_RETRY_ASSISTANCE_CHANGED')
    ref = json.loads(parent['request_json'])['document_independent']['binding']
    # A retry retains the original target mapping, but does not pretend that
    # the item is unseen or that the previous response was independent.
    base = {k:v for k,v in ref.items() if k != 'spoken_retry'}
    base.update(status='ready', diagnostic_item_id=source['diagnostic_item_id'],
        item_version=parent['item_version'], track=source['track'], subject=source['subject'],
        spoken_retry=dict(prompt_id=parent['prompt_id'], item_version=parent['item_version'],
            attempt_id=attempt['attempt_id'], evidence_id=attempt['evidence_id'],
            assessment_id=feedback['assessment_id'], review_revision=feedback['review_revision'],
            feedback_version=feedback['feedback_version'], after_feedback=True,
            required_hint_level=max(1, assistance[0])))
    return base


def begin(c, turn, packet, binding):
    from .diagnostic_prompt import DiagnosticPromptService
    from .document_independent_check import PREFIX
    from .document_open_check import bind_before_answer
    goal.require(isinstance(binding, dict) and binding == plan(c, packet, turn['session_id']),
        'DOCUMENT_RETRY_FEEDBACK_CHANGED')

    class TransactionPrompt(DiagnosticPromptService):
        def __init__(self):
            self.database = None

        def _transaction(self):
            return nullcontext(c)

    result = TransactionPrompt().prepare(turn['session_id'], track=binding['track'], subject=binding['subject'],
        preferred_item_id=binding['diagnostic_item_id'], client_action_id=PREFIX+turn['agent_turn_id'])
    prompt = result.get('prompt') or {}
    goal.require(result['status']=='waiting_answer' and prompt.get('item_version')==binding['item_version']
        and prompt.get('diagnostic_item_id')==binding['diagnostic_item_id'], 'DOCUMENT_RETRY_PROMPT_CHANGED')
    ref = dict(turn_id=turn['agent_turn_id'], binding=binding)
    c.execute("UPDATE diagnostic_prompt SET request_json=json_set(request_json,'$.document_independent',json(?)) WHERE prompt_id=?",
        (json.dumps(ref), prompt['prompt_id']))
    bind_before_answer(c, turn, prompt)
    return result, ('还是刚才这道题：'+prompt['item']['prompt']+
        '。请用自己的话完整解释一遍。我会保留前一次回答，把这次作为反馈后的练习，分别核对。')


def validate(c, row, packet, binding):
    retry = binding.get('spoken_retry')
    goal.require(isinstance(retry, dict), 'DOCUMENT_RETRY_BINDING_CHANGED')
    # Monotone row order rejects self/cyclic ancestry before following it.
    ordered = c.execute('''SELECT 1 FROM diagnostic_prompt child JOIN diagnostic_prompt parent
        ON parent.prompt_id=? AND parent.rowid<child.rowid AND parent.child_id=child.child_id
        AND parent.origin_session_id=child.origin_session_id WHERE child.prompt_id=?''',
        (retry.get('prompt_id'), row['prompt_id'])).fetchone()
    goal.require(ordered and binding == plan(c, packet, row['origin_session_id'], parent_id=retry['prompt_id']),
        'DOCUMENT_RETRY_PARENT_CHANGED')
    if row['status'] == 'answered':
        assistance = c.execute('''SELECT e.hint_level FROM diagnostic_attempt a
            JOIN learning_evidence e USING(evidence_id) WHERE a.attempt_id=? AND a.child_id=?''',
            (row['attempt_id'], row['child_id'])).fetchone()
        goal.require(assistance and type(assistance[0]) is int
            and retry['required_hint_level'] <= assistance[0] <= 5, 'DOCUMENT_RETRY_ASSISTANCE_ERASED')
