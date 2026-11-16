from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
import zipfile

from normal_provider_owner import normal_provider_owner
from test_mistake_cloud_learning import world, post
from home_study_agent.daily_diagnostic_bundle import build_daily_diagnostic_bundle
from home_study_agent.mistake_learning import _diagnostic
from home_study_agent.model_gateway import ModelResult


def test_actual_cloud_steps_and_failed_feedback_survive_daily_zip_without_learning_material(world, capsys, tmp_path):
    assert post(world, 'analyze', 'public-diagnostic-analysis').json()['action_status'] == 'completed'
    assert post(world, 'practice', 'public-diagnostic-practice', count=2).json()['action_status'] == 'completed'
    world[6]['response'] = {'bad_shape': 'PRIVATE_MODEL_CONTENT'}
    assert post(world, 'coach', 'public-diagnostic-invalid', utterance='PRIVATE_CHILD_WORDS').json()['action_status'] == 'failed'
    entries = [json.loads(line.split('HSC_DIAGNOSTIC ', 1)[1]) for line in capsys.readouterr().out.splitlines() if line.startswith('HSC_DIAGNOSTIC ')]
    requests = [e['details']['request_type'] for e in entries if e['event'] == 'mistake.learning.model_result']
    assert requests == ['mistake_learning_analyze', 'mistake_learning_practice', 'mistake_learning_verify',
                        'mistake_learning_verify', 'mistake_learning_coach', 'mistake_learning_coach']
    assert entries[-1]['details']['error_code'] == 'MISTAKE_LEARNING_MODEL_OUTPUT_INVALID'
    assert entries[0]['details']['model_provider'] == 'qwen'
    assert entries[0]['details']['input_tokens'] == 40
    now = datetime.now(timezone.utc)
    rows = [{'privacy_policy_version':'diagnostic_metadata_v2', 'timestamp':now.isoformat(), 'source':'local_service', **e} for e in entries]
    rows[0]['details']['question'] = 'PRIVATE_INJECTED_QUESTION'
    folder = tmp_path / 'diagnostics' / 'logs';folder.mkdir(parents=True)
    (folder/'runtime.jsonl').write_text('\n'.join(json.dumps(r) for r in rows)+'\n')
    bundle = build_daily_diagnostic_bundle(tmp_path, start_utc=now-timedelta(minutes=1), end_utc=now+timedelta(minutes=1))
    with zipfile.ZipFile(BytesIO(bundle.content)) as z:
        raw = z.read('events.jsonl');saved=[json.loads(line) for line in raw.splitlines()]
    assert len(saved) == len(entries)
    assert saved[-1]['details']['error_code'] == 'MISTAKE_LEARNING_MODEL_OUTPUT_INVALID'
    assert saved[0]['details']['latency_ms'] >= 0
    assert b'PRIVATE_' not in raw


def test_emitter_itself_omits_unregistered_model_and_error_strings(capsys):
    result = ModelResult(content='PRIVATE_CHILD_ANSWER',provider='PRIVATE_PROVIDER',model='PRIVATE_MODEL',latency_ms=1,prompt_tokens=3,completion_tokens=4)
    _diagnostic('mistake.learning.model_result','mistake_learning_answer','completed',result=result,error_code='PRIVATE_ERROR')
    raw=capsys.readouterr().out
    assert 'PRIVATE_' not in raw
    data=json.loads(raw.split('HSC_DIAGNOSTIC ',1)[1])
    assert data['details']=={'request_type':'mistake_learning_answer','mistake_status':'completed','latency_ms':1,'input_tokens':3,'output_tokens':4}


def test_provider_failure_retains_only_closed_code_or_numeric_http_status(capsys):
    _diagnostic('mistake.learning.action_result','mistake_learning_practice','unknown',error_code='MISTAKE_LEARNING_MODEL_FAILED',provider_error_code='MODEL_HTTP_503')
    _diagnostic('mistake.learning.action_result','mistake_learning_practice','unknown',error_code='MISTAKE_LEARNING_MODEL_FAILED',provider_error_code='MODEL_OUTPUT_TRUNCATED')
    _diagnostic('mistake.learning.action_result','mistake_learning_practice','unknown',error_code='MISTAKE_LEARNING_MODEL_FAILED',provider_error_code='PRIVATE_PROVIDER_MESSAGE')
    raw=capsys.readouterr().out
    rows=[json.loads(line.split('HSC_DIAGNOSTIC ',1)[1])['details'] for line in raw.splitlines()]
    assert rows[0]['statusCode']==503
    assert rows[1]['error']=={'code':'MODEL_OUTPUT_TRUNCATED'}
    assert 'PRIVATE_' not in raw
    assert 'statusCode' not in rows[2] and 'error' not in rows[2]
