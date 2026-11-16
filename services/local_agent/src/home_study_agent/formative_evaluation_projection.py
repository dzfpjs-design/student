"""A fresh, minimal evaluator payload, never a sanitized copy of a local envelope.

Ready means source and current text privacy checks passed, not model admission.
Call again immediately before dispatch and retain Runtime's policy/budget gates.
"""
import json
import re
from datetime import datetime

from .cloud_privacy import sanitize_cloud_text, sensitive_categories
from .diagnostic_prompt import DiagnosticPromptService, _version
from .learning_obligations import withdrawn_evidence_ids
from .learning_rubric import rubric_dimensions
from .local_supply import digest, validate_item, LocalSupplyError

VERSION = 'formative_evaluation_projection_v1'


def _require(ok, reason):
    if not ok:
        raise _Blocked(reason)


class _Blocked(ValueError):
    pass


def _original(c, prompt, attempt, item):
    node = c.execute('SELECT * FROM knowledge_node WHERE knowledge_node_id=?',(item['knowledge_node_id'],)).fetchone()
    question_authority = None
    if item['source_ref']=='hsc_original_v1':
        _require(node['source_ref']=='hsc_original_v1','question_source_local_or_unknown')
        _require(not c.execute('SELECT 1 FROM knowledge_source WHERE source_ref=?',(node['source_ref'],)).fetchone(),
                 'question_has_linked_material')
    else:
        from .writing_question_authorization import authorized_question_projection, question_text
        question_authority = authorized_question_projection(c,child_id=prompt['child_id'],diagnostic_item_id=item['diagnostic_item_id'])
        _require(question_authority['question']==question_text(item)
            and question_authority['task_points']==json.loads(item['expected_answer_json']).get('task_points'),
            'question_purpose_content_changed')
    _require(not item.get('stimulus_asset') and (not item.get('stimulus_text') or
        question_authority and question_authority['purpose_version']=='writing_question_material_formative_text_v2'),
        'question_has_linked_stimulus')
    authority_audit = {'question_purpose_authorization':{key:value for key,value in question_authority.items()
        if key not in {'question','task_points'}}} if question_authority else {}
    profile = None
    if item['answer_type']=='open_response':
        from .open_response_profile import profile_for_item
        profile = profile_for_item(item,dict(node))
    if item['answer_type'] == 'writing' or profile and profile['response_mode']=='written':
        from .writing_response_origin import original_written_response
        origin = original_written_response(c, prompt, attempt)
        if origin['kind']=='confirmed_document':
            from .paper_writing_projection import project_confirmed_writing_text
            paper=project_confirmed_writing_text(c,child_id=prompt['child_id'],session_id=prompt['origin_session_id'],
                prompt_id=prompt['prompt_id'],item_version=prompt['item_version'],attempt_id=attempt['attempt_id'],
                writing_original_artifact_id=origin['artifact_id'])
            _require(paper['status']=='ready_for_runtime_checks',paper['reason'])
            _require(paper['response']==attempt['submitted_answer'],'paper_original_response_changed')
            return {**origin,'paper_text_projection':paper['local_audit'],**authority_audit}
        _require(origin['kind'] == 'native_editor', 'written_original_kind_unsupported')
        return {**origin,**authority_audit}
    # Spoken text is still not an original written manuscript or acoustic score.
    from .pet_speaking_origin import original_answer_voice, original_answer_transcript
    turn = c.execute('SELECT action_json FROM agent_turn WHERE agent_turn_id=?',
                     (prompt['authorized_turn_id'],)).fetchone()
    _require(turn, 'original_voice_binding_missing')
    voice_id = json.loads(turn[0]).get('source_voice_input_id')
    capture = c.execute('SELECT wav_sha256 FROM voice_capture_binding WHERE voice_input_id=?', (voice_id,)).fetchone()
    _require(capture, 'original_voice_binding_missing')
    # This projection supports reference feedback on reasoning text, not an
    # audio review. PET audio and qualified school scores keep their own gates.
    reader = original_answer_transcript if profile and profile['response_mode']=='spoken_explanation' else original_answer_voice
    return reader(c, prompt, attempt,
        dict(child_id=prompt['child_id'],session_id=prompt['origin_session_id'],
             prompt_id=prompt['prompt_id'],item_version=prompt['item_version'],voice_input_id=voice_id), capture[0])


