"""Actual isolated HTTP host with explicit synthetic recognition/model boundaries."""
import argparse
import io
import json
import os
from pathlib import Path
import sys
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/local_agent/src'))
parser = argparse.ArgumentParser()
parser.add_argument('--data-dir', required=True)
parser.add_argument('--port', required=True, type=int)
args = parser.parse_args()
data = Path(args.data_dir).resolve()
if not any(p.name.startswith('hsc-pet-task-native-') for p in data.parents):
    raise ValueError('Only owned synthetic fixtures')
os.environ['HSC_DATA_DIR'] = str(data)
os.environ['HSC_DATABASE_PATH'] = str(data / 'pet-task.db')
os.environ['HSC_MAINTENANCE_ENABLED'] = '0'

import httpx
import uvicorn
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.local_media_ai import LocalMediaAI
from home_study_agent.model_gateway import ModelGateway
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService

ALLOWED = {'能和我一起编个小故事吗？', '我想让一位小探险家当主角。', '我想让主角先去河边。', '这个活动先不做了', '继续原任务',
           '刚才的评价现在保存了吗？', '我现在有危险，请帮我找身边的大人。'}

def recognize(self, wav_bytes, *unused, **kwargs):
    command = json.loads((data.parent / 'synthetic-recognition.json').read_text())
    text = command['transcript']
    if command.get('local_recovery_only') and kwargs.get('prefer_cloud') is not False:
        raise ValueError('Recovery recognition must explicitly disable cloud')
    if text not in ALLOWED:
        raise ValueError('Unapproved synthetic utterance')
    with wave.open(io.BytesIO(wav_bytes), 'rb') as audio:
        if audio.getnchannels() != 1 or audio.getsampwidth() != 2:
            raise ValueError('Actual renderer must send PCM WAV')
        duration = int(audio.getnframes() / audio.getframerate() * 1000)
    return text, duration, 'isolated-synthetic-recognizer-not-real-asr', False

def transport(request):
    body = json.loads(request.content)
    system = body['messages'][0]['content']
    if '只分类当前原话' in system:
        current = json.loads(body['messages'][-1]['content'])
        if set(current) != {'transcript', 'state'} or not all(type(v) is bool for v in current['state'].values()):
            raise ValueError('Unexpected semantic DTO')
        quote = current['transcript']
        if quote not in ALLOWED:
            raise ValueError('Unexpected model request')
        result = {'role': 'control', 'quote': quote, 'confidence': .99}
        if quote == '能和我一起编个小故事吗？':
            result.update(control='companion.start_activity', activity_type='interactive_story')
        elif quote in {'我想让一位小探险家当主角。', '我想让主角先去河边。'}:
            result.update(role='activity_reply', control=None)
        elif quote == '这个活动先不做了':
            result.update(control='companion.activity_feedback', activity_feedback={'operation': 'decline'})
        elif quote == '刚才的评价现在保存了吗？':
            result.update(control='evaluation.status')
        else:
            result.update(control='task.resume', selector={'subject': None, 'task_kind': None, 'relative_order': None})
        content = json.dumps(result, ensure_ascii=False)
    elif '开始任务' in body['messages'][-1]['content'] and '规划' in system:
        content = json.dumps({
            'objective': {'mode': 'new', 'type': 'execution', 'summary': '开始当前计划任务',
                          'success_criteria': ['收到真实执行结果'], 'phase': 'execute'},
            'next_step': {'capability': 'task.start', 'arguments': {}, 'reason': '孩子明确请求开始任务', 'expected_evidence': []},
            'spoken_transition': '我来开始当前任务。', 'requires_child_reply': False, 'confidence': .99,
            'utterance_binding': {'role': 'task_request', 'quote': '开始任务'},
        }, ensure_ascii=False)
    elif '故事' in json.dumps(body['messages'], ensure_ascii=False):
        content = '这位小探险家走到一条分岔路前。你想让主角先去树林还是河边？'
    else:
        raise ValueError('Unexpected synthetic model purpose')
    with (data.parent / 'synthetic-model-boundary.jsonl').open('a') as log:
        log.write(json.dumps({'synthetic': True, 'classification': '只分类当前原话' in system, 'network_sent': False,
                              'system_prefix': system[:240], 'synthetic_response': content}, ensure_ascii=False) + '\n')
    return httpx.Response(200, json={'choices': [{'message': {'content': content}}]})

# Only synthetic consent and a transport that cannot reach a network provider.
db = Database(data / 'pet-task.db')
privacy = PrivacyLifecycleService(db, data)
privacy.initialize()
privacy.update_preferences({'cloud_model_text_allowed': True, 'cloud_audio_allowed': False,
                            'raw_media_storage_allowed': False})
LocalMediaAI._recognize_wav = recognize
settings = Settings(data_dir=data, database_path=db.path, session_token=os.environ['HSC_SESSION_TOKEN'],
                    model_api_key='synthetic-ea083dde01ce574037d7', model_name='isolated-control',
                    voice_intent_model_name='isolated-control', maintenance_enabled=False)
application = create_app(settings, model_gateway=ModelGateway(settings, transport=httpx.MockTransport(transport)))
uvicorn.run(application, host='127.0.0.1', port=args.port, log_level='warning')
