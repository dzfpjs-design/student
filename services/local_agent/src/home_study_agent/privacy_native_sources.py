"""Exact native source erasure, borrowing the authorized deletion transaction.

No scoring, schema setup, connection, commit or downstream replay here. Original
answers remain facts; source snapshots and their scoring authority do not.
"""
import json
import sqlite3
from hashlib import sha256

from .privacy_evidence_cleanup import (
    PrivacyEvidenceCleanupError, _exists, _ids, _object, _transaction,
    purge_rubric_sources,
)
from .repository import utc_now


def _table_rows(c, table):
    return [dict(r) for r in c.execute(f'SELECT * FROM {table}')] if _exists(c, table) else []


def _linked(value, refs):
    if isinstance(value, dict):
        return any((key in {'source_ref', 'source_id', 'resource_id', 'segment_id', 'excerpt_id',
                           'diagnostic_item_id', 'seed_item_id', 'source_evidence_id', 'evidence_id',
                           'prompt_id', 'diagnostic_prompt_id', 'preferred_item_id', 'confirmation_id'}
                    and isinstance(item, str) and item in refs) or _linked(item, refs)
                   for key, item in value.items())
    return isinstance(value, list) and any(_linked(item, refs) for item in value)


def _snapshot(raw):
    if raw is None:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as error:
        raise PrivacyEvidenceCleanupError('PRIVACY_EVIDENCE_JSON_INVALID') from error
    if not isinstance(value, (dict, list)):
        raise PrivacyEvidenceCleanupError('PRIVACY_EVIDENCE_JSON_INVALID')
    return value


