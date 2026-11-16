"""Child source removal is not permission to retire a family's shared catalog."""
import pytest

from home_study_agent.database import Database
from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from home_study_agent.document_monitoring import DocumentMonitoringService
from home_study_agent.knowledge_base import KnowledgeBaseService
from home_study_agent.learning_obligations import project_child_mastery_evidence, withdrawn_evidence_ids
from home_study_agent.learning_state import LearningStateService
from home_study_agent.privacy_evidence_cleanup import PrivacyEvidenceCleanupError
from home_study_agent.privacy_native_sources import purge_native_sources
from home_study_agent.repository import StudyRepository
from test_curriculum_supply import prepared, publish, CHILD, SCOPE
from test_mistake_gate_reliability import flow

OTHER = 'child-junior-3'
ITEM = 'diag-p4-arithmetic-closed-1'
NODE = 'node-p4-math-arithmetic'


def import_source(db, child, ref, text):
    return KnowledgeBaseService(db).import_source(child, {
        'source_type':'school_material', 'title':'Isolated shared source', 'subject':'数学',
        'version_label':SCOPE['edition'],
        'grade_band':'四年级', 'source_ref':ref, 'license_status':'family_owned_index',
        'parent_reviewed':True, 'content_text':text})['source_id']


def owner_snapshot(c, child):
    result = {}
    for (table,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall():
        columns = {r[1] for r in c.execute('PRAGMA table_info("'+table+'")')}
        if 'child_id' in columns:
            result[table] = sorted((tuple(row) for row in c.execute('SELECT * FROM "'+table+'" WHERE child_id=?', (child,))), key=repr)
    return result


@pytest.fixture
def family(tmp_path):
    from home_study_agent.privacy_source_withdrawal import ensure_schema
    db, supply, payload = prepared(tmp_path)
    private = publish(supply, payload)
    learning, repo = LearningStateService(db), StudyRepository(db)
    with db.connect() as c:
        ensure_schema(c)
        # Two synthetic same-grade owners can legitimately answer one school item.
        # Set the profile before any source review or scoring, never alter receipts.
        c.execute("UPDATE child_profile SET grade='四年级' WHERE child_id=?", (OTHER,))
        ref = c.execute('SELECT source_ref FROM diagnostic_item WHERE diagnostic_item_id=?', (ITEM,)).fetchone()[0]
        private_source = c.execute('SELECT source_id FROM knowledge_source WHERE child_id=?', (CHILD,)).fetchone()[0]
        c.commit()
    mine = import_source(db, CHILD, ref, 'Shared catalog body, child one copy')
    theirs = import_source(db, OTHER, ref, 'Shared catalog body, child two copy')
    sid = repo.start_session(CHILD, 'shared-first')['session_id']
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
        c.commit()
    own = learning.submit_diagnostic(sid, ITEM, '90', 'shared-first-answer')
    private_answer = learning.submit_diagnostic(sid, private['diagnostic_item_id'], '5000', 'private-answer')
    prompts = DiagnosticPromptService(db)
    prompt = prompts.prepare(sid, track='school', preferred_item_id=ITEM, client_action_id='shared-first-prompt')['prompt']
    DocumentMonitoringService(db)
    other_sid = repo.switch_active_session_child(sid, OTHER, 'shared-switch')['session_id']
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (other_sid,))
        c.commit()
    other_answer = learning.submit_diagnostic(other_sid, ITEM, '90', 'shared-other-answer')
    other_prompt = prompts.prepare(other_sid, track='school', preferred_item_id=ITEM, client_action_id='shared-other-prompt')['prompt']
    assert own['score_status'] == other_answer['score_status'] == private_answer['score_status'] == 'verified_local'
    return db, {mine, private_source}, theirs, ref, private, own, other_answer, prompt, other_prompt


def erase(c, ids, scopes):
    return purge_native_sources(c, CHILD, owner_source_ids=ids,
        shared_detach_item_ids={ITEM}, affected_scopes=scopes)