def _revision_lineage(c, prompt, attempt, origin, *, original_reader=None):
    """Only persisted teaching links explain older work in this same goal."""
    from .writing_response_origin import _prompt
    from .writing_teaching import (_reference_receipt,_revision_request,WritingTeachingService,
        _reference_revision_action,_reference_revision_version)
    nodes={prompt['prompt_id']:{'prompt':prompt,'attempt':attempt,'original':origin}}
    links={}
    reader=original_reader or _original
    tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    current=prompt
    for _ in range(32):
        reference = c.execute('SELECT * FROM writing_reference_revision WHERE prompt_id=?',(current['prompt_id'],)).fetchone() if 'writing_reference_revision' in tables else None
        feedback = c.execute('SELECT * FROM writing_followup WHERE prompt_id=?',(current['prompt_id'],)).fetchone() if 'writing_followup' in tables else None
        try:
            request = json.loads(current['request_json'])
        except (TypeError, ValueError):
            _require(False, 'revision_request_changed')
        if request.get('oral_followup') or request.get('oral_authorized_turn_id'):
            from .agent_runtime import AgentRuntimeStore
            _require(not reference and not feedback, 'ambiguous_revision_lineage')
            try:
                proof = AgentRuntimeStore.inspect_oral_followup_execution(c,request.get('oral_authorized_turn_id'),
                    prepared=current,historical=True)
            except (ValueError,KeyError,TypeError):
                _require(False,'oral_teaching_lineage_changed')
            target = proof['binding']['action']
            _require(target['parent_prompt_id'] not in nodes,'revision_lineage_cycle')
            parent = c.execute('''SELECT * FROM diagnostic_prompt WHERE prompt_id=? AND item_version=?
                AND child_id=? AND origin_session_id=? AND status='answered' ''',
                (target['parent_prompt_id'],target['parent_item_version'],prompt['child_id'],prompt['origin_session_id'])).fetchone()
            _require(parent and parent['created_at'] < current['created_at'], 'oral_teaching_lineage_changed')
            parent = dict(parent)
            item = DiagnosticPromptService._item(c,prompt['child_id'],parent['diagnostic_item_id'])
            previous = c.execute('SELECT * FROM diagnostic_attempt WHERE attempt_id=? AND child_id=?',
                (parent['attempt_id'],prompt['child_id'])).fetchone()
            _require(previous and previous['created_at']<=current['created_at'], 'oral_teaching_lineage_changed')
            if target['kind']=='retry':
                hint = c.execute('SELECT hint_level FROM learning_evidence WHERE evidence_id=?',
                    (nodes[current['prompt_id']]['attempt']['evidence_id'],)).fetchone()
                _require(hint and hint[0]>=1,'revision_assistance_erased')
            links[current['prompt_id']] = {'row':dict(parent_prompt_id=parent['prompt_id'],
                kind='revision' if target['kind']=='retry' else 'independent_check',
                request_turn_id=request['oral_authorized_turn_id']), 'proof':proof}
            nodes[parent['prompt_id']] = {'prompt':parent,'attempt':previous,'original':reader(c,parent,previous,item)}
            current = parent
            continue
        _require(not (reference and feedback),'ambiguous_revision_lineage')
        row=reference or feedback
        if not row:
            return nodes,links
        _require(not feedback or feedback['kind'] in {'revision','independent_check'},'unknown_teaching_lineage')
        revision = not feedback or feedback['kind']=='revision'
        _require((row['child_id'],row['session_id'])==(prompt['child_id'],current['origin_session_id']), 'revision_lineage_owner_changed')
        stored=json.loads(row['binding_json'])
        _require(stored.get('prompt_id')==current['prompt_id'] and stored.get('item_version')==current['item_version']
            and stored.get('parent_prompt_id')==row['parent_prompt_id'], 'revision_lineage_changed')
        _require(row['parent_prompt_id'] not in nodes,'revision_lineage_cycle')
        if feedback:
            from .writing_followup_evidence import validate_formal_followup_evaluation
            proof=validate_formal_followup_evaluation(c,child_id=row['child_id'],session_id=row['session_id'],
                prompt_id=current['prompt_id'],item_version=current['item_version'],
                attempt_id=nodes[current['prompt_id']]['attempt']['attempt_id'])
        parent_session=(stored.get('parent_session_id') or row['session_id']) if feedback else row['session_id']
        parent,item=_prompt(c,parent_session,row['parent_prompt_id'],stored.get('parent_item_version'),live=False)
        _require(parent['child_id']==row['child_id'],'revision_lineage_owner_changed')
        _require(parent['status']=='answered' and (parent['diagnostic_item_id']==current['diagnostic_item_id']) == revision,
                 'revision_lineage_question_changed')
        current_node=c.execute('SELECT knowledge_node_id FROM diagnostic_item WHERE diagnostic_item_id=?',(current['diagnostic_item_id'],)).fetchone()
        _require(current_node and current_node[0]==item['knowledge_node_id'],'teaching_lineage_node_changed')
        previous=c.execute('SELECT * FROM diagnostic_attempt WHERE attempt_id=? AND child_id=? AND session_id=?',
            (parent['attempt_id'],row['child_id'],parent_session)).fetchone()
        _require(previous and previous['evidence_id'] not in withdrawn_evidence_ids(c,row['child_id']), 'revision_original_withdrawn')
        # Retyping a local page into an editor does not make its derivatives public.
        if feedback and proof.get('version')=='daily_writing_followup_evidence_v1':
            # The closed-day manuscript is a local provenance dependency, not
            # a second text projection or permission to upload old-session work.
            original=proof['parent_original']
        else:
            original=reader(c,parent,previous,item)
        if reference:
            receipt,source=_reference_receipt(c,row['session_id'],parent,row['reference_turn_id'])
            proof=_revision_request(c,row['session_id'],parent,source,receipt,row['request_turn_id'],live=False)
            _require(stored.get('reference_turn_id')==row['reference_turn_id'] and stored.get('request_turn_id')==row['request_turn_id'], 'revision_lineage_changed')
            action=_reference_revision_action(parent,row['reference_turn_id'],row['request_turn_id'])
            _require(stored.get('binding_version')==_reference_revision_version(action,source,receipt,proof),
                     'revision_lineage_version_changed')
        if revision:
            hint=c.execute('SELECT hint_level FROM learning_evidence WHERE evidence_id=?', (nodes[current['prompt_id']]['attempt']['evidence_id'],)).fetchone()
            _require(hint and hint[0]>=1, 'revision_assistance_erased')
        links[current['prompt_id']]={'row':dict(row),'proof':proof}
        nodes[parent['prompt_id']]={'prompt':parent,'attempt':previous,'original':original}
        current=parent
    raise _Blocked('revision_lineage_too_deep')


