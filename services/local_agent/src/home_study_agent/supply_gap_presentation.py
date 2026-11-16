"""Local current work projection; immutable scheduling history stays intact."""
import json

from .local_supply import LocalSupplyError, digest, validate_item


MESSAGES = {
    'ORAL_DAILY_CHECK_FEEDBACK_REQUIRED': '回答已保存，请先核对本次反馈，再结束复查；不用重新回答。',
    'SUPPLY_RESUME_ORAL_SOURCE_CHANGED': '原口述练习的题目、回答或安排需要重新核对，已保存的回答不会丢失，也不会换到另一题。',
    'SUPPLY_RESUME_BUDGET_UNKNOWN': '今天还能练习多久尚未确认，原任务已保留，确认可用时间后再继续。',
    'SUPPLY_RESUME_BUDGET_INSUFFICIENT': '今晚剩余时间或单次练习上限不够完整做这项，原任务已保留，不会缩短题目或延后休息时间。',
    'SUPPLY_RESUME_PLAN_CHANGED': '今天的计划已经变化，原进度已保留，请重新确认要继续的任务。',
    'SUPPLY_RESUME_SOURCE_CHANGED': '这项题目来源已变化或不再可用，原进度已保留，不会自动换题；材料重新核实后再继续。',
    'SUPPLY_RESUME_AUTHORIZATION_CHANGED': '这次操作的许可已变化，原进度已保留；当前许可确认后，需要重新说出要继续的任务。',
    'TASK_START_LEGACY_REVIEW_CHANGED': '原练习或材料已变化，进度还在，不会自动换题；先核对原练习再继续。',
    'TASK_START_LEGACY_REVIEW_VOICE_REQUIRED': '我需要听到你现在的确认，请告诉我想继续哪项练习。',
    'TASK_START_LEGACY_REVIEW_VOICE_CHANGED': '刚才的请求已经变化，请重新告诉我想继续哪项练习。',
    'TASK_START_LEGACY_REVIEW_PLAN_CHANGED': '今天的安排已经调整，请告诉我现在想继续哪项练习。',
    'TASK_START_LEGACY_REVIEW_BUDGET_UNKNOWN': '还没确定今天可用的学习时间，先请家长确认，原练习不会丢失。',
    'TASK_START_LEGACY_REVIEW_BUDGET_INSUFFICIENT': '今天剩余时间不够完成这项练习，先保留，不挤占休息时间。',
    'TASK_START_LEGACY_REVIEW_AUTHORIZATION_CHANGED': '材料的使用许可已变化，先请家长核对；现在不会开始这项练习。',
    'TASK_START_LEGACY_REVIEW_ACTIVE_TASK': '你还有正在进行的学习，先暂停或结束它，再继续这项练习。',
    'TASK_START_LEGACY_REVIEW_PENDING_QUESTION': '还有一道题正在等待回答，你想先答完还是暂时放下它？',
    'ANSWER_ORAL_FOLLOWUP_AUTHORITY_CHANGED': '口语练习的会话已变化，先确认当前孩子和练习，再继续。',
    'ANSWER_ORAL_FOLLOWUP_COMMAND_CHANGED': '刚才的口语练习请求已经变化，请告诉我现在想练什么。',
    'ANSWER_ORAL_FOLLOWUP_GOAL_CHANGED': '当前学习目标已变化，请告诉我想继续原来的口语练习，还是当前学习。',
    'ANSWER_ORAL_FOLLOWUP_CONSENT_CHANGED': '原口语材料的使用许可已变化，先请家长核对，现在不会开始新的练习。',
    'ANSWER_ORAL_FOLLOWUP_PROMPT_CHANGED': '当前口语题目已经变化，请告诉我想继续哪一道；不会把回答放到别的题目里。',
    'ANSWER_ORAL_FOLLOWUP_VOICE_CHANGED': '刚才的口语练习请求已经被后来的话更新，请告诉我现在想怎么继续。',
    'ANSWER_ORAL_FOLLOWUP_SOURCE_CHANGED': '原口语材料已变化或不再可用，先核对材料，不会自动换题。',
    'ANSWER_ORAL_FOLLOWUP_CANCELLED': '刚才的口语练习请求已取消，没有重新开始。你准备好后可以告诉我想练什么。',
    'ANSWER_ORAL_FOLLOWUP_FAILED_COMMAND': '上次的口语练习请求没有重新执行。请先告诉我现在看到哪一道题，再确认下一步。',
    'ANSWER_ORAL_FOLLOWUP_COMMAND_REQUIRED': '我需要你现在的确认：想根据反馈再说一次，还是另做一道题？',
}


