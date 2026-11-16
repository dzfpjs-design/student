"""Real dedicated Tutor dispatch and verifier retries revalidate authorization."""
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.model_gateway import ModelGateway
from test_teaching_context import flow
from test_teaching_question_target import target_case, request as tutor_request


@pytest.mark.parametrize('change',['none','privacy','privacy_cycle','question','verifier_privacy','before_persist','verifier_stale_poll','provider_error','verifier_retry_revoked'])
def test_actual_tutor_primary_retry_and_verifier_keep_exact_current_authority(target_case,tmp_path,change,monkeypatch):
    db,_,_,_,sid = target_case[0]
    settings = Settings(data_dir=tmp_path,database_path=db.path,maintenance_enabled=False,
        model_provider='deepseek',model_api_key='synthetic-primary',model_base_url='https://primary.invalid',
        tutor_explanation_api_key='synthetic-tutor-key-123',tutor_explanation_base_url='https://tutor.example.test/v1',
        tutor_explanation_model='deepseek-test',
        fallback_model_provider='qwen',fallback_model_api_key='synthetic-verifier',
        fallback_model_base_url='https://verifier.invalid',fallback_model_name='test-verifier')
    posts,mutations = [],[]
    application = None
    headers = None
    async def transport(request):
        posts.append((request.url.host,json.loads(request.content)))
        if change == 'provider_error':
            return httpx.Response(503)
        if change=='verifier_stale_poll' and request.url.host=='verifier.invalid':
            from home_study_agent.tutor_request_reservation import TutorRequestReservationService, TutorReservationUnknownOutcomeError
            with db.connect() as c:
                c.execute("UPDATE tutor_request_reservation SET updated_at=? WHERE action_id=?",
                    ((datetime.now(timezone.utc)-timedelta(minutes=3)).isoformat(),'tutor-dispatch-'+change))
                identity=c.execute("SELECT request_hash FROM tutor_request_reservation WHERE action_id=?",('tutor-dispatch-'+change,)).fetchone()[0]
                c.commit()
            with pytest.raises(TutorReservationUnknownOutcomeError):
                TutorRequestReservationService(db).lookup_completed_or_pending('tutor-dispatch-'+change,identity)
        if ((change in {'privacy','privacy_cycle','question'} and len(posts)==1)
                or (change in {'verifier_privacy','verifier_retry_revoked'} and request.url.host=='verifier.invalid')):
            if change in {'privacy','privacy_cycle','verifier_privacy','verifier_retry_revoked'}:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application),base_url='http://testserver') as parent:
                    changed = await parent.put('/v1/parent/privacy',headers=headers,json={'cloud_model_text_allowed':False})
                    assert changed.status_code==200,changed.text
                    mutations.append(changed.status_code)
                    if change=='privacy_cycle':
                        enabled = await parent.put('/v1/parent/privacy',headers=headers,json={'cloud_model_text_allowed':True})
                        assert enabled.status_code==200,enabled.text
            elif change=='question':
                with db.connect() as c:
                    c.execute("UPDATE diagnostic_item SET source_ref='local-restricted-source' WHERE diagnostic_item_id=?",(target_case[2]['item']['diagnostic_item_id'],))
                    c.commit()
                mutations.append('source_changed')
            if change=='verifier_retry_revoked':
                return httpx.Response(503)
        return httpx.Response(200,json={'choices':[{'message':{'content':
            'VERIFIED' if request.url.host=='verifier.invalid' else '先观察数量之间的关系，再列出等式。你能说明第一步吗？'}}],
            'usage':{'prompt_tokens':12,'completion_tokens':20}})
    gateway = ModelGateway(settings,transport=httpx.MockTransport(transport))
    application = create_app(settings,model_gateway=gateway)
    if change=='before_persist':
        from home_study_agent.repository import StudyRepository
        save = StudyRepository.save_tutor_turn
        def change_before_save(repository,*args,**kwargs):
            with db.connect() as c:
                c.execute("UPDATE diagnostic_item SET source_ref='local-restricted-source' WHERE diagnostic_item_id=?",(target_case[2]['item']['diagnostic_item_id'],))
                c.commit()
            mutations.append('source_changed_before_persist')
            return save(repository,*args,**kwargs)
        monkeypatch.setattr(StudyRepository,'save_tutor_turn',change_before_save)
    with TestClient(application) as client:
        token = client.post('/v1/parent/setup',json={'pin':'8264'})
        assert token.status_code==200,token.text
        headers={'X-HSC-Parent':token.json()['parent_token']}
        consent=client.put('/v1/parent/privacy',headers=headers,json={'cloud_model_text_allowed':True})
        assert consent.status_code==200,consent.text
        response = client.post(f'/v1/sessions/{sid}/tutor',json={**tutor_request(target_case),
            'hint_level':6 if change in {'verifier_privacy','verifier_stale_poll','verifier_retry_revoked'} else 1,'client_action_id':'tutor-dispatch-'+change})
        if change in {'none','verifier_stale_poll'}:
            assert response.status_code==200,response.text
            assert len(posts)==(2 if change=='verifier_stale_poll' else 1)
        elif change=='provider_error':
            assert response.status_code==409,response.text
            assert response.json()['code']=='TUTOR_RESULT_UNCONFIRMED',response.text
            assert len(posts)==1  # Dedicated Tutor has no automatic retransmission or provider fallback.
        else:
            assert response.status_code==409,response.text
            assert response.json()['code']=='TUTOR_SOURCE_AUTHORIZATION_CHANGED',response.text
            assert mutations
            assert len(posts)==(2 if change in {'verifier_privacy','verifier_retry_revoked'} else 1)
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM tutor_turn WHERE session_id=?',(sid,)).fetchone()[0]==(1 if change in {'none','verifier_stale_poll'} else 0)
        assert posts[0][0]=='tutor.example.test'
        if change in {'verifier_privacy','verifier_stale_poll','verifier_retry_revoked'}:
            assert posts[1][0]=='verifier.invalid'
        for _,payload in posts:
            assert 'pre_dispatch' not in payload and 'context_manifest' not in payload
            wire=json.dumps(payload,ensure_ascii=False)
            assert target_case[2]['prompt_id'] not in wire
            assert target_case[2]['item_version'] not in wire
