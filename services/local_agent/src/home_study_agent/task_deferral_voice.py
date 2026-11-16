"""A spoken deferral refers to one frozen paused task, never a later next task."""
import re

CONTROL = 'task.reschedule'
PROMPT = ('task.reschedule表示明确把当前唯一暂停任务延期并保留进度，不表示完成、删除或恢复。'
          '也可在刚才已说明原题不可用时，明确把那项未开始的任务延期；目标和原题状态由本机核验。'
          '其他进行中的任务须先暂停；不确定是哪项、多项、否定、引用或假设用mixed/unknown。')


def snapshot(connection, child_id, session_id, *, before_turn=None):
    rows = connection.execute('''SELECT i.*,p.child_id,p.session_id,p.version AS plan_version
        FROM plan_item i JOIN daily_plan p USING(plan_id)
        WHERE p.child_id=? AND p.session_id=? AND p.status='active'
        AND i.status='paused' AND i.source_type!='planned_break' ORDER BY i.plan_item_id''',
        (child_id, session_id)).fetchall()
    if rows:
        return dict(rows[0]) if len(rows) == 1 else None
    from .task_source_recovery import deferral_target
    return deferral_target(connection, child_id, session_id, before_turn=before_turn)


def valid_words(transcript):
    return not re.search(r'[“”「」『』"]|(?:假如|假设|如果|他说|她说|老师说|什么意思|是否支持|能否支持|'
                         r'先别|不要|不用|不想|暂不|别再|然后|同时|顺便|并且|还要|再帮我|算了|撤回)', transcript)


def target_current(connection, child_id, session_id, target, transcript, *, before_turn=None, capture_reference=None):
    if not target or snapshot(connection, child_id, session_id, before_turn=before_turn) != target or not valid_words(transcript):
        return False
    if target.get('_source_gap'):
        from .task_source_recovery import capture_reference as expected_reference
        if capture_reference != expected_reference(target):
            return False
    # The model never receives local task names; reject a locally contradictory name.
    titles = connection.execute('''SELECT i.title,i.subject FROM plan_item i JOIN daily_plan p USING(plan_id)
        WHERE p.child_id=? AND p.session_id=? AND i.source_type!='planned_break'
        AND i.plan_item_id!=? AND i.status IN ('pending','paused','active')''',
        (child_id, session_id, target['plan_item_id'])).fetchall()
    return not any((row['title'] != target['title'] and row['title'] and row['title'] in transcript)
                   or (row['subject'] != target['subject'] and row['subject'] and row['subject'] in transcript) for row in titles)
