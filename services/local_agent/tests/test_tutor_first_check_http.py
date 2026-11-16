"""Observed Windows over-help: one check must wait for a child response."""
import json
from dataclasses import replace

from home_study_agent.tutor_blackboard import presentation_visible_text
from test_reviewed_tutor_projection_http import prepared_client
from test_tutor_child_model_controls import RecordingGateway

PREFIX = '我们先回忆一下，一元二次方程的求根公式是什么？'
OVERHELP = PREFIX + '很好，接下来代入系数，先算判别式。判别式算出来是1，你能写出两个根吗？'


class OverhelpGateway(RecordingGateway):
    async def complete(self, request):
        result = await super().complete(request)
        return replace(result, content=json.dumps({'blackboard': {'title': '求根公式', 'segments': [
            {'narration': OVERHELP, 'blocks': [{'kind': 'formula', 'latex': 'b^2-4ac=1'},
                {'kind': 'question', 'text': '你能写出两个根吗？'}]}]}}, ensure_ascii=False))


def test_real_tutor_first_check_does_not_continue_or_show_later_computation(tmp_path):
    gateway = OverhelpGateway()
    client, db, _, _, _, _, sid, payload = prepared_client(tmp_path, gateway)
    try:
        response = client.post(f'/v1/sessions/{sid}/tutor', json=payload)
        assert response.status_code == 200, response.text
        saved = response.json()
        assert saved['answer'] == PREFIX
        board = saved['output_envelope']['result']['blackboard']
        assert ''.join(s['narration'] for s in board['segments']) == PREFIX
        visible = presentation_visible_text(board)
        for future in ['很好', '代入', '判别式', '两个根', '=1']:
            assert future not in visible
        assert saved['output_envelope'].get('local_only') is True
        assert len(gateway.requests) == 1
        replay = client.post(f'/v1/sessions/{sid}/tutor', json=payload)
        assert replay.json()['turn_id'] == saved['turn_id'] and len(gateway.requests) == 1
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
    finally:
        client.__exit__(None, None, None)
