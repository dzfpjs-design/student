import asyncio
import json

import pytest

from home_study_agent.database import Database
from home_study_agent.paper_response_origin import snapshot
from home_study_agent.paper_scoring import PaperScoringService
from home_study_agent.paper_scoring_source_authority import PaperScoringSourceAuthorityService
from home_study_agent.paper_scoring_source_match import PaperScoringSourceMatchService
from home_study_agent.paper_scoring_supply import PaperScoringAuthorityDraftService
from home_study_agent.paper_subjective_rubric import PaperSubjectiveRubricService, digest
from home_study_agent.repository import StudyRepository
from test_answer_attempt_ledger import setup_case
from test_paper_scoring_source_match import add_local_page
from test_paper_subjective_rubric import SubjectiveGateway, prepared


def freeze(db, request):
    PaperScoringSourceAuthorityService(db).process_next()
    view = PaperScoringService(db).for_candidate(request['candidate_id'], 'child')
    return view['authority_supply']['latest_evidence']['frozen_rubric_artifact_id']


def prepared_visual(tmp_path, *, latest=False, unmarked=False):
    db, monitor = setup_case(tmp_path)
    if unmarked:
        with db.connect() as c:
            details = json.loads(c.execute("SELECT details_json FROM capture_artifact WHERE artifact_id='page'").fetchone()[0])
            for candidate in details['local_pipeline']['wrong_question_candidates']:
                candidate['mark_kind'] = 'unverified_mark'
            details['cloud_vision_fallback']['blocks'] = [block for block in details['cloud_vision_fallback']['blocks']
                                                         if block.get('source') != 'grading_mark']
            c.execute("UPDATE capture_artifact SET details_json=? WHERE artifact_id='page'", (json.dumps(details, ensure_ascii=False),))
            c.commit()
    monitor.record_revision('study', 'page', 'page-signature', 'origin-revision')
    if latest:
        with db.connect() as c:
            c.execute('''INSERT INTO capture_artifact
                SELECT 'page-next',child_id,session_id,artifact_type,local_path,extracted_text,
                    confidence,details_json,status,created_at,'next-page-action'
                FROM capture_artifact WHERE artifact_id='page' ''')
            c.commit()
        monitor.record_revision('study', 'page-next', 'next-signature', 'next-origin-revision')
    with db.connect() as c:
        candidate = c.execute('SELECT * FROM mistake_capture_candidate').fetchone()
        visual = c.execute('SELECT * FROM observed_answer_evidence ORDER BY rowid DESC LIMIT 1').fetchone()
    request = PaperScoringService(db).request(candidate_id=candidate['candidate_id'], child_id='child',
        session_id='study', question_text=candidate['question_text'], answer_text=visual['answer_text'],
        answer_source='visual_unverified', visual_answer_evidence_id=visual['visual_answer_evidence_id'],
        fact_source='qualified_visual_observation', fact_confidence=.94, client_action_id='origin-request')
    asyncio.run(PaperScoringAuthorityDraftService(db, SubjectiveGateway(), StudyRepository(db), lambda: True).process_next())
    add_local_page(db, text=f"{request['question_text']}\n评分标准：\n1. 列出准确的数量关系（2.5分）\n2. 计算结果与单位正确（1分）")
    PaperScoringSourceMatchService(db).process_next()
    return db, request, freeze(db, request)


def test_exact_paper_execution_preserves_printed_scale_and_restarts(tmp_path):
    db, request, aid = prepared_visual(tmp_path)
    service = PaperSubjectiveRubricService(db)
    packet = service.execution_context('child', aid)
    assert packet['question'] == request['question_text'] and packet['response'] == '80'
    assert packet['maximum_score'] == 3.5 and packet['criteria'][0]['maximum_score'] == 2.5
    original = packet['original_response']
    assert original['modality'] == 'original_paper_text'
    assert original['artifact_id'] == 'page' and original['original_written_answer_available'] is True
    assert original['visual_answer_evidence_id'] == request['visual_answer_evidence_id']
    assert packet['score_authority_granted'] is False and packet['cloud_transmission_allowed'] is False
    restarted = PaperSubjectiveRubricService(Database(db.path, runtime_mode=db.runtime_mode))
    assert restarted.execution_context('child', aid) == packet
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM learning_evidence').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM paper_scoring_decision').fetchone()[0] == 0
    with pytest.raises(ValueError, match='UNKNOWN'):
        service.execution_context('other-child', aid)


@pytest.mark.parametrize('mutation', ['page_text', 'page_details', 'candidate_text', 'visual_confidence', 'revision_owner', 'request_answer'])
def test_any_changed_original_blocks_execution_and_live_supply(mutation, tmp_path):
    db, request, aid = prepared_visual(tmp_path)
    with db.connect() as c:
        if mutation == 'page_text':
            c.execute("UPDATE capture_artifact SET extracted_text='different original' WHERE artifact_id='page'")
        elif mutation == 'page_details':
            c.execute("UPDATE capture_artifact SET details_json='{}' WHERE artifact_id='page'")
        elif mutation == 'candidate_text':
            c.execute("UPDATE mistake_capture_candidate SET question_text='different question'")
        elif mutation == 'visual_confidence':
            c.execute('DROP TRIGGER observed_answer_evidence_no_update')
            c.execute('UPDATE observed_answer_evidence SET confidence=.4')
        elif mutation == 'revision_owner':
            c.execute('UPDATE document_page_revision SET artifact_id=?', (aid,))
        else:
            c.execute('DROP TRIGGER paper_scoring_request_no_update')
            c.execute("UPDATE paper_scoring_request SET answer_text='90'")
        c.commit()
    service = PaperSubjectiveRubricService(db)
    assert service.current('child', aid)['status'] == 'source_withdrawn'
    with pytest.raises(ValueError, match='SUBJECTIVE_ORIGINAL'):
        service.execution_context('child', aid)
    view = PaperScoringService(db).for_candidate(request['candidate_id'], 'child')
    assert view['authority_supply']['status'] == 'withdrawn' and view['decision_id'] is None
    with db.connect() as c:
        # Live rejection must not rewrite the original calibration-pending audit.
        last = c.execute('SELECT status FROM paper_scoring_supply_event ORDER BY rowid DESC LIMIT 1').fetchone()
        assert last[0] == 'calibration_pending'


