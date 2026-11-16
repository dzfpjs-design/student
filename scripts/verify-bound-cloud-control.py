"""Two paid synthetic control probes on disposable databases; ASR is a controlled port."""
import argparse
import base64
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from unittest.mock import patch
import wave


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    parser.add_argument('--case', choices=('exam_pause', 'camera_pause'))
    args = parser.parse_args()
    key = subprocess.run(['security', 'find-generic-password', '-s', 'hsc-deepseek-api-key', '-w'],
        capture_output=True, text=True, check=True).stdout.strip()
    with tempfile.TemporaryDirectory(prefix='hsc-bound-control-') as directory:
        root = Path(directory)
        os.environ['HSC_DATA_DIR'] = str(root / 'import')
        os.environ['HSC_DATABASE_PATH'] = str(root / 'import' / 'app.db')
        from fastapi.testclient import TestClient
        from home_study_agent.app import create_app
        from home_study_agent.config import Settings
        from home_study_agent.exam_simulation import ExamSimulationService
        from home_study_agent.local_media_ai import LocalMediaAI

        audio = io.BytesIO()
        with wave.open(audio, 'wb') as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(16000)
            stream.writeframes(b'\0\0' * 16000)
        cases = [
            ('exam_pause', '这份卷子我想暂停一会儿，替我留着进度，等我回来接着做', 'pause_exam_simulation'),
            ('camera_pause', '先让看我的那个镜头休息一下，等会儿再看我', 'pause_observation'),
        ]
        if args.case:
            cases = [case for case in cases if case[0] == args.case]
        results = []
        for name, utterance, expected in cases:
            folder = root / name
            settings = Settings(data_dir=folder, database_path=folder / 'test.db', maintenance_enabled=False,
                model_provider='deepseek', model_api_key=key, model_base_url='https://api.deepseek.com',
                model_name='deepseek-v4-pro', voice_intent_model_name='deepseek-v4-flash', model_timeout_seconds=25)
            with TestClient(create_app(settings), raise_server_exceptions=False) as client:
                db = client.app.state.agent_orchestrator.database
                sid = client.post('/v1/sessions', json={'child_id': 'child-primary-4',
                    'client_action_id': name + '-session'}).json()['session_id']
                with db.connect() as c:
                    c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
                    c.commit()
                exam = ExamSimulationService(db).start(sid, 'competition_math', '数学', 'practice', 2, 30, name + '-exam')
                with patch.object(LocalMediaAI, '_recognize_wav', return_value=(utterance, 1000, 'synthetic-port', False)):
                    captured = client.post(f'/v1/sessions/{sid}/asr', json={
                        'wav_base64': base64.b64encode(audio.getvalue()).decode(), 'purpose': 'agent_command',
                        'client_action_id': name + '-capture', 'capture_reference': {'child_id': 'child-primary-4',
                            'question_reference': {'kind': 'exam', 'binding': exam['answer_binding']}}})
                if captured.status_code != 200:
                    results.append({'case': name, 'passed': False, 'capture_http': captured.status_code})
                    continue
                gateway = client.app.state.model_gateway
                complete = gateway.complete
                sent = []
                async def inspect(request):
                    sent.append(request)
                    return await complete(request)
                gateway.complete = inspect
                begin = time.monotonic()
                response = client.post(f'/v1/sessions/{sid}/agent-turn', json={'transcript': utterance,
                    'voice_input_id': captured.json()['voice_input_id'], 'client_action_id': name + '-turn'})
                value = response.json() if response.status_code == 200 else {}
                result = {'case': name, 'http': response.status_code, 'action': value.get('action', {}).get('type'),
                    'seconds': round(time.monotonic() - begin, 2), 'model_requests': len(sent),
                    'asr_verified': False, 'physical_camera_verified': False}
                safe = len(sent) == 1 and sent[0].prompt_version == 'content_free_voice_control_v1'
                if safe:
                    payload = json.loads(sent[0].messages[-1]['content'])
                    serialized = json.dumps(sent[0].messages, ensure_ascii=False)
                    safe = (set(payload) == {'transcript', 'state'} and payload['transcript'] == utterance
                        and all(type(v) is bool for v in payload['state'].values())
                        and not sent[0].evidence_refs
                        and all(x not in serialized for x in [sid, exam['run_id'], exam['current_item']['prompt']]))
                result['source_isolation_passed'] = bool(safe)
                if result['action'] == expected:
                    tid = value['agent_turn_id']
                    endpoint = (f'/v1/agent-turns/{tid}/execute' if name == 'exam_pause' else
                        f'/v1/sessions/{sid}/agent-turns/{tid}/control-admission')
                    executed = client.post(endpoint)
                    retried = client.post(endpoint)
                    result.update(execute_http=executed.status_code, retry_http=retried.status_code)
                    result['execution_verified'] = (executed.status_code == retried.status_code == 200 and
                        (ExamSimulationService(db).status(exam['run_id'])['status'] == 'paused' if name == 'exam_pause'
                         else executed.json().get('status') == 'admitted' and retried.json().get('status') == 'execution_receipt_required'))
                with db.connect() as c:
                    result['attempt_count'] = c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]
                    result['calls'] = [dict(r) for r in c.execute('''SELECT purpose,provider,model,status,error_code,
                        prompt_tokens,completion_tokens FROM model_call WHERE session_id=?''', (sid,))]
                result['passed'] = bool(safe and result['action'] == expected and result.get('execution_verified')
                    and result['attempt_count'] == 0 and all(r['status'] == 'completed' for r in result['calls']))
                results.append(result)
                print(json.dumps({k: v for k, v in result.items() if k != 'calls'}, ensure_ascii=False))
        Path(args.output).write_text(json.dumps({'synthetic_only': True, 'results': results}, ensure_ascii=False, indent=2), encoding='utf-8')
        return 0 if len(results) == len(cases) and all(r['passed'] for r in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
