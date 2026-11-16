"""Synthetic pixels, real local OCR publication/revision/parent HTTP; no cloud."""
import base64
import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from home_study_agent.document_vision import DiagramRegionGate
from test_app import advance_to_study, client_for
from test_document_vision import RegionEngine
from test_visual_material import frame


class LocalLines(RegionEngine):
    name = 'synthetic-local-lines'

    def health(self):
        return {'ready': True, 'routes': ['cpu']}

    def recognize(self, image, route):
        return [{'text': '1. Calculate the perimeter.', 'confidence': .99,
                 'box': [[110, 110], [530, 110], [530, 140], [110, 140]]}]


def configure(client):
    client.app.state.local_ocr.engines = [LocalLines()]
    if hasattr(client.app.state, 'visual_capture'):
        client.app.state.visual_capture.detector.gate = DiagramRegionGate(RegionEngine(''))


def capture(client, sid, key='visual-frame', raw=None):
    raw = frame() if raw is None else raw
    response = client.post(f'/v1/sessions/{sid}/document-monitor/ocr', json={
        'image_base64': base64.b64encode(raw).decode(), 'client_action_id': key})
    assert response.status_code == 200, response.text
    return response.json(), raw


def revise(client, sid, artifact, key='visual-revision'):
    return client.post(f'/v1/sessions/{sid}/document-monitor/revisions', json={
        'artifact_id': artifact['artifact_id'], 'content_signature': 'exact-ocr-page',
        'client_action_id': key})


def parent_headers(client):
    r = client.post('/v1/parent/setup', json={'pin': '246810'})
    if r.status_code != 200:
        r = client.post('/v1/parent/unlock', json={'pin': '246810'})
    assert r.status_code == 200, r.text
    return {'X-HSC-Parent': r.json()['parent_token']}


def local_client(path, *, native_launch_attestation_key=None):
    return TestClient(client_for(path, native_launch_attestation_key=native_launch_attestation_key).app, client=('127.0.0.1', 42510))


