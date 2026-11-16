import asyncio
import json

import pytest

from home_study_agent.model_gateway import ModelResult
from home_study_agent.paper_scoring import PaperScoringService
from home_study_agent.paper_scoring_supply import PaperScoringAuthorityDraftService
from home_study_agent.repository import StudyRepository
from test_paper_scoring_authority_draft import pending_request, valid_payload


def mutate(db):
    with db.connect() as c:
        c.execute("UPDATE capture_artifact SET extracted_text='changed after claim' WHERE artifact_id='page'")
        c.commit()


class ChangingGateway:
    configured = True

    def __init__(self, db, phase):
        self.db, self.phase, self.physical_calls = db, phase, 0
        self.messages = None

    async def complete(self, request):
        self.messages = request.messages
        if self.phase == 'dispatch':
            mutate(self.db)
        request.pre_dispatch()
        self.physical_calls += 1
        if self.phase == 'commit':
            mutate(self.db)
        return ModelResult(json.dumps(valid_payload()), 'fixture', 'fixture', 1, 4, 5)


@pytest.mark.parametrize('phase', ['dispatch', 'commit'])
def test_changed_original_drops_model_draft_and_never_retries_paid_work(tmp_path, phase):
    db, request = pending_request(tmp_path)
    gateway = ChangingGateway(db, phase)
    service = PaperScoringAuthorityDraftService(db, gateway, StudyRepository(db), lambda: True)
    result = asyncio.run(service.process_next())
    assert result['status'] == 'blocked' and result['attempt_count'] == 1
    assert result['error_code'] == 'PAPER_AUTHORITY_ORIGINAL_CHANGED'
    assert gateway.physical_calls == (0 if phase == 'dispatch' else 1)
    assert asyncio.run(service.process_next()) is None
    view = PaperScoringService(db).for_candidate(request['candidate_id'], 'child')
    assert view['decision_id'] is None and view['authority_supply']['status'] == 'blocked_source'
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM paper_scoring_supply_event WHERE status='rubric_candidate'").fetchone()[0] == 0
        audit = c.execute("SELECT * FROM model_call WHERE purpose='paper_scoring_authority_draft'").fetchone()
        assert audit['status'] == 'failed' and audit['error_code'] == 'PAPER_AUTHORITY_ORIGINAL_CHANGED'


def test_deleted_original_does_not_dispatch_any_model_request(tmp_path):
    db, request = pending_request(tmp_path)
    with db.connect() as c:
        c.execute("UPDATE capture_artifact SET status='failed' WHERE artifact_id='page'")
        c.commit()
    gateway = ChangingGateway(db, 'normal')
    service = PaperScoringAuthorityDraftService(db, gateway, StudyRepository(db), lambda: True)
    result = asyncio.run(service.process_next())
    assert result['status'] == 'blocked' and result['error_code'] == 'PAPER_AUTHORITY_ORIGINAL_UNAVAILABLE'
    assert gateway.messages is None and gateway.physical_calls == 0
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0] == 0


def test_model_receives_only_existing_text_projection_not_origin_or_rubric(tmp_path):
    db, request = pending_request(tmp_path)
    gateway = ChangingGateway(db, 'normal')
    result = asyncio.run(PaperScoringAuthorityDraftService(db, gateway, StudyRepository(db), lambda: True).process_next())
    assert result['status'] == 'completed'
    projection = json.loads(gateway.messages[1]['content'])
    assert set(projection) == {'grade', 'curriculum_region', 'question', 'student_answer'}
    assert 'source_version' not in gateway.messages[1]['content'] and 'page' not in projection
    evidence = PaperScoringService(db).for_candidate(request['candidate_id'], 'child')['authority_supply']['latest_evidence']
    assert len(evidence['original_response_version']) == 64 and evidence['authority_granted'] is False