def _objective(c, child_id, session_id, objective_id, prompt, attempt, origin, *, original_reader=None):
    objective = c.execute("SELECT * FROM agent_objective WHERE objective_id=? AND child_id=? AND session_id=?",
                          (objective_id,child_id,session_id)).fetchone()
    _require(objective and objective['objective_type'] == 'learning' and objective['status'] == 'active',
             'objective_not_current')
    _require(datetime.fromisoformat(objective['created_at']) <= datetime.fromisoformat(attempt['created_at']),
             'objective_created_after_answer')
    rows = c.execute('''SELECT p.plan_json,p.context_manifest_json,t.agent_turn_id,t.action_json,s.arguments_json,s.capability_name,p.status,
        p.plan_id,s.step_id,s.step_index,s.capability_version,s.execution_location,s.risk_level,s.expected_evidence_json
        FROM agent_plan p JOIN agent_turn t ON t.agent_turn_id=p.agent_turn_id
          AND t.child_id=p.child_id AND t.session_id=p.session_id
        JOIN agent_step s USING(plan_id) WHERE p.objective_id=? AND p.child_id=? AND p.session_id=?''',
        (objective_id,child_id,session_id)).fetchall()
    bound = False
    lineage,links=_revision_lineage(c,prompt,attempt,origin,original_reader=original_reader)
    exact = {'child_id':child_id,'session_id':session_id,'objective_id':objective_id,
        'prompt_id':prompt['prompt_id'],'diagnostic_prompt_id':prompt['prompt_id'],
        'source_prompt_id':prompt['prompt_id'],'item_version':prompt['item_version'],
        'diagnostic_item_version':prompt['item_version'],'diagnostic_item_id':prompt['diagnostic_item_id'],
        'evidence_id':attempt['evidence_id'],'attempt_id':attempt['attempt_id'],
        'writing_original_artifact_id':origin.get('artifact_id')}

    def inspect(value, *, envelope=False):
        if isinstance(value, list):
            for entry in value:
                inspect(entry)
        elif isinstance(value, dict):
            if not envelope:
                _require(value.get('local_only') is not True and value.get('cloud_transmission_allowed') is not False,
                         'nested_local_material')
            for key, entry in value.items():
                if key in exact and entry is not None:
                    _require(entry == exact[key], 'mixed_target_reference')
                _require(not (key in {'artifact_id','revision_id','ocr_confirmation_id','local_path','stimulus_text',
                    'stimulus_asset','knowledge_context','document_text','original_page','previous_explanation'} and entry),
                    'mixed_local_material')
                if key == 'source_ref':
                    _require(entry == exact.get('source_ref','hsc_original_v1'), 'mixed_local_source')
                inspect(entry)

    for row in rows:
        if row['capability_name']=='task.complete' and row['status']=='failed':
            from .oral_daily_check_projection import validate_pending_completion
            try:
                validate_pending_completion(c,row,prompt,objective_id)
            except (ValueError,TypeError,KeyError):
                _require(False,'daily_check_refusal_changed')
            continue
        scheduled = False
        document_open = (row['capability_name'] == 'learning.review_revisit'
            and isinstance(json.loads(row['context_manifest_json']).get('document_open_check'), dict))
        supply_delivery=(row['capability_name']=='task.resume'
            and isinstance(json.loads(row['context_manifest_json']).get('supply_resume_receipt'),dict))
        if row['capability_name']=='task.start' or supply_delivery:
            owned = c.execute('SELECT * FROM agent_plan WHERE plan_id=?',(row['plan_id'],)).fetchone()
            if json.loads(owned['context_manifest_json']).get('scheduled_writing_goal'):
                from .scheduled_writing_goal import validate_origin
                selected_id = json.loads(owned['plan_json']).get('local_learning_binding',{}).get('prompt_id')
                selected = lineage.get(selected_id)
                _require(selected, 'mixed_target_reference')
                try:
                    validate_origin(c,owned,selected['prompt'])
                except (ValueError,TypeError,KeyError):
                    _require(False, 'scheduled_writing_origin_changed')
                scheduled = True
        daily_proof=next((entry['proof'] for entry in links.values()
            if entry['proof'].get('version')=='daily_writing_followup_evidence_v1'
            and entry['proof'].get('request_turn_id')==row['agent_turn_id']),None)
        _require(row['capability_name'] in {'diagnostic.start','diagnostic.replay','diagnostic.submit',
            'oral.retry','oral.independent_check','learning.explain',
            'learning.evaluate_response','writing.read_original','writing.confirm_original','writing.revise_original','writing.independent_check',
            'conversation.respond','task.pause','task.resume','break.begin','break.resume','document.monitor_control'} or document_open
            or (row['capability_name']=='task.start' and (daily_proof or scheduled)), 'objective_has_unprojected_task')
        _require(row['status'] not in {'cancelled','failed'}, 'objective_task_not_current')
        try:
            plan, action, arguments, manifest = (json.loads(row[key]) for key in
                ('plan_json','action_json','arguments_json','context_manifest_json'))
        except (ValueError, TypeError):
            _require(False, 'objective_transport_invalid')
        _require(all(isinstance(value, dict) for value in (plan, action, arguments, manifest)),
                 'objective_transport_invalid')
        binding = plan.get('local_learning_binding')
        candidate=(binding or {}).get('prompt_id') or action.get('source_prompt_id') or (manifest.get('writing_reference') or {}).get('prompt_id')
        if row['capability_name']=='learning.explain':
            candidate=action.get('diagnostic_prompt_id')
            _require(candidate in lineage,'learning_help_origin_changed')
        if row['capability_name'] in {'task.pause','task.resume','break.begin','break.resume','document.monitor_control'} and not scheduled:
            candidate=(action.get('voice_input_binding') or {}).get('prompt_id')
            _require(candidate in lineage, 'scheduled_writing_control_changed')
        if candidate is None and row['capability_name'] in {'writing.read_original','writing.confirm_original'}:
            from .writing_ocr_authorization import _confirmation
            reference_key = 'writing_ocr_confirmation' if row['capability_name']=='writing.read_original' else 'writing_ocr_binding'
            reference = action.get(reference_key)
            _require(isinstance(reference,dict) and {'confirmation_id','binding_version'} <= set(reference),
                     'writing_transport_binding_missing')
            packet,_,_ = _confirmation(c,session_id,reference['confirmation_id'],reference['binding_version'])
            candidate = packet['prompt_id']
        if candidate is None and arguments.get('evidence_id'):
            candidate=next((key for key,node in lineage.items() if node['attempt']['evidence_id']==arguments['evidence_id']),None)
        selected=lineage.get(candidate or prompt['prompt_id'])
        _require(selected,'mixed_target_reference')
        selected_prompt,selected_attempt=selected['prompt'],selected['attempt']
        exact['source_ref'] = c.execute('SELECT source_ref FROM diagnostic_item WHERE diagnostic_item_id=?',
            (selected_prompt['diagnostic_item_id'],)).fetchone()[0]
        exact.update(prompt_id=selected_prompt['prompt_id'],diagnostic_prompt_id=selected_prompt['prompt_id'],
            source_prompt_id=selected_prompt['prompt_id'],item_version=selected_prompt['item_version'],
            diagnostic_item_version=selected_prompt['item_version'],diagnostic_item_id=selected_prompt['diagnostic_item_id'],
            evidence_id=selected_attempt['evidence_id'],attempt_id=selected_attempt['attempt_id'],
            writing_original_artifact_id=selected['original'].get('artifact_id'))
        if binding:
            _require(isinstance(binding.get('phase'),str) and bool(binding['phase'])
                and isinstance(binding.get('success_criteria'),list) and bool(binding['success_criteria'])
                and all(isinstance(point,str) and bool(point.strip()) for point in binding['success_criteria']),
                'objective_binding_schema_invalid')
            inspect(binding,envelope=True)
            _require(all(binding.get(key) == exact[key] for key in ('child_id','session_id','objective_id','prompt_id','item_version')),
                     'objective_prompt_binding_changed')
            if selected_prompt['prompt_id']==prompt['prompt_id']:
                bound = bound or row['capability_name'] in {'diagnostic.start','writing.read_original'}
        if row['capability_name']=='learning.explain':
            from .learning_question_help import teaching_receipt
            try:
                proof=teaching_receipt(c,row['agent_turn_id'],historical=True)
            except (ValueError,KeyError,TypeError):
                _require(False,'learning_help_origin_changed')
            _require(proof and proof['prompt_id']==candidate,'learning_help_origin_changed')
            # This is local assistance history, not mastery or cloud context.
            continue
        if row['capability_name'] in {'oral.retry','oral.independent_check'}:
            link = links.get(selected_prompt['prompt_id']) or {}
            proof = link.get('proof') or {}
            _require(binding and link.get('row',{}).get('request_turn_id')==row['agent_turn_id']
                and proof.get('objective_id')==objective_id
                and proof.get('binding')==action.get('oral_followup_binding'), 'oral_teaching_origin_not_bound')
            bound = bound or selected_prompt['prompt_id']==prompt['prompt_id']
            # The admitted command and its provenance stay local; only the
            # selected question, answer and rubric are projected below.
            continue
        if 'agent_runtime' in action:
            _require(isinstance(action['agent_runtime'], dict), 'runtime_transport_changed')
            step = plan.get('next_step')
            _require(isinstance(step,dict) and set(step)=={'capability','capability_version','arguments',
                'reason','expected_evidence','execution_location','risk_level'}
                and row['step_index']==0, 'runtime_transport_changed')
            expected_step = {'capability':row['capability_name'],'capability_version':row['capability_version'],
                'arguments':arguments,'reason':step['reason'],
                'expected_evidence':json.loads(row['expected_evidence_json']),
                'execution_location':row['execution_location'],'risk_level':row['risk_level']}
            _require(isinstance(step['reason'],str) and digest(step)==digest(expected_step),
                     'runtime_transport_changed')
            _require(isinstance(plan.get('objective'),dict) and type(plan.get('requires_child_reply')) is bool
                and isinstance(plan.get('after_execution'),str), 'runtime_transport_changed')
            decision_record = c.execute('''SELECT decision_record_id,child_id,session_id,agent_turn_id,
                selected_capability,selected_capability_version FROM agent_decision_record
                WHERE plan_id=?''', (row['plan_id'],)).fetchone()
            expected_transport = {'plan_id':row['plan_id'],'step_id':row['step_id'],'objective_id':objective_id,
                'objective':plan['objective'],'capability':expected_step,
                'requires_child_reply':plan['requires_child_reply'],'after_execution':plan['after_execution']}
            mirrored_record_id = action['agent_runtime'].get('decision_record_id')
            if mirrored_record_id is not None:
                _require(decision_record
                    and decision_record['decision_record_id'] == mirrored_record_id
                    and decision_record['child_id'] == child_id
                    and decision_record['session_id'] == session_id
                    and decision_record['agent_turn_id'] == row['agent_turn_id']
                    and decision_record['selected_capability'] == row['capability_name']
                    and decision_record['selected_capability_version'] == row['capability_version'],
                    'runtime_transport_changed')
                expected_transport['decision_record_id'] = mirrored_record_id
            else:
                # Plans created before decision_record_v1 remain readable only
                # when no newer immutable decision record exists for the plan.
                _require(decision_record is None, 'runtime_transport_changed')
            _require(digest(action['agent_runtime'])==digest(expected_transport), 'runtime_transport_changed')
            # Only the exact persisted transport mirror is redundant. Inspect its
            # arguments separately as a root envelope; nested material stays denied.
            inspect({key:value for key,value in expected_transport.items() if key!='capability'})
            inspect({key:value for key,value in expected_step.items() if key!='arguments'})
            if row['capability_name'] not in {'writing.read_original','writing.confirm_original','writing.revise_original','writing.independent_check'}:
                inspect(arguments,envelope=True)
            action = {key:value for key,value in action.items() if key!='agent_runtime'}
        if document_open:
            from .document_open_check import validate_projection
            try:
                validate_projection(c, row, selected_prompt)
            except (ValueError, TypeError, KeyError):
                _require(False, 'document_open_origin_changed')
            bound = bound or selected_prompt['prompt_id'] == prompt['prompt_id']
            continue
        if row['capability_name']=='task.resume' and scheduled:
            from .scheduled_writing_goal import supply_projection_receipt
            supply_projection_receipt(c,row,selected_prompt,arguments)
            bound=bound or selected_prompt['prompt_id']==prompt['prompt_id']
            continue
        if row['capability_name'] in {'task.pause','task.resume'}:
            from .scheduled_writing_goal import execution_control_receipt
            # Keep scheduled authority at its original prompt; only traverse
            # the already validated same-question revision chain.
            control_origin=selected_prompt
            while control_origin['prompt_id'] in links:
                edge=links[control_origin['prompt_id']]['row']
                _require(edge.get('kind','revision')=='revision','scheduled_writing_control_changed')
                control_origin=lineage[edge['parent_prompt_id']]['prompt']
            try:
                execution_control_receipt(c,row,control_origin,objective_id,control_prompt=selected_prompt)
            except (ValueError,TypeError,KeyError):
                _require(False,'scheduled_writing_control_changed')
            _require(arguments=={'local_only':True,'cloud_transmission_allowed':False},'scheduled_writing_control_changed')
            continue
        if row['capability_name'] in {'break.begin','break.resume'}:
            from .scheduled_break_projection import validate_break_control
            control_origin=selected_prompt
            while control_origin['prompt_id'] in links:
                edge=links[control_origin['prompt_id']]['row']
                _require(edge.get('kind','revision')=='revision','scheduled_break_origin_changed')
                control_origin=lineage[edge['parent_prompt_id']]['prompt']
            try:
                validate_break_control(c,row,control_origin,objective_id,control_prompt=selected_prompt)
            except (ValueError,KeyError,TypeError):
                _require(False,'scheduled_break_control_changed')
            continue
        if row['capability_name']=='document.monitor_control':
            from .paper_writing_control_projection import validate_document_control
            validate_document_control(c,row,selected_prompt)
            continue
        if row['capability_name']=='conversation.respond':
            # Older Runtime versions attached neutral chat to an active goal.
            # Validate its original transport, but do not turn it into teaching
            # evidence or demand a writing-reference receipt that never existed.
            neutral = {'mode':'none','type':None,'summary':None,'success_criteria':[],'phase':'execute'}
            if (plan.get('objective') == neutral and 'local_learning_binding' not in plan
                    and action.get('type') == 'none' and action.get('source') == 'agent_runtime_planner'
                    and 'writing_reference' not in action and 'writing_reference' not in manifest
                    and 'agent_runtime' in json.loads(row['action_json'])):
                receipt = c.execute('''SELECT t.tool_status,t.response_text,s.status FROM agent_turn t
                    JOIN agent_plan p USING(agent_turn_id) JOIN agent_step s USING(plan_id)
                    WHERE t.agent_turn_id=? AND p.plan_id=? AND s.step_id=?''',
                    (row['agent_turn_id'],row['plan_id'],row['step_id'])).fetchone()
                _require(receipt and row['status']=='completed' and receipt['tool_status']=='completed'
                    and receipt['status']=='completed' and set(arguments)=={'response'}
                    and arguments['response']==receipt['response_text'], 'neutral_conversation_receipt_changed')
                inspect(action,envelope=True)
                inspect(manifest,envelope=True)
                continue
            from .writing_teaching import _reference_receipt
            _reference_receipt(c,session_id,selected_prompt,row['agent_turn_id'])
            continue
        if row['capability_name']=='task.start' and scheduled:
            _require(binding and arguments in ({},
                {'plan_item_id':manifest['scheduled_writing_goal']['plan_item_id']},
                {'local_only':True,'cloud_transmission_allowed':False}),
                'scheduled_writing_transport_changed')
            bound=bound or selected_prompt['prompt_id']==prompt['prompt_id']
        elif row['capability_name']=='task.start' and daily_proof:
            _require(binding and daily_proof['packet']==action.get('writing_followup_binding')
                and arguments in ({}, {'local_only':True,'cloud_transmission_allowed':False}),
                'daily_followup_transport_changed')
            action={key:value for key,value in action.items() if key not in {'writing_followup_binding','voice_input_binding'}}
            bound=bound or selected_prompt['prompt_id']==prompt['prompt_id']
        elif row['capability_name'] in {'writing.revise_original','writing.independent_check'} and action.get('writing_followup_binding'):
            link=links.get(selected_prompt['prompt_id'])
            proof=(link or {}).get('proof') or {}
            packet=action['writing_followup_binding']
            _require(proof.get('request_turn_id')==row['agent_turn_id'] and proof.get('packet')==packet,
                     'formal_followup_origin_not_bound')
            recognized={'writing_followup_binding','writing_followup_semantic_receipt'}
            for key in recognized & arguments.keys():
                _require(key in action and digest(arguments[key])==digest(action[key]), 'formal_followup_transport_changed')
            if 'agent_runtime' in json.loads(row['action_json']):
                _require('writing_followup_binding' in arguments,'formal_followup_transport_changed')
            action={key:value for key,value in action.items() if key not in recognized | {'voice_input_binding'}}
            arguments={key:value for key,value in arguments.items() if key not in recognized}
            if binding and selected_prompt['prompt_id']==prompt['prompt_id']:
                bound=True
        elif row['capability_name']=='writing.revise_original':
            link=links.get(selected_prompt['prompt_id'])
            _require(link and link['row'].get('request_turn_id')==row['agent_turn_id'], 'revision_origin_not_bound')
            if binding and selected_prompt['prompt_id']==prompt['prompt_id']:
                bound=True
            parent=lineage[link['row']['parent_prompt_id']]
            exact.update(prompt_id=parent['prompt']['prompt_id'],item_version=parent['prompt']['item_version'],
                diagnostic_prompt_id=parent['prompt']['prompt_id'],diagnostic_item_version=parent['prompt']['item_version'])
            action={k:v for k,v in action.items() if k not in {'writing_revision_binding','writing_revision_semantic_receipt','voice_input_binding'}}
        elif row['capability_name'].startswith('writing.'):
            from .writing_ocr_authorization import _confirmation
            reference_key = 'writing_ocr_confirmation' if row['capability_name']=='writing.read_original' else 'writing_ocr_binding'
            reference = action.get(reference_key)
            _require(isinstance(reference,dict) and {'confirmation_id','binding_version'} <= set(reference), 'writing_transport_binding_missing')
            if row['capability_name']=='writing.read_original':
                _require(set(reference)=={'confirmation_id','binding_version'}, 'writing_transport_binding_changed')
            packet,_,_ = _confirmation(c,session_id,reference['confirmation_id'],reference['binding_version'])
            _require(packet['child_id']==child_id and packet['prompt_id']==selected_prompt['prompt_id']
                and packet['item_version']==selected_prompt['item_version'], 'writing_transport_binding_changed')
            # Recognizing the read origin never authorizes that local page's text.
            _require(selected['original'].get('kind')=='confirmed_document', 'mixed_written_originals')
            audit = selected['original'].get('paper_text_projection') or {}
            if row['capability_name']=='writing.read_original' and packet['confirmation_id']!=audit.get('confirmation_id'):
                from .paper_writing_review_projection import validate_read_ancestor
                validate_read_ancestor(c,session_id,packet,audit)
            else:
                _require(all(packet[key]==audit.get(key) for key in ('confirmation_id','artifact_id','revision_id')),
                         'writing_transport_binding_changed')
            recognized = {reference_key}
            if row['capability_name']=='writing.confirm_original':
                # _original has already revalidated this exact event's source,
                # readback, original voice and semantic receipt, not just its IDs.
                event = c.execute("SELECT payload_json FROM writing_ocr_event WHERE event_id=? AND confirmation_id=? AND child_id=? AND kind='authorize'",
                    (audit.get('use_authorization_id'),packet['confirmation_id'],child_id)).fetchone()
                authority = json.loads(event[0]) if event else {}
                _require(authority.get('authorizing_turn_id')==row['agent_turn_id']
                    and digest(authority.get('voice_binding'))==digest(reference), 'writing_transport_binding_changed')
                recognized.add('writing_ocr_semantic_receipt')
            if 'agent_runtime' in json.loads(row['action_json']):
                _require(reference_key in arguments, 'writing_transport_binding_missing')
            for key in recognized & arguments.keys():
                _require(key in action and digest(arguments[key])==digest(action[key]), 'writing_transport_binding_changed')
            action = {key:value for key,value in action.items() if key not in recognized}
            arguments = {key:value for key,value in arguments.items() if key not in recognized}
        inspect(action,envelope=True)
        inspect(arguments,envelope=True)
    _require(bound, 'original_learning_objective_missing')
    return {'objective':dict(objective),'plans':[dict(row) for row in rows],
            'lineage':{key:{'prompt':dict(node['prompt']),'original':node['original']} for key,node in lineage.items()},'links':links}