def test_real_ocr_revision_parent_read_has_exact_unconfirmed_crop(tmp_path):
    with local_client(tmp_path) as client:
        configure(client)
        sid, _ = advance_to_study(client, action_prefix='automatic-visual')
        root = f'/v1/sessions/{sid}/document-monitor'
        assert client.post(root+'/start', json={'client_action_id': 'visual-start'}).status_code == 200
        artifact, raw = capture(client, sid)
        assert artifact['details'].get('visual_capture', {}).get('status') == 'prepared'
        with client.app.state.document_monitoring.database.connect() as c:
            assert c.execute('SELECT local_path FROM capture_artifact WHERE artifact_id=?',
                             (artifact['artifact_id'],)).fetchone()[0] is None
        assert not list((tmp_path / 'media').rglob('*.enc'))
        response = revise(client, sid, artifact)
        assert response.status_code == 200, response.text
        page_source = response.json()['page_source']
        assert page_source == {
            'version': 'document_page_source_v1',
            'status': 'retained',
            'sha256': hashlib.sha256(raw).hexdigest(),
            'byte_length': len(raw),
            'media_type': 'image/png',
            'encrypted': True,
            'local_only': True,
            'cloud_transmission_allowed': False,
            'retention_reason': 'accepted_document_page_revision',
        }
        projection = response.json()['visual_capture']
        assert projection['status'] == 'awaiting_verification'
        assert len(projection['items']) == 1
        item = projection['items'][0]
        assert item['facts_version'] is None and item['fact_status'] == 'unconfirmed'
        binding = item['binding']
        assert binding['artifact_id'] == artifact['artifact_id']
        assert binding['revision_id'] == response.json()['revision_id']
        db = client.app.state.document_monitoring.database
        with db.connect() as c:
            row = c.execute('SELECT local_path,details_json FROM capture_artifact WHERE artifact_id=?',
                            (artifact['artifact_id'],)).fetchone()
            assert row['local_path'] is not None
            stored_page = Path(row['local_path'])
            assert stored_page.is_file()
            encrypted = stored_page.read_bytes()
            assert encrypted.startswith(b'HSC1')
            assert raw not in encrypted
            details = json.loads(row['details_json'])
            assert details['page_source'] == page_source
            region = details['visual_materials'][binding['region_id']]
            assert region['frame_sha256'] == hashlib.sha256(raw).hexdigest()
            assert not region.get('facts')
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0
        assert not list((tmp_path / 'media').glob('*.png.enc'))
        assert len(list((tmp_path / 'media').rglob('*.png.enc'))) == 1
        h = parent_headers(client)
        base = f"/v1/parent/children/{binding['child_id']}/visual-materials"
        detail_url = base+f"/{binding['artifact_id']}/regions/{binding['region_id']}"
        args = {k: binding[k] for k in ('revision_id', 'region_version')}
        listing = client.get(base, headers=h)
        assert listing.status_code == 200, listing.text
        assert len(listing.json()['items']) == 1
        stored = client.post(detail_url+'/storage', headers=h, json={**args,
            'facts_version': None, 'purpose': 'local_visual_material_review_v1',
            'client_action_id': 'explicit-store'})
        assert stored.status_code == 200, stored.text
        image = client.get(detail_url+'/image', headers=h, params={**args,
            'sha256': item['image_descriptor']['sha256']})
        assert image.status_code == 200, image.text
        assert hashlib.sha256(image.content).hexdigest() == item['image_descriptor']['sha256']
        assert image.content != raw
        original_image = image.content
        assert client.post(root+'/stop', json={'client_action_id': 'visual-stop'}).status_code == 200
    with local_client(tmp_path) as client:
        h = parent_headers(client)
        monitor = client.get(root)
        assert monitor.status_code == 200, monitor.text
        assert monitor.json()['latest_revision']['page_source'] == page_source
        image = client.get(detail_url+'/image', headers=h, params={**args,
            'sha256': item['image_descriptor']['sha256']})
        assert image.status_code == 200, image.text
        assert image.content == original_image
        removed = client.request('DELETE',
            f"/v1/parent/capture-artifacts/{artifact['artifact_id']}/raw-media",
            headers=h, json={'confirmation': '确认删除', 'client_action_id': 'page-source-delete-001'})
        assert removed.status_code == 200, removed.text
        assert removed.json()['raw_file_deleted'] is True
        after_delete = client.get(root)
        assert after_delete.status_code == 200, after_delete.text
        assert after_delete.json()['latest_revision']['page_source']['status'] == 'deleted'
        assert not stored_page.exists()
        assert list((tmp_path / 'media').glob('*.png.enc'))  # Explicitly retained crop is independent.


def test_revision_replay_reuses_single_retained_page_source(tmp_path):
    with local_client(tmp_path) as client:
        configure(client)
        sid, root = begin(client)
        artifact, raw = capture(client, sid, key='page-source-replay-frame')
        first = revise(client, sid, artifact, key='page-source-replay-revision')
        assert first.status_code == 200, first.text
        replay = revise(client, sid, artifact, key='page-source-replay-revision')
        assert replay.status_code == 200, replay.text
        assert replay.json()['revision_id'] == first.json()['revision_id']
        assert replay.json()['page_source'] == first.json()['page_source']
        pages = list((tmp_path / 'media').rglob('*.png.enc'))
        assert len(pages) == 1
        with client.app.state.document_monitoring.database.connect() as c:
            path = c.execute('SELECT local_path FROM capture_artifact WHERE artifact_id=?',
                             (artifact['artifact_id'],)).fetchone()[0]
            assert Path(path) == pages[0]
            assert json.loads(c.execute('SELECT details_json FROM capture_artifact WHERE artifact_id=?',
                                        (artifact['artifact_id'],)).fetchone()[0])['page_source']['sha256'] == hashlib.sha256(raw).hexdigest()


def test_jpeg_page_source_keeps_truthful_media_type_and_extension(tmp_path):
    pixels = np.full((600, 800, 3), 245, dtype=np.uint8)
    cv2.putText(pixels, '1. Calculate 12 + 9.', (80, 180), cv2.FONT_HERSHEY_SIMPLEX,
                .9, (0, 0, 0), 2, cv2.LINE_AA)
    encoded, payload = cv2.imencode('.jpg', pixels, [cv2.IMWRITE_JPEG_QUALITY, 94])
    assert encoded
    raw = payload.tobytes()
    with local_client(tmp_path) as client:
        configure(client)
        sid, _ = begin(client)
        artifact, _ = capture(client, sid, key='jpeg-page-source-frame', raw=raw)
        saved = revise(client, sid, artifact, key='jpeg-page-source-revision')
        assert saved.status_code == 200, saved.text
        assert saved.json()['page_source']['media_type'] == 'image/jpeg'
        with client.app.state.document_monitoring.database.connect() as c:
            path = Path(c.execute('SELECT local_path FROM capture_artifact WHERE artifact_id=?',
                                  (artifact['artifact_id'],)).fetchone()[0])
        assert path.suffixes[-2:] == ['.jpg', '.enc']
        assert path.is_file()


