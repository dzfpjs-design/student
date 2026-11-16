from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.config import Settings

def test_parent_authority_endpoint_requires_local_and_parent_tokens(tmp_path):
    cfg=Settings(data_dir=tmp_path,database_path=tmp_path/'test.db',session_token='synthetic-local-session',maintenance_enabled=False)
    with TestClient(create_app(cfg),client=('127.0.0.1',50000)) as c:
        path='/v1/parent/tutor-explanation/authorization';local={'X-HSC-Session':'synthetic-local-session'}
        assert c.get(path).status_code==401
        assert c.get(path,headers=local).status_code==401
        setup=c.post('/v1/parent/setup',headers=local,json={'pin':'2468'});assert setup.status_code==200
        parent=setup.json()['parent_token']
        assert c.get(path,headers={'X-HSC-Parent':parent}).status_code==401
        response=c.get(path,headers={**local,'X-HSC-Parent':parent})
        assert response.status_code==200 and response.json()=={'authorized':True}

def test_missing_tutor_route_returns_actionable_nonretryable_error(tmp_path):
    from home_study_agent.model_gateway import ModelResponseError
    cfg=Settings(data_dir=tmp_path,database_path=tmp_path/'test.db',session_token='synthetic-local-session',maintenance_enabled=False)
    app=create_app(cfg)
    @app.get('/v1/test-tutor-config-error')
    async def fail(): raise ModelResponseError('MODEL_TUTOR_EXPLANATION_CONFIG_REQUIRED')
    with TestClient(app,client=('127.0.0.1',50000)) as c:
        response=c.get('/v1/test-tutor-config-error',headers={'X-HSC-Session':'synthetic-local-session'})
        assert response.status_code==503
        assert response.json()['retryable'] is False and response.json()['recovery_action']=='open_parent_model_settings'
        assert '服务地址' in response.json()['message']


def test_tutor_endpoint_accepts_safe_origin_and_preserves_strict_path_policy():
    from home_study_agent.config import validate_tutor_explanation_url
    assert validate_tutor_explanation_url('https://tutor.example.test:7791') == 'https://tutor.example.test:7791/v1'
    assert validate_tutor_explanation_url('https://tutor.example.test/') == 'https://tutor.example.test/v1'
    for unsafe in ('http://tutor.example.test', 'https://u:p@tutor.example.test', 'https://tutor.example.test/other'):
        assert validate_tutor_explanation_url(unsafe) is None


def test_diagnosis_schedules_variant_job_with_only_dedicated_provider(tmp_path, monkeypatch):
    import threading
    cfg = Settings(data_dir=tmp_path, database_path=tmp_path/'test.db', session_token='synthetic-local-session', maintenance_enabled=False,
                   model_api_key=None, tutor_explanation_api_key='synthetic-key-value-123',
                   tutor_explanation_base_url='https://tutor.example.test/v1')
    app = create_app(cfg)
    processed = threading.Event()
    monkeypatch.setattr(app.state.mistake_book, 'diagnose', lambda *a, **kw: {})
    monkeypatch.setattr(app.state.variant_supply, 'enqueue', lambda *a, **kw: {'status': 'pending'})
    monkeypatch.setattr(app.state.variant_supply, 'enqueue_local', lambda *a, **kw: {'status': 'pending'})
    async def process(): processed.set()
    monkeypatch.setattr(app.state.variant_supply, 'process_next', process)
    with TestClient(app, client=('127.0.0.1', 50000)) as c:
        response = c.post('/v1/mistakes/synthetic/diagnose', headers={'X-HSC-Session':'synthetic-local-session'}, json={
            'child_id':'child-junior-3','error_type':'concept_gap','explanation':'synthetic explanation','confidence':0.9,'client_action_id':'synthetic-diagnose-action'})
        assert response.status_code == 200, response.text
        assert processed.wait(timeout=2)
