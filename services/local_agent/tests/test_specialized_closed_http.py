"""Actual local app endpoints; synthetic reviewed closed references, no paid model."""
from time import perf_counter

from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.specialized_supply import SpecializedDiagnosticSupplyService
from test_diagnostic_prompt_http import environment, start, endpoint, binding
from test_specialized_closed_contract import ClosedGateway, counting
from test_specialized_stage_source_review import decision

CHILD = 'child-primary-4'


def generated_http(client, db):
    token = client.post('/v1/parent/setup', json={'pin': '2846'}).json()['parent_token']
    headers = {'X-HSC-Parent': token}
    client.put('/v1/parent/privacy', headers=headers, json={'cloud_model_text_allowed': True})
    service = SpecializedDiagnosticSupplyService(db)
    service.enqueue_gaps(CHILD, 'competition_math', 'http-closed-queue')
    with db.connect() as c:
        c.execute("UPDATE specialized_supply_job SET status='blocked' WHERE domain_id!='competition_math.counting'")
        c.commit()
    response = client.post('/v1/parent/specialized-supply/jobs/process-next', headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()['job']['status'] == 'completed'
    candidates = client.get(f'/v1/parent/children/{CHILD}/specialized-supply/candidates', headers=headers).json()['items']
    return headers, candidates


def test_typed_model_http_review_publish_stage_answer_restart_and_source_retract(environment):
    settings, db = environment
    gateway = ClosedGateway()
    timings = []
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        headers, candidates = generated_http(client, db)
        approved = {}
        for row in candidates:
            stage = {'discrimination': 'near_transfer', 'transfer': 'far_transfer'}.get(row['stage'])
            body = {'decision': 'approve', 'note': 'Synthetic human checked original full answer and same skill',
                    'client_action_id': 'http-review-' + row['candidate_id']}
            if stage:
                url = f"/v1/parent/children/{CHILD}/specialized-supply/candidates/{row['candidate_id']}/stage-review-context"
                context = client.get(url, headers=headers)
                assert context.status_code == 200, context.text
                body['stage_review'] = decision(context.json(), stage)
            url = f"/v1/parent/specialized-supply/candidates/{row['candidate_id']}/review"
            at = perf_counter()
            result = client.post(url, headers=headers, json=body)
            timings.append(('review-' + row['stage'], result.status_code, round((perf_counter() - at) * 1000, 2)))
            assert result.status_code == 200, result.text
            assert client.post(url, headers=headers, json=body).json() == result.json()
            approved[row['stage']] = result.json()
        sid = start(client)
        for label, stage in [('parallel', 'baseline'), ('discrimination', 'near_transfer'), ('transfer', 'far_transfer')]:
            item = approved[label]
            action = counting(db)['next_action']
            assert action['execution_status'] == 'ready'
            assert action['diagnostic_item_id'] == item['diagnostic_item_id']
            response = client.post(endpoint(sid, 'prepare'), json={'track': 'competition_math',
                'preferred_item_id': item['diagnostic_item_id'], 'client_action_id': 'http-prepare-' + stage})
            assert response.status_code == 200, response.text
            prompt = response.json()['prompt']
            request = {**binding(prompt), 'answer': '6', 'client_action_id': 'http-answer-' + stage}
            at = perf_counter()
            answer = client.post(endpoint(sid, 'answer'), json=request)
            timings.append(('answer-' + stage, answer.status_code, round((perf_counter() - at) * 1000, 2)))
            assert answer.status_code == 200, answer.text
            assert answer.json()['attempt']['correctness'] == 1
            assert answer.json()['attempt']['score_status'] == 'verified_local'
            assert counting(db)['qualified_stage'] == stage
            repeat = client.post(endpoint(sid, 'answer'), json=request)
            assert repeat.status_code == 200 and repeat.json()['attempt']['evidence_id'] == answer.json()['attempt']['evidence_id']
        before_restart = counting(db)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        assert counting(db) == before_restart
        assert counting(db)['current_stage'] == 'delayed_retention'
        assert counting(db)['next_action']['execution_status'] != 'ready'
        old = approved['parallel']
        with db.connect() as c:
            c.execute("UPDATE specialized_supply_candidate SET status='rejected' WHERE candidate_id=?", (old['candidate_id'],))
            c.commit()
        denied = client.post(endpoint(sid, 'prepare'), json={'track': 'competition_math',
            'preferred_item_id': old['diagnostic_item_id'], 'client_action_id': 'http-retracted-prepare'})
        assert denied.status_code == 200 and denied.json()['status'] == 'no_item', denied.text
        assert denied.json()['prompt'] is None and denied.json()['attempt'] is None
        assert counting(db)['qualified_stage'] is None
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt WHERE session_id=?', (sid,)).fetchone()[0] == 3
    assert len(gateway.requests) == 1
    print({'http_timings_ms': timings, 'source_retract_http': denied.status_code,
           'fixture_model_requests': len(gateway.requests), 'real_model_posts': 0})


def test_closed_http_stage_review_scope_and_stale_contract_fail_closed(environment):
    settings, db = environment
    with TestClient(create_app(settings, model_gateway=ClosedGateway())) as client:
        headers, candidates = generated_http(client, db)
        row = next(r for r in candidates if r['stage'] == 'discrimination')
        path = f"/v1/parent/children/{CHILD}/specialized-supply/candidates/{row['candidate_id']}/stage-review-context"
        assert client.get(path).status_code == 401
        assert client.get(path.replace(CHILD, 'child-junior-3'), headers=headers).status_code == 404
        context = client.get(path, headers=headers)
        assert context.status_code == 200, context.text
        bad = decision(context.json())
        bad['binding_version'] = 'old-version'
        url = f"/v1/parent/specialized-supply/candidates/{row['candidate_id']}/review"
        rejected = client.post(url, headers=headers, json={'decision': 'approve', 'note': 'Reviewed fixture',
            'client_action_id': 'stale-contract-review', 'stage_review': bad})
        assert rejected.status_code == 422 and rejected.json()['message'] == 'STAGE_REVIEW_SOURCE_CONFLICT', rejected.text
        assert SpecializedDiagnosticSupplyService(db).list(CHILD, 'needs_review')['items']
