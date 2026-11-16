"""Opt-in official semantic resume through the real dispatcher and durable goal check."""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile

import httpx

QUOTE = '我回来了，咱们接着刚才没做完的那个吧'
ROOT = Path(__file__).resolve().parents[1]


class OneControlPost(httpx.AsyncBaseTransport):
    def __init__(self, model, forbidden, inner=None):
        self.model, self.forbidden = model, forbidden
        self.inner = inner
        self.posts, self.rejected, self.summary = 0, 0, None

    async def handle_async_request(self, request):
        try:
            body = json.loads(request.content)
            messages = body['messages']
            data = json.loads(messages[-1]['content'])
            serialized = json.dumps(messages, ensure_ascii=False)
            assert request.method == 'POST' and str(request.url) == 'https://api.deepseek.com/chat/completions'
            assert self.posts == 0 and body['model'] == self.model and body['max_tokens'] <= 1200
            assert not set(body) - {'model', 'messages', 'temperature', 'max_tokens', 'stream', 'response_format',
                                     'thinking', 'enable_thinking', 'reasoning_effort'}
            assert len(messages) == 2 and [m['role'] for m in messages] == ['system', 'user']
            assert messages[0]['content'].startswith('只分类当前原话，不执行、不评分、不回答题目。')
            assert set(data) == {'transcript', 'state'} and data['transcript'] == QUOTE
            assert isinstance(data['state'], dict) and all(type(v) is bool for v in data['state'].values())
            assert not any(value and value in serialized for value in self.forbidden)
        except (AssertionError, ValueError, KeyError, TypeError, AttributeError, IndexError):
            self.rejected += 1
            raise RuntimeError('SYNTHETIC_PROBE_SCOPE_REJECTED') from None
        self.posts += 1
        async with (self.inner or httpx.AsyncHTTPTransport(retries=0)) as transport:
            response = await transport.handle_async_request(request)
            await response.aread()
        payload = response.json()
        usage = payload.get('usage') or {}
        self.summary = {'http_status': response.status_code, 'model': self.model,
            'returned_model': payload.get('model') if payload.get('model') == self.model else None,
            'prompt_tokens': usage.get('prompt_tokens'), 'completion_tokens': usage.get('completion_tokens')}
        return response

    async def aclose(self):
        pass  # Each bounded request closes its own transport before returning.


