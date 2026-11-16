"""Only the local source projection can authorize feedback delivery."""
import asyncio
from dataclasses import replace
import json

from test_formative_evaluator import evaluation
from test_formative_evaluation_projection import projection, writing


def test_actual_evaluator_discards_model_authored_oral_delivery_permission(evaluation,monkeypatch):
    _,_,service,gateway,calls,turn=evaluation
    original=gateway.complete
    async def claimed(request):
        response=await original(request)
        payload=json.loads(response.content)
        payload.update(feedback_delivery_allowed=True,oral_feedback={'version':'oral_teaching_v2',
            'status':'reviewed_audio_feedback','mastery_claim_allowed':False,'local_only':True,'cloud_transmission_allowed':False})
        return replace(response,content=json.dumps(payload))
    monkeypatch.setattr(gateway,'complete',claimed)
    result=asyncio.run(service.evaluate(turn))
    assert 'feedback_delivery_allowed' not in result['evaluation']
    assert 'oral_feedback' not in result['evaluation']
    restored=service.execution_status(turn)['result']
    assert 'feedback_delivery_allowed' not in restored['evaluation']
    assert 'oral_feedback' not in restored['evaluation']
    assert len(calls)==1
