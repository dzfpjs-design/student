from datetime import datetime,timedelta,timezone

from test_pet_audio_work import audio_case,loopback,media,CHILD


def test_browser_iso_z_grant_is_saved_canonically_and_same_key_is_idempotent(audio_case):
    service,_,_,grant,*_=audio_case
    browser_expiry=(datetime.now(timezone.utc)+timedelta(hours=24)).isoformat(timespec='milliseconds').replace('+00:00','Z')
    request={**grant,'expires_at':browser_expiry,'expected_revision':1,'action_id':'browser-date-toISOString'}
    saved=service.grant(CHILD,**request)
    assert saved['expires_at'].endswith('+00:00')
    assert service.grant(CHILD,**request)==saved
    with service.database.connect() as c:
        assert service._grant(c,CHILD)['grant_id']==saved['grant_id']
        assert c.execute('SELECT count(*) FROM pet_audio_grant WHERE action_id=?',(request['action_id'],)).fetchone()[0]==1
