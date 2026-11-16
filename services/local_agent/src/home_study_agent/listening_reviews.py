"""Listening source readiness is not permission to score an unheard answer."""
import json

from .local_supply import LocalSupplyError
from .pet_listening_requirement import listening_requirement
from .pet_listening_scoring import listening_scoring_item


def listening_review_source_item(connection, child_id, item_id):
    """Return an audited exact-answer source, without claiming any delivery."""
    row = connection.execute('''SELECT kn.*,di.answer_type FROM diagnostic_item di
        JOIN knowledge_node kn USING(knowledge_node_id) WHERE di.diagnostic_item_id=?''', (item_id,)).fetchone()
    from .listening_modality import listening_node
    if not row or not listening_node(row, row['answer_type']):
        return None
    try:
        item = listening_scoring_item(connection, child_id, item_id)
        requirement = listening_requirement(connection, child_id, item_id)
        if not item or requirement['status'] != 'known' or not requirement['version']:
            return None
        rubric = json.loads(item['expected_answer_json'])
        answers = rubric.get('exact_answers')
        if (rubric.get('local_rule') != 'local_exact_v1' or rubric.get('keyword_groups')
                or not isinstance(answers, list) or not answers
                or any(not isinstance(answer, str) or not answer.strip() for answer in answers)):
            return None
        return item
    except (LocalSupplyError, ValueError, TypeError, KeyError):
        return None


def reconcile_answered_listening_reviews(connection, child_id, prompt_id):
    """Schedule only after the original prompt-to-attempt binding is durable in this transaction."""
    if not connection.in_transaction:
        raise ValueError('LISTENING_REVIEW_TRANSACTION_REQUIRED')
    row = connection.execute('''SELECT a.*,e.knowledge_node_id FROM diagnostic_prompt p
        JOIN diagnostic_attempt a ON a.attempt_id=p.attempt_id AND a.child_id=p.child_id
          AND a.session_id=p.origin_session_id AND a.diagnostic_item_id=p.diagnostic_item_id
        JOIN learning_evidence e ON e.evidence_id=a.evidence_id AND e.child_id=a.child_id
          AND e.session_id=a.session_id
        WHERE p.prompt_id=? AND p.child_id=? AND p.status='answered' ''', (prompt_id, child_id)).fetchone()
    if not row or row['correctness'] is None or not listening_review_source_item(
            connection, child_id, row['diagnostic_item_id']):
        return
    from .learning_state import LearningStateService
    if not LearningStateService._qualified_review_source(connection, child_id, row['knowledge_node_id'],
            row['evidence_id'], as_of=row['created_at']):
        return
    args = (connection, child_id, row['knowledge_node_id'], row['diagnostic_item_id'],
            row['evidence_id'], row['created_at'])
    if row['review_task_id']:
        if row['correctness'] < 1:
            LearningStateService._schedule_next_day_review(*args)
    else:
        LearningStateService._schedule_reviews(*args)
