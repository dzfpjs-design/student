"""Synthetic native-task fixture: temporary SQLite and loopback only, no cloud keys."""
from __future__ import annotations

import json
import os
import socket
import sys
import shutil
import tempfile
import threading
from datetime import date
from pathlib import Path

source = Path(sys.argv[1])
root = source.resolve(strict=True)
if (source.is_symlink() or not root.is_dir() or root.parent != Path(tempfile.gettempdir()).resolve()
        or not root.name.startswith('hsc-synthetic-native-task-')):
    raise RuntimeError('SYNTHETIC_FIXTURE_DIRECTORY_REQUIRED')
os.environ['HSC_DATA_DIR'] = str(root)
os.environ['HSC_DATABASE_PATH'] = str(root / 'synthetic.db')

import uvicorn
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.repository import StudyRepository

database_path = root / 'synthetic.db'
database = Database(database_path)
database.initialize()
repository = StudyRepository(database)
repository.seed_family()
with database.connect() as connection:
    existing = connection.execute("SELECT session_id FROM daily_session WHERE client_action_id=?", ('synthetic-native-task-session',)).fetchone()
if existing:
    session_id = existing['session_id']
else:
    session_id = repository.start_session('child-primary-4', 'synthetic-native-task-session')['session_id']
    # The setup seeds a pending item; its actual admission, selection and start
    # are executed by the production native routes, not by this fixture.
    with database.connect() as connection:
        connection.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (session_id,))
        connection.execute("""INSERT INTO daily_plan(plan_id,child_id,session_id,plan_date,status,total_minutes,generator,version,created_at,client_action_id)
            VALUES ('synthetic-plan','child-primary-4',?,?,'active',10,'synthetic',1,datetime('now'),'synthetic-plan')""", (session_id, date.today().isoformat()))
        connection.execute("""INSERT INTO plan_item(plan_item_id,plan_id,subject,title,expected_minutes,priority,status,position,source_type)
            VALUES ('synthetic-task','synthetic-plan','数学','合成数学任务',10,1,'pending',1,'school_homework')""")
        connection.commit()
settings = Settings(data_dir=root, database_path=database_path,
    session_token='synthetic-native-task-token', native_launch_attestation_key='cd' * 32,
    maintenance_enabled=False, model_api_key=None, fallback_model_api_key=None)
application = create_app(settings)
sock = socket.socket()
sock.bind(('127.0.0.1', 0))
sock.listen(128)
server = uvicorn.Server(uvicorn.Config(application, log_level='error', access_log=False))
parent_disappeared = threading.Event()
original_root = root.stat()
def monitor_parent_pipe() -> None:
    # The test owns this pipe. EOF means the test process exited unexpectedly.
    while sys.stdin.buffer.read(4096):
        pass
    parent_disappeared.set()
    server.should_exit = True
threading.Thread(target=monitor_parent_pipe, daemon=True).start()
print(json.dumps({'port': sock.getsockname()[1], 'session': session_id}), flush=True)
try:
    server.run(sockets=[sock])
finally:
    if parent_disappeared.is_set():
        current = root.stat()
        if (not root.is_symlink() and current.st_dev == original_root.st_dev
                and current.st_ino == original_root.st_ino):
            shutil.rmtree(root)
