import asyncio
import httpx
import pytest
from home_study_agent.pet_audio_gateway import PetAudioGateway
from home_study_agent.pet_audio_source import PetAudioAuthorityError
from home_study_agent.pet_audio_work import PetAudioTeachingService
from test_pet_audio_work import audio_case, loopback, media, CHILD
from test_pet_audio_catalog import endpoint, catalog_clock


def test_existing_legacy_grant_cannot_authorize_new_managed_configuration_and_low_budget_never_posts(audio_case):
    old, assessment, _, grant, *_ = audio_case
    requests = []
    def forbidden(request):
        requests.append(request)
        raise AssertionError('No provider POST allowed for an insufficient budget')
    service = PetAudioTeachingService(old.database, media_root=old.media_root, data_encryption_key=old.data_encryption_key,
        gateway=PetAudioGateway(endpoint(), transport=httpx.MockTransport(forbidden)))
    with service.database.connect() as connection:
        with pytest.raises(PetAudioAuthorityError): service._grant(connection, CHILD)
    service.grant(CHILD, **{**grant, 'expected_revision': 1, 'action_id': 'managed-small-budget',
        'day_limit_micros': 1, 'request_limit_micros': 1})
    work = service.enqueue(CHILD, assessment['assessment_id'])
    with pytest.raises(PetAudioAuthorityError): asyncio.run(service.run(CHILD, work['work_id']))
    assert requests == []
    with service.database.connect() as connection:
        assert connection.execute('SELECT count(*) FROM pet_audio_dispatch').fetchone()[0] == 0


def test_new_snapshot_fences_old_grant_without_erasing_saved_feedback_or_spend_after_database_reopen(audio_case, loopback):
    from home_study_agent.database import Database
    old, assessment, *_ = audio_case
    work = old.enqueue(CHILD, assessment['assessment_id'])
    saved = asyncio.run(old.run(CHILD, work['work_id']))
    assert saved['state'] == 'response_saved'
    with old.database.connect() as c:
        before = tuple(c.execute('SELECT state,result_json,generation FROM pet_audio_work WHERE work_id=?', (work['work_id'],)).fetchone())
        spend = [tuple(r) for r in c.execute('SELECT cost_micros,state,generation FROM pet_audio_dispatch ORDER BY rowid')]
        grants = [tuple(r) for r in c.execute('SELECT grant_id,revision,endpoint_hash FROM pet_audio_grant ORDER BY revision')]
    requests = []
    def forbidden(request):
        requests.append(request)
        raise AssertionError('An old purpose grant must not send audio to the new snapshot')
    reopened = Database(old.database.path, media_root=old.media_root, data_encryption_key=old.data_encryption_key)
    current = PetAudioTeachingService(reopened, media_root=old.media_root, data_encryption_key=old.data_encryption_key,
        gateway=PetAudioGateway(endpoint(), transport=httpx.MockTransport(forbidden)))
    with reopened.connect() as c:
        with pytest.raises(PetAudioAuthorityError): current._grant(c, CHILD)
    projected = current.read(CHILD, work['work_id'])
    assert projected['state'] == 'invalidated' and projected['result'] is None
    assert projected['mastery_claim_allowed'] is False
    with reopened.connect() as c:
        assert tuple(c.execute('SELECT state,result_json,generation FROM pet_audio_work WHERE work_id=?', (work['work_id'],)).fetchone()) == before
        assert [tuple(r) for r in c.execute('SELECT cost_micros,state,generation FROM pet_audio_dispatch ORDER BY rowid')] == spend
        assert [tuple(r) for r in c.execute('SELECT grant_id,revision,endpoint_hash FROM pet_audio_grant ORDER BY revision')] == grants
        assert c.execute('SELECT count(*) FROM rubric_review_event').fetchone()[0] == 0
    assert len(loopback['posts']) == 1 and requests == []
