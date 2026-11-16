"""A confirmed written source must not be withdrawn during report submission."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Event
import pytest
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.writing_ocr_authorization import WritingOcrAuthorizationService, _confirmation
from test_writing_ocr_warning_voice import warning_read
from test_writing_voice_runtime_http import ControlGateway, environment


def test_ocr_retraction_waits_for_external_submission(environment, monkeypatch):
    settings, db = environment
    gateway = ControlGateway('')
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, _, turn, _ = warning_read(client, db, settings, monkeypatch, gateway)
        binding = turn['action']['writing_ocr_confirmation']
        started = Event()
        def retract():
            started.set()
            return WritingOcrAuthorizationService(db).retract(sid, **binding, client_action_id='guard-ocr-retract')
        with ThreadPoolExecutor(max_workers=1) as pool:
            with db.external_effect_guard():
                future = pool.submit(retract)
                assert started.wait(2)
                with pytest.raises(TimeoutError):
                    future.result(timeout=.3)
                with db.connect() as c:
                    _confirmation(c, sid, binding['confirmation_id'], binding['binding_version'])
            future.result(timeout=5)
        with db.connect() as c, pytest.raises(ValueError, match='OCR_AUTHORIZATION_WITHDRAWN'):
            _confirmation(c, sid, binding['confirmation_id'], binding['binding_version'])
