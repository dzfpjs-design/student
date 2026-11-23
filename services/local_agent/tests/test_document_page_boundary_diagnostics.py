from collections import Counter
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
import zipfile

import pytest

from home_study_agent import daily_diagnostic_bundle as diagnostic


REASONS = (
    'initial', 'source_changed', 'counter_reset', 'blackout_or_page_missing',
    'page_changed_signal', 'broad_spatial_change', 'explicit_invalidation',
)
NUMBERS = {
    'strong_cells': 48, 'quadrants': 4, 'mean_residual': 255,
    'exposure_offset_abs': 255, 'missing_elapsed_ms': 300_000,
}


def test_page_boundary_zip_preserves_closed_reasons_and_bounded_statistics_only(tmp_path):
    start = datetime(2026, 11, 23, tzinfo=timezone.utc)
    directory = tmp_path / 'diagnostics' / 'logs'
    directory.mkdir(parents=True)
    rows = [{
        'privacy_policy_version': 'diagnostic_metadata_v2',
        'timestamp': (start + timedelta(seconds=index + 1)).isoformat(),
        'source': 'renderer', 'level': 'info', 'event': 'document.preview.page_boundary',
        'details': {
            'preview_boundary_reason': reason, **NUMBERS,
            'source': 'PRIVATE_CAMERA', 'source_id': 'PRIVATE_SOURCE', 'page_token': 'PRIVATE_TOKEN',
            'child_id': 'PRIVATE_CHILD', 'client_action_id': 'PRIVATE_ACTION',
            'fingerprint': [14, 82, 91], 'text': 'PRIVATE_PAPER', 'image_base64': 'PRIVATE_IMAGE',
        },
    } for index, reason in enumerate(REASONS)]
    (directory / 'runtime.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
    bundle = diagnostic.build_daily_diagnostic_bundle(tmp_path, start_utc=start, end_utc=start + timedelta(days=1))
    with zipfile.ZipFile(BytesIO(bundle.content)) as archive:
        raw = archive.read('events.jsonl')
        exported = [json.loads(line) for line in raw.splitlines()]
    assert [row['event'] for row in exported] == ['document.preview.page_boundary'] * len(REASONS)
    assert [row['details'] for row in exported] == [
        {'preview_boundary_reason': reason, **NUMBERS} for reason in REASONS
    ]
    assert b'PRIVATE_' not in raw
    assert b'fingerprint' not in raw


@pytest.mark.parametrize('key,maximum', NUMBERS.items())
@pytest.mark.parametrize('value', [-1, True, False, '1', None, float('inf'), float('-inf'), float('nan')])
def test_page_boundary_drops_invalid_numeric_types_and_values(key, maximum, value):
    assert diagnostic._details({key: value}, Counter()) == {}


@pytest.mark.parametrize('key,maximum', NUMBERS.items())
def test_page_boundary_rejects_values_above_each_hard_limit(key, maximum):
    assert diagnostic._details({key: maximum + .01}, Counter()) == {}
    assert diagnostic._details({key: 0}, Counter()) == {key: 0}
    assert diagnostic._details({key: maximum}, Counter()) == {key: maximum}


@pytest.mark.parametrize('key', ['strong_cells', 'quadrants'])
def test_page_boundary_requires_integer_cell_and_quadrant_counts(key):
    assert diagnostic._details({key: .5}, Counter()) == {}


def test_page_boundary_reason_is_a_closed_enum():
    assert diagnostic._details({'preview_boundary_reason': 'PRIVATE_SCENE'}, Counter()) == {}
    for reason in REASONS:
        assert diagnostic._details({'preview_boundary_reason': reason}, Counter()) == {'preview_boundary_reason': reason}