def journey(settings, seed, transport=None):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.database import Database
    from home_study_agent.model_gateway import ModelGateway
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService

    db = Database(settings.database_path)
    sid, pid = seed['session_id'], seed['plan_item_id']
    forbidden = [sid, pid, seed['child_id'], seed['knowledge_node_id']]
    with db.connect() as c:
        forbidden.extend(row[0] for row in c.execute('SELECT prompt FROM diagnostic_item'))
    wire = OneControlPost(settings.model_name, forbidden, transport)
    def application():
        return create_app(settings, model_gateway=ModelGateway(settings, transport=wire))
    def post(client, path, value=None):
        result = client.post(path, json=value) if value is not None else client.post(path)
        assert result.status_code == 200, 'LOCAL_HTTP_' + str(result.status_code)
        return result.json()
    def execute(client, turn):
        return post(client, '/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
    def say(client, quote, key):
        return post(client, '/v1/sessions/'+sid+'/agent-turn', {'transcript': quote, 'client_action_id': key})
    privacy = PrivacyLifecycleService(db, settings.data_dir)
    privacy.update_preferences({'cloud_model_text_allowed': False})
    phase = 'start_and_pause'
    try:
        with TestClient(create_app(replace(settings,model_api_key=None))) as client:
            original = execute(client, say(client, '开始任务', 'probe-start'))['execution']['result']['diagnostic_prompt']['prompt']
            post(client, '/v1/plan-items/'+pid+'/pause', {'client_action_id': 'probe-pause'})
        privacy.update_preferences({'cloud_model_text_allowed': True})
        phase = 'semantic_resume'
        with TestClient(application()) as client:
            resumed = say(client, QUOTE, 'probe-natural-resume')
            if resumed['action']['type'] != 'resume_paused_task':
                return {'passed': False, 'phase': 'semantic_resume', 'action': resumed['action']['type'],
                        'posts': wire.posts, 'rejected': wire.rejected, 'usage': wire.summary}
            result = execute(client, resumed)['execution']['result']
            restored = result['diagnostic_prompt']['prompt']
            assert all(restored[key] == original[key] for key in ('prompt_id','item_version','diagnostic_item_id'))
            assert result['plan_item_id'] == pid
            phase = 'answer_and_complete'
            answer = post(client, '/v1/sessions/'+sid+'/diagnostic-prompt/answer', {
                'prompt_id': original['prompt_id'], 'item_version': original['item_version'],
                'answer': '-9999', 'client_action_id': 'probe-wrong-answer'})
            assert answer['attempt']['correctness'] == 0
            completion = {'actual_minutes':1,'completion_evidence_type':'voice_confirmed',
                          'completion_note':'Synthetic execution check, not mastery','client_action_id':'probe-finish'}
            post(client, '/v1/plan-items/'+pid+'/complete', completion)
        phase = 'restart_and_replay'
        with TestClient(application()) as client:
            post(client, '/v1/plan-items/'+pid+'/complete', completion)
        with db.connect() as c:
            assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?',(pid,)).fetchone()[0] == 'completed'
            assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='completed'",(pid,)).fetchone()[0] == 1
            assert c.execute('SELECT 1 FROM review_task WHERE source_evidence_id=?',(answer['attempt']['evidence_id'],)).fetchone()
        assert wire.posts == 1 and wire.rejected == 0
        return {'passed': True, 'phase': 'completed_and_restarted', 'posts': wire.posts,
                'rejected': wire.rejected, 'usage': wire.summary, 'synthetic_only': True,
                'official_model': transport is None, 'asr': False, 'electron': False, 'windows': False}
    except Exception as error:
        return {'passed':False,'phase':phase,'error_type':type(error).__name__,
                'posts':wire.posts,'rejected':wire.rejected,'usage':wire.summary}
    finally:
        wire.forbidden.clear()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--output')
    parser.add_argument('--keychain-service', default='hsc-deepseek-api-key')
    args = parser.parse_args()
    if not args.execute:
        print(json.dumps({'network': False, 'utterance': QUOTE, 'max_posts': 1}, ensure_ascii=False))
        return 0
    if not args.output:
        parser.error('--execute requires --output')
    with tempfile.TemporaryDirectory(prefix='hsc-goal-model-', dir='/private/tmp') as directory:
        path = Path(directory)
        environment = {'PATH':'/usr/bin:/bin', 'HOME':'/private/tmp'}
        seed_run = subprocess.run([sys.executable, str(ROOT/'scripts/seed-goal-check-native.py'), str(path/'goal.db'), str(path/'seed.json')],
                                  env=environment, capture_output=True)
        if seed_run.returncode:
            raise RuntimeError('SYNTHETIC_SEED_FAILED')
        saved = subprocess.run(['security','find-generic-password','-s',args.keychain_service,'-w'],capture_output=True,text=True)
        if saved.returncode or not saved.stdout.strip():
            raise RuntimeError('CREDENTIAL_UNAVAILABLE')
        from home_study_agent.config import Settings
        settings = Settings(data_dir=path, database_path=path/'goal.db', maintenance_enabled=False,
            model_provider='deepseek', model_base_url='https://api.deepseek.com', model_api_key=saved.stdout.strip(),
            model_name='deepseek-flash', voice_intent_model_name='deepseek-flash')
        try:
            result = journey(settings, json.loads((path/'seed.json').read_text()))
        except Exception as error:
            result = {'passed': False, 'error_type': type(error).__name__}
        Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        return 0 if result['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
