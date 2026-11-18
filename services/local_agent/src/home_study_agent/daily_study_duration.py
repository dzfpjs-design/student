"""Project recorded task timers without claiming measured attention or mastery."""
from datetime import date, datetime, time, timedelta, timezone

from .family_clock import FAMILY_ZONE


def _instant(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (TypeError, AttributeError, ValueError, OverflowError):
        return None


def project_recorded_task_time(connection, child_id, learning_date):
    """Completed tasks wholly within one family day; unknown intervals stay unknown."""
    if not connection.in_transaction:
        raise ValueError('TASK_TIME_TRANSACTION_REQUIRED')
    try:
        day = date.fromisoformat(learning_date)
        if day.isoformat() != learning_date:
            raise ValueError()
    except (TypeError, ValueError):
        raise ValueError('TASK_TIME_DATE_INVALID') from None
    try:
        begin = datetime.combine(day, time.min, FAMILY_ZONE).astimezone(timezone.utc)
        end = datetime.combine(day + timedelta(days=1), time.min, FAMILY_ZONE).astimezone(timezone.utc)
    except OverflowError:
        raise ValueError('TASK_TIME_DATE_INVALID') from None
    rows = connection.execute('''SELECT i.plan_item_id,i.subject,i.title,i.started_at,i.completed_at,
            i.active_seconds,i.actual_minutes
        FROM plan_item i JOIN daily_plan p ON p.plan_id=i.plan_id
        JOIN daily_session s ON s.session_id=p.session_id AND s.child_id=p.child_id
        WHERE p.child_id=? AND i.status='completed'
          AND julianday(i.completed_at)>=julianday(?) AND julianday(i.completed_at)<julianday(?)
        ORDER BY i.completed_at,i.plan_item_id''', (child_id,begin.isoformat(),end.isoformat())).fetchall()
    tasks=[]
    omitted=0
    from .report_task_provenance import task_source_state
    for row in rows:
        task_source_state(connection, child_id, row['plan_item_id'])
        started, completed = _instant(row['started_at']), _instant(row['completed_at'])
        active, declared = row['active_seconds'], row['actual_minutes']
        if (started is None or completed is None or not begin <= started <= completed < end
                or type(active) is not int or active < 0
                or active > (completed-started).total_seconds()+1
                or (declared is not None and (type(declared) is not int or declared < 0))):
            omitted += 1
            continue
        tasks.append({'plan_item_id':row['plan_item_id'],'subject':row['subject'],'title':row['title'],
                      'started_at':started.isoformat(),'completed_at':completed.isoformat(),
                      'recorded_active_seconds':active,'reported_minutes':declared})
    return {'tasks':tasks,'recorded_active_seconds':sum(task['recorded_active_seconds'] for task in tasks),
            'omitted_count':omitted,'source_ids':[row['plan_item_id'] for row in rows]}
