"""Bind a scheduled roadmap action to a real prompt in the same write snapshot."""
from contextlib import nullcontext
import json

from .diagnostic_prompt import DiagnosticPromptService
from .goal_roadmap import GoalRoadmapService
from .task_start_binding import TaskStartBinding, TaskStartBindingError


def prepare_goal_task_prompt(database, turn_id, session_id, plan_item_id, operation, key, *, stage=False, pet=False, school_review=False, closed_review=False, oral_supply=False, connection=None):
    if sum((stage, pet, school_review, closed_review, oral_supply)) > 1:
        raise TaskStartBindingError('TASK_START_BINDING_CHANGED')
    scheduled=pet or school_review or closed_review or oral_supply
    if connection is None:
        DiagnosticPromptService(database)
    with database.connect() if connection is None else nullcontext(connection) as c:
        if connection is None:
            c.execute('BEGIN IMMEDIATE')
        elif not c.in_transaction:
            raise TaskStartBindingError('TASK_START_TRANSACTION_REQUIRED')
        TaskStartBinding.authorize(c, turn_id, plan_item_id, operation)
        class TransactionPrompt(DiagnosticPromptService):
            def __init__(self):
                self.database = None

            def _transaction(self):
                return nullcontext(c)

        prompts = TransactionPrompt()
        row = c.execute('SELECT plan_id,context_manifest_json FROM agent_plan WHERE agent_turn_id=?', (turn_id,)).fetchone()
        manifest = json.loads(row['context_manifest_json'])
        binding = manifest.get('goal_prompt_binding')
        if scheduled:
            if oral_supply:
                from .oral_supply_execution import resolve_execution as resolve_pet_task_execution
            elif closed_review:
                from .closed_review_execution import resolve_execution as resolve_pet_task_execution
            elif school_review:
                from .school_review_execution import resolve_execution as resolve_pet_task_execution
            else:
                from .pet_task_execution import resolve_pet_task_execution
            resolved = resolve_pet_task_execution(c, session_id, plan_item_id)
            if resolved['status'] != 'ready':
                return resolved
            if not binding:
                previous = c.execute('''SELECT context_manifest_json FROM agent_plan
                    WHERE session_id=? AND child_id=(SELECT child_id FROM daily_session WHERE session_id=?)
                    AND json_extract(CASE WHEN json_valid(context_manifest_json) THEN context_manifest_json ELSE '{}' END,
                        '$.goal_prompt_binding.plan_item_id')=?''',
                    (session_id, session_id, plan_item_id)).fetchall()
                originals = [json.loads(p[0])['goal_prompt_binding'] for p in previous]
                if originals:
                    if any(value != originals[0] for value in originals[1:]):
                        raise TaskStartBindingError('TASK_START_QUESTION_CHANGED')
                    binding = originals[0]
                    manifest['goal_prompt_binding'] = binding
                    c.execute('UPDATE agent_plan SET context_manifest_json=? WHERE plan_id=?', (json.dumps(manifest), row['plan_id']))
        if not binding and operation == 'resume' and not scheduled:
            previous = c.execute("""SELECT context_manifest_json FROM agent_plan WHERE session_id=? AND child_id=
                (SELECT child_id FROM daily_session WHERE session_id=?) AND status!='cancelled'
                AND json_extract(context_manifest_json,'$.goal_prompt_binding.plan_item_id')=?
                ORDER BY created_at DESC,plan_id DESC LIMIT 1""", (session_id, session_id, plan_item_id)).fetchone()
            if previous:
                binding = json.loads(previous['context_manifest_json'])['goal_prompt_binding']
                if bool(binding.get('stage')) != stage:
                    raise TaskStartBindingError('TASK_START_BINDING_CHANGED')
                manifest['goal_prompt_binding'] = binding
                c.execute('UPDATE agent_plan SET context_manifest_json=? WHERE plan_id=?', (json.dumps(manifest), row['plan_id']))
        if binding:
            if (binding['plan_item_id'] != plan_item_id or bool(binding.get('pet')) != pet
                    or bool(binding.get('school_review')) != school_review
                    or bool(binding.get('closed_review')) != closed_review
                    or bool(binding.get('oral_supply')) != oral_supply
                    or bool(binding.get('stage')) != stage):
                raise TaskStartBindingError('TASK_START_BINDING_CHANGED')
            if scheduled and (binding.get('binding') != resolved['binding'] or binding.get('item_version') != resolved['item_version']):
                raise TaskStartBindingError('TASK_START_QUESTION_CHANGED')
            result = prompts.read(session_id, prompt_id=binding['prompt_id'])
            if scheduled and (result.get('prompt') or {}).get('review_task_id') != resolved.get('review_task_id'):
                raise TaskStartBindingError('TASK_START_REVIEW_CHANGED')
        else:
            if scheduled:
                pass  # Already resolved against the original persisted plan snapshot.
            elif stage:
                from .stage_execution import resolve_stage_execution
                resolved = resolve_stage_execution(c, session_id, plan_item_id)
            else:
                resolved = GoalRoadmapService._resolve_execution(c, session_id, plan_item_id)
            if resolved['status'] != 'ready':
                return resolved
            item = resolved['diagnostic_item']
            pending = c.execute("SELECT prompt_id FROM diagnostic_prompt WHERE origin_session_id=? AND status IN ('waiting_answer','answer_saved')",
                                (session_id,)).fetchone()
            if pending:
                return {'status': 'blocked', 'reason': '先完成或取消正在回答的题目，才开始新的检查。',
                        'reason_code': 'pending_prompt_exists', 'local_only': True, 'cloud_transmission_allowed': False}
            result = prompts.prepare(session_id, track=item['track'], preferred_item_id=item['diagnostic_item_id'],
                subject=item['subject'], review_task_id=resolved.get('review_task_id'), client_action_id=f'goal-task:{key}')
            prompt = result.get('prompt') or {}
            if (result['status'] != 'waiting_answer' or prompt.get('diagnostic_item_id') != resolved['diagnostic_item_id']
                    or prompt.get('item_version') != resolved['item_version']):
                raise TaskStartBindingError('TASK_START_QUESTION_CHANGED')
            manifest['goal_prompt_binding'] = {'plan_item_id': plan_item_id, 'prompt_id': prompt['prompt_id'],
                'item_version': prompt['item_version'], 'binding': resolved['binding'], 'stage': stage}
            if pet:
                manifest['goal_prompt_binding']['pet'] = True
            if school_review:
                manifest['goal_prompt_binding']['school_review'] = True
            if closed_review:
                manifest['goal_prompt_binding']['closed_review'] = True
            if oral_supply:
                manifest['goal_prompt_binding']['oral_supply'] = True
            c.execute('UPDATE agent_plan SET context_manifest_json=? WHERE plan_id=?', (json.dumps(manifest), row['plan_id']))
        c.execute("UPDATE agent_turn SET action_json=json_set(action_json,'$.local_only',json('true'),'$.cloud_transmission_allowed',json('false')) WHERE agent_turn_id=?",
                  (turn_id,))
        if pet:
            if resolved.get('review_task_id') and result['status'] in {'waiting_answer', 'awaiting_task_start'}:
                from .pet_listening_reuse import bind_reviewed_listening_resource
                bind_reviewed_listening_resource(c, session_id, resolved['child_id'],
                    manifest['goal_prompt_binding']['prompt_id'], resolved['item_version'])
            result = prompts.read(session_id, prompt_id=manifest['goal_prompt_binding']['prompt_id'])
        from .scheduled_writing_goal import bind_before_answer
        bind_before_answer(c, turn_id, manifest['goal_prompt_binding']['prompt_id'])
        if connection is None:
            c.commit()
        return {**result, 'local_only': True, 'cloud_transmission_allowed': False}
