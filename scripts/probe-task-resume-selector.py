"""Opt-in, one-POST official-model probe of the real content-free control service.

Without --execute this only lists eight synthetic utterances. No production
database, raw media or credentials are read by the preview path.
"""
import argparse
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import tempfile
from time import monotonic
from urllib.parse import urlparse


CASES = {
    'math': ('数学那项我想接着做', {'subject': 'math'}),
    'english': ('先继续英语', {'subject': 'english'}),
    'review': ('刚才的复习我接着做', {'task_kind': 'review', 'relative_order': 'displayed_question'}),
    'oral': ('口头的那项我还想接着说', {'task_kind': 'oral'}),
    'written': ('笔头的那项先接着做', {'task_kind': 'written'}),
    'negative': ('数学那项先别继续', None),
    'unknown': ('台灯那项先继续', None),
    'mixed': ('英语和数学一起继续', None),
}


async def probe(settings, case, *, transport=None):
    """The optional transport exists only for an isolated no-network entry test."""
    import httpx
    from home_study_agent.app import create_app
    from home_study_agent.model_gateway import ModelGateway
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    from home_study_agent.task_start_binding import TaskStartBinding
    from home_study_agent.voice_control_intent import CONTROLS, STATE_FIELDS

    quote, expected = CASES[case]
    class OnePost(httpx.AsyncBaseTransport):
        def __init__(self):
            self.inner = transport or httpx.AsyncHTTPTransport(retries=0)
            self.posts = 0
            self.scope = ()
            self.wire_safe = False
            self.response_summary = None

        async def handle_async_request(self, request):
            if self.posts or request.method != 'POST':
                raise RuntimeError('PROBE_ONE_POST_LIMIT')
            body = json.loads(request.content)
            messages = body['messages']
            data = json.loads(messages[-1]['content'])
            allowed = {'model', 'messages', 'temperature', 'max_tokens', 'stream', 'response_format',
                       'enable_thinking', 'thinking', 'reasoning_effort'}
            serialized = json.dumps(messages, ensure_ascii=False)
            if (set(body) - allowed or len(messages) != 2
                    or [m['role'] for m in messages] != ['system', 'user']
                    or set(data) != {'transcript', 'state'} or data['transcript'] != quote
                    or any(type(v) is not bool for v in data['state'].values())
                    or any(secret in serialized for secret in self.scope)):
                raise RuntimeError('PROBE_WIRE_SCOPE_REJECTED')
            self.posts += 1
            self.wire_safe = True
            response = await self.inner.handle_async_request(request)
            await response.aread()
            summary = {'http_status': response.status_code, 'request_format': body.get('response_format')}
            try:
                value = json.loads(response.json()['choices'][0]['message']['content'])
                fields = {'role', 'control', 'quote', 'confidence', 'selector'}
                roles = {'control', 'answer', 'question', 'acknowledgement', 'mixed', 'unknown'}
                selector_values = {'math', 'english', 'chinese', 'physics', 'chemistry', 'biology', 'history',
                    'geography', 'politics', 'review', 'written', 'oral', 'reading', 'listening', 'displayed_question'}
                def safe(value, choices):
                    return value if value is None or isinstance(value, str) and value in choices else '[invalid]'
                selection = value.get('selector')
                summary.update(json_object=True, fields=sorted(k if k in fields else '[unexpected]' for k in value),
                    role=safe(value.get('role'), roles), control=safe(value.get('control'), CONTROLS),
                    quote_exact=value.get('quote') == quote, selector_present='selector' in value,
                    selector={k: safe(selection.get(k), selector_values) for k in ('subject', 'task_kind', 'relative_order')}
                    if isinstance(selection, dict) else None,
                    selector_keys=sorted(k if k in {'subject', 'task_kind', 'relative_order'} else '[unexpected]'
                                         for k in selection) if isinstance(selection, dict) else None)
            except (ValueError, KeyError, TypeError, AttributeError):
                summary['json_object'] = False
            self.response_summary = summary
            return response

        async def aclose(self):
            await self.inner.aclose()

    wire = OnePost()
    gateway = ModelGateway(settings, transport=wire)
    app = create_app(settings, model_gateway=gateway)
    async with app.router.lifespan_context(app):
        orchestrator = app.state.agent_orchestrator
        db, repo = orchestrator.database, orchestrator.repository
        PrivacyLifecycleService(db, settings.data_dir).update_preferences({'cloud_model_text_allowed': True})
        sid = repo.start_session('child-primary-4', 'synthetic-selector-session')['session_id']
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
            # These are synthetic paused candidates, not a user's saved plan.
            c.execute('''INSERT INTO daily_plan(plan_id,child_id,session_id,plan_date,status,total_minutes,
                generator,version,created_at,client_action_id) VALUES ('probe-plan','child-primary-4',?,
                '2026-09-08','active',20,'synthetic-probe',1,datetime('now'),'probe-plan')''', (sid,))
            for number, subject in enumerate(('数学', '英语')):
                c.execute('''INSERT INTO plan_item(plan_item_id,plan_id,subject,title,expected_minutes,priority,
                    status,position,source_type,task_type) VALUES (?,'probe-plan',?,'LOCAL PRIVATE TITLE',10,1,
                    'paused',?,'school_homework','written')''', (f'probe-item-{number}', subject, number))
                c.execute('''INSERT INTO plan_item_event(event_id,plan_item_id,event_type,occurred_at,payload_json,client_action_id)
                    VALUES (?,?,'paused',datetime('now'),'{}',?)''',
                    (f'probe-event-{number}', f'probe-item-{number}', f'probe-pause-{number}'))
            action_id = 'synthetic-selector-' + case
            orchestrator.learning_input_revisions.capture_arrival(sid, action_id, quote, connection=c)
            binding = {'task_resume': TaskStartBinding.resume_snapshot(c, sid, 'child-primary-4')}
            consent = orchestrator.voice_control_intent.text_consent_version(c)
            guard = orchestrator._input_dispatch_guard(c, sid, 'child-primary-4', action_id, quote, None, consent)
            c.commit()
        wire.scope = (sid, action_id, 'child-primary-4', 'probe-plan', 'probe-item-', 'LOCAL PRIVATE TITLE')
        state = {key: False for key in STATE_FIELDS}
        state.update(is_study_phase=True, has_paused_task=True)
        started = monotonic()
        result = await orchestrator.voice_control_intent.resolve(
            session_id=sid, child_id='child-primary-4', action_id=action_id, transcript=quote,
            state=state, local_sources=['LOCAL PRIVATE TITLE'], names=[], allowed=True,
            binding=binding, pre_dispatch=guard)
        selector = result.get('selector') or {}
        passed = (result.get('status') == 'control' and result.get('control') == 'task.resume'
                  and all(selector.get(k) == v for k, v in expected.items())) if expected else (
                      result.get('status') == 'clarify' or (result.get('status') == 'non_control'
                          and result.get('role') in {'unknown', 'mixed', 'question'}))
        with db.connect() as c:
            attempts = c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]
        raw = wire.response_summary or {}
        semantic_passed = (raw.get('role') == 'control' and raw.get('control') == 'task.resume'
            and raw.get('selector_present') and all((raw.get('selector') or {}).get(k) == v for k, v in expected.items())) if expected else (
                raw.get('role') in {'unknown', 'mixed', 'question'} and raw.get('control') is None)
        safety_passed = bool(passed and wire.posts == 1 and wire.wire_safe and attempts == 0)
        return {'case': case, 'utterance': quote, 'provider': settings.model_provider,
                'model': settings.voice_intent_model_name or settings.model_name,
                'status': result.get('status'), 'role': result.get('role'),
                'control': result.get('control'), 'selector': selector,
                'reason': result.get('reason'), 'seconds': round(monotonic() - started, 2),
                'posts': wire.posts, 'wire_isolated': wire.wire_safe, 'attempts': attempts,
                'model_output_summary': wire.response_summary,
                'semantic_passed': bool(semantic_passed), 'safety_passed': safety_passed,
                'passed': bool(semantic_passed and safety_passed),
                'synthetic_only': True, 'asr_verified': False, 'task_execution_verified': False,
                'official_model': transport is None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', choices=tuple(CASES))
    parser.add_argument('--execute', action='store_true', help='Explicitly allow at most one official HTTP POST.')
    parser.add_argument('--keychain-service', help='Existing macOS keychain service, only if configured env key is absent.')
    parser.add_argument('--output', help='Safe summary JSON, without IDs, payloads or credentials.')
    args = parser.parse_args()
    if not args.execute:
        print(json.dumps({'network': False, 'cases': {k: v[0] for k, v in CASES.items()}}, ensure_ascii=False, indent=2))
        return 0
    if not args.case or not args.output:
        parser.error('--execute requires one --case and --output; there is no batch mode')
    with tempfile.TemporaryDirectory(prefix='hsc-resume-probe-') as directory:
        root = Path(directory)
        os.environ['HSC_DATA_DIR'], os.environ['HSC_DATABASE_PATH'] = str(root), str(root / 'probe.db')
        from home_study_agent.config import Settings
        settings = Settings.from_environment()
        official = {'deepseek': 'api.deepseek.com', 'qwen': 'dashscope.aliyuncs.com'}
        endpoint = urlparse(settings.model_base_url)
        if (endpoint.scheme != 'https' or endpoint.netloc != official.get(settings.model_provider)
                or endpoint.username or endpoint.password):
            parser.error('Only an already configured official DeepSeek/Qwen endpoint is supported')
        key = settings.model_api_key
        if not key and args.keychain_service:
            saved = subprocess.run(['security', 'find-generic-password', '-s', args.keychain_service, '-w'],
                                   capture_output=True, text=True, check=False)
            key = saved.stdout.strip() if saved.returncode == 0 else None
        if not key:
            parser.error('No existing configured/keychain credential available')
        settings = replace(settings, model_api_key=key, maintenance_enabled=False,
                           fallback_model_provider=None, fallback_model_api_key=None,
                           fallback_model_base_url=None, fallback_model_name=None)
        try:
            result = asyncio.run(probe(settings, args.case))
        except Exception as error:
            # Do not print provider exceptions, headers, response bodies or credentials.
            result = {'case': args.case, 'passed': False, 'error_type': type(error).__name__}
        Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
