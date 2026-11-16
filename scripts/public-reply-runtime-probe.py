"""Owned isolated Runtime for the D-030 desktop qualification.

Credentials arrive only through the parent's anonymous stdin pipe. They are
never serialized to disk, echoed, or included in command arguments.
"""
import json
import os
from pathlib import Path
import socket
import sys


def main():
    packet = json.loads(sys.stdin.readline())
    root = Path(sys.argv[1]).resolve()
    owned = str(root).startswith('/private/tmp/hsc-public-reply-') or (
        os.name == 'nt' and root.parent == Path(r'C:\HSC\diagnostics') and root.name.startswith('hsc-public-reply-20261006-'))
    if not owned or not root.is_dir():
        raise ValueError('owned isolated root required')
    os.environ['HSC_DATA_DIR'] = str(root/'import')
    os.environ['HSC_DATABASE_PATH'] = str(root/'import'/'bootstrap.db')
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    import uvicorn
    model = packet['model']
    settings = Settings(data_dir=root/'runtime', database_path=root/'runtime'/'synthetic.db',
        runtime_mode='trial', maintenance_enabled=False, agent_runtime_enabled=True,
        model_provider=model['provider'], model_api_key=model['apiKey'], model_base_url=model['baseUrl'],
        model_name=model['model'], voice_intent_model_name=model['voiceIntentModel'],
        model_timeout_seconds=20, session_token='synthetic-public-reply-runtime-token',
        native_launch_attestation_key='ab'*32, data_encryption_key=b'0'*32)
    packet.clear()
    app = create_app(settings)
    listener = socket.socket(); listener.bind(('127.0.0.1', 0))
    print(json.dumps({'status': 'ready', 'localApiUrl': 'http://127.0.0.1:'+str(listener.getsockname()[1]),
        'sessionToken': settings.session_token, 'databasePath': str(settings.database_path)}), flush=True)
    uvicorn.Server(uvicorn.Config(app, log_level='error', access_log=False)).run(sockets=[listener])


if __name__ == '__main__':
    try: main()
    except Exception as error:
        print(json.dumps({'status': 'failed', 'error_type': type(error).__name__}), flush=True)
        sys.exit(1)
