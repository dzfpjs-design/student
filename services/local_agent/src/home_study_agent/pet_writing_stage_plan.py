"""Read-only writing tasks from the existing qualified specialized-stage projection."""
from .specialized_stages import project_specialized_stages


WRITING_STAGE_MINUTES = 12  # Existing PET scheduling estimate, not an exam time limit.
STAGE_FIELDS = ('domain_id', 'knowledge_node_id', 'diagnostic_item_id', 'source_version',
                'review_task_id', 'not_before', 'execution_status', 'mastery_claim_allowed')
CONSTRAINT_FIELDS = ('version', 'current_stage', 'prerequisite_version')


def project_writing_stage_plan(connection, child_id, *, as_of):
    child = connection.execute("SELECT grade FROM child_profile WHERE child_id=? AND status='active'", (child_id,)).fetchone()
    if not child or child['grade'] != '四年级':
        return {'items': [], 'unplaced_obligations': [], 'source_available': False}
    projection = project_specialized_stages(connection, child_id, 'pet', as_of=as_of)
    ready, unplaced, source_available = [], [], False
    for domain in projection['domains']:
        if not domain['domain_id'].startswith('pet.writing.'):
            continue
        for node in domain['nodes']:
            if node['current_stage'] == 'maintenance':
                source_available = True
                continue
            action = node['next_action']
            evidence = {key: action[key] for key in STAGE_FIELDS}
            evidence['stage_constraint'] = {key: action['stage_constraint'][key] for key in CONSTRAINT_FIELDS}
            item = dict(homework_id=None, subject='英语', title='PET写作 · ' + domain['label'],
                minutes=WRITING_STAGE_MINUTES, source_type='pet_daily',
                source_ref=action['diagnostic_item_id'] or 'pet-writing-gap:' + node['knowledge_node_id'],
                completion_standard='完成计划绑定的原题并保存书面原稿；反馈、时长或完成任务不代表阶段已合格。',
                cognitive_load='medium', task_type='writing', status='pending', planning_evidence=evidence)
            if action['execution_status'] == 'ready':
                source_available = True
                ready.append(item)
            elif action['execution_status'] == 'waiting_due':
                source_available = True
                unplaced.append({**item, 'reason_code': 'stage_not_due',
                    'reason': '尚未到原定写作复测时间，保留原题和日期，不提前检查。',
                    'blockers': list(action['blockers'])})
            else:
                unplaced.append({**item, 'reason_code': 'writing_stage_source_unavailable',
                    'reason': '当前写作阶段缺少合格原题或前置依据，保留供给缺口，不以词汇训练替代。',
                    'blockers': list(action['blockers'])})
    ready.sort(key=lambda item: (item['planning_evidence']['knowledge_node_id'], item['source_ref']))
    return {'items': ready, 'unplaced_obligations': unplaced, 'source_available': source_available}
