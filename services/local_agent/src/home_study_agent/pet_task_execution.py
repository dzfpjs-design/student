"""Exact generic PET task identity, never media delivery or scoring authority."""
from contextlib import nullcontext
from datetime import datetime
import json

from .diagnostic_prompt import DiagnosticPromptService, _ITEM_FIELDS, _version
from .local_supply import LocalSupplyError, digest, validate_item
from .specialized_program import SpecializedProgramService


VERSION = 'pet_question_binding_v1'
REVIEW_VERSION = 'pet_listening_review_binding_v1'
FIELDS = {'version', 'child_id', 'diagnostic_item_id', 'item_version', 'knowledge_node_id',
          'source_ref', 'skill', 'source_version'}
VOCABULARY_PREFIX = 'pet-vocabulary-due:'


class PetTaskExecutionError(ValueError):
    pass


class PetListeningResourceError(PetTaskExecutionError):
    pass


def is_generic_pet_task(c, item):
    if item['source_type'] == 'due_review':
        return is_listening_review_task(c, item)
    if item['source_type'] != 'pet_daily' or str(item['source_ref'] or '').startswith(VOCABULARY_PREFIX):
        return False
    if 'task_type' in item.keys() and item['task_type'] == 'writing':
        return False
    row = c.execute('SELECT answer_type FROM diagnostic_item WHERE diagnostic_item_id=?', (item['source_ref'],)).fetchone()
    return not row or row['answer_type'] != 'writing'


def is_listening_review_task(c, item):
    if item['source_type'] != 'due_review':
        return False
    # An already frozen listening task cannot fall back to an unguarded timer
    # when its source has subsequently been removed or reclassified.
    if 'plan_item_id' in item.keys() and c.execute('''SELECT 1 FROM plan_constraint_decision
        WHERE plan_id=(SELECT plan_id FROM plan_item WHERE plan_item_id=?) AND obligation_type='due_review' AND source_ref=?
        AND json_extract(metadata_json,'$.planning_evidence.pet_question_binding.version')=?''',
        (item['plan_item_id'], item['source_ref'], REVIEW_VERSION)).fetchone():
        return True
    row = c.execute('''SELECT n.*,d.answer_type FROM review_task r JOIN diagnostic_item d USING(diagnostic_item_id)
        JOIN knowledge_node n ON n.knowledge_node_id=d.knowledge_node_id
        WHERE r.review_task_id=? AND r.knowledge_node_id=d.knowledge_node_id''', (item['source_ref'],)).fetchone()
    from .listening_modality import listening_node
    return bool(row and listening_node(row, row['answer_type']))


def freeze_listening_review(c, child_id, review_task_id):
    from .repository import StudyRepository, PlanInputError
    from .listening_reviews import listening_review_source_item
    try:
        review = StudyRepository._validate_review_source(c, review_task_id, child_id,
            allowed_statuses=('pending', 'completed'))
    except PlanInputError as error:
        raise PetTaskExecutionError('pet_review_source_unavailable') from error
    if listening_review_source_item(c, child_id, review['diagnostic_item_id']) is None:
        raise PetTaskExecutionError('pet_review_source_unavailable')
    question = freeze_pet_question(c, child_id, review['diagnostic_item_id'])
    identity = {key: review[key] for key in ('review_task_id', 'child_id', 'knowledge_node_id',
        'diagnostic_item_id', 'source_evidence_id', 'interval_label', 'due_at', 'created_at')}
    return {**question, 'version': REVIEW_VERSION, 'review_task_id': review_task_id,
        'review_source_version': digest(identity)}


