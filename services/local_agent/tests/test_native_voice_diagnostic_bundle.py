"""Public metadata through the actual shipping ZIP collector."""
from collections import Counter
from datetime import datetime,timedelta,timezone
from io import BytesIO
import json,zipfile
from home_study_agent import daily_diagnostic_bundle as diagnostic

def test_native_echo_and_response_metadata_survive_daily_zip_without_private_content(tmp_path):
    start=datetime(2026,11,2,tzinfo=timezone.utc)
    examples=[
        ('voice.realtime.echo_cancellation_policy',{'echo_mode':'browser-selected','echo_negotiation':'rejected','acoustic_quality_verified':False}),
        ('voice.realtime.native_response',{'response_status':'cancelled','response_reason':'client_cancelled','response_elapsed_ms':3000,'response_text_chars':1,'response_text_kind':'punctuation','audio_chunk_count':0,'pcm_chunk_count':0,'pcm_scheduled':False,'new_input_before_pcm':True,'terminal_current':False,'has_tool_call':False,'response_error_present':False,'physical_audibility_verified':False}),
    ]
    rows=[{'privacy_policy_version':'diagnostic_metadata_v2','timestamp':(start+timedelta(hours=2)).isoformat(),'source':'renderer','level':'warn','event':name,'details':{**details,'transcript':'PRIVATE_SENTINEL','response_id':'PRIVATE_SENTINEL','error':{'message':'PRIVATE_SENTINEL'}}}for name,details in examples]
    logs=tmp_path/'diagnostics/logs';logs.mkdir(parents=True)
    (logs/'runtime.jsonl').write_text(''.join(json.dumps(row)+'\n'for row in rows))
    bundle=diagnostic.build_daily_diagnostic_bundle(tmp_path,start_utc=start,end_utc=start+timedelta(days=1))
    with zipfile.ZipFile(BytesIO(bundle.content))as archive:
        assert archive.testzip()is None
        raw=archive.read('events.jsonl').decode();events=[json.loads(line)for line in raw.splitlines()]
    assert [row['event']for row in events]==[name for name,_ in examples]
    assert 'PRIVATE_SENTINEL'not in raw
    for row,(_,details)in zip(events,examples):
        assert row['details']=={**details,'error':{}}

def test_native_voice_metadata_rejects_untrusted_values_and_unverified_acoustics():
    assert diagnostic._details({'echo_mode':'PRIVATE_SENTINEL','echo_negotiation':'PRIVATE_SENTINEL','response_status':'PRIVATE_SENTINEL','response_reason':'PRIVATE_SENTINEL','response_text_kind':'PRIVATE_SENTINEL','acoustic_quality_verified':True,'physical_audibility_verified':True,'response_id':'PRIVATE_SENTINEL'},Counter())=={}
