from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from home_study_agent.database import Database
from home_study_agent.learning_state import LearningStateService
from home_study_agent.mistake_book import MistakeBookService
from home_study_agent.parent_reports import ParentReportService
from home_study_agent.period_reports import PeriodEvidenceActionConflictError, PeriodReportService
from home_study_agent.repository import StudyRepository
from test_daily_learning_closure_audit import learning_clock


CHILD = 'child-primary-4'
OTHER = 'child-junior-3'
ITEM = 'diag-p4-arithmetic-closed-1'
SECOND = 'diag-p4-arithmetic-closed-2'


@pytest.fixture
def flow(tmp_path, learning_clock):
    today = datetime.now(timezone.utc)
    monday = today.date() - timedelta(days=today.weekday() + 7)
    learning_clock[0] = datetime(monday.year, monday.month, monday.day, 12, tzinfo=timezone.utc).isoformat()
    db = Database(tmp_path / 'period.db')
    db.initialize()
    repo = StudyRepository(db)
    repo.seed_family()
    learning = LearningStateService(db)
    learning.seed_catalog()
    sid = repo.start_session(CHILD, 'period-session')['session_id']
    with db.connect() as connection:
        connection.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
        connection.commit()
    reports = PeriodReportService(db)
    reports.initialize()
    return SimpleNamespace(db=db, repo=repo, learning=learning, reports=reports, sid=sid,
                           clock=learning_clock, reference=monday)


def answer(f, text='12', item=ITEM, hint=0):
    return f.learning.submit_diagnostic(f.sid, item, text, str(uuid4()), hint_level=hint)


def case_for(f, source):
    mistakes = MistakeBookService(f.db)
    case = mistakes.capture_from_attempt(source)
    assert case is not None
    return case['mistake_id']


def transfer(f, case, *, hint=0):
    mistakes = MistakeBookService(f.db)
    mistakes.confirm(case, CHILD, 'confirm', str(uuid4()))
    mistakes.diagnose(case, CHILD, 'unknown', '待核实运算顺序', .9, str(uuid4()))
    mistakes.add_remediation(case, CHILD, 'active_recall', 'completed', None, str(uuid4()))
    f.clock[0] = (datetime.fromisoformat(f.clock[0]) + timedelta(minutes=1)).isoformat()
    result = mistakes.submit_attempt(case, CHILD, 'near_transfer', SECOND, '42', hint, str(uuid4()))
    assert result['attempt_result']['correctness'] == 1
    with f.db.connect() as connection:
        return dict(connection.execute('SELECT * FROM mistake_attempt WHERE mistake_id=? ORDER BY rowid DESC LIMIT 1', (case,)).fetchone())


def report(f, kind='weekly'):
    return f.reports.generate(CHILD, kind, f.reference)


def summary(result):
    return result['content']['learning_summary']


@pytest.mark.parametrize('kind', ['weekly', 'monthly'])
def test_valid_source_and_independent_transfer_are_live_and_do_not_claim_mastery(flow, kind):
    f = flow
    case = case_for(f, answer(f))
    transfer(f, case)
    result = report(f, kind)
    data = summary(result)
    assert data['coverage']['eligible_evidence_count'] == 2
    assert data['coverage']['independent_answered_count'] == 2
    assert data['coverage']['sufficient_for_mastery_claim'] is False
    assert len(data['new_weaknesses']) == len(data['pending_confirmation']) == 1
    assert len(data['independent_variant_retests']) == 1
    assert all(row['evidence_ref'].startswith('record-') for row in data['evidence'])
    assert 'evidence_id' not in json.dumps(data)
    assert summary(PeriodReportService(Database(f.db.path)).get(result['period_report_id'])) == data