def test_readback_not_falsely_promoted_to_original_handwriting(tmp_path):
    db, request, _ = prepared(tmp_path)
    aid = freeze(db, request)
    original = PaperSubjectiveRubricService(db).execution_context('child', aid)['original_response']
    assert original['modality'] == 'confirmed_readback_text'
    assert original['original_written_answer_available'] is False


def test_workflow_state_changes_do_not_change_answer_identity(tmp_path):
    db, request, aid = prepared_visual(tmp_path)
    service = PaperSubjectiveRubricService(db)
    packet = service.execution_context('child', aid)
    with db.connect() as c:
        c.execute("UPDATE mistake_capture_candidate SET status='saved'")
        c.execute("UPDATE daily_session SET status='completed',state='completed'")
        c.commit()
    assert service.execution_context('child', aid) == packet


def test_old_unbound_rubric_is_preserved_but_not_an_execution_authority(tmp_path):
    db, request, aid = prepared_visual(tmp_path)
    with db.connect() as c:
        details = json.loads(c.execute('SELECT details_json FROM capture_artifact WHERE artifact_id=?', (aid,)).fetchone()[0])
        details.pop('original_response')
        details['version'] = 'paper_subjective_source_spec_v1'
        c.execute('UPDATE capture_artifact SET details_json=?,client_action_id=? WHERE artifact_id=?',
            (json.dumps(details, ensure_ascii=False), 'paper-subjective-spec:'+digest(details), aid))
        c.commit()
    view = PaperSubjectiveRubricService(db).current('child', aid)
    assert view['status'] == 'original_binding_required'
    with pytest.raises(ValueError, match='BINDING_REQUIRED'):
        PaperSubjectiveRubricService(db).execution_context('child', aid)
    assert PaperScoringService(db).for_candidate(request['candidate_id'], 'child')['authority_supply']['status'] == 'blocked_source'


def test_borrowed_transaction_never_observes_another_connection(tmp_path):
    db, request, aid = prepared_visual(tmp_path)
    service = PaperSubjectiveRubricService(None)
    with db.connect() as c:
        before = service.execution_context('child', aid, connection=c)
        c.execute('BEGIN IMMEDIATE')
        c.execute("UPDATE capture_artifact SET extracted_text='uncommitted' WHERE artifact_id='page'")
        with pytest.raises(ValueError, match='ORIGINAL_(REVISION|RESPONSE)_CHANGED'):
            service.execution_context('child', aid, connection=c)
        c.rollback()
        assert service.execution_context('child', aid, connection=c) == before


def test_later_page_answer_binds_its_page_not_the_first_question_candidate(tmp_path):
    db, request, aid = prepared_visual(tmp_path, latest=True)
    original = PaperSubjectiveRubricService(db).execution_context('child', aid)['original_response']
    assert original['artifact_id'] == 'page-next'
    with db.connect() as c:
        assert c.execute('SELECT artifact_id FROM mistake_capture_candidate WHERE candidate_id=?',
                         (request['candidate_id'],)).fetchone()[0] == 'page'


def test_changed_page_before_freeze_cannot_become_a_new_original(tmp_path):
    db, request, aid = prepared_visual(tmp_path)
    with db.connect() as c:
        c.execute("UPDATE capture_artifact SET extracted_text='changed OCR after recorded revision' WHERE artifact_id='page'")
        c.commit()
        with pytest.raises(ValueError, match='REVISION_CHANGED'):
            snapshot(c, 'child', request['request_id'])


def test_grade_change_invalidates_execution_scope_without_rewriting_original(tmp_path):
    db, request, aid = prepared_visual(tmp_path)
    before = PaperSubjectiveRubricService(db).current('child', aid)
    with db.connect() as c:
        c.execute("UPDATE child_profile SET grade='四年级' WHERE child_id='child'")
        c.commit()
    after = PaperSubjectiveRubricService(db).current('child', aid)
    assert after['status'] == 'source_withdrawn'
    assert after['original_response'] == before['original_response']


def test_export_preserves_original_binding_and_delete_does_not_leave_usable_spec(tmp_path):
    from pathlib import Path
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    db, request, aid = prepared_visual(tmp_path)
    before = PaperSubjectiveRubricService(db).execution_context('child', aid)
    privacy = PrivacyLifecycleService(db, tmp_path)
    privacy.initialize()
    exported = json.loads(Path(privacy.export_child('child')['path']).read_text())
    frozen = next(row for row in exported['capture_artifacts'] if row['artifact_id'] == aid)
    assert json.loads(frozen['details_json'])['original_response'] == before['original_response']
    with db.connect() as c:
        c.execute("DELETE FROM capture_artifact WHERE artifact_id='page'")
        c.commit()
    assert PaperSubjectiveRubricService(db).current('child', aid)['status'] == 'source_withdrawn'
    with pytest.raises(ValueError, match='RUBRIC_SOURCE_CHANGED'):
        PaperSubjectiveRubricService(db).execution_context('child', aid)
