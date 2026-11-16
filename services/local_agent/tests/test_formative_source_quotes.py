"""Exact quotation checks; synthetic inputs and no external model requests."""
import json
from dataclasses import replace

import pytest

from home_study_agent.learning_outcome_evaluator import LearningOutcomeEvaluator, InvalidLearningEvaluationError
from home_study_agent.learning_rubric import RubricEvidenceService
from home_study_agent.database import Database
from test_learning_rubric_review import gate, candidate, CHILD
from test_learning_outcome_mastery_gate import flow


@pytest.mark.parametrize('quotes', [['Invented measurement'], 'Given observation', [42], ['']])
def test_formative_receipt_rejects_fabricated_or_malformed_source_quotes(quotes):
    payload = {'question': 'Given observation: the wire is longer.', 'response': 'Length matters.',
        'rubric': {'dimensions': [{'dimension_id': 'evidence', 'criterion': 'Use the observation'}], 'task_points': []}}
    result = {'rubric_dimensions': [{'dimension_id': 'evidence', 'score': 2,
        'response_quotes': ['Length matters.'], 'source_quotes': quotes, 'rationale': 'Observed'}]}
    with pytest.raises(InvalidLearningEvaluationError, match='FORMATIVE_SOURCE_QUOTE_INVALID'):
        LearningOutcomeEvaluator._validate_formative_receipt(payload, result)


@pytest.mark.parametrize('valid_quote', [True, False])
def test_frozen_candidate_preserves_only_real_material_quotes_after_restart(gate, monkeypatch, valid_quote):
    passage = '雨停后，父亲把伞向孩子这边倾了倾。'
    answer = '父亲把伞向孩子这边倾，表现了关心。'
    with gate.db.connect() as c:
        c.execute("UPDATE diagnostic_item SET stimulus_text=? WHERE diagnostic_item_id='diag-j3-chinese-reading-1'", (passage,))
        c.commit()
    original_complete = gate.gateway.complete

    async def complete(request):
        result = await original_complete(request)
        content = json.loads(result.content)
        content['rubric_dimensions'] = [{'dimension_id': 'evidence', 'score': 2,
            'response_quotes': [answer], 'source_quotes': [passage if valid_quote else '这是虚构的材料句子'],
            'rationale': '对照给定原文。'}]
        return replace(result, content=json.dumps(content))

    monkeypatch.setattr(gate.gateway, 'complete', complete)
    snapshot, _, _ = candidate(gate, item='diag-j3-chinese-reading-1', subject='语文', answer=answer)
    current = RubricEvidenceService(Database(gate.db.path)).get(CHILD, snapshot['assessment_id'])
    dimension = next(d for d in current['dimensions'] if d['dimension_id'] == 'evidence')
    assert dimension['source_quotes'] == ([passage] if valid_quote else [])
    assert dimension['score'] == (2 if valid_quote else None)
    assert current['score_status'] == 'candidate'
