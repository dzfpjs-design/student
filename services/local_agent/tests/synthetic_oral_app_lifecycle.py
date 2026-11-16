"""Synthetic oral journeys: original context exit and a fresh actual App/gateway."""
from contextlib import contextmanager, ExitStack
from home_study_agent.app import create_app


def parent_headers(case):
    state = case.get('_oral_lifecycle')
    if state is not None:
        assert state['active'], 'Parent HTTP needs the current live original context'
    headers = case['headers']
    if not headers.get('X-HSC-Parent'):
        assert case.get('_oral_parent_authorized'), 'No original Parent setup authority'
        response = case['client'].post('/v1/parent/unlock', json={'pin': '8264'})
        assert response.status_code == 200, response.text
        headers.update({'X-HSC-Parent': response.json()['parent_token']})
    return headers


@contextmanager
def fresh_oral_case(case, client_factory, *, client):
    finish = case['finish_client']
    factory = case['gateway_factory']
    assert callable(finish) and callable(factory)
    prior_parent = bool(case['headers'].get('X-HSC-Parent') or case.get('_oral_parent_authorized'))
    ordinary_headers = {key: value for key, value in case['client'].headers.items()
                        if key.lower() != 'x-hsc-parent'}
    finish()  # Exit the original entered context, including a real HTTP server adapter.
    gateway = factory()  # Original explicit Settings and controlled transport; no closed-client borrowing.
    state = {'active': True}
    with ExitStack() as lifecycle:
        current = lifecycle.enter_context(client_factory(create_app(case['settings'], model_gateway=gateway), client=client, **case['client_options']))
        current.headers.update(ordinary_headers)
        def finish_current():
            if state['active']:
                state['active'] = False
                lifecycle.close()
        fresh = {**case, 'client': current, 'gateway': gateway, 'finish_client': finish_current,
                 'headers': {}, '_oral_parent_authorized': prior_parent, '_oral_lifecycle': state}
        try:
            yield fresh
        finally:
            state['active'] = False
