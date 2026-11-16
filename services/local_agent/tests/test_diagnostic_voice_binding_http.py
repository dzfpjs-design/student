import json

from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from home_study_agent.model_gateway import ModelResult
from test_app import client_for
from test_cloud_first_agent import Planner


def test_late_local_answer_cannot_rebind_to_replacement_question(tmp_path):
    class SwitchQuestion(Planner):
        async def complete(self, request):
            self.requests.append(request)
            self.prompts.cancel(self.sid, prompt_id=self.original['prompt_id'], item_version=self.original['item_version'])
            self.replacement = self.prompts.prepare(self.sid, preferred_item_id='diag-p4-arithmetic-closed-2', client_action_id='replacement')['prompt']
            return ModelResult(content=json.dumps(self.payload), provider='test', model='test', latency_ms=1, prompt_tokens=1, completion_tokens=1)

    gateway = SwitchQuestion('diagnostic.submit', {'answer': '90'}, '90', role='answer', objective_type='learning')
    with client_for(tmp_path, model_gateway=gateway, agent_runtime_enabled=True) as client:
        repo = client.app.state.repository
        sid = repo.start_session('child-primary-4', 'start')['session_id']
        prompts = DiagnosticPromptService(repo.database)
        original = prompts.prepare(sid, preferred_item_id='diag-p4-arithmetic-closed-1', client_action_id='original')['prompt']
        gateway.prompts, gateway.sid, gateway.original = prompts, sid, original
        response = client.post(f'/v1/sessions/{sid}/agent-turn', json={ 'transcript': '90', 'client_action_id': 'bound-answer', 'cloud_text_allowed': True })
        assert response.status_code == 200, response.text
        action = response.json()['action']
        assert action['type'] == 'submit_diagnostic_answer', response.json()
        assert action['diagnostic_prompt_id'] == original['prompt_id']
        assert action['diagnostic_item_version'] == original['item_version']
        # Local question content never enters the cloud planner. Replace the
        # question in the real plan-to-score window, not an unreachable callback.
        assert gateway.requests == []
        prompts.cancel(sid, prompt_id=original['prompt_id'], item_version=original['item_version'])
        replacement = prompts.prepare(sid, preferred_item_id='diag-p4-arithmetic-closed-2', client_action_id='replacement')['prompt']
        saved = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/answer', json={
            'prompt_id': action['diagnostic_prompt_id'], 'item_version': action['diagnostic_item_version'],
            'answer': '90', 'client_action_id': 'diagnostic-answer:' + original['prompt_id'],
            'authorized_turn_id': response.json()['agent_turn_id'],
        })
        assert saved.status_code == 200, saved.text
        assert saved.json()['status'] == 'cancelled'
        assert prompts.read(sid)['prompt']['prompt_id'] == replacement['prompt_id']
        assert prompts.read(sid)['status'] == 'waiting_answer'
        with repo.database.connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt WHERE session_id=?', (sid,)).fetchone()[0] == 0
