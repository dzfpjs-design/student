"""Isolated, no-cloud local service for the physical camera stability probe."""
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys


def main():
    root = Path(sys.argv[1]).resolve()
    if root.parent != Path(r'C:\HSC\diagnostics') or not root.name.startswith('hsc-camera-stability-'):
        raise ValueError('owned Windows diagnostic root required')
    database = root / 'runtime' / 'synthetic.db'
    if '--inspect' in sys.argv:
        connection = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)
        result = {
            'integrity': connection.execute('PRAGMA integrity_check').fetchone()[0],
            'leases': [dict(zip(('status', 'reason', 'count'), row)) for row in connection.execute(
                'SELECT status, release_reason, count(*) FROM sensor_lease GROUP BY status, release_reason')],
            'incidents': [dict(zip(('code', 'count'), row)) for row in connection.execute(
                'SELECT error_code, count(*) FROM device_incident GROUP BY error_code')],
            'raw_media_files': sum(1 for path in (root / 'runtime').rglob('*')
                if path.is_file() and path.suffix.lower() in {'.jpg', '.jpeg', '.png', '.wav', '.mp4'}),
        }
        connection.close()
        print(json.dumps(result), flush=True)
        return
    os.environ['HSC_DATA_DIR'] = str(root / 'bootstrap')
    os.environ['HSC_DATABASE_PATH'] = str(root / 'bootstrap' / 'bootstrap.db')
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    import uvicorn
    settings = Settings(data_dir=root / 'runtime', database_path=database,
        runtime_mode='trial', maintenance_enabled=False, model_api_key=None,
        session_token='synthetic-camera-stability-session', data_encryption_key=b'0' * 32)
    app = create_app(settings)
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    print(json.dumps({'status': 'ready', 'localApiUrl': 'http://127.0.0.1:' + str(listener.getsockname()[1]),
        'sessionToken': settings.session_token}), flush=True)
    uvicorn.Server(uvicorn.Config(app, log_level='error', access_log=False)).run(sockets=[listener])


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'status': 'failed', 'error_type': type(error).__name__}), flush=True)
        sys.exit(1)
