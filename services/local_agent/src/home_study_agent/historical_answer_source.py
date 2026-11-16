"""Read original diagnostic evidence, never authorize a new answer submission."""
import json


def validate_historical_diagnostic_answer(c, turn_id, *, child_id, session_id,
                                         prompt_id, item_version, item_id, answer):
    from .agent_runtime import AgentRuntimeStore as Runtime
    from .agent_orchestrator import InvalidAgentTurnOutcomeError

    def require(value):
        if not value:
            raise ValueError('HISTORICAL_ANSWER_SOURCE_CHANGED')

    def require_not_deleted(source_id):
        try:
            Runtime.assert_turn_source_not_deleted(c, source_id)
        except InvalidAgentTurnOutcomeError as exc:
            raise ValueError('HISTORICAL_ANSWER_SOURCE_CHANGED') from exc

    prompt = c.execute('''SELECT p.* FROM diagnostic_prompt p
        JOIN daily_session ds ON ds.session_id=p.origin_session_id AND ds.child_id=p.child_id
        WHERE p.prompt_id=? AND p.child_id=? AND p.origin_session_id=?
        AND p.diagnostic_item_id=? AND p.item_version=? AND p.authorized_turn_id=?''',
        (prompt_id, child_id, session_id, item_id, item_version, turn_id)).fetchone()
    require(prompt and prompt['status'] in {'answer_saved', 'answered'})
    if prompt['status'] == 'answered':
        saved_answer = c.execute('''SELECT submitted_answer FROM diagnostic_attempt
            WHERE attempt_id=? AND child_id=? AND session_id=? AND diagnostic_item_id=?''',
            (prompt['attempt_id'], child_id, session_id, item_id)).fetchone()
        require(saved_answer and saved_answer[0] == answer)
    else:
        require(json.loads(prompt['answer_json']).get('answer') == answer)
    binding = {'prompt_id': prompt_id, 'item_version': item_version}
    row = c.execute('''SELECT t.*,s.capability_name,s.arguments_json FROM agent_turn t
        JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id AND p.child_id=t.child_id AND p.session_id=t.session_id
        JOIN agent_step s ON s.plan_id=p.plan_id AND s.step_index=0
        WHERE t.agent_turn_id=? AND t.child_id=? AND t.session_id=?''', (turn_id, child_id, session_id)).fetchone()
    require(row and row['intent'] != 'runtime_continuation' and row['tool_status'] != 'cancelled')
    require_not_deleted(turn_id)
    # This resolver has a dedicated historical mode: authorization event time,
    # exact page/readback/source checks, with no current-arrival requirement.
    if row['capability_name'] == 'writing.confirm_original':
        written = Runtime.confirmed_writing_submission(c, turn_id, for_write=False)
        require(written and written['child_id'] == child_id and written['session_id'] == session_id
                and all(written[k] == v for k, v in binding.items()) and written['answer'] == answer)
        return
    require(row['capability_name'] == 'diagnostic.submit')
    action, arguments = json.loads(row['action_json']), json.loads(row['arguments_json'])
    require(isinstance(action, dict) and isinstance(arguments, dict))
    require(action.get('type') == 'submit_diagnostic_answer'
            and {'prompt_id': action.get('diagnostic_prompt_id'), 'item_version': action.get('diagnostic_item_version')} == binding
            and arguments.get('answer') == answer)
    source_id = action.get('answer_source_turn_id')
    if not source_id:
        require(not Runtime.local_answer_requires_clarification(row['transcript'])
                and answer in Runtime.local_answer_forms(row['transcript'], 'diagnostic.submit'))
        Runtime._answer_voice_metadata(c, row, row, 'diagnostic.submit', binding)
        return
    require(Runtime.is_answer_role_confirmation(row['transcript']))
    source = c.execute('''SELECT t.* FROM agent_turn t JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id
        AND p.child_id=t.child_id AND p.session_id=t.session_id
        WHERE t.agent_turn_id=? AND t.child_id=? AND t.session_id=?''', (source_id, child_id, session_id)).fetchone()
    previous = c.execute('''SELECT agent_turn_id FROM agent_turn WHERE child_id=? AND session_id=?
        AND rowid<(SELECT rowid FROM agent_turn WHERE agent_turn_id=?) ORDER BY rowid DESC LIMIT 1''',
        (child_id, session_id, turn_id)).fetchone()
    require(source and previous and previous[0] == source_id and source['tool_status'] != 'cancelled')
    require_not_deleted(source_id)
    saved = json.loads(source['action_json'])
    require(isinstance(saved, dict))
    candidate = saved.get('pending_answer_candidate') or {}
    require(isinstance(candidate, dict))
    target = candidate.get('target') or {}
    require(isinstance(target, dict))
    require(saved.get('source') == 'pending_answer_clarification' and saved.get('local_only') is True
            and target.get('action') == 'submit_diagnostic_answer' and target.get('binding') == binding
            and candidate.get('answer') == source['transcript'] == answer and action.get('local_only') is True
            and not Runtime.local_answer_requires_clarification(source['transcript']))
    Runtime._answer_voice_metadata(c, row, source, 'diagnostic.submit', binding)
