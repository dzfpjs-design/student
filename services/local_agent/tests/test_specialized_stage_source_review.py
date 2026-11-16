"""Explicit review, promotion and subsequent stage reads use one source binding."""
import json
from datetime import datetime, timezone

import pytest

from home_study_agent.specialized_supply import SpecializedDiagnosticSupplyService
from home_study_agent.specialized_stages import _item_source
from home_study_agent.local_supply import LocalSupplyError
from test_specialized_stages import case, CHILD, SEED


def pending(case):
    service = SpecializedDiagnosticSupplyService(case[0])
    service.enqueue_local(SEED, CHILD, 'stage-source', count=2)
    service.process_local_pending()
    candidate = next(r for r in service.list(CHILD)['items'] if r['stage'] == 'discrimination')
    context = service.stage_review_context(CHILD, candidate['candidate_id'])
    return service, candidate, context


def decision(context, stage='near_transfer'):
    return dict(schema='specialized_stage_review_v1', stage=stage, binding_version=context['binding_version'],
        source_quote=context['source_prompt'], candidate_quote=context['candidate_prompt'],
        same_skill_rationale='Human confirms the same counting decision is assessed in this source.',
        changed_context_rationale='Human checks the alternatives and changed conditions in the quoted question.')


def test_explicit_stage_review_is_persisted_replayed_and_revalidated(case):
    service, candidate, context = pending(case)
    review = decision(context)
    promoted = service.review(candidate['candidate_id'], 'approve', 'Original sources and stage checked', 'review-stage', stage_review=review)
    case[3][0] = datetime.now(timezone.utc)
    assert service.review(candidate['candidate_id'], 'approve', 'Original sources and stage checked', 'review-stage', stage_review=review) == promoted
    with case[0].connect() as c:
        row = c.execute('SELECT * FROM diagnostic_item WHERE diagnostic_item_id=?', (promoted['diagnostic_item_id'],)).fetchone()
        source, reason = _item_source(c, CHILD, row, case[3][0])
        assert reason is None, reason
        assert source['stage'] == 'near_transfer'
        assert source['source_root_item_id'] == SEED
        assert json.loads(c.execute('SELECT validation_json FROM specialized_supply_candidate WHERE candidate_id=?',
            (candidate['candidate_id'],)).fetchone()[0])['stage_review'] == review
    with pytest.raises(ValueError):
        service.review(candidate['candidate_id'], 'approve', 'Original sources and stage checked', 'review-stage', stage_review=decision(context, 'far_transfer'))
    with case[0].connect() as c:
        c.execute("UPDATE diagnostic_item SET prompt=prompt||' changed' WHERE diagnostic_item_id=?", (SEED,))
        c.commit()
    with pytest.raises(LocalSupplyError):
        service.stage_review_context(CHILD, candidate['candidate_id'])


@pytest.mark.parametrize('fault', ['stale', 'quote', 'rationale', 'extra', 'child', 'reject'])
def test_stage_review_cannot_approve_unbound_or_invented_source(case, fault):
    service, candidate, context = pending(case)
    review = decision(context, 'far_transfer')
    if fault == 'stale': review['binding_version'] = 'different'
    if fault == 'quote': review['candidate_quote'] = 'invented source excerpt'
    if fault == 'rationale': review['same_skill_rationale'] = ''
    if fault == 'extra': review['allow_stage'] = True
    with pytest.raises((ValueError, KeyError)):
        if fault == 'child': service.stage_review_context('child-junior-3', candidate['candidate_id'])
        else: service.review(candidate['candidate_id'], 'reject' if fault == 'reject' else 'approve', 'review note', 'review', stage_review=review)
    assert next(r for r in service.list(CHILD)['items'] if r['candidate_id'] == candidate['candidate_id'])['status'] == 'needs_review'
