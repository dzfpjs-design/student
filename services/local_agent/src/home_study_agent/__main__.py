from __future__ import annotations

import argparse
from multiprocessing import freeze_support
import sys


def main() -> None:
    # Required before parsing service arguments so a PyInstaller Windows spawn
    # child exits through multiprocessing rather than recursively launching the
    # local FastAPI service.
    freeze_support()
    if sys.argv[1:] == ['--build-info']:
        import json
        from home_study_agent.build_info import BUILD_COMPONENT, BUILD_VERSION
        print(json.dumps({'component': BUILD_COMPONENT, 'version': BUILD_VERSION}, separators=(',', ':')))
        return
    if sys.argv[1:] == ['--internal-smtp-worker-v1']:
        from home_study_agent.smtp_owned_worker import main as worker_main
        worker_main()
        return
    # Keep heavyweight app/database imports below freeze_support: PyInstaller
    # spawn children must exit before application construction or recovery work.
    import uvicorn
    parser = argparse.ArgumentParser(description="Home Study local agent service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--rotation-auth-only", action="store_true")
    args = parser.parse_args()
    if args.rotation_auth_only:
        if args.host not in ('127.0.0.1', '::1'):
            raise RuntimeError('ROTATION_AUTH_LOOPBACK_REQUIRED')
        from home_study_agent.rotation_auth_http import create_rotation_auth_app
        app = create_rotation_auth_app()
    else:
        from home_study_agent.app import app
    # Endpoint paths contain child and session identifiers. Keep operational
    # errors, but do not duplicate those identifiers into the Electron log.
    config = uvicorn.Config(app, host=args.host, port=args.port, log_level="info", access_log=False)
    server = uvicorn.Server(config)
    app.state.request_shutdown = lambda: setattr(server, "should_exit", True)
    server.run()


if __name__ == "__main__":
    main()
