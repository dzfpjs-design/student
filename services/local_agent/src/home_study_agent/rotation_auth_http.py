"""PIN-only loopback surface used before a model-key rotation decision.

No ordinary learning, device, model, or cloud route is registered here.
"""
from __future__ import annotations
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from contextlib import contextmanager
import sqlite3
from pathlib import Path
from .build_info import BUILD_COMPONENT, BUILD_VERSION
from .config import Settings
from .parent_auth import ParentAuth
from .parent_restore_security import install_local_session, install_parent_auth_handlers
from .restore_publication import connection_admission, _original_read_lease
from .runtime_data_scope import assert_scope


class PinRequest(BaseModel):
    pin: str = Field(min_length=4, max_length=8)


class _GuardedConnection(sqlite3.Connection):
    def commit(self):
        _original_read_lease(self.admission, self.expected_path)
        super().commit()
        _original_read_lease(self.admission, self.expected_path)


class _ExistingParentDatabase:
    def __init__(self, path, runtime_mode):
        self.path, self.runtime_mode = path, runtime_mode

    @contextmanager
    def connect(self):
        # Existing-only open: never create, migrate, or recover a family DB.
        # The common admission gate refuses an active restore journal.
        with connection_admission(self.path) as admission:
            identity = _original_read_lease(admission, self.path)['identity']
            if identity is None:
                raise RuntimeError('ROTATION_AUTH_PARENT_REQUIRED')
            connection = sqlite3.connect(self.path.resolve().as_uri() + '?mode=rw', uri=True, timeout=10, factory=_GuardedConnection)
            connection.row_factory = sqlite3.Row
            connection.admission, connection.expected_path = admission, self.path
            try:
                _original_read_lease(admission, self.path)
                databases = connection.execute('PRAGMA database_list').fetchall()
                if len(databases) != 1 or databases[0][1] != 'main' or Path(databases[0][2]).resolve() != self.path.resolve():
                    raise RuntimeError('ROTATION_AUTH_DATABASE_CHANGED')
                assert_scope(connection, self.runtime_mode)
                yield connection
                _original_read_lease(admission, self.path)
            finally:
                try:
                    connection.close()
                except BaseException:
                    admission.poison(('rotation_auth_close_unconfirmed', connection))
                    raise


def create_rotation_auth_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_environment()
    if not settings.session_token or not settings.native_launch_attestation_key:
        raise RuntimeError('ROTATION_AUTH_SESSION_REQUIRED')
    database = _ExistingParentDatabase(settings.database_path, settings.runtime_mode)
    auth = ParentAuth(database)
    # Fail closed if there is no existing parent identity. No setup route exists.
    if not auth.status()['configured']:
        raise RuntimeError('ROTATION_AUTH_PARENT_REQUIRED')
    app = FastAPI(title='Home Study Parent Authentication', docs_url=None,
                  redoc_url=None, openapi_url=None)
    app.add_middleware(CORSMiddleware, allow_origins=[],
                       allow_origin_regex=r'^http://(?:localhost|127\.0\.0\.1):\d+$',
                       allow_credentials=False, allow_methods=['GET', 'POST'],
                       allow_headers=['Content-Type', 'X-HSC-Session', 'X-HSC-Parent', 'X-HSC-Trace'])
    app.state.request_shutdown = None
    install_local_session(app, settings)
    install_parent_auth_handlers(app)

    @app.get('/v1/health')
    async def health():
        return {'status': 'rotation_auth_only', 'normal_business_ready': False, 'build_component': BUILD_COMPONENT,
                'build_version': BUILD_VERSION}

    @app.post('/v1/runtime/shutdown', status_code=202)
    async def shutdown():
        if not callable(app.state.request_shutdown):
            from fastapi import HTTPException
            raise HTTPException(status_code=503, detail='RUNTIME_SHUTDOWN_NOT_READY')
        app.state.request_shutdown()
        return {'status': 'shutting_down'}

    @app.get('/v1/parent/status')
    async def status():
        return auth.status()

    @app.post('/v1/parent/unlock')
    async def unlock(payload: PinRequest):
        return {'parent_token': auth.unlock(payload.pin), 'expires_in_seconds': 1800}

    @app.post('/v1/parent/rotation-authority')
    async def authority(request: Request):
        auth.require(request.headers.get('X-HSC-Parent'))
        return {'authorized': True}

    return app
