"""A real ordinary conversation must not become the identity of a writing task."""
import json

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.learning_outcome_evaluator import LearningOutcomeEvaluator
from home_study_agent.model_gateway import ModelResult
from test_cloud_first_agent import Planner
from test_diagnostic_prompt_http import environment
from test_writing_feedback_focus_http import PaperJourney, JourneyGateway, assert_specific_feedback
from test_paper_writing_runtime_http import synthetic_page
from test_writing_followup_paper_http import revised_page


class ConversationGateway(JourneyGateway):
    async def complete(self, request):
        if self.control == 'conversation.respond':
            self.requests.append(request)
            if request.request_type == 'voice_interaction':
                value = {'role': 'unknown', 'control': None, 'quote': self.quote, 'confidence': .99}
            else:
                assert request.request_type == 'agent_plan', request.request_type
                value = Planner('conversation.respond', {'response': '体育课里有让你开心的事呀。'}, self.quote, role='companionship').payload
                value['objective'] = {'mode': 'none', 'type': None, 'summary': None, 'success_criteria': [], 'phase': 'execute'}
                value['spoken_transition'] = '体育课里有让你开心的事呀。'
            return ModelResult(content=json.dumps(value), provider='fixture', model='fixture', latency_ms=1)
        return await super().complete(request)


@pytest.mark.parametrize('check', ['feedback', 'followup'])
def test_actual_conversation_restart_keeps_original_writing_assessment(environment, monkeypatch, check):
    settings, db = environment
    journey = PaperJourney(settings, db, monkeypatch)
    journey.gateway = ConversationGateway()
    with TestClient(create_app(settings, model_gateway=journey.gateway)) as client:
        journey.begin(client)
        first_id, first = journey.submit(client, journey.prompt, 'before-chat', *synthetic_page(monkeypatch))
        assert_specific_feedback(first)
        chat = journey.say(client, '我今天上体育课很开心', 'ordinary-conversation', 'conversation.respond', None)
        assert chat.get('runtime_capability') == 'conversation.respond', chat
        done = client.post(f"/v1/agent-turns/{chat['agent_turn_id']}/execute")
        assert done.status_code == 200, done.text
        assert done.json()['execution']['response_text'] == '体育课里有让你开心的事呀。'
        with db.connect() as c:
            last = c.execute('''SELECT s.capability_name,p.objective_id FROM agent_turn t JOIN agent_plan p USING(agent_turn_id)
                JOIN agent_step s USING(plan_id) WHERE t.session_id=? ORDER BY t.rowid DESC LIMIT 1''', (journey.sid,)).fetchone()
            assert last[0] == 'conversation.respond'
            assert last['objective_id'] is None
            original = c.execute('''SELECT o.status FROM agent_objective o JOIN agent_plan p USING(objective_id)
                WHERE p.agent_turn_id=?''', (first_id,)).fetchone()
            assert original['status'] == 'active'
    with TestClient(create_app(settings, model_gateway=journey.gateway)) as client:
        if check == 'feedback':
            assert_specific_feedback(journey.recovered(client, first_id))
            assert len(journey.gateway.evaluations) == 1
            return
        chosen = journey.say(client, '我想按刚才的反馈自己改一遍', 'original-after-chat', 'writing.revise_original', journey.prompt)
        assert chosen.get('runtime_capability') == 'writing.revise_original', chosen
        packet = chosen['action']['writing_followup_binding']['action']
        assert packet['parent_prompt_id'] == journey.prompt['prompt_id']
        assert packet['assessment_id'] == first['execution']['result']['rubric_assessment']['assessment_id']
        prepared = client.post(f"/v1/agent-turns/{chosen['agent_turn_id']}/execute")
        assert prepared.status_code == 200, prepared.text
        target = prepared.json()['execution']['result']['prompt']
        assert target['prompt_id'] != journey.prompt['prompt_id']
        second_id, second = journey.submit(client, target, 'after-chat', *revised_page(monkeypatch, 'revision'))
        assert_specific_feedback(second)
        assert second_id != first_id and len(journey.gateway.evaluations) == 2


def test_child_hears_specific_advice_not_internal_certification_vocabulary():
    feedback = 'Make the meeting time easier to find.'
    spoken = LearningOutcomeEvaluator._response_text({'formative_only': True, 'formative_feedback_kind': 'written',
        'objective_update_applied': True, 'mastery_gate': {'status': 'unknown'},
        'spoken_feedback': feedback, 'next_check': 'Do not present an unbound task.'})
    assert spoken == feedback


def test_filtered_mastery_claim_leaves_a_child_facing_self_check():
    spoken = LearningOutcomeEvaluator._response_text({'formative_only': True, 'formative_feedback_kind': 'written',
        'objective_update_applied': True, 'mastery_gate': {'status': 'unknown'},
        'spoken_feedback': 'You have mastered writing.', 'next_check': 'PRIVATE_UNBOUND_TASK'})
    assert spoken == '我们先对照这道题的要求，看看原稿有没有漏答的部分。'
    assert 'PRIVATE_UNBOUND_TASK' not in spoken
