"""Parent's live, read-only view of a child's saved oral-question request."""
import json
from datetime import datetime, timezone

from .local_supply import digest
from .oral_supply_obligation import ORIGIN, LOCAL
from .family_clock import evidence_calendar_date


def current_followups(c, child_id):
    from .agent_runtime import AgentRuntimeStore
    if not c.in_transaction:
        raise ValueError('ORAL_SUPPLY_FOLLOWUP_TRANSACTION_REQUIRED')
    result = []
    rows = c.execute('''SELECT ac.*,ao.session_id,ao.summary,ao.status AS goal_status,
        ds.status AS session_status FROM agent_commitment ac
        JOIN agent_objective ao ON ao.objective_id=ac.objective_id AND ao.child_id=ac.child_id
        JOIN daily_session ds ON ds.session_id=ao.session_id AND ds.child_id=ac.child_id
        WHERE ac.child_id=? AND CASE WHEN json_valid(ac.payload_json)
        THEN json_extract(ac.payload_json,'$.origin') END=? ORDER BY ac.created_at,ac.commitment_id''', (child_id,ORIGIN))
    for row in rows:
        payload = json.loads(row['payload_json'])
        source = payload.get('source')
        if (not isinstance(source,dict) or source.get('child_id')!=child_id
                or source.get('session_id')!=row['session_id'] or payload.get('source_hash')!=digest(source)):
            raise ValueError('ORAL_SUPPLY_FOLLOWUP_SOURCE_CHANGED')
        original = c.execute('''SELECT p.*,n.subject FROM diagnostic_prompt p
            JOIN diagnostic_item d ON d.diagnostic_item_id=p.diagnostic_item_id
            JOIN knowledge_node n ON n.knowledge_node_id=d.knowledge_node_id
            WHERE n.knowledge_node_id=? AND p.prompt_id=? AND p.child_id=? AND p.origin_session_id=?''',
            (source.get('knowledge_node_id'),source.get('parent_prompt_id'),child_id,row['session_id'])).fetchone()
        state, message = 'source_changed','原题记录或来源需核对；不从旧请求直接开始新题。'
        question, subject, material = '', '', None
        if original:
            frozen = json.loads(original['item_json'])
            if not isinstance(frozen,dict):
                raise ValueError('ORAL_SUPPLY_FOLLOWUP_SOURCE_CHANGED')
            question = frozen.get('prompt','')
            if not isinstance(question,str):
                raise ValueError('ORAL_SUPPLY_FOLLOWUP_SOURCE_CHANGED')
            subject = original['subject']
            material = c.execute('''SELECT p.candidate_id FROM curriculum_supply_package p JOIN diagnostic_item d
                ON d.source_ref='parent_approved_specialized:'||p.candidate_id
                WHERE p.child_id=? AND d.diagnostic_item_id=?''', (child_id,original['diagnostic_item_id'])).fetchone()
            if row['status']=='cancelled' or row['goal_status'] in ('cancelled','failed'):
                state, message = 'cancelled','原目标已结束或取消；请求记录保留，不使用旧请求重新开始。'
            elif row['status']=='completed' and payload.get('phase')=='question_prepared':
                resolution = payload.get('resolution') or {}
                if not isinstance(resolution,dict):
                    raise ValueError('ORAL_SUPPLY_FOLLOWUP_SOURCE_CHANGED')
                prepared = c.execute('''SELECT * FROM diagnostic_prompt WHERE prompt_id=? AND child_id=?
                    AND origin_session_id=? AND item_version=? AND diagnostic_item_id=?''',
                    (resolution.get('prompt_id'),child_id,row['session_id'],resolution.get('item_version'),resolution.get('diagnostic_item_id'))).fetchone()
                if prepared and prepared['status'] in ('answered','answer_saved'):
                    state, message = 'practice_completed','新题已有作答记录；作答不等于掌握，仍需检查反馈与后续保持。'
                elif row['goal_status']=='completed':
                    state, message = 'cancelled','原目标已结束；请求记录保留，不使用旧请求重新开始。'
                elif row['session_status']!='active':
                    state, message = 'needs_plan','已准备的新题保留；跨日接续仍需核对原目标和今天安排，本页不自动启动。'
                elif row['goal_status']=='paused':
                    state, message = 'paused','原目标已暂停，新题保留；等孩子愿意继续，不追加另一项练习。'
                elif prepared and prepared['status']=='waiting_answer':
                    from .diagnostic_prompt import DiagnosticPromptService, _version
                    from .specialized_stages import _item_source
                    current = DiagnosticPromptService._item(c,child_id,prepared['diagnostic_item_id'])
                    current_source = _item_source(c,child_id,current,datetime.now(timezone.utc))[0] if current else None
                    if current_source and current_source['score_qualified'] and _version(current)==prepared['item_version']:
                        state, message = 'ready','新题已准备，等待孩子作答；不要重复出题或代答。'
            elif row['goal_status']=='completed':
                state, message = 'cancelled','原目标已结束；请求记录保留，不使用旧请求重新开始。'
            elif row['session_status']!='active':
                state, message = 'needs_plan','原请求与回答已保留；跨日接续仍需核对原目标和今天安排，本页不自动启动。'
            elif row['goal_status']=='paused':
                state, message = 'paused','原目标已暂停，原回答保留；等孩子愿意继续，不追加另一项练习。'
            elif original['item_version']==source.get('parent_item_version'):
                packet = AgentRuntimeStore.resolve_oral_followup(c,child_id=child_id,session_id=row['session_id'],
                    prompt_id=original['prompt_id'],item_version=original['item_version'],kind='independent_check')
                if packet.get('status')=='ready':
                    state, message = 'ready','合格新题已可用；系统会在合适时机询问孩子是否继续原练习，不提前开始或认定掌握。'
                elif packet.get('reason')=='oral_no_unexposed_source':
                    state, message = 'missing_source','缺少同能力且未展示的新题。核对原题材料，补入不同题目并校验、审核发布；原回答不重做。'
                elif packet.get('reason')=='oral_current_focus_changed':
                    state, message = 'needs_replan','孩子当前已转到别的题目；原请求保留，不能覆盖正在学习的内容。'
        view = dict(child_id=child_id,root_obligation_id=row['commitment_id'],original_session_id=row['session_id'],
            original_date=str(evidence_calendar_date(row['created_at'])),title=row['summary'],
            original_reason='孩子明确要求一道同能力、没做过的新题，当时没有合格题目。',
            decision_id=payload['request_turn_id'],current_session_id=row['session_id'] if row['session_status']=='active' else None,
            plan_item_id=None,status=state,deferral_reason='原学习目标与原回答保留。',next_action=message,
            original_question=question,subject=subject,
            material_object_id=material['candidate_id'] if material else 'selection:'+child_id,**LOCAL)
        view['version']='oral_supply_followup_v1:'+digest({**view,'receipt':payload.get('resolution')})
        if row['status']=='completed' and payload.get('phase')=='question_prepared' and (payload.get('resolution') or {}).get('session_id'):
            from .oral_supply_execution import delivery_projection
            from .oral_delivery_resume import lineage
            latest,lineage_version=payload['resolution'],None
            try:
                latest,lineage_version=lineage(c,child_id,row['commitment_id'])
                current=delivery_projection(c,child_id,row['commitment_id'],latest)
            except (ValueError,KeyError,TypeError):
                current=dict(status='source_changed',next_action='跨日交付的来源或回执需核对，实际记录保留，不重复出题。')
            view.update(current)
            view['version']='oral_supply_followup_v1:'+digest({**view,'receipt':latest,'lineage_version':lineage_version})
        if original and state!='source_changed' and row['status'] in ('pending','due') and row['goal_status'] in ('active','paused'):
            from .oral_supply_carryover import current_plan, projection
            carried = current_plan(c,child_id,row['commitment_id'],payload['source_hash'])
            if carried and carried['session_id']!=row['session_id']:
                current = projection(c,child_id,carried['session_id'],row['commitment_id'])
                view.update(current_session_id=carried['session_id'],status=current['status'],next_action=current['message'])
                view['version']='oral_supply_followup_v1:'+digest({**view,'carryover':carried['decision_id'],
                    'source_check':current['version']})
        result.append(view)
    return result