def freeze_pet_question(c, child_id, item_id):
    """Freeze source-qualified inventory, not an assumed successful answer."""
    try:
        validate_item(c, child_id, item_id)
        item = DiagnosticPromptService._item(c, child_id, item_id)
        from .listening_modality import school_listening_node
        node = c.execute('SELECT * FROM knowledge_node WHERE knowledge_node_id=?', (item['knowledge_node_id'],)).fetchone() if item else None
        school = bool(node and school_listening_node(node) and item['answer_type'] == 'listening_text')
        if not item or (item['track'] != 'pet' and not school) or item['answer_type'] == 'writing':
            raise PetTaskExecutionError('pet_original_unavailable')
        domain = SpecializedProgramService._mastery_domain('pet', item)
        skill = 'listening' if school else domain.split('.')[1] if domain else None
        if skill not in {'listening', 'reading', 'speaking'}:
            raise PetTaskExecutionError('pet_skill_unavailable')
        node = c.execute('SELECT * FROM knowledge_node WHERE knowledge_node_id=?', (item['knowledge_node_id'],)).fetchone()
        return {'version': VERSION, 'child_id': child_id, 'diagnostic_item_id': item_id,
            'item_version': _version(item), 'knowledge_node_id': item['knowledge_node_id'],
            'source_ref': item['source_ref'], 'skill': skill,
            'source_version': digest({'item': item, 'node': dict(node)})}
    except (LocalSupplyError, KeyError, TypeError) as error:
        raise PetTaskExecutionError('pet_original_unavailable') from error


def revalidate_pet_question(c, child_id, binding):
    if isinstance(binding, dict) and binding.get('version') == REVIEW_VERSION:
        if (set(binding) != FIELDS | {'review_task_id', 'review_source_version'}
                or binding.get('child_id') != child_id
                or freeze_listening_review(c, child_id, binding['review_task_id']) != binding):
            raise PetTaskExecutionError('pet_review_source_changed')
        return binding
    if (not isinstance(binding, dict) or set(binding) != FIELDS or binding['version'] != VERSION
            or binding['child_id'] != child_id):
        raise PetTaskExecutionError('pet_question_binding_invalid')
    current = freeze_pet_question(c, child_id, binding['diagnostic_item_id'])
    if current != binding:
        raise PetTaskExecutionError('pet_question_source_changed')
    return current


def resolve_pet_task_execution(c, session_id, plan_item_id, *, completed_receipt=False):
    """Read the scheduled item/decision from one snapshot; never choose another."""
    if not c.in_transaction:
        c.execute('BEGIN')
        try:
            return resolve_pet_task_execution(c, session_id, plan_item_id, completed_receipt=completed_receipt)
        finally:
            c.rollback()
    result = dict(status='blocked', reason_code='pet_source_snapshot_missing',
        reason='PET原题或计划快照不可用，请核对原计划后再安排，不自动更换题目。',
        diagnostic_item=None, diagnostic_item_id=None, item_version=None, binding=None,
        local_only=True, cloud_transmission_allowed=False, mastery_claim_allowed=False)
    plan = c.execute('''SELECT pi.*,dp.child_id,dp.session_id,dp.status AS plan_status,
        ds.child_id AS session_child,ds.state,ds.status AS session_status,cp.status AS child_status
        FROM plan_item pi JOIN daily_plan dp USING(plan_id)
        JOIN daily_session ds ON ds.session_id=dp.session_id JOIN child_profile cp ON cp.child_id=dp.child_id
        WHERE pi.plan_item_id=? AND dp.session_id=?''', (plan_item_id, session_id)).fetchone()
    if (not plan or plan['child_id'] != plan['session_child'] or plan['child_status'] != 'active'
            or plan['session_status'] != 'active' or plan['state'] in {'evolving', 'completed'}
            or plan['plan_status'] != 'active' or plan['status'] not in (
                {'active', 'paused', 'completed'} if completed_receipt else {'pending', 'active', 'paused'})
            or not is_generic_pet_task(c, plan)):
        return {**result, 'reason_code': 'pet_plan_scope_unavailable'}
    records = c.execute('''SELECT metadata_json,allocated_minutes FROM plan_constraint_decision
        WHERE plan_id=? AND obligation_type=? AND source_ref=? AND decision='scheduled' ''',
        (plan['plan_id'], plan['source_type'], plan['source_ref'])).fetchall()
    bindings = []
    try:
        for row in records:
            metadata = json.loads(row['metadata_json'])
            if metadata.get('plan_item_id') != plan_item_id:
                continue
            if row['allocated_minutes'] != plan['expected_minutes'] or row['allocated_minutes'] <= 0:
                raise PetTaskExecutionError('pet_plan_allocation_changed')
            bindings.append(metadata['planning_evidence']['pet_question_binding'])
        if not bindings:
            return result
        binding = bindings[0]
        if any(value != binding for value in bindings[1:]):
            raise PetTaskExecutionError('pet_plan_binding_ambiguous')
        revalidate_pet_question(c, plan['child_id'], binding)
        review = plan['source_type'] == 'due_review'
        if (review and (binding.get('version') != REVIEW_VERSION or binding.get('review_task_id') != plan['source_ref'])):
            raise PetTaskExecutionError('pet_review_binding_missing')
        if not review and binding['diagnostic_item_id'] != plan['source_ref']:
            raise PetTaskExecutionError('pet_plan_source_changed')
        item = DiagnosticPromptService._item(c, plan['child_id'], binding['diagnostic_item_id'])
        expected_task = 'review' if review else 'speaking' if binding['skill'] in {'speaking', 'listening'} else 'reading'
        if item['subject'] != plan['subject'] or plan['task_type'] != expected_task:
            raise PetTaskExecutionError('pet_plan_task_changed')
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        return {**result, 'reason_code': str(error) if isinstance(error, PetTaskExecutionError) else 'pet_plan_binding_invalid'}
    return {**result, 'status': 'ready', 'reason_code': 'pet_original_binding_current',
        'diagnostic_item': {key: item[key] for key in _ITEM_FIELDS}, 'diagnostic_item_id': item['diagnostic_item_id'],
        'item_version': binding['item_version'], 'binding': binding, 'child_id': plan['child_id'],
        **({'review_task_id': binding['review_task_id']} if review else {})}


