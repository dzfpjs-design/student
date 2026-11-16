"""Isolated synthetic native-rename integration server; loopback only."""
import socket
import sys
from pathlib import Path

import uvicorn
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database

directory = Path(sys.argv[1]).resolve()
database = directory / 'synthetic-native-rename.db'
Database(database).initialize()
settings = Settings(data_dir=directory, database_path=database,
                    session_token='synthetic-native-rename-token',
                    native_launch_attestation_key='cd' * 32,
                    maintenance_enabled=False, model_api_key=None,
                    fallback_model_api_key=None)
app = create_app(settings)
sock = socket.socket()
sock.bind(('127.0.0.1', 0))
sock.listen(128)
print(sock.getsockname()[1], flush=True)
uvicorn.Server(uvicorn.Config(app, log_level='error', access_log=False)).run(sockets=[sock])
