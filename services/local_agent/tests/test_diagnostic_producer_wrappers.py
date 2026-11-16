"""Public fixed producer identities through the actual shipping daily ZIP builder."""
from collections import Counter
from datetime import datetime,timedelta,timezone
from io import BytesIO
import json,zipfile
import pytest
from home_study_agent import daily_diagnostic_bundle as diagnostic
EXAMPLES = [{'event': 'structured_event', 'source': 'local_service', 'details': {}}, {'event': 'document.capture.frame_deferred', 'source': 'renderer', 'details': {'reason': 'transient_occlusion'}}, {'event': 'voice.capture.protected_tail_failed', 'source': 'renderer', 'details': {'reason_code': 'quiet_not_verified'}}, {'event': 'voice.capture.unknown_overlap_held', 'source': 'renderer', 'details': {'reason_code': 'source_or_close_unknown'}}, {'event': 'voice.capture.echo_cancellation_policy', 'source': 'renderer', 'details': {'echo_mode': 'all', 'echo_negotiation': 'accepted', 'acoustic_quality_verified': False}}, {'event': 'voice.capture.original_prefix_admitted', 'source': 'renderer', 'details': {}}, {'event': 'voice.capture.original_prefix_held', 'source': 'renderer', 'details': {}}, {'event': 'voice.capture.original_prefix_discarded', 'source': 'renderer', 'details': {}}, {'event': 'privacy_database_delete_failed', 'source': 'local_service', 'details': {}}, {'event': 'privacy_analysis_candidate_residual', 'source': 'local_service', 'details': {}}, {'event': 'privacy_delete_dependency_blocked', 'source': 'local_service', 'details': {}}, {'event': 'learning_adaptation_pending', 'source': 'local_service', 'details': {}}, {'event': 'document_learning_worker_error', 'source': 'local_service', 'details': {}}, {'event': 'speaking_candidate_worker_error', 'source': 'local_service', 'details': {}}, {'event': 'pet_audio_worker_error', 'source': 'local_service', 'details': {}}]
@pytest.mark.parametrize('example',EXAMPLES,ids=lambda x:x['event'])
def test_fixed_wrapper_and_structured_stdout_producer_survives_daily_zip(tmp_path,example):
 start=datetime(2026,11,2,tzinfo=timezone.utc);row={'privacy_policy_version':'diagnostic_metadata_v2','timestamp':(start+timedelta(hours=1)).isoformat(),'source':example['source'],'level':'warn','event':example['event'],'correlation_id':'11111111-1111-4111-8111-111111111111','details':{**example['details'],'transcript':'PRIVATE_SENTINEL','session_id':'PRIVATE_SENTINEL','input_id':'PRIVATE_SENTINEL','samples':12345}}
 logs=tmp_path/'diagnostics/logs';logs.mkdir(parents=True);(logs/'runtime.jsonl').write_text(json.dumps(row)+'\n')
 bundle=diagnostic.build_daily_diagnostic_bundle(tmp_path,start_utc=start,end_utc=start+timedelta(days=1))
 with zipfile.ZipFile(BytesIO(bundle.content))as archive:
  assert archive.testzip()is None
  raw=archive.read('events.jsonl').decode();events=[json.loads(x)for x in raw.splitlines()]
 assert len(events)==1 and events[0]['event']==example['event']and events[0]['source']==example['source']
 assert events[0]['details']==example['details']and events[0]['correlation_id']=='r000001'
 assert 'PRIVATE_SENTINEL'not in raw and '11111111-1111-4111-8111-111111111111'not in raw

def test_echo_and_reason_schema_stays_finite_and_false_only():
 assert diagnostic._details({'echo_mode':'browser-selected','echo_negotiation':'timed_out','acoustic_quality_verified':False},Counter())=={'echo_mode':'browser-selected','echo_negotiation':'timed_out','acoustic_quality_verified':False}
 assert diagnostic._details({'mode':'all','negotiation':'accepted','echo_mode':'PRIVATE_SENTINEL','echo_negotiation':'PRIVATE_SENTINEL','acoustic_quality_verified':True,'reason':'transient_occlusion_PRIVATE_SENTINEL','reason_code':'quiet_not_verified_PRIVATE_SENTINEL','samples':12345},Counter())=={}