def validate_pet_task_start(c, item, turn_id):
    """Called inside the real timer transaction, after command authorization."""
    if not is_generic_pet_task(c, item):
        return
    resolved = resolve_pet_task_execution(c, item['session_id'], item['plan_item_id'])
    if resolved['status'] != 'ready' or not turn_id:
        raise PetTaskExecutionError('pet_original_prompt_required')
    command = c.execute('''SELECT context_manifest_json FROM agent_plan
        WHERE agent_turn_id=? AND child_id=? AND session_id=? AND status!='cancelled' ''',
        (turn_id, item['child_id'], item['session_id'])).fetchone()
    binding = json.loads(command[0]).get('goal_prompt_binding') if command else None
    if (not binding or binding.get('pet') is not True or binding.get('stage') is not False
            or binding.get('plan_item_id') != item['plan_item_id'] or binding.get('binding') != resolved['binding']
            or binding.get('item_version') != resolved['item_version']):
        raise PetTaskExecutionError('pet_original_prompt_required')
    class TransactionPrompt(DiagnosticPromptService):
        def __init__(self):
            self.database = None
        def _transaction(self):
            return nullcontext(c)
    prompt = TransactionPrompt().read(item['session_id'], prompt_id=binding['prompt_id'])
    if (prompt['status'] not in {'awaiting_task_start', 'waiting_answer', 'answer_saved', 'answered'}
            or prompt['prompt']['diagnostic_item_id'] != resolved['diagnostic_item_id']
            or prompt['prompt']['item_version'] != resolved['item_version']):
        raise PetTaskExecutionError('pet_original_prompt_changed')
    from .pet_listening_admission import resolve_pet_listening_resource
    resource = resolve_pet_listening_resource(c, item['session_id'], item['child_id'],
        binding['prompt_id'], binding['item_version'])
    if resource['status'] not in {'ready', 'not_applicable'}:
        raise PetListeningResourceError(resource['reason'])


