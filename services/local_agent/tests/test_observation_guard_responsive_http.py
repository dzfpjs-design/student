"""Guarded correction and voice-cancel work must not stall the HTTP event loop."""
import asyncio
import threading

import httpx
import pytest

from test_app import client_for
from test_privacy_operation_recovery_http import auth


@pytest.mark.parametrize('route', ['correction', 'behavior_retract', 'asr_cancel', 'report_approve', 'period_retract', 'mistake_exclude'])
def test_locked_effect_wait_leaves_health_responsive(tmp_path, monkeypatch, route):
    with client_for(tmp_path) as client:
        app=client.app
        headers=auth(client, True)
        entered, release=threading.Event(),threading.Event()

        def blocked(*args, **kwargs):
            entered.set()
            if not release.wait(4): raise RuntimeError('test did not release guard')
            return {'status':'saved'}

        if route == 'correction':
            monkeypatch.setattr(app.state.observation_engine,'correct',blocked)
            monkeypatch.setattr(app.state.behavior_integration,'child_correction',lambda *args: {})
            path='/v1/sessions/synthetic/observation/correct'
            body={'correction_text':'没有走神','client_action_id':'synthetic-correction'}
        elif route == 'behavior_retract':
            monkeypatch.setattr(app.state.behavior_model,'retract_observation_state',blocked)
            path='/v1/children/child-primary-4/behavior/states/synthetic/retract'
            body={'note':'记录有误','client_action_id':'synthetic-retract'}
        elif route == 'asr_cancel':
            monkeypatch.setattr(app.state.voice_captures,'cancel',lambda *args: None)
            monkeypatch.setattr(app.state.media_ai,'cancel_transcription',lambda *args: {'status':'cancelled'})
            monkeypatch.setattr(app.state.repository,'get_workspace',lambda *args: {'session':{'child_id':'child-primary-4'}})
            monkeypatch.setattr(app.state.behavior_model,'retract_event',blocked)
            path='/v1/sessions/synthetic/asr/actions/synthetic-voice/cancel'
            body={}
        elif route == 'report_approve':
            monkeypatch.setattr(app.state.parent_reports,'approve',blocked)
            path='/v1/parent/report-snapshots/synthetic/approve'
            body={}
        elif route == 'period_retract':
            monkeypatch.setattr(app.state.period_reports,'retract_evidence',blocked)
            monkeypatch.setattr(app.state.period_reports,'refresh_current',lambda *args, **kwargs: {'reports':[], 'failures':[]})
            path='/v1/parent/children/child-primary-4/period-evidence/synthetic/retract'
            body={'note':'记录有误','client_action_id':'synthetic-retract'}
        else:
            monkeypatch.setattr(app.state.mistake_book,'confirm',blocked)
            path='/v1/mistakes/synthetic/confirm'
            body={'child_id':'child-primary-4','decision':'exclude','client_action_id':'synthetic-exclude'}

        async def exercise():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://testserver',headers=headers) as http:
                task=asyncio.create_task(http.post(path,json=body))
                try:
                    assert await asyncio.wait_for(asyncio.to_thread(entered.wait,2),3)
                    health=await asyncio.wait_for(http.get('/v1/health'),1)
                    assert health.status_code==200 and not task.done()
                finally:
                    release.set()
                assert (await asyncio.wait_for(task,3)).status_code==200
        client.portal.call(exercise)