def test_revision_failure_removes_unreferenced_page_source(tmp_path, monkeypatch):
    with local_client(tmp_path) as client:
        configure(client)
        sid, _ = begin(client)
        artifact, _ = capture(client, sid, key='page-source-rollback-frame')
        producer = client.app.state.visual_capture

        def fail_after_page_source(*_args, **_kwargs):
            raise RuntimeError('synthetic region publication failure')

        monkeypatch.setattr(producer.host, 'stage_prepared', fail_after_page_source)
        with pytest.raises(RuntimeError, match='synthetic region publication failure'):
            producer.record_revision(sid, artifact['artifact_id'], 'rollback-page',
                                     'page-source-rollback-revision',
                                     expected_generation=producer.monitor.status(sid)['generation'])
        with producer.database.connect() as c:
            row = c.execute('SELECT local_path FROM capture_artifact WHERE artifact_id=?',
                            (artifact['artifact_id'],)).fetchone()
            assert row['local_path'] is None
            assert c.execute('SELECT count(*) FROM document_page_revision').fetchone()[0] == 0
        assert not list((tmp_path / 'media').rglob('*.enc'))


def begin(client):
    configure(client)
    sid, _ = advance_to_study(client, action_prefix='capture-boundary')
    root = f'/v1/sessions/{sid}/document-monitor'
    r = client.post(root+'/start', json={'client_action_id': 'capture-start'})
    assert r.status_code == 200, r.text
    return sid, root


def no_region(client, artifact):
    with client.app.state.document_monitoring.database.connect() as c:
        row = c.execute('SELECT details_json FROM capture_artifact WHERE artifact_id=?', (artifact['artifact_id'],)).fetchone()
        assert not json.loads(row[0]).get('visual_materials')
        assert c.execute("SELECT count(*) FROM capture_artifact WHERE artifact_type='document_visual_region'").fetchone()[0] == 0


def test_slow_real_gate_allows_pause_write_then_rejects_late_candidate(tmp_path, monkeypatch):
    with local_client(tmp_path) as client:
        sid, root = begin(client)
        entered, release = Event(), Event()
        producer = client.app.state.visual_capture
        original = producer.detector.prepare
        def slow(*args, **kwargs):
            entered.set()
            assert release.wait(8)
            return original(*args, **kwargs)
        monkeypatch.setattr(producer.detector, 'prepare', slow)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(client.post, root+'/ocr', json={
                'image_base64': base64.b64encode(frame()).decode(), 'client_action_id': 'slow-crop'})
            try:
                assert entered.wait(8)
                r = client.post(root+'/pause', json={'client_action_id': 'pause-during-gate'})
                assert r.status_code == 200, r.text
            finally:
                release.set()
            assert future.result(timeout=8).status_code == 409
        assert not producer._pending and not producer.host._staged
        with producer.database.connect() as c:
            assert c.execute('SELECT count(*) FROM document_page_revision').fetchone()[0] == 0
        assert not list((tmp_path / 'media').glob('*.png.enc'))


