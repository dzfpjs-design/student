"""Disposable parent camera HTTP witness; no default family stores allowed."""
import argparse
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-root', required=True)
    parser.add_argument('--device-hash', required=True)
    args = parser.parse_args()
    root = Path(args.data_root).resolve()
    temporary_roots = {Path(tempfile.gettempdir()).resolve()}
    if os.name == 'posix':
        temporary_roots.add(Path('/tmp').resolve())
    if sys.platform == 'darwin':
        temporary_roots.add(Path('/private/var/folders').resolve())
    if (not root.is_dir() or not root.name.startswith('hsc-parent-camera-')
            or not any(parent in root.parents for parent in temporary_roots)):
        raise ValueError('only disposable parent camera data are allowed')
    forbidden = {Path.home() / '.home-study-companion'}
    for key in ('HSC_DATA_DIR', 'HSC_DATABASE_PATH'):
        if os.environ.get(key):
            inherited = Path(os.environ[key]).expanduser().resolve()
            if inherited != root and root not in inherited.parents:
                forbidden.add(inherited)
    import_root = root / 'import-only'
    os.environ.update(HSC_DATA_DIR=str(import_root),
        HSC_DATABASE_PATH=str(import_root / 'home-study.db'),
        HSC_MODEL_PROVIDER='disabled', HSC_ASR_PROVIDER='disabled',
        HSC_TTS_PROVIDER='disabled', HSC_MAINTENANCE_ENABLED='0')
    sys.dont_write_bytecode = True

    def audit(event, values):
        if event not in {'open', 'os.mkdir', 'os.remove', 'os.rmdir', 'sqlite3.connect'}:
            return
        value = values[0]
        if not isinstance(value, (str, bytes, os.PathLike)):
            return
        value = os.fsdecode(value)
        if event == 'sqlite3.connect' and value.startswith('file:'):
            uri = urlsplit(value)
            if uri.netloc not in {'', 'localhost'}:
                raise RuntimeError('PARENT_CAMERA_FIXTURE_PATH_ESCAPE')
            value = unquote(uri.path)
        path = Path(value).resolve()
        if (any(path == blocked or blocked in path.parents for blocked in forbidden)
                or (event == 'sqlite3.connect' and root not in path.parents)):
            raise RuntimeError('PARENT_CAMERA_FIXTURE_PATH_ESCAPE')

    sys.addaudithook(audit)
    workspace = Path(__file__).resolve().parents[2]
    sys.path[:0] = [str(workspace / 'src'), str(workspace / 'tests')]
    from test_parent_textbook_capture import setup, Adapter, Auth
    from home_study_agent.app import app as imported_app
    from home_study_agent.database import Database
    from home_study_agent.knowledge_graph.http_api import install_knowledge_graph_routes
    from fastapi import FastAPI
    import uvicorn
    assert imported_app.state.settings.data_dir == import_root
    path = root / 'isolated.db'
    fresh = not path.exists()
    database = Database(path, media_root=root, data_encryption_key=b'0' * 32)
    if fresh:
        setup(database)
    else:
        database.initialize()
    with database.connect() as c:
        c.execute("UPDATE device_binding SET system_id_hash=? WHERE device_type='document_camera'",
                  (args.device_hash,))
        c.commit()
    app = FastAPI()
    app.state.ocr_workflow = SimpleNamespace(adapter=Adapter())
    install_knowledge_graph_routes(app, database, Auth())
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    sock.listen(128)
    print('HSC_PARENT_PORT=' + str(sock.getsockname()[1]), flush=True)
    uvicorn.Server(uvicorn.Config(app, log_level='error')).run(sockets=[sock])


if __name__ == '__main__':
    main()