def _runtime_source_links(value):
    if isinstance(value, dict):
        return {item for key, item in value.items() if key in {'prompt_id', 'diagnostic_prompt_id', 'confirmation_id'}
                and isinstance(item, str)} | set().union(*(_runtime_source_links(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_runtime_source_links(item) for item in value))
    return set()


def purge_native_sources(c, child_id, source_refs=(), *, owner_source_ids=None,
                         shared_detach_item_ids=(), affected_scopes=None, erasure_manifest=None):
    """Borrow an authorized erasure transaction, never infer ownership from text.

    owner_source_ids selects exact owned source entities. Shared catalog rows
    stay intact; only this child's frozen copies and score authority detach.
    shared_detach_item_ids is an optional checked subset, not a scope override.
    Legacy source_refs remains for the verified captured-resource closure only.

    affected_scopes is a transaction-local child -> node-id set collector, not
    an HTTP receipt. The host replays these scopes only at transaction end.
    """
    _transaction(c)
    if erasure_manifest is not None and (not isinstance(erasure_manifest, dict) or erasure_manifest):
        raise PrivacyEvidenceCleanupError('PRIVACY_EVIDENCE_SCOPE_INVALID')
    refs = _ids(source_refs)
    owner = None
    detach = _ids(shared_detach_item_ids)
    if owner_source_ids is not None:
        from .privacy_native_owner import NativeOwnerScope
        if refs:
            raise PrivacyEvidenceCleanupError('PRIVACY_EVIDENCE_SCOPE_INVALID')
        owner = NativeOwnerScope(c, child_id, owner_source_ids)
        if not owner.sources:
            return {}
        refs = owner.refs
    elif detach:
        raise PrivacyEvidenceCleanupError('PRIVACY_EVIDENCE_SCOPE_INVALID')
    _rows = owner.rows if owner else _table_rows
    if affected_scopes is not None and (not isinstance(affected_scopes, dict) or any(
            not isinstance(key, str) or not isinstance(value, set) for key, value in affected_scopes.items())):
        raise PrivacyEvidenceCleanupError('PRIVACY_EVIDENCE_SCOPE_INVALID')
    sources = owner.sources if owner else [r for r in _rows(c, 'knowledge_source') if r['source_ref'] in refs]
    if any(r['child_id'] != child_id for r in sources):
        raise PrivacyEvidenceCleanupError('PRIVACY_EVIDENCE_SOURCE_NOT_OWNED')
    refs |= {r['source_id'] for r in sources}
    excerpts = [r for r in _rows(c, 'knowledge_excerpt') if r['source_id'] in refs]
    refs |= {r['excerpt_id'] for r in excerpts}
    nodes = [r for r in _rows(c, 'knowledge_node') if r['source_ref'] in refs]
    items = {r['diagnostic_item_id']: r for r in _rows(c, 'diagnostic_item') if r['source_ref'] in refs}
    all_items = {r['diagnostic_item_id']: r for r in _rows(c, 'diagnostic_item')}
    packages = _rows(c, 'curriculum_supply_package')
    parsed_packages = {r['candidate_id']: (_object(r['payload_json']), _object(r['source_snapshot_json'])) for r in packages}
    candidates = {name: _rows(c, name) for name in ('specialized_supply_candidate', 'variant_candidate')}
    cases = _rows(c, 'mistake_case')
    selected_candidates = {name: {} for name in candidates}
    selected_packages = set()
    lineage_items = ({iid for iid, row in items.items() if row['source_ref'] in owner.exact_lineage}
                     if owner else set(items))
    # Published descendants can themselves seed another approved question.
    while True:
        old = (len(items), len(selected_packages), sum(map(len, selected_candidates.values())))
        identity = (owner.exact_lineage | lineage_items) if owner else (refs | set(items))
        for r in packages:
            if any(_linked(value, identity) for value in parsed_packages[r['candidate_id']]):
                selected_packages.add(r['candidate_id'])
        mistake_ids = {r['mistake_id'] for r in cases if r['diagnostic_item_id'] in items or r['source_ref'] in refs}
        for table, rows in candidates.items():
            for r in rows:
                selected = (r['candidate_id'] in selected_packages or r.get('seed_item_id') in (lineage_items if owner else items)
                            or r.get('mistake_id') in mistake_ids or r['diagnostic_item_id'] in items)
                if selected:
                    selected_candidates[table][r['candidate_id']] = r
                    if r['diagnostic_item_id'] in all_items:
                        items[r['diagnostic_item_id']] = all_items[r['diagnostic_item_id']]
                        lineage_items.add(r['diagnostic_item_id'])
        if old == (len(items), len(selected_packages), sum(map(len, selected_candidates.values()))):
            break
    if detach - set(items):
        raise PrivacyEvidenceCleanupError('PRIVACY_SOURCE_DETACH_NOT_BOUND')
    erased_items = (owner.erasable_items(items) - detach) if owner else set(items)
    erased_nodes = owner.erasable_nodes(nodes, erased_items) if owner else {r['knowledge_node_id'] for r in nodes}
    selected_cases = [r for r in cases if r['mistake_id'] in mistake_ids]
    diagnosis_events = [r for r in _rows(c, 'mistake_diagnosis_event') if r['mistake_id'] in mistake_ids]
    for row in diagnosis_events:
        _object(row['payload_json'])
        _object(row['request_json'])
    evidence = {}
    receipts = []
    all_evidence = {r['evidence_id']: r for r in _rows(c, 'learning_evidence')}
    sessions = {r['session_id']: r for r in _rows(c, 'daily_session')}
    for table in ('diagnostic_attempt', 'mistake_attempt'):
        for row in _rows(c, table):
            if row['diagnostic_item_id'] not in items:
                continue
            le = all_evidence.get(row['evidence_id'])
            session = sessions.get(row.get('session_id') or (le or {}).get('session_id'))
            if not le or not session or row['child_id'] != le['child_id'] or session['child_id'] != le['child_id']:
                raise PrivacyEvidenceCleanupError('PRIVACY_EVIDENCE_OWNER_MISMATCH')
            evidence[le['evidence_id']] = le
            receipts.append((table, row))
    # A source-owned node invalidates qualification, not every child's answer.
    node_ids = {r['knowledge_node_id'] for r in nodes}
    evidence.update({eid: r for eid, r in all_evidence.items() if r['knowledge_node_id'] in node_ids})
    scopes = {}
    for row in list(evidence.values()) + selected_cases:
        scopes.setdefault(row['child_id'], set()).add(row['knowledge_node_id'])
    identity = refs | set(items) | set(evidence)
    jobs = {}
    for table in ('specialized_supply_job', 'variant_generation_job'):
        jobs[table] = []
        for row in _rows(c, table):
            snapshot = _object(row['source_snapshot_json']) if row.get('source_snapshot_json') else {}
            job_identity = (owner.exact_lineage | lineage_items | set(evidence)) if owner else identity
            if row.get('seed_item_id') in (lineage_items if owner else items) or row.get('mistake_id') in mistake_ids or _linked(snapshot, job_identity):
                jobs[table].append(row)
    assessments = []
    for row in _rows(c, 'rubric_assessment'):
        if _linked(_object(row['payload_json']), identity):
            assessments.append(row)
            scopes.setdefault(row['child_id'], set()).update(r['knowledge_node_id'] for r in items.values())
    if scopes and affected_scopes is None:
        raise PrivacyEvidenceCleanupError('PRIVACY_NATIVE_PROJECTION_COLLECTOR_REQUIRED')
    prompts = {r['prompt_id']: r for r in _rows(c, 'diagnostic_prompt') if r['diagnostic_item_id'] in items}
    identity |= set(prompts)
    for prompt in prompts.values():
        if sessions.get(prompt['origin_session_id'], {}).get('child_id') != prompt['child_id']:
            raise PrivacyEvidenceCleanupError('PRIVACY_EVIDENCE_OWNER_MISMATCH')
        scopes.setdefault(prompt['child_id'], set()).add(items[prompt['diagnostic_item_id']]['knowledge_node_id'])
    # Readback commands may only retain a confirmation ID. Resolve that ID
    # through the real question FK, never through the child's answer text.
    runtime_bindings = dict(prompts)
    confirmations = {r['confirmation_id']: r for r in _rows(c, 'writing_ocr_confirmation')
                     if r['prompt_id'] in prompts}
    for cid, row in confirmations.items():
        prompt = prompts[row['prompt_id']]
        if (cid in runtime_bindings or (row['child_id'], row['session_id'], row['item_version']) !=
                (prompt['child_id'], prompt['origin_session_id'], prompt['item_version'])):
            raise PrivacyEvidenceCleanupError('PRIVACY_EVIDENCE_OWNER_MISMATCH')
        runtime_bindings[cid] = {**row, 'origin_session_id': row['session_id']}
    for row in _rows(c, 'writing_ocr_event'):
        if row['confirmation_id'] in confirmations:
            if row['child_id'] != confirmations[row['confirmation_id']]['child_id']:
                raise PrivacyEvidenceCleanupError('PRIVACY_EVIDENCE_OWNER_MISMATCH')
            _object(row['request_json']); _object(row['payload_json'])
    identity |= set(confirmations)
    step_ids = {r['step_id'] for r in assessments}
    # Running requests settle before erasure. Saved responses have no network
    # owner; their claims are tombstoned below and late finalization rejects them.
    executions = []
    for row in _rows(c, 'learning_evaluation_execution'):
        values = [_snapshot(row.get(key)) for key in ('record_json', 'evaluation_json', 'result_json')]
        if row['step_id'] in step_ids or any(_linked(value, identity) for value in values):
            executions.append(row)
    if any(r['state'] == 'running' for r in executions):
        raise PrivacyEvidenceCleanupError('PRIVACY_NATIVE_EVALUATION_IN_FLIGHT')
    step_ids.update(r['step_id'] for r in executions)
    plans = {r['plan_id']: r for r in _rows(c, 'agent_plan')}
    turns = {r['agent_turn_id']: r for r in _rows(c, 'agent_turn')}
    all_steps = _rows(c, 'agent_step')
    step_values = {r['step_id']: [_snapshot(r.get(key)) for key in
        ('arguments_json', 'expected_evidence_json', 'result_json')] for r in all_steps}
    plan_values = {pid: [_object(r['plan_json']), _object(r['context_manifest_json'])] for pid, r in plans.items()}
    turn_values = {tid: _object(r['action_json']) for tid, r in turns.items()}
    steps = [r for r in all_steps if r['step_id'] in step_ids or
             any(_linked(value, identity) for value in step_values[r['step_id']])]
    selected_turns = {tid for tid, value in turn_values.items() if _linked(value, identity)}
    selected_plans = {r['plan_id'] for r in steps} | {pid for pid, values in plan_values.items()
        if any(_linked(value, identity) for value in values) or plans[pid]['agent_turn_id'] in selected_turns}
    for pid in selected_plans:
        if pid not in plans:
            raise PrivacyEvidenceCleanupError('PRIVACY_EVIDENCE_OWNER_MISMATCH')
        members = [row for row in all_steps if row['plan_id'] == pid]
        if len(members) == 1 and members[0] not in steps:
            steps.append(members[0])
        if any(row not in steps for row in members):
            raise PrivacyEvidenceCleanupError('PRIVACY_NATIVE_MIXED_PLAN_REQUIRES_REVIEW')
        plan = plans[pid]
        turn = turns.get(plan['agent_turn_id'])
        if (sessions.get(plan['session_id'], {}).get('child_id') != plan['child_id'] or not turn
                or (turn['child_id'], turn['session_id']) != (plan['child_id'], plan['session_id'])):
            raise PrivacyEvidenceCleanupError('PRIVACY_EVIDENCE_OWNER_MISMATCH')
        values = plan_values[pid] + [value for row in members for value in step_values[row['step_id']]]
        for value in values:
            for reference in _runtime_source_links(value) & set(runtime_bindings):
                prompt = runtime_bindings[reference]
                if (prompt['child_id'], prompt['origin_session_id']) != (plan['child_id'], plan['session_id']):
                    raise PrivacyEvidenceCleanupError('PRIVACY_EVIDENCE_OWNER_MISMATCH')
        selected_turns.add(plan['agent_turn_id'])
        scopes.setdefault(plan['child_id'], set()).update(r['knowledge_node_id'] for r in items.values())
    if any(r['status'] in {'running', 'waiting_child'} for r in steps):
        raise PrivacyEvidenceCleanupError('PRIVACY_NATIVE_EXECUTION_IN_FLIGHT')
    for tid in selected_turns:
        turn = turns[tid]
        if sessions.get(turn['session_id'], {}).get('child_id') != turn['child_id']:
            raise PrivacyEvidenceCleanupError('PRIVACY_EVIDENCE_OWNER_MISMATCH')
        for reference in _runtime_source_links(turn_values[tid]) & set(runtime_bindings):
            prompt = runtime_bindings[reference]
            if (prompt['child_id'], prompt['origin_session_id']) != (turn['child_id'], turn['session_id']):
                raise PrivacyEvidenceCleanupError('PRIVACY_EVIDENCE_OWNER_MISMATCH')
        scopes.setdefault(turn['child_id'], set()).update(r['knowledge_node_id'] for r in items.values())
    if scopes and affected_scopes is None:
        raise PrivacyEvidenceCleanupError('PRIVACY_NATIVE_PROJECTION_COLLECTOR_REQUIRED')
    counts = {'diagnostic_item_redacted': 0, 'knowledge_excerpt': 0, 'knowledge_source': 0,
              'rubric_review_event': 0, 'rubric_assessment': 0}
    c.execute('SAVEPOINT privacy_native_source')
    try:
        if owner:
            from .privacy_source_withdrawal import record
            counts['privacy_source_evidence_withdrawal'] = record(c, child_id, owner.source_ids, set(evidence))
        deferred = set()
        for owner in scopes:
            result = purge_rubric_sources(c, owner, diagnostic_item_ids=set(items),
                source_evidence_ids={eid for eid, row in evidence.items() if row['child_id'] == owner},
                deferred_reconciliation=deferred)
            for table, count in result.items():
                counts[table] = counts.get(table, 0) + count
        for row in executions:
            original = _object(row['record_json'])
            retained = {key: original[key] for key in ('step_id', 'objective_id', 'child_id', 'session_id', 'child_response') if key in original}
            retained['source_deleted'] = True
            # Keep the claim key as a non-replayable tombstone, not a missing
            # claim that could authorize a second model call after a restart.
            c.execute("UPDATE learning_evaluation_execution SET state='unknown',record_json=?,evaluation_json=NULL,result_json=NULL,error_code='SOURCE_DELETED' WHERE step_id=?",
                      (json.dumps(retained, ensure_ascii=False), row['step_id']))
            counts['learning_evaluation_execution_redacted'] = counts.get('learning_evaluation_execution_redacted', 0) + 1
        for row in steps:
            args = _object(row['arguments_json'])
            answer = {key: args[key] for key in ('response', 'answer', 'source_evidence_id') if key in args}
            c.execute("UPDATE agent_step SET arguments_json=?,expected_evidence_json='[]',result_json=NULL,error_code='SOURCE_DELETED',status=CASE WHEN status IN ('planned','failed') THEN 'cancelled' ELSE status END WHERE step_id=?",
                      (json.dumps(answer, ensure_ascii=False), row['step_id']))
            counts['agent_step_redacted'] = counts.get('agent_step_redacted', 0) + 1
        for pid in selected_plans:
            plan = plans[pid]
            # One-step runtime plans are frozen copies of the same erased
            # action. Preserve DB identity, recorded completion and child turn.
            c.execute("UPDATE agent_plan SET plan_json='{}',context_manifest_json='{}',status=CASE WHEN status IN ('planned','running','waiting_child') THEN 'cancelled' ELSE status END WHERE plan_id=?", (pid,))
            counts['agent_plan_redacted'] = counts.get('agent_plan_redacted', 0) + 1
        for tid in selected_turns:
            c.execute("UPDATE agent_turn SET response_text='题源已删除，原作答仍保留。',action_json='{}',tool_error_code='SOURCE_DELETED' WHERE agent_turn_id=?", (tid,))
            counts['agent_turn_redacted'] = counts.get('agent_turn_redacted', 0) + 1
        selected_step_ids = {r['step_id'] for r in steps}
        for row in _rows(c, 'agent_outcome'):
            try:
                outcome_refs = json.loads(row['evidence_refs_json'])
            except (ValueError, TypeError) as error:
                raise PrivacyEvidenceCleanupError('PRIVACY_EVIDENCE_JSON_INVALID') from error
            if not isinstance(outcome_refs, list):
                raise PrivacyEvidenceCleanupError('PRIVACY_EVIDENCE_JSON_INVALID')
            if any(isinstance(ref, dict) and ref.get('type') == 'child_response' and ref.get('agent_step_id') in selected_step_ids for ref in outcome_refs):
                c.execute("UPDATE agent_outcome SET summary='题源已删除；历史结果不再具有当前评分资格。' WHERE outcome_id=?", (row['outcome_id'],))
                counts['agent_outcome_redacted'] = counts.get('agent_outcome_redacted', 0) + 1
        for table, row in receipts:
            c.execute(f"UPDATE {table} SET feedback='题源已删除；保留原作答，不再作为有效评分证据。' WHERE attempt_id=?", (row['attempt_id'],))
        for eid in evidence:
            c.execute("UPDATE learning_evidence SET evidence_summary='source_deleted; original_answer_preserved; scoring_unavailable' WHERE evidence_id=?", (eid,))
        for row in _rows(c, 'diagnostic_prompt'):
            if row['diagnostic_item_id'] in items:
                _object(row['item_json']); _object(row['request_json'])
                if row['answer_json'] is not None:
                    _object(row['answer_json'])
                # request_json contains the original selector IDs, not a frozen
                # question. Retain it so exact retries return invalidated.
                c.execute("UPDATE diagnostic_prompt SET status='invalidated',reason='source_deleted',item_json='{}',updated_at=? WHERE prompt_id=?", (utc_now(), row['prompt_id']))
                counts['diagnostic_prompt_redacted'] = counts.get('diagnostic_prompt_redacted', 0) + 1
        for row in selected_cases:
            from .privacy_preserved_response import preserve_diagnosis_answers
            counts['child_responses_preserved'] = counts.get('child_responses_preserved', 0) + preserve_diagnosis_answers(
                c, row, [event for event in diagnosis_events if event['mistake_id'] == row['mistake_id']])
            c.execute("UPDATE mistake_case SET title='题源已删除',prompt_summary='',source_ref='privacy_deleted_source',status='excluded',updated_at=? WHERE mistake_id=?", (utc_now(), row['mistake_id']))
            for table in ('mistake_diagnosis_event',):
                if _exists(c, table):
                    counts[table] = counts.get(table, 0) + c.execute(f'DELETE FROM {table} WHERE mistake_id=?', (row['mistake_id'],)).rowcount
            c.execute("UPDATE mistake_diagnosis SET explanation='' WHERE mistake_id=?", (row['mistake_id'],))
            c.execute("UPDATE remediation_step SET notes=NULL WHERE mistake_id=?", (row['mistake_id'],))
        for table, rows in selected_candidates.items():
            for row in rows.values():
                c.execute(f"UPDATE {table} SET status='rejected',prompt='题源已删除',expected_answer_json='{{}}',explanation='',validation_json='{{}}',review_note=NULL WHERE candidate_id=?", (row['candidate_id'],))
                job_table = 'specialized_supply_job' if table == 'specialized_supply_candidate' else 'variant_generation_job'
                c.execute(f"UPDATE {job_table} SET status='blocked',error_code='SOURCE_DELETED',source_snapshot_json='{{}}',updated_at=? WHERE job_id=?", (utc_now(), row['job_id']))
                if table == 'specialized_supply_candidate':
                    selected_packages.add(row['candidate_id'])
        for table, rows in jobs.items():
            for row in rows:
                c.execute(f"UPDATE {table} SET status='blocked',error_code='SOURCE_DELETED',source_snapshot_json='{{}}',updated_at=? WHERE job_id=?", (utc_now(),row['job_id']))
        from .learning_obligations import cancel_unstarted_plans
        for row in _rows(c, 'review_task'):
            if row['diagnostic_item_id'] in items or row['source_evidence_id'] in evidence:
                c.execute("UPDATE review_task SET status='cancelled' WHERE review_task_id=? AND status='pending'", (row['review_task_id'],))
                cancel_unstarted_plans(c, row['child_id'], 'due_review', row['review_task_id'], utc_now())
        for row in selected_cases:
            cancel_unstarted_plans(c, row['child_id'], 'mistake_review', row['mistake_id'], utc_now())
        for cid in selected_packages:
            if _exists(c, 'curriculum_supply_package'):
                c.execute("UPDATE curriculum_supply_package SET state='withdrawn',version=version+1,payload_json='{}',source_snapshot_json='{}',blockers_json='[\"SOURCE_DELETED\"]' WHERE candidate_id=?", (cid,))
                counts['curriculum_supply_event'] = counts.get('curriculum_supply_event', 0) + c.execute('DELETE FROM curriculum_supply_event WHERE candidate_id=?', (cid,)).rowcount
        for row in items.values():
            if _exists(c, 'writing_question_authorization_event'):
                counts['writing_question_authorization_event'] = counts.get('writing_question_authorization_event', 0) + c.execute(
                    'DELETE FROM writing_question_authorization_event WHERE child_id=? AND diagnostic_item_id=?',
                    (child_id, row['diagnostic_item_id'])).rowcount
            if row['diagnostic_item_id'] not in erased_items:
                continue
            counts['diagnostic_item_redacted'] += c.execute("UPDATE diagnostic_item SET status='retired',prompt='题源已删除',expected_answer_json='{}',explanation='',stimulus_text='',stimulus_asset=NULL,source_ref='privacy_deleted_source' WHERE diagnostic_item_id=?", (row['diagnostic_item_id'],)).rowcount
        for row in nodes:
            if row['knowledge_node_id'] not in erased_nodes:
                continue
            c.execute("UPDATE knowledge_node SET status='retired',code=?,title='来源已删除',description='',source_ref='privacy_deleted_source' WHERE knowledge_node_id=?",
                      ('privacy-deleted:' + sha256(row['knowledge_node_id'].encode()).hexdigest(), row['knowledge_node_id']))
        for row in excerpts:
            counts['knowledge_excerpt'] += c.execute('DELETE FROM knowledge_excerpt WHERE excerpt_id=?', (row['excerpt_id'],)).rowcount
        for row in sources:
            # No native source FK remains after excerpts. Keeping this row's
            # unique content hash would poison a later explicit parent import.
            counts['knowledge_source'] += c.execute('DELETE FROM knowledge_source WHERE source_id=? AND child_id=?', (row['source_id'], child_id)).rowcount
        c.execute('RELEASE SAVEPOINT privacy_native_source')
    except BaseException as error:
        c.execute('ROLLBACK TO SAVEPOINT privacy_native_source')
        c.execute('RELEASE SAVEPOINT privacy_native_source')
        if isinstance(error, (sqlite3.Error, OSError)):
            raise PrivacyEvidenceCleanupError('PRIVACY_EVIDENCE_STORAGE_FAILURE') from error
        raise
    if affected_scopes is not None:
        for owner, values in scopes.items():
            affected_scopes.setdefault(owner, set()).update(values)
    if erasure_manifest is not None:
        erasure_manifest.update(source_ids=sorted(r['source_id'] for r in sources),
            excerpt_ids=sorted(r['excerpt_id'] for r in excerpts), item_ids=sorted(items),
            erased_item_ids=sorted(erased_items), candidate_ids=sorted(selected_packages),
            prompt_ids=sorted(prompts), evidence_ids=sorted(evidence))
    return counts