def pet_prompt_pending_start(c, row):
    """A prepared question is recoverable, not answerable before a real start."""
    if row['status'] not in {'waiting_answer', 'answer_saved'} or not c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_plan'").fetchone():
        return None
    commands = c.execute('''SELECT context_manifest_json FROM agent_plan WHERE child_id=? AND session_id=?
        AND json_extract(CASE WHEN json_valid(context_manifest_json) THEN context_manifest_json ELSE '{}' END,'$.goal_prompt_binding.prompt_id')=?
        AND json_extract(CASE WHEN json_valid(context_manifest_json) THEN context_manifest_json ELSE '{}' END,'$.goal_prompt_binding.pet')=1''',
        (row['child_id'], row['origin_session_id'], row['prompt_id'])).fetchall()
    for command in commands:
        binding = json.loads(command[0])['goal_prompt_binding']
        pid = binding.get('plan_item_id')
        started = c.execute('''SELECT 1 FROM plan_item pi JOIN daily_plan dp USING(plan_id)
            JOIN plan_item_event pe ON pe.plan_item_id=pi.plan_item_id AND pe.event_type='started'
                AND pe.occurred_at=pi.started_at
            WHERE pi.plan_item_id=? AND dp.child_id=? AND dp.session_id=?
              AND ((pi.source_type='pet_daily' AND pi.source_ref=?) OR
                   (pi.source_type='due_review' AND pi.source_ref=? AND ?=?))''',
            (pid, row['child_id'], row['origin_session_id'], row['diagnostic_item_id'],
             row['review_task_id'], binding.get('binding', {}).get('version'), REVIEW_VERSION)).fetchone()
        if not started:
            return pid
    return None


def pending_pet_task_binding(c, child_id, session_id):
    """Resolve the only retained, unstarted original; never use queue recency."""
    from .task_start_binding import TaskStartBindingError
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='diagnostic_prompt'").fetchone():
        return None
    rows = c.execute('''SELECT * FROM diagnostic_prompt WHERE child_id=? AND origin_session_id=?
        AND status IN ('waiting_answer','answer_saved')''', (child_id, session_id)).fetchall()
    pending = [(row, pet_prompt_pending_start(c, row)) for row in rows]
    pending = [(row, pid) for row, pid in pending if pid]
    if not pending:
        return None
    if len(pending) != 1:
        raise TaskStartBindingError('TASK_START_PET_RECOVERY_AMBIGUOUS')
    row, pid = pending[0]
    if row['status'] != 'waiting_answer':
        raise TaskStartBindingError('TASK_START_PET_PRESTART_ANSWER_CONFLICT')
    resolved = resolve_pet_task_execution(c, session_id, pid)
    if (resolved['status'] != 'ready' or resolved['child_id'] != child_id
            or resolved['diagnostic_item_id'] != row['diagnostic_item_id']
            or resolved['item_version'] != row['item_version']):
        raise TaskStartBindingError('TASK_START_PET_RECOVERY_SOURCE_CHANGED')
    if c.execute('''SELECT 1 FROM plan_item pi JOIN daily_plan dp USING(plan_id)
        WHERE dp.session_id=? AND dp.child_id=? AND dp.status='active' AND pi.status='active' ''',
        (session_id, child_id)).fetchone():
        raise TaskStartBindingError('TASK_START_PET_RECOVERY_ACTIVE_TASK')
    task = c.execute('SELECT status,started_at,source_type,source_ref FROM plan_item WHERE plan_item_id=?', (pid,)).fetchone()
    if task['status'] != 'pending' or task['started_at'] is not None:
        raise TaskStartBindingError('TASK_START_PET_RECOVERY_STATE_CHANGED')
    binding = {'plan_item_id': pid, 'source_type': task['source_type'],
               'source_ref': task['source_ref'], 'operation': 'start'}
    commands = c.execute('''SELECT context_manifest_json FROM agent_plan WHERE child_id=? AND session_id=?
        AND json_extract(context_manifest_json,'$.goal_prompt_binding.prompt_id')=?''',
        (child_id, session_id, row['prompt_id'])).fetchall()
    for command in commands:
        manifest = json.loads(command[0])
        original = manifest['goal_prompt_binding']
        if (manifest.get('task_start_binding') != binding or original.get('pet') is not True
                or original.get('binding') != resolved['binding'] or original.get('item_version') != row['item_version']):
            raise TaskStartBindingError('TASK_START_PET_RECOVERY_BINDING_CHANGED')
    return binding