@pytest.mark.parametrize('mutation', ['keyword', 'retired', 'withdrawn', 'missing_receipt', 'wrong_child'])
def test_source_invalidation_removes_learning_mistake_and_transfer_from_get_and_regenerate(flow, mutation):
    f = flow
    source = answer(f)
    case = case_for(f, source)
    transfer(f, case)
    before = report(f)
    with f.db.connect() as connection:
        original_rows = [tuple(row) for row in connection.execute('SELECT * FROM learning_evidence ORDER BY rowid')]
        if mutation == 'keyword':
            connection.execute('UPDATE diagnostic_item SET expected_answer_json=? WHERE diagnostic_item_id=?',
                               (json.dumps({'keywords': ['90']}), ITEM))
        elif mutation == 'retired':
            connection.execute("UPDATE diagnostic_item SET status='inactive' WHERE diagnostic_item_id=?", (ITEM,))
        elif mutation == 'missing_receipt':
            connection.execute('DELETE FROM diagnostic_attempt WHERE attempt_id=?', (source['attempt_id'],))
        elif mutation == 'wrong_child':
            connection.execute('UPDATE diagnostic_attempt SET child_id=? WHERE attempt_id=?', (OTHER, source['attempt_id']))
        connection.commit()
    if mutation == 'withdrawn':
        MistakeBookService(f.db).confirm(case, CHILD, 'exclude', 'exclude-source')
    reopened = PeriodReportService(Database(f.db.path))
    for result in (reopened.get(before['period_report_id']), reopened.list(CHILD)[0], reopened.generate(CHILD, 'weekly', f.reference)):
        data = summary(result)
        assert data['coverage']['eligible_evidence_count'] == 0
        assert data['new_weaknesses'] == data['independent_variant_retests'] == data['pending_confirmation'] == []
        assert result['content']['parent_coaching']['evidence_refs'] == []
    with f.db.connect() as connection:
        assert [tuple(row) for row in connection.execute('SELECT * FROM learning_evidence ORDER BY rowid')] == original_rows
        assert connection.execute('SELECT count(*) FROM mistake_attempt').fetchone()[0] == 1


def test_keyword_objective_is_not_independent_and_does_not_trigger_refresh(flow):
    f = flow
    with f.db.connect() as connection:
        connection.execute('UPDATE diagnostic_item SET expected_answer_json=? WHERE diagnostic_item_id=?',
                           (json.dumps({'keywords': ['90']}), ITEM))
        connection.commit()
    answer(f, '90')
    assert f.reports._has_evidence(CHILD, f.reference, f.reference + timedelta(days=6)) is False
    data = summary(report(f))
    assert data['coverage']['eligible_evidence_count'] == data['coverage']['independent_answered_count'] == 0


def test_same_item_exposure_and_hinted_receipts_never_count_as_independent_success(flow):
    f = flow
    answer(f, '90')
    answer(f, '90')
    answer(f, '42', SECOND, hint=1)
    data = summary(report(f))
    assert data['coverage']['eligible_evidence_count'] == 2
    assert data['coverage']['independent_answered_count'] == 1
    assert sum(row['independent'] for row in data['evidence']) == 1
    assert data['knowledge_support'][0]['independent_correct_count'] == 1
    assert data['independent_variant_retests'] == []


def test_hinted_or_legacy_same_item_transfer_is_not_called_independent_variant(flow):
    f = flow
    case = case_for(f, answer(f))
    receipt = transfer(f, case, hint=1)
    assert summary(report(f))['independent_variant_retests'] == []
    # Old records could label a same-item check as near transfer; score and timestamps alone cannot certify a variant.
    f.clock[0] = (datetime.fromisoformat(f.clock[0]) + timedelta(days=2)).isoformat()
    with f.db.connect() as connection:
        connection.execute('UPDATE mistake_attempt SET diagnostic_item_id=?,submitted_answer=?,hint_level=0,independent=1,created_at=? WHERE attempt_id=?',
                           (ITEM, '90', f.clock[0], receipt['attempt_id']))
        connection.execute('UPDATE learning_evidence SET hint_level=0,independent=1,occurred_at=? WHERE evidence_id=?',
                           (f.clock[0], receipt['evidence_id']))
        connection.commit()
    assert summary(report(f))['independent_variant_retests'] == []