def test_shared_catalog_other_child_and_private_descendant_restart_reimport(family):
    db, ids, theirs, ref, private, own, other, prompt, other_prompt = family
    with db.connect() as c:
        before = owner_snapshot(c, OTHER)
        projection = project_child_mastery_evidence(c, OTHER)
        shared = tuple(c.execute('SELECT * FROM diagnostic_item WHERE diagnostic_item_id=?', (ITEM,)).fetchone())
        c.execute('BEGIN IMMEDIATE')
        scopes = {}
        counts = erase(c, ids, scopes)
        assert scopes == {CHILD:{NODE}}
        assert c.in_transaction
        assert owner_snapshot(c, OTHER) == before
        assert project_child_mastery_evidence(c, OTHER) == projection
        assert tuple(c.execute('SELECT * FROM diagnostic_item WHERE diagnostic_item_id=?', (ITEM,)).fetchone()) == shared
        assert c.execute('SELECT status FROM diagnostic_item WHERE diagnostic_item_id=?', (private['diagnostic_item_id'],)).fetchone()[0] == 'retired'
        assert c.execute('SELECT status,item_json FROM diagnostic_prompt WHERE prompt_id=?', (prompt['prompt_id'],)).fetchone()[:] == ('invalidated','{}')
        assert c.execute('SELECT status FROM diagnostic_prompt WHERE prompt_id=?', (other_prompt['prompt_id'],)).fetchone()[0] == 'waiting_answer'
        assert c.execute('SELECT submitted_answer FROM diagnostic_attempt WHERE attempt_id=?', (own['attempt_id'],)).fetchone()[0] == '90'
        assert own['evidence_id'] in withdrawn_evidence_ids(c, CHILD)
        assert other['evidence_id'] not in withdrawn_evidence_ids(c, OTHER)
        assert counts['knowledge_source'] == 2 and counts['knowledge_excerpt'] == 2
        LearningStateService(db).replay_mastery(CHILD, NODE, connection=c, reason='source_deleted')
        assert c.execute('PRAGMA foreign_key_check').fetchall() == []
        c.commit()
    new_id = import_source(Database(db.path), CHILD, ref, 'Shared catalog body, child one copy')
    assert new_id not in ids
    with Database(db.path).connect() as c:
        assert own['evidence_id'] in withdrawn_evidence_ids(c, CHILD)
        assert not project_child_mastery_evidence(c, CHILD)[NODE]['effective_evidence']
        assert project_child_mastery_evidence(c, OTHER) == projection
        assert owner_snapshot(c, OTHER) == before
        c.execute('BEGIN IMMEDIATE')
        assert erase(c, ids, {}).get('privacy_source_evidence_withdrawal', 0) == 0
        c.rollback()
    replay = LearningStateService(db).submit_diagnostic(prompt['origin_session_id'], ITEM, '90', 'shared-first-answer')
    assert replay['score_status'] == 'needs_review'
    assert replay['score_reason'] == 'withdrawn' and not replay['mastery_update_allowed']
    repo = StudyRepository(db)
    active = repo.get_active_session()['session_id']
    resumed = repo.switch_active_session_child(active, CHILD, 'explicit-return-after-erasure')['session_id']
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (resumed,))
        c.commit()
    new_answer = LearningStateService(db).submit_diagnostic(resumed, ITEM, '90', 'new-answer-after-reimport')
    with db.connect() as c:
        assert new_answer['evidence_id'] != own['evidence_id']
        assert new_answer['evidence_id'] not in withdrawn_evidence_ids(c, CHILD)
        assert own['evidence_id'] in withdrawn_evidence_ids(c, CHILD)


def test_failure_rolls_back_withdrawals_body_and_collector(family):
    db, ids, *rest = family
    with db.connect() as c:
        before = list(c.iterdump())
        c.execute("CREATE TEMP TRIGGER synthetic_shared_fail BEFORE UPDATE ON diagnostic_prompt BEGIN SELECT RAISE(ABORT,'isolated failure'); END")
        c.execute('BEGIN IMMEDIATE')
        scopes = {}
        with pytest.raises(PrivacyEvidenceCleanupError):
            erase(c, ids, scopes)
        assert scopes == {}
        assert list(c.iterdump()) == before
        assert c.in_transaction
        c.execute('DROP TRIGGER synthetic_shared_fail')
        erase(c, ids, scopes)
        c.rollback()
        assert list(c.iterdump()) == before


def test_foreign_owner_id_and_unbound_detach_rejected(family):
    db, ids, theirs, *rest = family
    with db.connect() as c:
        before = list(c.iterdump())
        c.execute('BEGIN IMMEDIATE')
        with pytest.raises(PrivacyEvidenceCleanupError, match='SOURCE_NOT_OWNED'):
            erase(c, ids | {theirs}, {})
        with pytest.raises(PrivacyEvidenceCleanupError, match='DETACH_NOT_BOUND'):
            purge_native_sources(c, CHILD, owner_source_ids=ids,
                shared_detach_item_ids={'unrelated-unbound-item'}, affected_scopes={})
        c.rollback()
        assert list(c.iterdump()) == before