def conflict_message(code):
    return MESSAGES.get(code, '任务或操作授权已经变化，未继续旧指令。已保存的实际进度保留，请核对当前任务。')


def project_supply_obligations(c, session_id, child_id, plan_id):
    from .diagnostic_prompt import DiagnosticPromptService, _version
    from .supply_gap_resume import VERSION, candidates, budget
    from .task_start_binding import TaskStartBindingError
    from .supply_obligation_history import is_obligation, validate_inherited

    owns = not c.in_transaction
    if owns:
        c.execute('BEGIN')
    try:
        plan=c.execute('''SELECT * FROM daily_plan WHERE plan_id=? AND session_id=?
            AND child_id=? AND status='active' ''',(plan_id,session_id,child_id)).fetchone()
        if not plan:
            return []
        rows=[dict(row) for row in c.execute('SELECT * FROM plan_constraint_decision WHERE plan_id=? ORDER BY rowid',
            (plan_id,)).fetchall()]
        gaps=[row for row in rows if is_obligation(row)]
        if not gaps:
            return []
        available={item['obligation_id']:item for item in candidates(c,session_id,child_id)}
        result=[]
        for gap in gaps:
            if gap['source_ref']=='pet-skill-gap:writing' and any(str(g['source_ref']).startswith('pet-writing-gap:') for g in gaps):
                continue
            metadata=json.loads(gap['metadata_json'] or '{}')
            skill=str(gap['source_ref']).split(':')[-1]
            label={'reading':'阅读','listening':'听力','speaking':'口语','writing':'写作'}.get(skill,'写作')
            current={'obligation_id':gap['decision_id'],'title':metadata.get('title') or f'PET {label}练习',
                'status':'missing_source','message':'原练习已保留，仍需合格题目材料。','plan_item_id':None,
                'task_status':None,'local_only':True,'cloud_transmission_allowed':False,'mastery_claim_allowed':False}
            carried = metadata.get('supply_carryover') or {}
            if carried:
                current.update(root_obligation_id=carried['root_decision_id'], carryover_kind=carried['kind'],
                    original_session_id=carried['root_session_id'])
                if not validate_inherited(c, child_id, gap):
                    current.update(status='source_changed', message=MESSAGES['SUPPLY_RESUME_SOURCE_CHANGED'])
                    result.append(current)
                    continue
            placed=[]
            for row in rows:
                receipt=json.loads(row['metadata_json'] or '{}')
                binding=receipt.get('supply_resume') or {}
                if row['decision']=='scheduled' and binding.get('obligation_id')==gap['decision_id']:
                    placed.append((receipt,binding))
            if placed:
                current.update(status='needs_replan',message='原安排需要重新核对，已保存进度，不会自动换题。')
                if len(placed)==1:
                    receipt,binding=placed[0]
                    task=c.execute('SELECT * FROM plan_item WHERE plan_id=? AND plan_item_id=?',
                        (plan_id,receipt.get('plan_item_id'))).fetchone()
                    valid=(binding.get('version')==VERSION and binding.get('child_id')==child_id
                        and binding.get('session_id')==session_id and binding.get('obligation_version')==digest(gap)
                        and task and receipt.get('plan_item_id')==binding.get('plan_item_id')
                        and all(task[k]==binding.get(k) for k in ('plan_id','source_ref','subject','task_type','expected_minutes')))
                    if valid:
                        current.update(plan_item_id=task['plan_item_id'],task_status=task['status'],title=task['title'])
                        item=DiagnosticPromptService._item(c,child_id,task['source_ref'])
                        source_ok=bool(item and _version(item)==binding.get('item_version'))
                        if source_ok:
                            try:
                                validate_item(c,child_id,task['source_ref'])
                            except LocalSupplyError:
                                source_ok=False
                        states={
                            'pending':('scheduled','已补题并安排，等待开始原练习。'),
                            'active':('active','已补题并安排，正在练习。'),
                            'paused':('paused','原练习已安排并暂停，进度保留，可以继续原任务。'),
                            'completed':('practice_completed','这项练习已完成；练习完成不等于已经掌握。'),
                        }
                        if not source_ok:
                            current.update(status='source_changed',message=MESSAGES['SUPPLY_RESUME_SOURCE_CHANGED'])
                        elif task['status'] in states:
                            current.update(zip(('status','message'),states[task['status']]))
            elif gap['decision_id'] in available:
                try:
                    budget(c,available[gap['decision_id']])
                    current.update(status='ready',message='已补好题目；可以说继续原练习，确认后才会安排。')
                except TaskStartBindingError as error:
                    current.update(status='waiting_constraints',message=conflict_message(str(error)))
            result.append(current)
        return result
    finally:
        if owns:
            c.rollback()