def test_report_withdrawal_cascades_and_restore_cannot_override_retired_source_or_parent_decision(flow):
    f = flow
    source = answer(f)
    case = case_for(f, source)
    transfer(f, case)
    before = report(f)
    parents = ParentReportService(f.db)
    snapshot = parents.create_from_period(before)
    parents.edit_draft(snapshot['snapshot_id'], parent_note='家长决定：先核实，不追加练习')
    with f.db.connect() as connection:
        frozen = tuple(connection.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?', (snapshot['snapshot_id'],)).fetchone())
    source_ref = next(row['evidence_ref'] for row in summary(before)['evidence'] if row['correctness'] == 0)
    assert f.reports.retract_evidence(CHILD, source_ref, '误记待核实', 'period-retract')
    assert f.reports.retract_evidence(CHILD, source_ref, '误记待核实', 'period-retract')
    with pytest.raises(PeriodEvidenceActionConflictError):
        f.reports.retract_evidence(OTHER, source_ref, '误记待核实', 'period-retract')
    assert summary(f.reports.get(before['period_report_id']))['coverage']['eligible_evidence_count'] == 0
    assert f.reports.restore_evidence(CHILD, source_ref, '已核实', 'period-restore')
    assert summary(f.reports.get(before['period_report_id']))['coverage']['eligible_evidence_count'] == 2
    assert f.reports.retract_evidence(CHILD, source_ref, '来源待核实', 'period-retract-again')
    with f.db.connect() as connection:
        connection.execute("UPDATE diagnostic_item SET status='inactive' WHERE diagnostic_item_id=?", (ITEM,))
        connection.commit()
    assert f.reports.restore_evidence(CHILD, source_ref, '保留家长意见', 'period-restore-retired')
    assert summary(f.reports.get(before['period_report_id']))['coverage']['eligible_evidence_count'] == 0
    with f.db.connect() as connection:
        assert tuple(connection.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?', (snapshot['snapshot_id'],)).fetchone()) == frozen
        assert connection.execute('SELECT count(*) FROM period_report_evidence_action').fetchone()[0] == 4
        assert connection.execute('SELECT count(*) FROM learning_evidence').fetchone()[0] == 2


def test_report_window_does_not_discard_valid_older_source_and_other_child_stays_empty(flow):
    f = flow
    current = f.clock[0]
    f.clock[0] = (datetime.fromisoformat(current) - timedelta(days=10)).isoformat()
    source = answer(f)
    case = case_for(f, source)
    f.clock[0] = current
    transfer(f, case)
    data = summary(report(f))
    assert data['coverage']['eligible_evidence_count'] == 1
    assert len(data['independent_variant_retests']) == len(data['new_weaknesses']) == 1
    assert summary(f.reports.generate(OTHER, 'weekly', f.reference))['coverage']['eligible_evidence_count'] == 0
    assert f.reports.list(OTHER)[0]['content']['parent_coaching']['evidence_refs'] == []


def test_unqualified_out_of_period_source_cannot_make_a_current_transfer_valid(flow):
    f = flow
    current = f.clock[0]
    f.clock[0] = (datetime.fromisoformat(current) - timedelta(days=10)).isoformat()
    source = answer(f)
    case = case_for(f, source)
    f.clock[0] = current
    transfer(f, case)
    with f.db.connect() as connection:
        connection.execute('UPDATE diagnostic_item SET expected_answer_json=? WHERE diagnostic_item_id=?',
                           (json.dumps({'keywords': ['90']}), ITEM))
        connection.commit()
    assert summary(report(f))['coverage']['eligible_evidence_count'] == 0


@pytest.mark.parametrize('mutation', ['low_confidence', 'wrong_child', 'report_retracted'])
def test_invalid_transfer_does_not_erase_its_still_valid_original_mistake(flow, mutation):
    f = flow
    case = case_for(f, answer(f))
    receipt = transfer(f, case)
    before = report(f)
    with f.db.connect() as connection:
        if mutation == 'low_confidence':
            connection.execute('UPDATE learning_evidence SET confidence=.35 WHERE evidence_id=?', (receipt['evidence_id'],))
        elif mutation == 'wrong_child':
            connection.execute('UPDATE mistake_attempt SET child_id=? WHERE attempt_id=?', (OTHER, receipt['attempt_id']))
        connection.commit()
    if mutation == 'report_retracted':
        ref = summary(before)['independent_variant_retests'][0]['evidence_ref']
        f.reports.retract_evidence(CHILD, ref, '这次复测待核实', 'retract-transfer')
    after = summary(f.reports.get(before['period_report_id']))
    assert after['coverage']['eligible_evidence_count'] == 1
    assert len(after['new_weaknesses']) == len(after['pending_confirmation']) == 1
    assert after['independent_variant_retests'] == []


def test_get_never_uses_legacy_cached_probability_or_counts_as_authority(flow):
    f = flow
    before = report(f)
    with f.db.connect() as connection:
        legacy = {'as_of_date': f.reference.isoformat(), 'headline': '已经掌握',
                  'learning_summary': {'coverage': {'eligible_evidence_count': 99, 'independent_answered_count': 99}}}
        connection.execute('UPDATE period_report SET content_json=? WHERE period_report_id=?',
                           (json.dumps(legacy), before['period_report_id']))
        connection.commit()
    live = PeriodReportService(Database(f.db.path)).get(before['period_report_id'])
    assert summary(live)['coverage']['eligible_evidence_count'] == 0
    assert '已经掌握' not in live['content']['headline']


def test_multi_node_batch_matches_single_node_results_and_never_caches_withdrawals(flow, monkeypatch):
    from home_study_agent import learning_obligations as obligations

    f = flow
    source = answer(f)
    case = case_for(f, source)
    transfer(f, case)
    fraction = answer(f, '0.3', 'diag-p4-fraction-decimal-closed-1')
    answer(f, 'bag', 'diag-p4-english-phonics-1')
    with f.db.connect() as connection:
        f.reports._ensure_evidence_refs(connection, CHILD)
        connection.commit()
        node_ids = [row[0] for row in connection.execute(
            'SELECT DISTINCT knowledge_node_id FROM learning_evidence WHERE child_id=?', (CHILD,))]
        assert len(node_ids) == 3
        expected = {}
        for node_id in node_ids:
            projected = obligations.project_mastery_evidence(connection, CHILD, node_id, as_of=f.clock[0])
            expected.update({row['evidence_id']: {**row, 'knowledge_node_id': node_id}
                             for row in projected['effective_evidence']})
        source_ref = connection.execute('SELECT evidence_ref FROM period_report_evidence_reference WHERE evidence_id=?',
                                        (source['evidence_id'],)).fetchone()[0]
    assert len(expected) == 3
    original = obligations.project_child_mastery_evidence
    calls = []

    def counted(connection, child_id, *, as_of=None):
        calls.append((child_id, as_of))
        return original(connection, child_id, as_of=as_of)

    monkeypatch.setattr(obligations, 'project_child_mastery_evidence', counted)
    with f.db.connect() as connection:
        assert f.reports._effective_evidence(connection, CHILD) == expected
    assert calls == [(CHILD, f.clock[0])]
    f.reports.retract_evidence(CHILD, source_ref, '核实原始依据', 'batch-retract')
    with f.db.connect() as connection:
        assert f.reports._effective_evidence(connection, CHILD) == {fraction['evidence_id']: expected[fraction['evidence_id']]}
    assert len(calls) == 2
    f.reports.restore_evidence(CHILD, source_ref, '原始依据已核实', 'batch-restore')
    with f.db.connect() as connection:
        assert f.reports._effective_evidence(connection, CHILD) == expected
    assert calls == [(CHILD, f.clock[0])] * 3


@pytest.mark.parametrize('rejected_only', [False, True])
def test_single_batch_handles_empty_and_rejected_only_nodes(flow, monkeypatch, rejected_only):
    from home_study_agent import learning_obligations as obligations

    f = flow
    if rejected_only:
        answer(f, 'bag', 'diag-p4-english-phonics-1')
    original = obligations.project_child_mastery_evidence
    batches = []

    def counted(connection, child_id, *, as_of=None):
        result = original(connection, child_id, as_of=as_of)
        batches.append(result)
        return result

    monkeypatch.setattr(obligations, 'project_child_mastery_evidence', counted)
    with f.db.connect() as connection:
        assert f.reports._effective_evidence(connection, CHILD) == {}
    assert len(batches) == 1
    assert bool(batches[0]) == rejected_only
