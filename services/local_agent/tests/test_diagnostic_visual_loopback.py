"""Real TCP localhost + SQLite + encrypted synthetic PNG, not ASGI-only."""
from contextlib import contextmanager
import socket
from threading import Thread
from time import monotonic, sleep

import httpx
import uvicorn

from home_study_agent.app import create_app
from test_diagnostic_visual_http import LOCAL, publish, urls
from test_pet_reviewed_source_actual_http import NoCloud
from test_visual_material_host import setup_case, headers, REGION


@contextmanager
def host(settings):
    application = create_app(settings, model_gateway=NoCloud())
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(application, log_level='error', access_log=False, lifespan='on'))
    failures = []
    def run():
        try:
            server.run(sockets=[listener])
        except BaseException as error:
            failures.append(error)
    worker = Thread(target=run, name='diagnostic-visual-isolated-http', daemon=True)
    worker.start()
    try:
        deadline = monotonic() + 25
        while worker.is_alive() and not server.started and monotonic() < deadline:
            sleep(.02)
        assert server.started and not failures, failures
        with httpx.Client(base_url='http://127.0.0.1:' + str(port), trust_env=False, timeout=15) as client:
            client.app = application
            yield client
    finally:
        server.should_exit = True
        worker.join(20)
        if worker.is_alive():
            server.force_exit = True
            worker.join(5)
        listener.close()
        assert not worker.is_alive(), 'owned HTTP server did not drain'
        assert not failures, failures


def test_real_loopback_publish_prepare_pixels_restart_then_retract(tmp_path):
    db, settings, prepared, args, region = setup_case(tmp_path)
    with host(settings) as client:
        state = publish(client, db, prepared, args, region)
        base, query = urls(state)
        image_query = {**query, 'material_key': 'figure', 'sha256': region['safe_png_sha256']}
        first = client.get(base, params=query, headers=LOCAL)
        assert first.status_code == 200, first.text
        image = client.get(base + '/image', params=image_query, headers=LOCAL)
        assert image.status_code == 200 and image.content == prepared.png
        assert image.headers['cache-control'] == 'no-store'
        assert client.get(base, params=query).status_code == 401
    with host(settings) as client:
        repeated = client.get(base, params=query, headers=LOCAL)
        assert repeated.status_code == 200 and repeated.json() == first.json(), repeated.text
        assert client.get(base + '/image', params=image_query, headers=LOCAL).content == prepared.png
        r = client.post(REGION + '/retract', headers=headers(client), json={**args, 'client_action_id': 'tcp-retract'})
        assert r.status_code == 200, r.text
        for path, params in ((base, query), (base + '/image', image_query)):
            r = client.get(path, params=params, headers=LOCAL)
            assert r.status_code == 409 and r.headers['cache-control'] == 'no-store', r.text
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0] == 0
