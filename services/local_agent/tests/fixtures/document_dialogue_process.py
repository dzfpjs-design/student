"""Synthetic models only; subprocess durability fixture, never a dev server."""
import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import socket
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn



QUOTE = '我们先不看这张纸，你有PET学习资料可以给我介绍吗？'


class SyntheticGateway:
    configured = True

    async def complete(self, request):
        from home_study_agent.model_gateway import ModelResult
        request.pre_dispatch()
        if request.request_type == 'voice_interaction':
            value = {'role':'question', 'control':None, 'quote':QUOTE, 'confidence':.97}
            if json.loads(request.messages[-1]['content']).get('state', {}).get('has_foreground_independent_dialogue'):
                value['question_scope'] = 'independent'
        else:
            value = {'objective':{'mode':'none', 'phase':'query'},
                'next_step':{'capability':'system.inspect_learning', 'arguments':{'scope':'resources','track':'pet'}},
                'utterance_binding':{'role':'question','quote':QUOTE},
                'spoken_transition':'先核对本机PET资料。','requires_child_reply':False,'confidence':.97}
        return ModelResult(content=json.dumps(value,ensure_ascii=False),provider='test',model='synthetic-only',
                           latency_ms=1,prompt_tokens=20,completion_tokens=20)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-root',required=True)
    parser.add_argument('--database-name',choices=['http-prompts.db','bound-vocabulary.db','exam-http.db','evaluation.db'],default='http-prompts.db')
    parser.add_argument('--session-token',choices=['public-realtime-resume-tcp-session'],default=None)
    args=parser.parse_args()
    root = Path(args.data_root).resolve()
    if not root.name.startswith('test_') or not any(p.name.startswith('pytest-') for p in root.parents):
        raise ValueError('only disposable pytest fixture data are allowed')
    import_root = root / 'import-only'
    os.environ['HSC_DATA_DIR'] = str(import_root)
    os.environ['HSC_DATABASE_PATH'] = str(import_root / 'home-study.db')
    from home_study_agent.app import create_app, app as imported_app
    from home_study_agent.config import Settings
    assert imported_app.state.settings.data_dir == import_root
    from original_money_owner_fixture import close_unused_imported_default_app_owner
    close_unused_imported_default_app_owner(imported_app)
    settings = Settings(data_dir=root,database_path=root/args.database_name,maintenance_enabled=False,
                        data_encryption_key=b'D'*32 if args.database_name=='http-prompts.db' else None)
    if args.session_token is not None:
        settings=replace(settings,session_token=args.session_token)
    app = create_app(settings,model_gateway=SyntheticGateway())
    sock = socket.socket(); sock.bind(('127.0.0.1',0))
    ready={'port':sock.getsockname()[1],'synthetic_models':True,'pid':os.getpid()}
    if args.session_token is not None:
        ready['session_token']=app.state.settings.session_token
    print(json.dumps(ready),flush=True)
    uvicorn.Server(uvicorn.Config(app,log_level='error')).run(sockets=[sock])


if __name__ == '__main__':
    main()
