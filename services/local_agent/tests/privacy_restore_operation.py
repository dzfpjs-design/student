"""Explicit HTTP operation identity for isolated tests with no renderer journals.

Call at the legitimate parent request site. This helper neither restores data
nor wraps a client, authenticates a parent, or changes an existing request.
"""
from __future__ import annotations

from hashlib import sha256
from uuid import uuid4


def restore_operation(client, *, headers=None) -> dict[str, str]:
    response = client.get('/v1/parent/privacy-operations/generation', headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers['cache-control'] == 'no-store'
    return {
        'request_id': str(uuid4()),
        'expected_generation': response.json()['generation'],
        # These backend fixtures have an explicitly empty local cleanup set.
        'cleanup_manifest_sha256': sha256(b'[]').hexdigest(),
    }