@pytest.mark.parametrize('mutation', ['pause_resume', 'frame_hash', 'name', 'other_child'])
def test_revision_checks_actual_scope_not_client_claims(tmp_path, mutation):
    with local_client(tmp_path) as client:
        sid, root = begin(client)
        artifact, _ = capture(client, sid)
        db = client.app.state.document_monitoring.database
        target_sid = sid
        if mutation == 'pause_resume':
            assert client.post(root+'/pause', json={'client_action_id': 'pause-captured'}).status_code == 200
            assert client.post(root+'/resume', json={'client_action_id': 'resume-captured'}).status_code == 200
        elif mutation == 'other_child':
            switched = client.post(f'/v1/sessions/{sid}/switch-child', json={
                'target_child_id': 'child-primary-4', 'client_action_id': 'other-capture-001'})
            assert switched.status_code == 200, switched.text
            target_sid, _ = advance_to_study(client, child_id='child-primary-4', action_prefix='other-capture')
            assert client.post(f'/v1/sessions/{target_sid}/document-monitor/start',
                               json={'client_action_id': 'other-monitor'}).status_code == 200
        else:
            with db.connect() as c:
                if mutation == 'frame_hash':
                    details = artifact['details']
                    details['monitor_frame_sha256'] = 'f'*64
                    c.execute('UPDATE capture_artifact SET details_json=? WHERE artifact_id=?',
                              (json.dumps(details), artifact['artifact_id']))
                else:
                    c.execute("UPDATE child_profile SET nickname='new private name' WHERE child_id='child-primary-4'")
                c.commit()
        r = revise(client, target_sid, artifact)
        assert r.status_code == 409, r.text
        no_region(client, artifact)
        if mutation in {'pause_resume', 'other_child'}:
            fresh, _ = capture(client, target_sid, key='fresh-owned-frame')
            accepted = revise(client, target_sid, fresh, key='fresh-owned-revision')
            assert accepted.status_code == 200, accepted.text
            binding = accepted.json()['visual_capture']['items'][0]['binding']
            assert binding['session_id'] == target_sid
            assert binding['child_id'] == ('child-primary-4' if mutation == 'other_child' else 'child-junior-3')


def test_ttl_capacity_no_reprepare_on_old_key_and_staged_expiry(tmp_path):
    with local_client(tmp_path) as client:
        sid, root = begin(client)
        producer = client.app.state.visual_capture
        now = [producer.clock()]
        producer.clock = lambda: now[0]
        producer.host._stage_clock = lambda: now[0]
        artifacts = [capture(client, sid, key=f'bounded-frame-{i}')[0] for i in range(5)]
        assert len(producer._pending) == 4
        assert artifacts[4]['details']['visual_capture']['reason'] == 'VISUAL_PREPARATION_CAPACITY'
        now[0] += 121
        producer.sweep()
        assert not producer._pending
        repeated, _ = capture(client, sid, key='bounded-frame-0')
        assert repeated['artifact_id'] == artifacts[0]['artifact_id']
        assert not producer._pending
        expired = revise(client, sid, artifacts[0])
        assert expired.status_code == 409
        assert expired.json()['detail'] == 'PAGE_SOURCE_PREPARATION_UNAVAILABLE'
        assert not list((tmp_path / 'media').rglob('*.enc'))
        current, _ = capture(client, sid, key='fresh-after-ttl')
        r = revise(client, sid, current, key='fresh-revision')
        assert r.status_code == 200, r.text
        assert producer.host._staged
        for batch in producer._pending.values():
            assert batch.frame_bytes
        now[0] += 301
        producer.sweep()
        assert not producer.host._staged
        assert not list((tmp_path / 'media').glob('*.png.enc'))


def test_failed_staging_rolls_back_revision_region_and_cache_then_same_key_recovers(tmp_path, monkeypatch):
    with local_client(tmp_path) as client:
        sid, _ = begin(client)
        artifact, _ = capture(client, sid)
        producer = client.app.state.visual_capture
        original = producer.host.stage_prepared
        def fail_after_stage(*args, **kwargs):
            original(*args, **kwargs)
            raise sqlite3.OperationalError('isolated disk failure')
        with monkeypatch.context() as m:
            m.setattr(producer.host, 'stage_prepared', fail_after_stage)
            with pytest.raises(sqlite3.OperationalError, match='isolated disk failure'):
                revise(client, sid, artifact)
        no_region(client, artifact)
        assert not producer.host._staged
        with producer.database.connect() as c:
            assert c.execute('SELECT count(*) FROM document_page_revision').fetchone()[0] == 0
        r = revise(client, sid, artifact)
        assert r.status_code == 200, r.text
        again = revise(client, sid, artifact)
        assert again.status_code == 200, again.text
        assert again.json()['visual_capture'] == r.json()['visual_capture']
        with producer.database.connect() as c:
            assert c.execute('SELECT count(*) FROM document_page_revision').fetchone()[0] == 1


