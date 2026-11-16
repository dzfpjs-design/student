"""Closed startup/recovery metadata through the real shipping daily ZIP builder."""
from collections import Counter
from datetime import datetime,timedelta,timezone
from io import BytesIO
import json,zipfile
import pytest
from home_study_agent import daily_diagnostic_bundle as diagnostic

EXAMPLES = [('runtime_awake_unconfirmed', {'error_code': 'RUNTIME_AWAKE_UNCONFIRMED'}), ('protected_startup_failed', {}), ('voice.realtime.pending_input_recovery', {}), ('voice.realtime.cancellation_journal_unconfirmed', {}), ('voice.agent.identity_index_restored', {'restored': 2, 'has_more': False, 'learning_tasks_replayed': 0, 'contains_transcript': False}), ('voice.agent.identity_index_unconfirmed', {}), ('voice.realtime.identity_index_restored', {'restored': 2, 'has_more': False, 'learning_tasks_replayed': 0, 'contains_transcript': False}), ('voice.realtime.identity_index_unconfirmed', {}), ('voice.realtime.cancellation_recovered', {'confirmed': 3, 'scope': 'canonical_voice_only', 'learning_tasks_replayed': 0}), ('voice.realtime.cancellation_recovery_unconfirmed', {}), ('voice.realtime.cancellation_recovery_invalid', {}), ('voice.recovery.receipt_check_incomplete', {'reason': 'deadline'}), ('voice.realtime.resume_receipt_retained', {}), ('voice.original_prompt.admission', {'allowed': True, 'capture_ready': False}), ('voice.capture.protected_tail_window_failed', {'window': 'recovery', 'recovery_windows': 2}), ('app_quit_backend_abnormal_acknowledged', {'error_code': 'LOCAL_SERVICE_SHUTDOWN_ABNORMAL', 'exitCode': 1}), ('restart_after_abnormal_shutdown', {'error_code': 'LOCAL_SERVICE_SHUTDOWN_ABNORMAL'})]

@pytest.mark.parametrize('name,expected',EXAMPLES)
def test_reviewed_recovery_event_and_metadata_survive_daily_zip(tmp_path,name,expected):
    start=datetime(2026,11,2,tzinfo=timezone.utc)
    row={'privacy_policy_version':'diagnostic_metadata_v2','timestamp':(start+timedelta(hours=2)).isoformat(),'source':'renderer','level':'warn','event':name,'correlation_id':'11111111-1111-4111-8111-111111111111','details':{**expected,'transcript':'PRIVATE_SENTINEL','session_id':'PRIVATE_SENTINEL','error':{'message':'PRIVATE_SENTINEL'}}}
    logs=tmp_path/'diagnostics/logs';logs.mkdir(parents=True)
    (logs/'runtime.jsonl').write_text(json.dumps(row)+'\n')
    bundle=diagnostic.build_daily_diagnostic_bundle(tmp_path,start_utc=start,end_utc=start+timedelta(days=1))
    with zipfile.ZipFile(BytesIO(bundle.content))as archive:
        assert archive.testzip()is None
        raw=archive.read('events.jsonl').decode();events=[json.loads(line)for line in raw.splitlines()]
    assert len(events)==1 and events[0]['event']==name
    assert events[0]['details']=={**expected,'error':{}}
    assert events[0]['correlation_id']=='r000001'
    assert 'PRIVATE_SENTINEL'not in raw and '11111111-1111-4111-8111-111111111111'not in raw

def test_recovery_metadata_bounds_and_privacy_assertions_remain_closed():
    invalid={'restored':1.5,'confirmed':-1,'has_more':'PRIVATE_SENTINEL','learning_tasks_replayed':1,'contains_transcript':True,'recovery_windows':3,'window':'PRIVATE_SENTINEL','scope':'PRIVATE_SENTINEL','reason':'PRIVATE_SENTINEL','allowed':'PRIVATE_SENTINEL','capture_ready':'PRIVATE_SENTINEL','input_id':'PRIVATE_SENTINEL'}
    assert diagnostic._details(invalid,Counter())=={}
    for value in (-1,1.5,1000001,'PRIVATE_SENTINEL',True):assert diagnostic._details({'restored':value,'confirmed':value},Counter())=={}
    for value in (1,False,'0'):assert diagnostic._details({'learning_tasks_replayed':value},Counter())=={}
    assert diagnostic._details({'contains_transcript':False,'learning_tasks_replayed':0,'restored':0,'confirmed':1000000,'recovery_windows':2},Counter())=={'contains_transcript':False,'learning_tasks_replayed':0,'restored':0,'confirmed':1000000,'recovery_windows':2}
