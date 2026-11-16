"""Parent edits and feedback must refer to the same adopted action."""
import sqlite3

import pytest

from home_study_agent.database import Database
from home_study_agent.parent_action_evidence import ParentActionEvidenceService
from home_study_agent.parent_reports import ParentReportService
from home_study_agent.repository import StudyRepository
from test_parent_coaching_feedback_semantics import CHILD, repository, report
from test_parent_action_evidence import submit


def approve_custom(repo, saved, decision='custom'):
    reviews = ParentReportService(repo.database)
    reviewed = reviews.create_from_daily(saved)
    reviews.approve(reviewed['snapshot_id'], parent_edit_patch={
        'coaching_decision': decision, 'coaching_action': '一起把今天最难的一步画出来',
        'coaching_script': '你来选择先画哪一步，我只听你的想法。',
        'coaching_duration_minutes': 3, 'coaching_observation': '孩子自己解释画中的一步'})
    return reviews, reviewed


def test_parent_custom_action_is_frozen_in_approval_transaction_and_survives_restart(repository):
    saved = report(repository)
    original = saved['parent_action']
    reviews, reviewed = approve_custom(repository, saved)
    restarted = StudyRepository(Database(repository.database.path))
    current = restarted.get_daily_report(saved['report_id'])
    action = current['parent_action']
    assert action['snapshot_id'] != original['snapshot_id']
    assert action['coaching']['action']['what'] == '一起把今天最难的一步画出来'
    assert action['coaching']['script'] == ['你来选择先画哪一步，我只听你的想法。']
    assert action['timing']['duration_minutes'] == 3
    assert action['review_binding']['snapshot_id'] == reviewed['snapshot_id']
    assert action['review_binding']['decision'] == 'custom'
    assert action['execution_allowed'] is True
    receipt = submit(restarted, current, client_action_id='custom-executed')
    assert receipt['action_snapshot_id'] == action['snapshot_id']
    with restarted.database.connect() as c:
        feedback = ParentActionEvidenceService(restarted.database).feedback_for_child(c, CHILD)
        assert len(feedback) == 1
        assert 'parent_custom_action_fingerprint' in feedback[0]['feedback_context']
    reviews.approve(reviewed['snapshot_id'])
    assert restarted.get_daily_report(saved['report_id'])['parent_action']['snapshot_id'] == action['snapshot_id']
    with pytest.raises(ValueError, match='REVIEWED_VERSION_REQUIRED'):
        submit(restarted, saved, client_action_id='late-original-form')


def test_skipped_action_is_not_replaced_with_original_or_given_effectiveness(repository):
    saved = report(repository)
    approve_custom(repository, saved, 'skip')
    current = repository.get_daily_report(saved['report_id'])
    action = current['parent_action']
    assert action['execution_allowed'] is False
    assert action['coaching']['action'] is None and action['coaching']['script'] == []
    with pytest.raises(ValueError, match='SKIPPED_NOT_EXECUTABLE'):
        submit(repository, current)
    submit(repository, current, executed=False, executed_at=None, effectiveness=None, stress_rating=None)
    with repository.database.connect() as c:
        assert ParentActionEvidenceService(repository.database).feedback_for_child(c, CHILD) == []


def test_revocation_keeps_old_receipt_but_removes_current_action_effect(repository):
    saved = report(repository)
    reviews, reviewed = approve_custom(repository, saved)
    current = repository.get_daily_report(saved['report_id'])
    receipt = submit(repository, current, client_action_id='original-feedback')
    reviews.revoke(reviewed['snapshot_id'], '家长撤销行动，重新核对')
    assert repository.get_daily_report(saved['report_id'])['parent_action']['binding_status'] == 'unknown'
    assert submit(repository, current, client_action_id='original-feedback') == receipt
    with pytest.raises(ValueError, match='REVIEW_NOT_CURRENT'):
        submit(repository, current, expected_revision=1, client_action_id='new-after-withdrawal')
    with repository.database.connect() as c:
        service = ParentActionEvidenceService(repository.database)
        assert service.feedback_for_child(c, CHILD) == []
        assert len(service.history(CHILD, current['parent_action']['snapshot_id'])) == 1


def test_review_action_failure_rolls_back_approval_and_edit(repository):
    saved = report(repository)
    reviews = ParentReportService(repository.database)
    review = reviews.create_from_daily(saved)
    with repository.database.connect() as c:
        c.execute("CREATE TRIGGER reject_reviewed_action BEFORE INSERT ON parent_action_snapshot BEGIN SELECT RAISE(ABORT,'synthetic review failure'); END")
        c.commit()
    with pytest.raises(sqlite3.IntegrityError):
        reviews.approve(review['snapshot_id'], parent_edit_patch={'coaching_decision': 'skip'})
    unchanged = reviews.get(review['snapshot_id'])
    assert unchanged['status'] == 'draft'
    assert unchanged['parent_edit']['coaching_decision'] == 'adopt'
