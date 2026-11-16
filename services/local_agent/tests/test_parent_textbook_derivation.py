"""Synthetic raster evidence, real local storage/HTTP; no family media."""
import base64
import json
from io import BytesIO

from PIL import Image
import pytest

from home_study_agent.database import Database
from home_study_agent.knowledge_graph.parent_textbook_capture import ParentTextbookCaptureService, CaptureConflictError
from test_parent_textbook_capture import Adapter, setup


def page():
    image = Image.new('RGB', (120, 80), 'white')
    image.paste('red', (0, 0, 60, 80))
    stream = BytesIO()
    image.save(stream, format='PNG')
    return stream.getvalue()


class RecordingAdapter(Adapter):
    def run_ocr_claim(self, claim, image):
        self.images = getattr(self, 'images', []) + [image]
        return super().run_ocr_claim(claim, image)


def test_original_is_not_cropped_and_retry_recreates_same_derived_pixels(tmp_path):
    database = Database(tmp_path/'parent.db')
    setup(database)
    adapter = RecordingAdapter()
    service = ParentTextbookCaptureService(database, adapter)
    lease = service.acquire('child-4', 'bound-document-hash', {})['lease_id']
    raw = page()
    transform = {'rotation':90, 'crop':{'left':50, 'right':0, 'top':0, 'bottom':0}}
    encoded = base64.b64encode(raw).decode()
    artifact = service.recognize('child-4', lease, encoded, {'title':'四上英语'}, 'parent-transform-action', image_transform=transform)
    assert service.source_image('child-4', artifact['capture_ref'])[0] == raw
    derived = Image.open(BytesIO(adapter.images[0]))
    assert derived.size == (80,60) and derived.getpixel((40,30)) == (255,255,255)
    assert adapter.images[0] != raw
    assert service.recognize('child-4', lease, encoded, {'title':'四上英语'}, 'parent-transform-action', image_transform=transform)['capture_ref'] == artifact['capture_ref']
    assert adapter.enqueued == 1
    with pytest.raises(CaptureConflictError):
        service.recognize('child-4', lease, encoded, {'title':'四上英语'}, 'parent-transform-action', image_transform={**transform,'rotation':180})
    restarted = ParentTextbookCaptureService(database, adapter)
    restarted.retry_requested('child-4', artifact['capture_ref'], 'parent-transform-retry')
    assert adapter.images == [adapter.images[0], adapter.images[0]]
    assert restarted.source_image('child-4', artifact['capture_ref'])[0] == raw


@pytest.mark.parametrize('transform', [
    {'rotation':45}, {'rotation':True}, {'rotation':float('nan')},
    {'crop':{'left':float('nan')}}, {'crop':{'top':-1}},
    {'crop':{'left':50,'right':50}}, {'crop':{'unknown':3}}, {'unknown':'source'},
])
def test_bad_transform_does_not_create_artifact_or_enqueue_ocr(tmp_path, transform):
    database = Database(tmp_path/'parent.db')
    setup(database)
    adapter = RecordingAdapter()
    service = ParentTextbookCaptureService(database, adapter)
    lease = service.acquire('child-4', 'bound-document-hash', {})['lease_id']
    with pytest.raises(ValueError):
        service.recognize('child-4',lease,base64.b64encode(page()).decode(),{'title':'Page'},'bad-transform-command',image_transform=transform)
    assert adapter.enqueued == 0
    with database.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM kg_parent_textbook_ocr_artifact').fetchone()[0] == 0


def test_retry_cannot_adopt_a_mutated_derivation_event(tmp_path):
    database = Database(tmp_path/'parent.db')
    setup(database)
    adapter = RecordingAdapter()
    service = ParentTextbookCaptureService(database, adapter)
    lease = service.acquire('child-4', 'bound-document-hash', {})['lease_id']
    raw = page()
    artifact = service.recognize('child-4',lease,base64.b64encode(raw).decode(),{'title':'Page'},
        'original-page-parameters',image_transform={'rotation':90})
    with database.connect() as c:
        row = c.execute("SELECT event_id,payload_json FROM kg_parent_textbook_ocr_review_event WHERE event_type='created'").fetchone()
        payload = json.loads(row['payload_json'])
        payload['request']['image_transform']['rotation'] = 180
        c.execute('UPDATE kg_parent_textbook_ocr_review_event SET payload_json=? WHERE event_id=?',
            (json.dumps(payload),row['event_id']))
        c.commit()
    with pytest.raises(ValueError, match='provenance changed'):
        service.retry_requested('child-4',artifact['capture_ref'],'reject-altered-derivation')
    assert adapter.enqueued == 1
    assert service.source_image('child-4',artifact['capture_ref'])[0] == raw
    assert service.pending('child-4')[0]['status'] == 'needs_review'


def test_http_transform_keeps_authenticated_original_and_retry_after_restart(tmp_path):
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from home_study_agent.knowledge_graph.http_api import install_knowledge_graph_routes
    from test_parent_textbook_capture import Auth
    database = Database(tmp_path/'parent.db')
    setup(database)
    adapter = RecordingAdapter()
    def app():
        application = FastAPI()
        application.state.ocr_workflow = SimpleNamespace(adapter=adapter)
        install_knowledge_graph_routes(application, database, Auth())
        return application
    headers = {'X-HSC-Parent':'ok'}
    root = '/v1/parent/knowledge-graph/textbook-capture'
    raw = page()
    with TestClient(app()) as client:
        lease = client.post(root+'/child-4/leases',headers=headers,json={
            'system_id_hash':'bound-document-hash'}).json()['lease_id']
        payload = {'lease_id':lease,'image_base64':base64.b64encode(raw).decode(),
            'image_transform':{'rotation':90},'page_context':{'title':'四上英语'},
            'client_action_id':'parent-http-derived-page'}
        denied = client.post(root+'/child-4/ocr',json=payload)
        assert denied.status_code == 401
        first = client.post(root+'/child-4/ocr',headers=headers,json=payload)
        assert first.status_code == 200, first.text
        ref = first.json()['artifact']['capture_ref']
        source = client.get(root+f'/child-4/artifacts/{ref}/source',headers=headers)
        assert source.content == raw and source.headers['cache-control'] == 'no-store'
        assert Image.open(BytesIO(adapter.images[0])).size == (80,120)
    with TestClient(app()) as client:
        retained = client.get(root+'/child-4/artifacts',headers=headers).json()['items']
        assert retained[0]['capture_ref'] == ref
        retry = client.post(root+f'/child-4/artifacts/{ref}/retry',headers=headers,
            json={'client_action_id':'parent-http-derive-retry'})
        assert retry.status_code == 200, retry.text
        assert adapter.images[1] == adapter.images[0]
        corrected = client.put(root+f'/child-4/artifacts/{ref}/review',headers=headers,
            json={'extracted_text':'Words: apple','client_action_id':'parent-http-derive-review'})
        assert corrected.status_code == 200
        imported = client.post('/v1/parent/knowledge-graph/textbook-pages',headers=headers,json={
            'title':'四上英语','ocr_text':'Words: apple','child_id':'child-4','capture_ref':ref,
            'client_action_id':'parent-http-derive-import'})
        assert imported.status_code == 200, imported.text
    with TestClient(app()) as client:
        assert client.get(root+f'/child-4/artifacts/{ref}/source',headers=headers).content == raw
    with database.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM kg_parent_textbook_ocr_artifact').fetchone()[0] == 1
        assert c.execute('PRAGMA foreign_key_check').fetchall() == []