@pytest.mark.parametrize('kind', ['blank', 'color'])
def test_unsupported_actual_ocr_page_keeps_text_but_no_fake_roi(tmp_path, kind):
    with local_client(tmp_path) as client:
        sid, _ = begin(client)
        pixels = cv2.imdecode(np.frombuffer(frame(), np.uint8), cv2.IMREAD_COLOR)
        if kind == 'blank': pixels[180:560,180:610] = 255
        else: cv2.rectangle(pixels,(250,240),(540,480),(0,0,255),6)
        raw = cv2.imencode('.png',pixels)[1].tobytes()
        artifact, _ = capture(client, sid, raw=raw)
        assert artifact['details']['visual_capture']['status'] == 'unsupported'
        assert artifact['extracted_text']
        r = revise(client, sid, artifact)
        assert r.status_code == 200, r.text
        assert r.json()['visual_capture']['items'] == []
        no_region(client, artifact)


def test_stop_and_delete_clear_unretained_bytes_not_durable_stored_images(tmp_path):
    with local_client(tmp_path) as client:
        sid, root = begin(client)
        artifact, _ = capture(client, sid)
        assert revise(client, sid, artifact).status_code == 200
        producer = client.app.state.visual_capture
        assert producer.host._staged
        assert client.post(root+'/stop', json={'client_action_id': 'clear-visual-stop'}).status_code == 200
        assert not producer.host._staged and not producer._pending
        assert client.post(root+'/resume', json={'client_action_id': 'new-generation'}).status_code == 200
        another, _ = capture(client, sid, key='capture-before-delete')
        client.app.state.privacy_lifecycle.delete_capture_artifact(
            another['artifact_id'], '确认删除', 'delete-transient-visual')
        producer.sweep()
        assert not producer._pending and not producer.host._staged
        assert not list((tmp_path / 'media').glob('*.png.enc'))


def test_client_roi_safe_and_facts_cannot_choose_or_certify_region(tmp_path):
    with local_client(tmp_path) as client:
        sid, root = begin(client)
        raw = frame()
        r = client.post(root+'/ocr', json={'image_base64':base64.b64encode(raw).decode(),
            'client_action_id':'untrusted-roi-frame', 'roi':[0,0,800,800], 'safe':True,
            'facts':{'answer':'client claim'}})
        assert r.status_code == 200, r.text
        result = revise(client, sid, r.json())
        assert result.status_code == 200, result.text
        item = result.json()['visual_capture']['items'][0]
        assert item['facts_version'] is None and item['fact_status'] == 'unconfirmed'
        assert item['image_descriptor']['width'] < 500
        with client.app.state.visual_capture.database.connect() as c:
            details = json.loads(c.execute('SELECT details_json FROM capture_artifact WHERE artifact_id=?',
                                          (r.json()['artifact_id'],)).fetchone()[0])
            assert 'client claim' not in json.dumps(details)
            assert details['visual_materials'][item['binding']['region_id']]['region_xyxy'] != [0,0,800,800]


def test_restart_drops_unretained_candidates_and_does_not_reprepare_old_ocr_key(tmp_path):
    with local_client(tmp_path) as client:
        sid, root = begin(client)
        artifact, _ = capture(client, sid)
        original = client.app.state.visual_capture
        assert original._pending
    assert not original._pending and not original.host._staged
    with local_client(tmp_path) as client:
        configure(client)
        assert not client.app.state.visual_capture._pending
        assert client.post(root+'/resume', json={'client_action_id':'after-restart-resume'}).status_code == 200
        prior, _ = capture(client, sid)
        assert prior['artifact_id'] == artifact['artifact_id']
        assert not client.app.state.visual_capture._pending
        old_revision = revise(client, sid, artifact)
        assert old_revision.status_code == 409, old_revision.text
        assert old_revision.json()['detail'] == 'PAGE_SOURCE_PREPARATION_UNAVAILABLE'
        no_region(client, artifact)
        fresh, _ = capture(client, sid, key='after-restart-fresh-frame')
        accepted = revise(client, sid, fresh, key='after-restart-revision')
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()['visual_capture']['items'][0]['binding']['artifact_id'] == fresh['artifact_id']
        assert not list((tmp_path / 'media').glob('*.png.enc'))
