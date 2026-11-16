"""Published non-seed task, parent purpose grant and two actual paper drafts."""
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from test_writing_question_authorization import published_question
from test_writing_question_authorization_http import host, path, grant_body, parent
from test_writing_feedback_focus_http import PaperJourney, assert_specific_feedback
from test_paper_writing_runtime_http import synthetic_page
from test_writing_followup_paper_http import revised_page


def test_published_purpose_bound_writing_feedback_survives_query_restart_and_revision(published_question, monkeypatch):
    settings = host(published_question, monkeypatch)
    db, _, _, published, _ = published_question
    journey = PaperJourney(settings, db, monkeypatch)
    url = path(published_question)
    with TestClient(create_app(settings, model_gateway=journey.gateway)) as client:
        journey.begin(client, item_id=published['diagnostic_item_id'])
        current = client.get(url, headers=journey.headers)
        assert current.status_code == 200 and current.json()['status'] == 'authorization_required', current.text
        body = grant_body(current.json())
        granted = client.post(url+'/grant', headers=journey.headers, json=body)
        assert granted.status_code == 200 and granted.json()['current']['status'] == 'authorized', granted.text
        first_id, first = journey.submit(client, journey.prompt, 'published-first', *synthetic_page(monkeypatch))
        assert_specific_feedback(first)
        query = journey.say(client, '今天计划做到哪了', 'published-query', 'planning.describe', None)
        assert query.get('runtime_capability') == 'planning.describe', query
        done = client.post(f"/v1/agent-turns/{query['agent_turn_id']}/execute")
        assert done.status_code == 200, done.text
    with TestClient(create_app(settings, model_gateway=journey.gateway)) as client:
        assert_specific_feedback(journey.recovered(client, first_id))
        chosen = journey.say(client, '我想按刚才的反馈自己改一遍', 'published-revision', 'writing.revise_original', journey.prompt)
        assert chosen.get('runtime_capability') == 'writing.revise_original', chosen
        prepared = client.post(f"/v1/agent-turns/{chosen['agent_turn_id']}/execute")
        assert prepared.status_code == 200, prepared.text
        target = prepared.json()['execution']['result']['prompt']
        assert target['diagnostic_item_id'] == published['diagnostic_item_id']
        assert target['prompt_id'] != journey.prompt['prompt_id']
        second_id, second = journey.submit(client, target, 'published-second', *revised_page(monkeypatch, 'revision'))
        assert_specific_feedback(second)
        assert second_id != first_id and len(journey.gateway.evaluations) == 2
        headers = parent(client)
        revoked = client.post(url+'/revoke', headers=headers, json={'expected_revision': 1, 'client_action_id': 'revoke-published-teaching'})
        assert revoked.status_code == 200 and revoked.json()['current']['status'] == 'revoked', revoked.text
        for turn_id in (first_id, second_id):
            result = journey.recovered(client, turn_id)
            assert result['execution']['result']['feedback_status'] == 'blocked'
            assert 'Make the meeting time easier to find.' not in result['execution']['response_text']
    with TestClient(create_app(settings, model_gateway=journey.gateway)) as client:
        replay = client.post(url+'/grant', headers=parent(client), json=body)
        assert replay.status_code == 200 and replay.json()['current']['status'] == 'revoked', replay.text
        assert journey.recovered(client, second_id)['execution']['result']['feedback_status'] == 'blocked'
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 2
            assert c.execute('SELECT COUNT(*) FROM rubric_assessment').fetchone()[0] == 2
            assert c.execute('SELECT COUNT(*) FROM writing_followup').fetchone()[0] == 1
            assert not c.execute('PRAGMA foreign_key_check').fetchall()
        assert len(journey.gateway.evaluations) == 2
