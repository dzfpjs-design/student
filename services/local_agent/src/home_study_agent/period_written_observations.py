"""Qualified original-response observations, never an exact/mastery projection."""
from datetime import date, datetime, timedelta

from .writing_score import current_written_score, written_score_candidates
from .family_clock import evidence_calendar_date


def observations_for_period(c, child_id, start, end, effective, *, now):
    """Read current authority even before a report has allocated display refs."""
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='rubric_assessment'").fetchone():
        return []
    start, end = date.fromisoformat(str(start)), date.fromisoformat(str(end))
    rows = c.execute('''SELECT le.evidence_id,kn.subject,kn.title,le.occurred_at,ref.evidence_ref
        FROM learning_evidence le JOIN knowledge_node kn USING(knowledge_node_id)
        LEFT JOIN period_report_evidence_reference ref
          ON ref.evidence_id=le.evidence_id AND ref.child_id=le.child_id
        WHERE le.child_id=? AND date(le.occurred_at) BETWEEN ? AND ?
        ORDER BY le.occurred_at,le.evidence_id''', (child_id, str(start - timedelta(days=1)), str(end))).fetchall()
    return qualified_written_observations(c, child_id,
        [row for row in rows if row['evidence_id'] not in effective
         and start <= evidence_calendar_date(row['occurred_at']) <= end], now=now)


def _withdrawn_lineage(c, child_id):
    excluded = {row[0] for row in c.execute(
        'SELECT evidence_id FROM period_report_evidence_state WHERE child_id=? AND is_retracted=1',
        (child_id,))}
    edges = list(c.execute('SELECT result_evidence_id,source_evidence_id FROM review_task WHERE child_id=?',
                          (child_id,)))
    edges.extend(c.execute('''SELECT a.evidence_id,m.source_evidence_id FROM mistake_attempt a
        JOIN mistake_case m USING(mistake_id) WHERE a.child_id=? AND m.child_id=?''', (child_id, child_id)))
    while True:
        descendants = {result for result, source in edges if result and source in excluded}
        if descendants <= excluded:
            return excluded
        excluded.update(descendants)


def _possible_written_originals(c, child_id):
    """Necessary journal conditions only; never a score/qualification cache."""
    from .writing_rubric import REVIEW_PROFILES
    rows = c.execute('''SELECT
        json_extract(CASE WHEN json_valid(a.payload_json) THEN a.payload_json ELSE '{}' END,
                     '$.source_evidence_id') AS payload_evidence,
        CASE WHEN p.child_id=a.child_id THEN
          json_extract(CASE WHEN json_valid(s.arguments_json) THEN s.arguments_json ELSE '{}' END,
                       '$.evidence_id') END AS origin_evidence,
        r.event_id,r.operation,
        json_extract(CASE WHEN json_valid(r.payload_json) THEN r.payload_json ELSE '{}' END,
                     '$.review_profile') AS review_profile
        FROM rubric_assessment a LEFT JOIN agent_step s ON s.step_id=a.step_id
        LEFT JOIN agent_plan p ON p.plan_id=s.plan_id
        LEFT JOIN rubric_review_event r ON r.assessment_id=a.assessment_id AND r.revision=(
          SELECT MAX(revision) FROM rubric_review_event WHERE assessment_id=a.assessment_id)
        WHERE a.child_id=?''', (child_id,))
    possible = set()
    for row in rows:
        # Blind calibration labels are not original-writing reviews. A later
        # real original review can admit this same answer, even in a holdout.
        if row['event_id'] is not None and (
                row['operation'] != 'approve' or row['review_profile'] not in REVIEW_PROFILES):
            continue
        possible.update(value for value in (row['payload_evidence'], row['origin_evidence'])
                        if isinstance(value, str) and value)
    return possible


def qualified_written_sources(c, child_id, rows, *, now):
    """Yield each current original once; callers choose their observation view."""
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='rubric_assessment'").fetchone():
        return
    excluded = _withdrawn_lineage(c, child_id)
    possible = _possible_written_originals(c, child_id)
    seen = set()
    for row in rows:
        if row['evidence_id'] in seen or row['evidence_id'] in excluded or row['evidence_id'] not in possible:
            continue
        seen.add(row['evidence_id'])
        try:
            occurred = datetime.fromisoformat(row['occurred_at'].replace('Z', '+00:00'))
            if occurred.tzinfo is None or occurred > now:
                continue
            candidates = written_score_candidates(c, child_id, row['evidence_id'])
        except (ValueError, KeyError, TypeError):
            continue
        current = []
        for candidate in candidates:
            try:
                score = current_written_score(c, child_id, candidate['assessment_id'], now=now)
            except (ValueError, KeyError, TypeError):
                continue
            if score['current']['source_evidence_id'] == row['evidence_id']:
                current.append(score)
        if len(current) != 1:
            continue
        yield row, current[0]


def qualified_written_observations(c, child_id, rows, *, now):
    observations = []
    for row, score in qualified_original_sources(c, child_id, rows, now=now):
        decision, receipt = score['decision'], score['receipt']
        author = '人工核对' if decision['status'] == 'reviewed_human' else '已校准自动评分'
        result = '本次达到量表要求' if decision['passed'] else '本次尚未达到量表要求'
        observations.append(dict(
            **({'response_mode':'spoken_explanation'} if score.get('assessment_modality') in
                {'reviewed_spoken_reasoning_text','calibrated_spoken_reasoning_text'} else {}),
            evidence_ref=row['evidence_ref'], subject=row['subject'], knowledge_title=row['title'],
            date=evidence_calendar_date(row['occurred_at']).isoformat(), score_status=decision['status'],
            score_authority_id=decision['authority_id'], score_revision=decision['review_revision'],
            passed=decision['passed'], independent=bool(receipt.get('independent') and receipt.get('hint_level') == 0),
            summary=f'{author}：{result}。这是一份原作答的核对结果，不等于迁移、保持或稳定掌握。',
            mastery_claim_allowed=False,
        ))
    return observations


def qualified_original_sources(c, child_id, rows, *, now):
    """Preserve written authority; add original speech through its own proof."""
    rows = list(rows)
    seen = set()
    for row, score in qualified_written_sources(c, child_id, rows, now=now):
        seen.add(row['evidence_id'])
        yield row, score
    if not rows or not c.execute("SELECT 1 FROM sqlite_master WHERE name='rubric_assessment'").fetchone():
        return
    excluded = _withdrawn_lineage(c, child_id)
    remaining = []
    for row in rows:
        if row['evidence_id'] in seen or row['evidence_id'] in excluded:
            continue
        seen.add(row['evidence_id'])
        remaining.append(row)
    if remaining:
        from .school_spoken_score import qualified_sources
        yield from qualified_sources(c, child_id, remaining, now=now)