def validate_pet_task_completion(c, item):
    """Validate this task's original practice receipt, not a mastery threshold."""
    resolved = resolve_pet_task_execution(c, item['session_id'], item['plan_item_id'], completed_receipt=True)
    if resolved['status'] != 'ready' or resolved['child_id'] != item['child_id']:
        raise PetTaskExecutionError('pet_completion_source_snapshot_unavailable')
    commands = c.execute('''SELECT context_manifest_json FROM agent_plan WHERE child_id=? AND session_id=?
        AND json_extract(context_manifest_json,'$.goal_prompt_binding.plan_item_id')=?''',
        (item['child_id'], item['session_id'], item['plan_item_id'])).fetchall()
    originals = [json.loads(row[0])['goal_prompt_binding'] for row in commands]
    if not originals or any(value != originals[0] for value in originals[1:]):
        raise PetTaskExecutionError('pet_completion_prompt_binding_unavailable')
    binding = originals[0]
    if (binding.get('pet') is not True or binding.get('stage') is not False
            or binding.get('binding') != resolved['binding'] or binding.get('item_version') != resolved['item_version']):
        raise PetTaskExecutionError('pet_completion_prompt_binding_changed')
    prompt = c.execute('''SELECT * FROM diagnostic_prompt WHERE prompt_id=? AND child_id=?
        AND origin_session_id=? AND diagnostic_item_id=? AND item_version=? AND status='answered' ''',
        (binding.get('prompt_id'), item['child_id'], item['session_id'], resolved['diagnostic_item_id'],
         resolved['item_version'])).fetchone()
    if not prompt or json.loads(prompt['item_json']) != resolved['diagnostic_item']:
        raise PetTaskExecutionError('pet_completion_original_answer_required')
    if prompt['review_task_id'] != resolved.get('review_task_id'):
        raise PetTaskExecutionError('pet_completion_review_binding_changed')
    attempt = DiagnosticPromptService._receipt(c, prompt)
    if attempt is None:
        raise PetTaskExecutionError('pet_completion_original_attempt_unavailable')
    action_id = 'diagnostic-prompt-answer:' + prompt['prompt_id']
    evidence = c.execute('''SELECT * FROM learning_evidence WHERE evidence_id=? AND child_id=? AND session_id=?
        AND knowledge_node_id=? AND evidence_type='diagnostic_answer' AND client_action_id=?''',
        (attempt['evidence_id'], item['child_id'], item['session_id'], resolved['binding']['knowledge_node_id'], action_id)).fetchone()
    if (not evidence or attempt['client_action_id'] != action_id
            or evidence['correctness'] != attempt['correctness'] or evidence['occurred_at'] != attempt['created_at']):
        raise PetTaskExecutionError('pet_completion_evidence_binding_changed')
    from .learning_obligations import withdrawn_evidence_ids
    if attempt['evidence_id'] in withdrawn_evidence_ids(c, item['child_id']):
        raise PetTaskExecutionError('pet_completion_evidence_withdrawn')
    started = c.execute('''SELECT pi.started_at FROM plan_item pi JOIN plan_item_event pe USING(plan_item_id)
        WHERE pi.plan_item_id=? AND pe.event_type='started' AND pe.occurred_at=pi.started_at''',
        (item['plan_item_id'],)).fetchone()
    try:
        if (not started or not started[0]
                or not datetime.fromisoformat(prompt['created_at']) <= datetime.fromisoformat(started[0])
                    <= datetime.fromisoformat(attempt['created_at'])):
            raise ValueError('original practice chronology unavailable')
    except (ValueError, TypeError):
        raise PetTaskExecutionError('pet_completion_start_receipt_required') from None
    return dict(attempt)