def test_single_child_import_cannot_claim_shared_builtin_catalog(family):
    db, ids, theirs, *rest = family
    with db.connect() as c:
        # Even without a second import, absence of an owner FK is not ownership.
        c.execute('DELETE FROM knowledge_excerpt WHERE source_id=?', (theirs,))
        c.execute('DELETE FROM knowledge_source WHERE source_id=?', (theirs,))
        c.commit()
        before = [tuple(r) for r in c.execute('SELECT * FROM diagnostic_item WHERE diagnostic_item_id=?', (ITEM,))]
        c.execute('BEGIN IMMEDIATE')
        erase(c, ids, {})
        assert [tuple(r) for r in c.execute('SELECT * FROM diagnostic_item WHERE diagnostic_item_id=?', (ITEM,))] == before
        c.rollback()


def test_schema_ddl_and_withdrawal_are_borrowed_and_owner_checked(tmp_path):
    from home_study_agent.privacy_source_withdrawal import ensure_schema, record
    db = Database(tmp_path / 'schema-boundary.db')
    db.initialize()
    StudyRepository(db).seed_family()
    with db.connect() as c:
        before = list(c.iterdump())
        c.execute('BEGIN IMMEDIATE')
        ensure_schema(c)
        assert c.in_transaction
        ensure_schema(c)
        with pytest.raises(PrivacyEvidenceCleanupError, match='OWNER_MISMATCH'):
            record(c, CHILD, {'not-a-source'}, {'not-an-evidence'})
        c.rollback()
        assert list(c.iterdump()) == before
        c.execute('PRAGMA query_only=ON')
        assert withdrawn_evidence_ids(c, CHILD) == set()


@pytest.mark.parametrize('foreign_corrupt', [False, True])
def test_shared_runtime_copies_are_scoped_and_foreign_steps_survive_restart(family, foreign_corrupt):
    from test_native_runtime_source_cleanup import prompt_command, snapshot
    db, ids, _, _, _, _, _, prompt, other_prompt = family
    # Actual persisted Runtime receipts, not invented successful result JSON.
    repo = StudyRepository(db)
    first_session = repo.switch_active_session_child(other_prompt['origin_session_id'], CHILD, 'runtime-first-child')['session_id']
    first, _ = prompt_command(db, CHILD, first_session, ITEM)
    second_session = repo.switch_active_session_child(first_session, OTHER, 'runtime-second-child')['session_id']
    second, _ = prompt_command(db, OTHER, second_session, ITEM)
    with db.connect() as c:
        if foreign_corrupt:
            c.execute("UPDATE agent_step SET result_json='{foreign-broken' WHERE plan_id IN (SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)", (second,))
            c.commit()
        kept = snapshot(c, second)
        c.execute('BEGIN IMMEDIATE')
        counts = erase(c, ids, {})
        assert counts['agent_step_redacted'] == counts['agent_turn_redacted'] == 1
        assert snapshot(c, first)['error_code'] == 'SOURCE_DELETED'
        assert snapshot(c, first)['result_json'] is None
        assert snapshot(c, second) == kept
        c.commit()
    with Database(db.path).connect() as c:
        assert snapshot(c, second) == kept


def test_unselected_review_descendant_loses_authority_without_erasing_its_question(family, monkeypatch):
    from datetime import datetime, timedelta
    from home_study_agent import learning_state
    db, _, _, _, private, *_ = family
    with db.connect() as c:
        original = c.execute('SELECT evidence_id FROM diagnostic_attempt WHERE diagnostic_item_id=?', (private['diagnostic_item_id'],)).fetchone()[0]
        review = c.execute("SELECT * FROM review_task WHERE source_evidence_id=? AND status='pending' ORDER BY due_at LIMIT 1", (original,)).fetchone()
        assert review is not None
        source_id = c.execute("SELECT source_id FROM knowledge_source WHERE child_id=? AND source_ref='isolated-fixture:page12'", (CHILD,)).fetchone()[0]
        target = c.execute('SELECT * FROM diagnostic_item WHERE diagnostic_item_id=?', (review['diagnostic_item_id'],)).fetchone()
    repo = StudyRepository(db)
    sid = repo.switch_active_session_child(repo.get_active_session()['session_id'], CHILD, 'return-for-due-review')['session_id']
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
        c.commit()
    import json
    answer = json.loads(target['expected_answer_json'])['exact_answers'][0]
    now = (datetime.fromisoformat(review['due_at']) + timedelta(seconds=1)).isoformat()
    monkeypatch.setattr(learning_state, 'utc_now', lambda: now)
    receipt = LearningStateService(db).submit_diagnostic(sid, target['diagnostic_item_id'], answer,
        'real-due-review', review_task_id=review['review_task_id'])
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        purge_native_sources(c, CHILD, owner_source_ids={source_id}, affected_scopes={})
        assert not c.execute('SELECT 1 FROM privacy_source_evidence_withdrawal WHERE evidence_id=?', (receipt['evidence_id'],)).fetchone()
        assert receipt['evidence_id'] in withdrawn_evidence_ids(c, CHILD)
        assert tuple(c.execute('SELECT * FROM diagnostic_item WHERE diagnostic_item_id=?', (target['diagnostic_item_id'],)).fetchone()) == tuple(target)
        c.rollback()


