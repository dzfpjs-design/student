"""Read-only exact writing selection using isolated reviewed sources, never generated scores."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from home_study_agent.database import Database
from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from home_study_agent.local_supply import digest
from home_study_agent.repository import StudyRepository
from home_study_agent.writing_response_origin import WritingOriginalService
from home_study_agent.writing_teaching import WritingTeachingService
from test_writing_response_origin import writing, CHILD, TEXT
from test_writing_teaching import ready
from test_writing_source_publication import prepared as source_fixture, publish


def select(service, w, **kwargs):
    return service.select_independent_action(w[1], prompt_id=w[3]['prompt_id'],
        item_version=w[3]['item_version'], **kwargs)


def recheck(service, w, packet, **kwargs):
    return service.revalidate_action(w[1], action=packet['action'],
        binding_version=packet['binding_version'], **kwargs)


@pytest.fixture
def supplied(tmp_path):
    db, supply, spec, _ = source_fixture(tmp_path)
    published = publish(supply, spec)
    sid = StudyRepository(db).start_session(CHILD, 'start-selection')['session_id']
    prompts = DiagnosticPromptService(db)
    p = prompts.prepare(sid, track='pet', preferred_item_id='diag-pet-writing-1', client_action_id='ask')['prompt']
    w = db, sid, prompts, p, WritingOriginalService(db)
    service, rubric, snapshot = ready(w)
    return w, service, supply, published, rubric, snapshot


def test_seed_selection_is_read_only_exact_and_restart_stable(writing):
    service, _, _ = ready(writing)
    db = writing[0]
    with db.connect() as c:
        before = list(c.iterdump())
        c.execute('PRAGMA query_only=ON')
        packet = select(service, writing, connection=c)
        assert packet['status'] == 'ready'
        assert packet['action']['diagnostic_item_id'] == 'diag-pet-writing-2'
        assert packet['action']['assessment_id']
        assert packet['action']['feedback_version'] and packet['action']['source_version']
        assert packet['action']['required_hint_level'] == 0
        assert packet['action']['mastery_claim_allowed'] is False
        assert packet['local_only'] and not packet['cloud_transmission_allowed']
        assert recheck(service, writing, packet, connection=c) == packet
        assert list(c.iterdump()) == before
    restarted = WritingTeachingService(Database(db.path))
    assert select(restarted, writing) == packet
    assert recheck(restarted, writing, packet) == packet


def test_real_reviewed_nonseed_selected_after_ineligible_seed_and_withdrawal_blocks(supplied):
    w, service, supply, published, _, _ = supplied
    with w[0].connect() as c:
        c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id='diag-pet-writing-2'")
        c.commit()
    packet = select(service, w)
    assert packet['status'] == 'ready', packet
    assert packet['action']['diagnostic_item_id'] == published['diagnostic_item_id']
    supply.withdraw(CHILD, published['candidate_id'], actor='parent', note='Withdraw original source',
        action_id='withdraw-selection', expected_version=4)
    assert recheck(service, w, packet)['status'] == 'blocked'
    empty = select(service, w)
    assert empty['status'] == 'blocked' and empty['action'] is None
    assert empty['reason'] == 'writing_no_eligible_independent_source'


def test_frozen_item_not_replaced_when_another_reviewed_item_is_now_available(supplied):
    w, service, _, published, _, _ = supplied
    packet = service.resolve_next_action(w[1], prompt_id=w[3]['prompt_id'], item_version=w[3]['item_version'],
        kind='independent_check', diagnostic_item_id='diag-pet-writing-2')
    with w[0].connect() as c:
        c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id='diag-pet-writing-2'")
        c.commit()
    assert select(service, w)['action']['diagnostic_item_id'] == published['diagnostic_item_id']
    assert recheck(service, w, packet)['status'] == 'blocked'


@pytest.mark.parametrize('fault', ['source_content', 'source_owner', 'review_missing', 'original_withdrawn',
    'feedback_retracted', 'terminal', 'child_inactive', 'source_revision'])
def test_recheck_current_authority_cannot_be_replaced_by_frozen_hash(supplied, fault):
    w, service, _, published, rubric, snapshot = supplied
    packet = service.resolve_next_action(w[1], prompt_id=w[3]['prompt_id'], item_version=w[3]['item_version'],
        kind='independent_check', diagnostic_item_id=published['diagnostic_item_id'])
    assert packet['status'] == 'ready'
    if fault == 'feedback_retracted':
        rubric.review(CHILD, snapshot['assessment_id'], expected_revision=1, client_action_id='retract-selection',
            reviewer_id='parent', reviewer_role='parent', operation='retract', reason='Feedback withdrawn')
    else:
        with w[0].connect() as c:
            if fault == 'source_content':
                c.execute("UPDATE diagnostic_item SET prompt=prompt || ' Changed' WHERE diagnostic_item_id=?", (published['diagnostic_item_id'],))
            elif fault == 'source_owner':
                c.execute("UPDATE specialized_supply_candidate SET child_id='child-junior-3' WHERE candidate_id=?", (published['candidate_id'],))
            elif fault == 'review_missing':
                c.execute('UPDATE specialized_supply_candidate SET review_action_id=NULL WHERE candidate_id=?', (published['candidate_id'],))
            elif fault == 'original_withdrawn':
                c.execute("UPDATE capture_artifact SET status='withdrawn' WHERE artifact_type='writing_original'")
            elif fault == 'terminal':
                c.execute("UPDATE daily_session SET state='completed' WHERE session_id=?", (w[1],))
            elif fault == 'child_inactive':
                c.execute("UPDATE child_profile SET status='archived' WHERE child_id=?", (CHILD,))
            else:
                c.execute("UPDATE knowledge_excerpt SET content_text='Changed original source'")
            c.commit()
    result = recheck(service, w, packet)
    assert result['status'] == 'blocked' and result['action'] is None, result


@pytest.mark.parametrize('field,value', [('child_id','child-junior-3'), ('session_id','other'),
    ('assessment_id','another'), ('feedback_version','changed'), ('source_version','changed'),
    ('item_version','changed'), ('knowledge_node_id','other'), ('required_hint_level',1),
    ('mastery_claim_allowed',True), ('kind','revision'), ('unexpected','body')])
def test_even_rehashed_tampered_action_is_not_a_trusted_execution_permission(writing, field, value):
    service, _, _ = ready(writing)
    packet = select(service, writing)
    action = {**packet['action'], field:value}
    result = service.revalidate_action(writing[1], action=action, binding_version=digest(action))
    assert result['status'] == 'blocked' and result['action'] is None


def test_missing_feedback_wrong_parent_and_unexpected_errors_are_not_ready(writing):
    service = WritingTeachingService(writing[0])
    missing = select(service, writing)
    assert missing['status'] == 'blocked' and missing['action'] is None
    assert service.select_independent_action('other-session', prompt_id=writing[3]['prompt_id'],
        item_version=writing[3]['item_version'])['status'] == 'blocked'
    with writing[0].connect() as c:
        c.execute('DROP TABLE diagnostic_prompt')
        with pytest.raises(sqlite3.OperationalError):
            select(service, writing, connection=c)


def test_invalid_unreviewed_candidate_does_not_hide_later_real_source(supplied):
    w, service, _, published, _, _ = supplied
    with w[0].connect() as c:
        row = dict(c.execute("SELECT * FROM diagnostic_item WHERE diagnostic_item_id='diag-pet-writing-2'").fetchone())
        row.update(diagnostic_item_id='000-unreviewed', source_ref='parent_approved_specialized:missing')
        c.execute('INSERT INTO diagnostic_item (' + ','.join(row) + ') VALUES (' + ','.join('?' for _ in row) + ')', tuple(row.values()))
        c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id='diag-pet-writing-2'")
        c.commit()
    assert select(service, w)['action']['diagnostic_item_id'] == published['diagnostic_item_id']


@pytest.mark.parametrize('action', [None, [], {}, {'kind':'independent_check'}])
def test_malformed_binding_is_explicitly_blocked_without_read_or_fallback(writing, action):
    result = WritingTeachingService(None).revalidate_action(writing[1], action=action, binding_version='bad')
    assert result['reason'] == 'writing_followup_binding_invalid'
    assert result['action'] is None


def test_frozen_independent_packet_uses_actual_prepare_binding_and_exact_retry(writing):
    service, _, _ = ready(writing)
    packet = select(service, writing)
    a = packet['action']
    args = dict(track='pet', preferred_item_id=a['diagnostic_item_id'], client_action_id='followup-prepare',
        writing_followup={'parent_prompt_id':a['parent_prompt_id'], 'parent_item_version':a['parent_item_version'],
            'kind':a['kind'], 'binding_version':packet['binding_version']})
    first = writing[2].prepare(writing[1], **args)
    assert first['status'] == 'waiting_answer', first
    restarted = DiagnosticPromptService(Database(writing[0].path))
    assert restarted.prepare(writing[1], **args)['prompt']['prompt_id'] == first['prompt']['prompt_id']
    with writing[0].connect() as c:
        row = c.execute('SELECT * FROM writing_followup').fetchone()
        assert row['assessment_id'] == a['assessment_id'] and row['kind'] == 'independent_check'
        assert json.loads(row['binding_json'])['binding_version'] == packet['binding_version']
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1


def test_intervening_actual_answer_blocks_old_packet_including_borrowed_transaction(writing):
    service, _, _ = ready(writing)
    packet = select(service, writing)
    target = writing[2].prepare(writing[1], track='pet', preferred_item_id=packet['action']['diagnostic_item_id'],
        client_action_id='another-task')['prompt']
    writing[2].answer(writing[1], prompt_id=target['prompt_id'], item_version=target['item_version'],
        answer=TEXT, client_action_id='intervening-answer')
    with writing[0].connect() as c:
        c.execute('BEGIN IMMEDIATE')
        changes = c.total_changes
        assert recheck(service, writing, packet, connection=c)['status'] == 'blocked'
        assert c.in_transaction and c.total_changes == changes
        c.rollback()


def test_assessment_revision_recheck_preserves_support_and_original_question(writing):
    service, _, _ = ready(writing)
    packet = service.resolve_next_action(writing[1], prompt_id=writing[3]['prompt_id'],
        item_version=writing[3]['item_version'], kind='revision')
    assert packet['action']['required_hint_level'] == 1
    assert recheck(service, writing, packet) == packet
    assert packet['action']['diagnostic_item_id'] == writing[3]['diagnostic_item_id']


def test_old_attempt_cannot_age_into_a_new_independent_writing_task(writing):
    service, _, _ = ready(writing)
    packet = select(service, writing)
    target = writing[2].prepare(writing[1], track='pet', preferred_item_id=packet['action']['diagnostic_item_id'],
        client_action_id='old-task')['prompt']
    answered = writing[2].answer(writing[1], prompt_id=target['prompt_id'], item_version=target['item_version'],
        answer=TEXT, client_action_id='old-answer')
    with writing[0].connect() as c:
        earlier = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        c.execute('UPDATE diagnostic_attempt SET created_at=? WHERE attempt_id=?', (earlier, answered['attempt']['attempt_id']))
        c.execute('UPDATE learning_evidence SET occurred_at=? WHERE evidence_id=?', (earlier, answered['attempt']['evidence_id']))
        c.commit()
    assert recheck(service, writing, packet)['status'] == 'blocked'
    assert select(service, writing)['status'] == 'blocked'


def test_actual_formative_model_receipt_selects_without_parent_approval(writing):
    from test_writing_teaching import test_real_evaluator_candidate_can_teach_without_parent_review_but_not_certify as actual_feedback
    actual_feedback(writing)
    service = WritingTeachingService(writing[0])
    packet = select(service, writing)
    assert packet['status'] == 'ready', packet
    assert recheck(service, writing, packet) == packet
    with writing[0].connect() as c:
        assert c.execute('SELECT COUNT(*) FROM rubric_review_event').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0] == 1
    assert packet['action']['mastery_claim_allowed'] is False


def test_actual_prepare_transaction_rejects_source_change_without_new_prompt(supplied):
    w, service, supply, published, _, _ = supplied
    packet = service.resolve_next_action(w[1], prompt_id=w[3]['prompt_id'], item_version=w[3]['item_version'],
        kind='independent_check', diagnostic_item_id=published['diagnostic_item_id'])
    a = packet['action']
    with w[0].connect() as c:
        before = c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0]
    supply.withdraw(CHILD, published['candidate_id'], actor='parent', note='Withdraw before prepare',
        action_id='withdraw-before-prepare', expected_version=4)
    try:
        result = w[2].prepare(w[1], track='pet', preferred_item_id=a['diagnostic_item_id'], client_action_id='stale-selection',
            writing_followup={k:a[k] for k in ('parent_prompt_id','parent_item_version','kind')} | {'binding_version':packet['binding_version']})
    except ValueError:
        pass
    else:
        assert result['status'] != 'waiting_answer' and not result.get('prompt')
    with w[0].connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0] == before
        assert c.execute('SELECT COUNT(*) FROM writing_followup').fetchone()[0] == 0


def test_real_other_child_session_cannot_read_select_or_rebind_original(writing):
    from home_study_agent.document_monitoring import DocumentMonitoringService
    service, _, _ = ready(writing)
    packet = select(service, writing)
    DocumentMonitoringService(writing[0])
    other = StudyRepository(writing[0]).switch_active_session_child(writing[1], 'child-junior-3', 'other-child-session')['session_id']
    assert service.select_independent_action(other, prompt_id=writing[3]['prompt_id'],
        item_version=writing[3]['item_version'])['status'] == 'blocked'
    action = {**packet['action'], 'child_id':'child-junior-3', 'session_id':other}
    assert service.revalidate_action(other, action=action, binding_version=digest(action))['status'] == 'blocked'


def test_old_exact_tutor_hint_is_not_a_new_independent_writing_question(writing):
    service, _, _ = ready(writing)
    packet = select(service, writing)
    with writing[0].connect() as c:
        question = c.execute('SELECT prompt FROM diagnostic_item WHERE diagnostic_item_id=?',
            (packet['action']['diagnostic_item_id'],)).fetchone()[0]
        c.execute('''INSERT INTO tutor_turn(turn_id,session_id,child_id,subject,question,answer,status,
            hint_level,response_kind,safety_status,created_at,client_action_id)
            VALUES('old-writing-hint',?,?,'English',?,'Include the reason and an invitation.',
                'completed',1,'hint','passed',?,'old-writing-hint')''',
            (writing[1], CHILD, question, (datetime.now(timezone.utc)-timedelta(days=3)).isoformat()))
        c.commit()
    assert recheck(service, writing, packet)['status'] == 'blocked'
    assert select(service, writing)['status'] == 'blocked'
