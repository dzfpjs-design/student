"""Explicit target-machine acceptance: actual local detector, geometry and persistence."""
import base64
import io
import sys
from uuid import uuid4

import pytest

from PIL import Image, ImageDraw, ImageFont

from test_app import client_for
from test_document_ocr_preview import start_homework


def paper(*, first):
    image = Image.new('RGB', (1600, 1600), (48, 48, 48))
    draw = ImageDraw.Draw(image)
    draw.rectangle((90, 65, 1510, 1535), fill='white')
    font = ImageFont.load_default(size=26)
    observations = [
        ('Triangle perimeter calculation', 190, 245),
        ('Compare product including decimals', 870, 285),
        ('Horizontal axis of the diagram', 870, 565),
        ('Find the missing dimension', 190, 845),
        ('Rectangle with equal diagonals', 870, 1065),
        ('Estimate kilograms and ounces', 190, 1285),
    ]
    observations.append(('Identify the equivalent fractions.', 190, 525) if first
                        else ('Calculate the change in volume.', 870, 825))
    for text, x, y in observations:
        draw.text((x, y), text, fill='black', font=font)
    data = io.BytesIO()
    image.save(data, format='PNG')
    return base64.b64encode(data.getvalue()).decode()


def test_real_detector_merges_complementary_paper_and_reads_it_after_restart(tmp_path):
    token = str(uuid4())
    with client_for(tmp_path) as client:
        monitor_route, _, generation = start_homework(client)
        route = monitor_route + '/previews'
        def read(first):
            response = client.post(route, json={
                'image_base64': paper(first=first), 'client_action_id': str(uuid4()),
                'expected_generation': generation, 'expected_lease_id': 'preview-lease',
                'expected_phase': 'homework_capture', 'page_token': token,
            })
            assert response.status_code == 200, response.status_code
            return response.json()
        first, second = read(True), read(False)
        assert first['page_draft']['reason'] == 'anchor_started'
        draft = second['page_draft']
        assert draft['merged'] is True, draft['reason']
        assert draft['frame_count'] == 2
        lines = draft['lines']
        assert any('equivalent fractions' in line['text'].lower() and line['status'] == 'historical' for line in lines)
        assert any('change in volume' in line['text'].lower() and line['status'] == 'current' for line in lines)
        assert len({line['frame_sha256'] for line in lines}) == 2
    with client_for(tmp_path) as client:
        items = client.get(route).json()['items']
        assert next(item for item in items if item['preview_id'] == second['preview_id'])['page_draft'] == draft


@pytest.mark.skipif(sys.platform != 'win32', reason='Explicit target Windows RapidOCR acceptance')
def test_real_detector_short_arithmetic_uses_original_ink_for_complementary_draft(tmp_path):
    from test_document_visual_registration import math_paper
    token = str(uuid4())
    with client_for(tmp_path) as client:
        monitor_route, _, generation = start_homework(client)
        route = monitor_route + '/previews'
        def read(hidden):
            raw, _, _ = math_paper(hidden=hidden)
            response = client.post(route, json={'image_base64': base64.b64encode(raw).decode(),
                'client_action_id': str(uuid4()), 'expected_generation': generation,
                'expected_lease_id': 'preview-lease', 'expected_phase': 'homework_capture', 'page_token': token})
            assert response.status_code == 200, response.text
            return response.json()
        first, second = read((7,)), read((6,))
        assert first['page_draft']['reason'] == 'anchor_started', first['page_draft']['reason']
        draft = second['page_draft']
        assert draft['merged'] and draft['frame_count'] == 2, draft
        assert draft['alignment_method'] == 'local_orb_v1'
        assert any(line['status'] == 'historical' and line['preview_id'] == first['preview_id']
                   and min(point[1] for point in line['canonical_polygon']) > .70 for line in draft['lines'])
        assert len({line['frame_sha256'] for line in draft['lines']}) == 2