def test_shared_question_probe_original_answer_archived_without_scoring(flow):
    from test_mistake_gate_reliability import prepare
    from test_mistake_diagnosis import propose, ask, respond
    from home_study_agent.privacy_preserved_response import ensure_schema as ensure_archive, read_preserved_responses
    from home_study_agent.privacy_source_withdrawal import ensure_schema
    db, _, _, _ = flow
    with db.connect() as c:
        ensure_schema(c)
        ensure_archive(c)
        ref = c.execute('SELECT source_ref FROM diagnostic_item WHERE diagnostic_item_id=?', (ITEM,)).fetchone()[0]
        c.commit()
    source = import_source(db, CHILD, ref, 'Own shared source copy')
    import_source(db, OTHER, ref, 'Other shared source copy')
    book, case = prepare(flow)
    propose(book, case)
    probe = ask(book, case)
    answer = respond(book, case, probe['event_id'], 'Exact original explanation, not scored')
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        counts = purge_native_sources(c, CHILD, owner_source_ids={source}, affected_scopes={})
        assert counts['child_responses_preserved'] == 1
        saved = read_preserved_responses(c, CHILD, case)
        assert saved[0]['response_id'] == answer['event_id']
        assert saved[0]['response_text'] == 'Exact original explanation, not scored'
        assert saved[0]['score_authority'] == 'none'
        assert c.execute('SELECT status FROM diagnostic_item WHERE diagnostic_item_id=?', (ITEM,)).fetchone()[0] == 'active'
        c.commit()
    with Database(db.path).connect() as c:
        assert read_preserved_responses(c, CHILD, case) == saved


def test_same_child_retained_source_version_and_published_package_are_not_selected_by_ref(family):
    from home_study_agent.curriculum_supply import CurriculumSupplyService
    db, ids, _, _, private, *_ = family
    supply = CurriculumSupplyService(db)
    source = import_source(db, CHILD, 'isolated-fixture:page12', '计算：40*150。')
    with db.connect() as c:
        old_source = c.execute("SELECT source_id FROM knowledge_source WHERE child_id=? AND source_ref='isolated-fixture:page12' AND source_id!=?", (CHILD,source)).fetchone()[0]
        excerpt = c.execute('SELECT excerpt_id FROM knowledge_excerpt WHERE source_id=?', (source,)).fetchone()[0]
    payload = supply.get(CHILD, private['candidate_id'])['payload']
    supply.select_curriculum(CHILD, {**SCOPE,'textbook_source_id':source}, actor='parent',
        action_id='retained-version-selection', expected_version=1)
    payload.update(excerpt_id=excerpt, prompt='计算：40*150。', expected_answer='6000', explanation='40乘150等于6000。')
    kept = publish(supply, payload, action='retained-version')
    assert kept['eligible']
    with db.connect() as c:
        saved = {table:[tuple(r) for r in c.execute(f'SELECT * FROM {table} WHERE candidate_id=?', (kept['candidate_id'],))]
                 for table in ('curriculum_supply_package','curriculum_supply_event','specialized_supply_candidate')}
        c.execute('BEGIN IMMEDIATE')
        purge_native_sources(c, CHILD, owner_source_ids={old_source}, affected_scopes={})
        for table, rows in saved.items():
            assert [tuple(r) for r in c.execute(f'SELECT * FROM {table} WHERE candidate_id=?', (kept['candidate_id'],))] == rows
        c.commit()
    assert supply.get(CHILD, kept['candidate_id'])['eligible']


def test_legacy_empty_ref_is_erased_by_exact_id_without_matching_catalog(family):
    db, *_ = family
    source = import_source(db, CHILD, 'isolated-empty-ref-before-migration', 'Legacy source body')
    with db.connect() as c:
        c.execute("UPDATE knowledge_source SET source_ref='' WHERE source_id=?", (source,))
        c.commit()
        catalog = [tuple(r) for r in c.execute('SELECT * FROM diagnostic_item ORDER BY diagnostic_item_id')]
        other = owner_snapshot(c, OTHER)
        c.execute('BEGIN IMMEDIATE')
        counts = purge_native_sources(c, CHILD, owner_source_ids={source}, affected_scopes={})
        assert counts['knowledge_source'] == counts['knowledge_excerpt'] == 1
        assert [tuple(r) for r in c.execute('SELECT * FROM diagnostic_item ORDER BY diagnostic_item_id')] == catalog
        assert owner_snapshot(c, OTHER) == other
        c.rollback()
