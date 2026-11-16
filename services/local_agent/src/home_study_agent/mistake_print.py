"""Read-only, complete notebook snapshots for local printing.

Printing must not start analysis, refresh graph matches, or advance mastery.
Review counts are saved answers to the current question revision, not sessions.
"""
from datetime import datetime, timezone

from .learning_obligations import project_child_mastery_evidence
from .mistake_book import MistakeBookService
from .mistake_learning import _exists, _ids, _now, _owner


CAUSE_LABELS = {
    'concept_gap': '概念理解', 'prerequisite_gap': '前置知识', 'reading_gap': '审题理解',
    'method_gap': '解题方法', 'execution_error': '执行计算', 'retrieval_failure': '记忆提取',
    'transfer_gap': '迁移应用', 'incidental': '偶发失误', 'unknown': '错因待核对',
}


def _time(value):
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _stats(connection, record):
    child, mid, revision = record['child_id'], record['mistake_id'], record['source_revision']
    original = connection.execute('''SELECT count(*),max(created_at) FROM mistake_learning_original_review
        WHERE child_id=? AND mistake_id=? AND source_revision=?''', (child, mid, revision)).fetchone()
    practice = connection.execute('''SELECT count(*),max(updated_at) FROM mistake_learning_action
        WHERE child_id=? AND mistake_id=? AND source_revision=? AND operation='answer' AND status='completed' ''',
        (child, mid, revision)).fetchone()
    # Legacy formal attempts have no source hash. A question correction creates
    # a new printing scope; earlier answers remain preserved in the notebook.
    changed = connection.execute('''SELECT max(created_at) FROM mistake_source_revision
        WHERE child_id=? AND mistake_id=?''', (child, mid)).fetchone()[0] if _exists(connection, 'mistake_source_revision') else None
    formal = connection.execute('''SELECT count(*),max(ma.created_at) FROM mistake_attempt ma
        WHERE ma.child_id=? AND ma.mistake_id=? AND (? IS NULL OR julianday(ma.created_at)>=julianday(?))
          AND NOT EXISTS (SELECT 1 FROM learning_evidence_withdrawal w WHERE w.evidence_id=ma.evidence_id)''',
        (child, mid, changed, changed)).fetchone()
    counts = {'original': original[0], 'practice': practice[0], 'formal': formal[0]}
    dates = [row[1] for row in (original, practice, formal) if row[1]]
    return counts, max(dates, key=_time) if dates else None, changed


def _mastery(record, detail, source_changed, mastered_at, formal_status_at):
    original = record['original_review']
    # Every formal status belongs to its recorded question version. A source
    # change cannot inherit an older "pending review" or "relapsed" label.
    if source_changed and (not formal_status_at or _time(formal_status_at) < _time(source_changed)):
        detail = None
    gate = detail.get('mastery_gate') if detail else None
    # A bare stored flag, model feedback, and one correct answer are not proof.
    last_wrong = (original.get('last_result') or {}).get('created_at') if original['status'] == 'not_mastered' else None
    current_source = not source_changed or mastered_at and _time(mastered_at) >= _time(source_changed)
    later_failure = last_wrong and (not mastered_at or _time(last_wrong) >= _time(mastered_at))
    if (detail and detail['status'] == 'mastered' and gate and not gate['missing']
            and current_source and not later_failure):
        return {'status': 'mastered', 'label': '已掌握', 'detail': '当前有效独立验证与延迟复测已通过。'}
    if original['status'] == 'not_mastered' or detail and detail['status'] == 'relapsed':
        return {'status': 'not_mastered', 'label': '尚未掌握', 'detail': '最近原题复做未通过，或同类错误再次出现。'}
    if original['status'] == 'retest_pending' or detail and detail['status'] in {'fragile', 'delayed_verification'}:
        return {'status': 'retest_pending', 'label': '待复测', 'detail': '已有作答进展，仍需独立复测确认，不能等同于已掌握。'}
    if detail and detail['status'] not in {'awaiting_confirmation', 'mastered'}:
        return {'status': 'not_mastered', 'label': '尚未掌握', 'detail': '正在诊断或强化，掌握证据尚未齐备。'}
    return {'status': 'unassessed', 'label': '尚未确认掌握', 'detail': '目前缺少足够的独立作答与复测证据。'}


def snapshot(service, child_id):
    book = MistakeBookService(service.database)
    with service.database.connect() as connection:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        _owner(connection, child_id)
        generated_at = _now()
        version = service.graph_matcher.cache_version(connection=connection) if service.graph_matcher is not None else None
        projections = None
        items = []
        for row in _ids(connection, child_id):
            mid = row['mistake_id']
            formal = connection.execute('SELECT 1 FROM mistake_case WHERE mistake_id=? AND child_id=?', (mid, child_id)).fetchone()
            if formal and book._pending_revision_mistake(connection, mid):
                continue
            record = service._view(connection, mid, child_id, graph_version=version)
            detail = None
            if formal:
                if projections is None:
                    projections = project_child_mastery_evidence(connection, child_id, as_of=generated_at)
                detail = book._detail(connection, mid, _projections=projections)
            counts, last_review, changed = _stats(connection, record)
            status_times = connection.execute('''SELECT
                max(CASE WHEN to_status='mastered' THEN created_at END),
                max(CASE WHEN to_status=? THEN created_at END)
                FROM mistake_status_event WHERE child_id=? AND mistake_id=?''',
                (detail['recorded_status'], child_id, mid)).fetchone() if formal else (None, None)
            diagnosis = detail.get('diagnosis') if detail else None
            # Rejected or superseded diagnoses must not appear as current causes.
            if diagnosis and (diagnosis['status'] == 'rejected' or changed and _time(diagnosis['created_at']) < _time(changed)):
                diagnosis = None
            items.append({'record': record, 'mastery': _mastery(record, detail, changed, *status_times),
                          'review_count': sum(counts.values()), 'review_counts': counts,
                          'last_review_at': last_review,
                          'diagnosis': {'label': CAUSE_LABELS.get(diagnosis['error_type'], '错因待核对'),
                                        'explanation': diagnosis['explanation'], 'status': diagnosis['status']} if diagnosis else None})
        return {'version': 1, 'child_id': child_id, 'generated_at': generated_at, 'total': len(items), 'items': items}
