"""Actual request descriptors match pending and immutable saved contracts."""
import json
from types import SimpleNamespace

import pytest

from home_study_agent.formative_criterion_review import VERSION
from home_study_agent.learning_outcome_evaluator import (
    FORMATIVE_SYSTEM_PROMPT,LEGACY_FORMATIVE_SYSTEM_PROMPT,PROMPT_VERSION,
)
from home_study_agent.rubric_calibration_evidence import digest
from home_study_agent.rubric_calibration_host import describe_execution
from test_formative_evaluator import evaluation
from test_formative_evaluation_projection import projection,writing


@pytest.mark.parametrize('state',['pending','claimed','legacy_claimed','legacy_step','unknown_version'])
def test_descriptor_matches_actual_response_contract(evaluation,state):
    writing,scope,service,_,calls,turn=evaluation
    service.settings=SimpleNamespace(model_provider='qwen',model_name='fixture',
        model_base_url='https://isolated.invalid/v1')
    if state=='legacy_step':
        with writing[0].connect() as c:
            c.execute("UPDATE agent_step SET status='completed',result_json=? WHERE capability_name='learning.evaluate_response'",
                (json.dumps({'understanding':'partial','confidence':.5}),))
            c.commit()
    elif state!='pending':
        claim=service._claim(service._load(turn))
        assert claim['acquired']
        record=json.loads(claim['record_json'])
        if state=='legacy_claimed':
            record.pop('formative_protocol_version')
        elif state=='unknown_version':
            record['formative_protocol_version']='unrecognized-future-version'
        with writing[0].connect() as c:
            c.execute('UPDATE learning_evaluation_execution SET record_json=?',(json.dumps(record),))
            c.commit()
    if state=='unknown_version':
        with pytest.raises(ValueError,match='FORMATIVE_CHECK_VERSION'):
            describe_execution(service,scope['child_id'],turn)
    else:
        actual=describe_execution(service,scope['child_id'],turn)['scorer']
        legacy=state in {'legacy_claimed','legacy_step'}
        assert actual['parser_version']==(PROMPT_VERSION if legacy else VERSION)
        assert actual['prompt_hash']==digest([LEGACY_FORMATIVE_SYSTEM_PROMPT if legacy else FORMATIVE_SYSTEM_PROMPT])
    assert calls==[]