def visual_material_projection(connection, *, child_id, source, candidate_id=None):
    """Local handoff only. Text-purpose grants cannot authorize diagram bytes.

    The future multimodal host must bind PURPOSE, crop hash, confirmed facts,
    source rights, original response and current consent at dispatch/persist.
    No image or fact description is silently appended to the old text payload.
    """
    from .visual_material import PURPOSE
    from .open_response_materials import resolve_source_materials
    base = dict(status='local_required', payload=None, required_purpose=PURPOSE,
                local_only=True, cloud_transmission_allowed=False, automatic_certification=False)
    try:
        material = resolve_source_materials(connection,child_id=child_id,source=source,candidate_id=candidate_id)
        return {**base,'reason':'visual_purpose_authorization_and_transport_required',
                'local_audit':material,'projection_version':digest({'purpose':PURPOSE,'material':material})}
    except LocalSupplyError as error:
        return {**base,'reason':str(error),'local_audit':None}


def build_formative_projection(connection, *, child_id, session_id, prompt_id, item_version, objective_id):
    """Read-only. Transmit only `payload`; `local_audit` must never enter messages.

    No fallback to current task, question text matching, model-parsed references,
    or an unrelated historical recording. No score or automatic certification.
    """
    c = connection
    if not c.in_transaction:
        c.execute('BEGIN')
        try:
            return build_formative_projection(c,child_id=child_id,session_id=session_id,prompt_id=prompt_id,
                item_version=item_version,objective_id=objective_id)
        finally:
            c.rollback()
    base = dict(version=VERSION,status='local_required',reason=None,payload=None,
                local_only=True,cloud_transmission_allowed=False,automatic_certification=False)
    try:
        session = c.execute('''SELECT ds.*,cp.nickname,cp.status AS child_status FROM daily_session ds
            JOIN child_profile cp USING(child_id) WHERE ds.session_id=? AND ds.child_id=?''', (session_id,child_id)).fetchone()
        _require(session and session['status']=='active' and session['child_status']=='active'
                 and session['state'] not in {'evolving','completed'}, 'session_not_active')
        privacy = c.execute('SELECT * FROM privacy_preference WHERE singleton_id=1').fetchone()
        _require(privacy and privacy['cloud_model_text_allowed'] == 1, 'current_text_consent_required')
        consent = c.execute("SELECT * FROM consent_record WHERE scope='cloud_model_text_allowed' ORDER BY rowid DESC LIMIT 1").fetchone()
        _require(not consent or consent['allowed'] == 1, 'text_consent_withdrawn')
        prompt = c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=? AND child_id=? AND origin_session_id=?',
                            (prompt_id,child_id,session_id)).fetchone()
        _require(prompt and prompt['status']=='answered' and prompt['item_version']==item_version, 'prompt_not_answered_or_changed')
        item = validate_item(c,child_id,prompt['diagnostic_item_id'])
        if item['answer_type']=='open_response' and json.loads(item['expected_answer_json']).get('material_schema') == 'reviewed_open_response_source_v3':
            raise _Blocked('visual_purpose_authorization_and_transport_required')
        current = DiagnosticPromptService._item(c,child_id,prompt['diagnostic_item_id'])
        _require(_version(current)==item_version, 'question_version_changed')
        node = c.execute('SELECT * FROM knowledge_node WHERE knowledge_node_id=?',(item['knowledge_node_id'],)).fetchone()
        attempt = c.execute('SELECT * FROM diagnostic_attempt WHERE attempt_id=? AND child_id=? AND session_id=?',
                             (prompt['attempt_id'],child_id,session_id)).fetchone()
        _require(attempt and attempt['diagnostic_item_id']==item['diagnostic_item_id'], 'original_attempt_missing')
        evidence = c.execute('SELECT * FROM learning_evidence WHERE evidence_id=? AND child_id=? AND session_id=?',
                              (attempt['evidence_id'],child_id,session_id)).fetchone()
        _require(evidence and attempt['evidence_id'] not in withdrawn_evidence_ids(c,child_id), 'evidence_withdrawn_or_missing')
        original = _original(c,prompt,attempt,item)
        context = _objective(c,child_id,session_id,objective_id,prompt,attempt,original)
        if item['answer_type']=='open_response':
            from .open_response_profile import profile_for_item
            dimensions = profile_for_item(item,dict(node))['dimensions']
        else:
            dimensions = rubric_dimensions(node['subject'])
        _require(dimensions, 'rubric_unavailable')
        points = json.loads(item['expected_answer_json']).get('task_points',[]) if item['answer_type'] in {'writing','open_response'} else []
        names = [r[0] for r in c.execute('SELECT nickname FROM child_profile')]
        redactions = []

        def clean(text):
            _require(not sensitive_categories(text), 'sensitive_response_requires_local_support')
            sanitized = sanitize_cloud_text(text,names)
            # The shared filter treats a sentence's final dot as part of email.
            # Close that boundary here without modifying the stored original.
            safe, count = re.subn(r'(?<![\w.-])[\w.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![\w-])',
                                  '[邮箱已脱敏]',sanitized.text)
            redactions.append(sanitized.redaction_count + count)
            return safe

        from .writing_question_authorization import question_text
        payload = {'question':clean(question_text(item)),'response':clean(attempt['submitted_answer']),
                   'rubric':{'dimensions':dimensions,'task_points':[clean(point) for point in points]}}
        audit = {'child_id':child_id,'session_id':session_id,'prompt_id':prompt_id,'item_version':item_version,
                 'attempt_id':attempt['attempt_id'],'evidence_id':attempt['evidence_id'],'objective_id':objective_id,
                 'original':original,'redaction_count':sum(redactions),
                 'source_version':digest({'item':item,'node':dict(node),'attempt':dict(attempt),'evidence':dict(evidence)}),
                 'context_version':digest(context),'consent_version':digest({'privacy':dict(privacy),'consent':dict(consent) if consent else None}),
                 'local_only':True,'cloud_transmission_allowed':False}
        version = digest({'version':VERSION,'payload':payload,'audit':audit})
        return {**base,'status':'ready_for_runtime_checks','projection_version':version,'payload':payload,'local_audit':audit,
                'required_checks':['runtime_model_policy','budget','source_and_consent_revalidation_before_dispatch']}
    except (_Blocked, LocalSupplyError) as error:
        return {**base,'reason':str(error)}
    except ValueError as error:
        if str(error).startswith(('WRITING_','PET_SPEAKING_','VOICE_CAPTURE_','ANSWER_VOICE_','OPEN_RESPONSE_')):
            return {**base,'reason':str(error)}
        raise


def revalidate_formative_projection(connection, *, projection_version, **scope):
    """Rebuild rather than trust a cached body; never release a changed payload."""
    current = build_formative_projection(connection,**scope)
    if current['status']=='ready_for_runtime_checks' and current['projection_version'] != projection_version:
        return {**current,'status':'local_required','reason':'projection_changed','payload':None}
    return current
