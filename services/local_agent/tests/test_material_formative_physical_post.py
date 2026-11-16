"""Real ModelGateway/HTTPX serialization, isolated transport, no network or credentials."""
import json

import httpx
import pytest

from home_study_agent.config import Settings
from home_study_agent.model_gateway import ModelGateway
from home_study_agent.database import Database
import test_open_response_paper_http as journey
from test_paper_writing_runtime_http import PaperGateway


class PhysicalPaperGateway(PaperGateway):
    def __init__(self, fault, root):
        super().__init__()
        self.posts, self.mutate, self.reply = [], None, None
        self.actual_results = []
        self.fault = fault
        self.actual = ModelGateway(Settings(data_dir=root, database_path=root/'transport.db',
            model_provider='deepseek', model_api_key='synthetic-only',
            model_base_url='https://material.invalid', model_name='synthetic-formative'),
            transport=httpx.MockTransport(self.transport))

    async def transport(self, request):
        assert request.method=='POST' and request.url.host=='material.invalid'
        payload=json.loads(request.content)
        context=json.loads(payload['messages'][1]['content'])
        assert set(context)=={'question','response','rubric'}
        assert '[conditions]' in context['question']
        self.posts.append(payload)
        if self.mutate:
            self.mutate()
        if self.fault=='revoke_retry':
            return httpx.Response(500,json={'error':'synthetic retry'})
        return httpx.Response(200,json={'choices':[{'message':{'content':self.reply}}],
            'usage':{'prompt_tokens':100,'completion_tokens':40}})

    async def complete(self, request):
        reply=await super().complete(request)
        if request.request_type=='voice_interaction':
            return reply
        assert callable(request.pre_dispatch)
        self.reply=reply.content
        result = await self.actual.complete(request)
        self.actual_results.append(result)
        return result

    async def close(self):
        await self.actual.close()


def test_unreviewed_formula_does_not_reach_grader(tmp_path,monkeypatch):
    gateway=PhysicalPaperGateway(None,tmp_path)
    monkeypatch.setattr(journey,'PaperGateway',lambda:gateway)
    journey.test_school_paper_voice_feedback_revision_restart_and_no_false_mastery(
        tmp_path,monkeypatch,'物理','revision',True,formula_revision=True)
    assert len(gateway.posts)==1


@pytest.mark.parametrize('fault',[None,'revoke_retry','revoke_result'])
def test_material_paper_actual_gateway_post_and_revocation(tmp_path,monkeypatch,fault):
    gateway=PhysicalPaperGateway(fault,tmp_path)
    monkeypatch.setattr(journey,'PaperGateway',lambda:gateway)
    journey.test_school_paper_voice_feedback_revision_restart_and_no_false_mastery(
        tmp_path,monkeypatch,'物理','revision',True,transport_fault=fault)
    assert len(gateway.posts)==(2 if fault is None else 1)
    if fault is None:
        with Database(tmp_path/'open-supply.db').connect() as c:
            assessments=[json.loads(r[0]) for r in c.execute('SELECT payload_json FROM rubric_assessment ORDER BY rowid')]
            executions=[json.loads(r[0]) for r in c.execute("SELECT record_json FROM learning_evaluation_execution WHERE state='completed' ORDER BY rowid")]
        assert len(assessments)==len(executions)==len(gateway.actual_results)==2
        for assessment,execution,result in zip(assessments,executions,gateway.actual_results):
            assert assessment['scorer_provenance']==execution['scorer_provenance']==result.scorer_provenance
            assert assessment['automatic_quality_status']=='not_calibrated'
            assert assessment['scorer_provenance']['immutable_revision_verified'] is False
