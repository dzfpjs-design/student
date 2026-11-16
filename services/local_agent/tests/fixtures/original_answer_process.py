"""Disposable TCP host; controlled recognizer, no family files or paid models."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import wave

import uvicorn


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-root', required=True)
    parser.add_argument('--real-vad', action='store_true')
    parser.add_argument('--dialogue-marker', action='store_true')
    args = parser.parse_args()
    root = Path(args.data_root).resolve()
    if not root.name.startswith('test_') or not any(p.name.startswith('pytest-') for p in root.parents):
        raise ValueError('only disposable pytest data allowed')
    os.environ['HSC_DATA_DIR'] = str(root / 'import-only')
    os.environ['HSC_DATABASE_PATH'] = str(root / 'import-only' / 'home-study.db')
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from home_study_agent.local_media_ai import LocalMediaAI

    class NoModels:
        configured = False

        async def complete(self, *args, **kwargs):
            raise AssertionError('No paid/model requests allowed')

    def recognize(self, wav, **kwargs):
        with (root / 'recognizer-calls.jsonl').open('a') as output:
            output.write(json.dumps({'sha256': hashlib.sha256(wav).hexdigest(),
                                     'byte_length': len(wav), 'purpose': kwargs['purpose']}) + '\n')
        with wave.open(io.BytesIO(wav), 'rb') as source:
            duration_ms = round(source.getnframes() / source.getframerate() * 1000)
        return '我的答案是I enjoy reading books.', duration_ms, 'synthetic-recognition-port', False

    if not args.real_vad:
        LocalMediaAI._recognize_wav = recognize
    settings = Settings(data_dir=root, database_path=root / 'http-prompts.db',
                        maintenance_enabled=False, data_encryption_key=b'D' * 32)
    app = create_app(settings, model_gateway=NoModels())
    if args.real_vad:
        from types import SimpleNamespace
        from home_study_agent.cloud_asr import CloudAsrResult

        class ControlledAsr:
            configured = True
            context_supported = False
            settings = SimpleNamespace(cloud_asr_model='controlled-short-word')

            def transcribe(self, wav, context_text=None):
                with (root / 'recognizer-calls.jsonl').open('a') as output:
                    output.write(json.dumps({'sha256': hashlib.sha256(wav).hexdigest(),
                        'byte_length': len(wav), 'purpose': 'pet_speaking'}) + '\n')
                return CloudAsrResult('嗯' if args.dialogue_marker else '对', 'controlled-short-word', 1)

        app.state.media_ai._cloud_asr = ControlledAsr()
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    print(json.dumps({'port': sock.getsockname()[1], 'synthetic_models': True}), flush=True)
    uvicorn.Server(uvicorn.Config(app, log_level='error')).run(sockets=[sock])


if __name__ == '__main__':
    main()
