"""A previously frozen cloud page cannot change during an external send."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from hashlib import sha256
import json
from threading import Event

import pytest

from home_study_agent.daily_report_frozen_dependencies import _hash
from home_study_agent.repository import utc_now
from test_document_trial_capture import fixture as trial_fixture
from test_document_vision import context, seed_artifact, service, normal_provider_owner


def cloud(text):
    return {'status': 'needs_review', 'blocks': [{'text': text}],
            'privacy': {'original_frame_uploaded': False}}


def add_revision(db, child, session, artifact):
    with db.connect() as c:
        monitor = c.execute('SELECT monitor_id FROM document_monitor_session WHERE session_id=?', (session,)).fetchone()
        if not monitor:
            c.execute("INSERT INTO document_monitor_session(monitor_id,child_id,session_id,status,started_at,updated_at) VALUES('monitor',?,?,'active',?,?)", (child, session, utc_now(), utc_now()))
            monitor_id = 'monitor'
        else:
            monitor_id = monitor[0]
        c.execute("""INSERT INTO document_page_revision(revision_id,monitor_id,child_id,session_id,artifact_id,revision_index,
            content_signature,content_sha256,change_summary_json,ocr_confidence,review_required,created_at,client_action_id)
            VALUES('revision',?,?,?,?,1,'synthetic',?,'{}',.4,1,?,'source-action')""", (monitor_id, child, session, artifact, sha256(b'').hexdigest(), utc_now()))
        c.commit()


def test_cloud_candidate_replacement_waits_without_holding_database_writer(context, monkeypatch):
    _, db = context
    artifact = seed_artifact(db, text='')
    with db.connect() as c:
        details = {**artifact['details'], 'cloud_vision_fallback': cloud('old synthetic page')}
        c.execute('UPDATE capture_artifact SET details_json=? WHERE artifact_id=?', (json.dumps(details), artifact['artifact_id']))
        c.commit()
    add_revision(db, 'child-one', 'session-one', artifact['artifact_id'])
    with db.connect() as c:
        digest = _hash(c, 'child-one', 'cloud_page', 'revision')
    vision = service(context)
    reached_provider = Event()
    def completed_provider(*args, **kwargs):
        reached_provider.set()
        return cloud('new synthetic page')
    monkeypatch.setattr(vision, 'analyze_page', completed_provider)
    with ThreadPoolExecutor(max_workers=2) as pool, db.external_effect_guard():
        future = pool.submit(vision._enrich_artifact, 'session-one', artifact, b'synthetic', True)
        assert reached_provider.wait(2)
        with pytest.raises(TimeoutError): future.result(timeout=.3)
        # A lock waiting for SMTP must not hold the SQLite writer.
        def ordinary_write():
            with db.connect() as c:
                c.execute("UPDATE child_profile SET nickname=nickname WHERE child_id='child-one'")
                c.commit()
        pool.submit(ordinary_write).result(timeout=3)
        with db.connect() as c: assert _hash(c, 'child-one', 'cloud_page', 'revision') == digest
    future.result(timeout=5)
    with db.connect() as c: assert _hash(c, 'child-one', 'cloud_page', 'revision') != digest


def test_trial_binding_preserves_frozen_cloud_source_without_waiting(tmp_path):
    db, trial, image, fields = trial_fixture(tmp_path)
    frame = trial.capture('one-session', image, **{**fields, 'reason': 'ocr_capture'})
    image_hash = sha256(image).hexdigest()
    with db.connect() as c:
        c.execute("INSERT INTO capture_artifact VALUES('ocr','one','one-session','document_ocr',NULL,'',.4,?,'quality_warning',?,'ocr-action')",
                  (json.dumps({'monitor_frame_sha256': image_hash, 'cloud_vision_fallback': cloud('old synthetic page')}), utc_now()))
        c.commit()
    add_revision(db, 'one', 'one-session', 'ocr')
    with db.connect() as c: digest = _hash(c, 'one', 'cloud_page', 'revision')
    with ThreadPoolExecutor(max_workers=1) as pool, db.external_effect_guard():
        pool.submit(trial.bind_ocr, 'one-session', frame['artifact_id'], 'ocr', image_hash).result(timeout=2)
        with db.connect() as c: assert _hash(c, 'one', 'cloud_page', 'revision') == digest


def test_first_cloud_candidate_does_not_wait_for_submission(context, monkeypatch):
    _, db = context
    artifact = seed_artifact(db, text='')
    add_revision(db, 'child-one', 'session-one', artifact['artifact_id'])
    vision = service(context)
    monkeypatch.setattr(vision, 'analyze_page', lambda *args, **kwargs: cloud('new synthetic page'))
    with ThreadPoolExecutor(max_workers=1) as pool, db.external_effect_guard():
        result = pool.submit(vision._enrich_artifact, 'session-one', artifact, b'synthetic', True).result(timeout=2)
        assert result['details']['cloud_vision_fallback']['blocks'][0]['text'] == 'new synthetic page'


def test_new_trial_binding_does_not_wait_for_submission(tmp_path):
    db, trial, image, fields = trial_fixture(tmp_path)
    frame = trial.capture('one-session', image, **{**fields, 'reason': 'ocr_capture'})
    image_hash = sha256(image).hexdigest()
    with db.connect() as c:
        c.execute("INSERT INTO capture_artifact VALUES('ocr','one','one-session','document_ocr',NULL,'',.4,?,'quality_warning',?,'ocr-action')",
                  (json.dumps({'monitor_frame_sha256': image_hash}), utc_now()))
        c.commit()
    add_revision(db, 'one', 'one-session', 'ocr')
    with ThreadPoolExecutor(max_workers=1) as pool, db.external_effect_guard():
        pool.submit(trial.bind_ocr, 'one-session', frame['artifact_id'], 'ocr', image_hash).result(timeout=2)
    with db.connect() as c:
        assert json.loads(c.execute("SELECT details_json FROM capture_artifact WHERE artifact_id='ocr'").fetchone()[0])['trial_frame_id'] == frame['artifact_id']


def test_vision_busy_retry_reuses_completed_provider_receipt(context, normal_provider_owner):
    import httpx
    from home_study_agent.database import ExternalEffectBusyError
    from test_document_vision import response_body, synthetic_frame

    _, db = context
    artifact = seed_artifact(db, text='')
    with db.connect() as c:
        details = {**artifact['details'], 'cloud_vision_fallback': cloud('old synthetic page')}
        c.execute('UPDATE capture_artifact SET details_json=? WHERE artifact_id=?', (json.dumps(details), artifact['artifact_id']))
        c.commit()
    add_revision(db, 'child-one', 'session-one', artifact['artifact_id'])
    calls = []
    def provider(request):
        calls.append(1)
        return httpx.Response(200, json=response_body())
    vision = service(context, provider)
    frame = synthetic_frame()
    with ThreadPoolExecutor(max_workers=1) as pool, db.external_effect_guard():
        with pytest.raises(ExternalEffectBusyError):
            pool.submit(vision._enrich_artifact, 'session-one', artifact, frame, True).result(timeout=8)
    assert len(calls) == 1
    result = vision._enrich_artifact('session-one', artifact, frame, True)
    assert result['details']['cloud_vision_fallback']['blocks']
    assert len(calls) == 1



def test_monitor_replacement_waits_for_existing_cloud_source(context):
    from home_study_agent.document_report_source_guard import persist_monitor_details
    _, db = context
    artifact = seed_artifact(db, text='')
    details = {**artifact['details'], 'cloud_vision_fallback': cloud('old synthetic page')}
    with db.connect() as c:
        c.execute('UPDATE capture_artifact SET details_json=? WHERE artifact_id=?', (json.dumps(details), artifact['artifact_id']))
        c.commit()
    add_revision(db, 'child-one', 'session-one', artifact['artifact_id'])
    with db.connect() as c: digest = _hash(c, 'child-one', 'cloud_page', 'revision')
    with ThreadPoolExecutor(max_workers=2) as pool, db.external_effect_guard():
        future = pool.submit(persist_monitor_details, db, 'session-one', artifact['artifact_id'],
                             {'cloud_vision_fallback': cloud('new synthetic page')}, baseline=details)
        with pytest.raises(TimeoutError): future.result(timeout=.3)
        def ordinary_write():
            with db.connect() as c:
                c.execute("UPDATE child_profile SET nickname=nickname WHERE child_id='child-one'")
                c.commit()
        pool.submit(ordinary_write).result(timeout=3)
        with db.connect() as c: assert _hash(c, 'child-one', 'cloud_page', 'revision') == digest
    future.result(timeout=5)
    with db.connect() as c: assert _hash(c, 'child-one', 'cloud_page', 'revision') != digest


def test_pipeline_metadata_preserves_frozen_cloud_source_without_waiting(context, monkeypatch):
    from home_study_agent.local_ocr.workflow import LocalOcrWorkflow
    _, db = context
    artifact = seed_artifact(db, text='')
    details = {**artifact['details'], 'cloud_vision_fallback': cloud('old synthetic page')}
    with db.connect() as c:
        c.execute('UPDATE capture_artifact SET details_json=? WHERE artifact_id=?', (json.dumps(details), artifact['artifact_id']))
        c.commit()
    add_revision(db, 'child-one', 'session-one', artifact['artifact_id'])
    with db.connect() as c: digest = _hash(c, 'child-one', 'cloud_page', 'revision')
    class Media:
        database = db
        def _artifact_for_workflow(self, artifact_id): return {'artifact_id': artifact_id}
    workflow = object.__new__(LocalOcrWorkflow)
    workflow.media_ai = Media()
    monkeypatch.setattr(workflow, '_pipeline_details', lambda *args: {'engine': 'synthetic'})
    with ThreadPoolExecutor(max_workers=1) as pool, db.external_effect_guard():
        pool.submit(workflow._attach_pipeline, artifact, {'job_id': 'synthetic'}, {}).result(timeout=2)
        with db.connect() as c: assert _hash(c, 'child-one', 'cloud_page', 'revision') == digest


@pytest.mark.parametrize('entry', ['monitor', 'pipeline'])
def test_new_ocr_publication_does_not_wait_for_submission(context, monkeypatch, entry):
    from home_study_agent.document_report_source_guard import persist_monitor_details
    from home_study_agent.local_ocr.workflow import LocalOcrWorkflow

    _, db = context
    artifact = seed_artifact(db, text='')
    add_revision(db, 'child-one', 'session-one', artifact['artifact_id'])
    if entry == 'monitor':
        def operation():
            persist_monitor_details(db, 'session-one', artifact['artifact_id'], {'monitor_generation': 3}, baseline=artifact['details'])
    else:
        class Media:
            database = db
            def _artifact_for_workflow(self, artifact_id):
                return {'artifact_id': artifact_id}
        workflow = object.__new__(LocalOcrWorkflow)
        workflow.media_ai = Media()
        monkeypatch.setattr(workflow, '_pipeline_details', lambda *args: {'engine': 'synthetic'})
        def operation():
            workflow._attach_pipeline(artifact, {'job_id': 'synthetic'}, {})
    with ThreadPoolExecutor(max_workers=1) as pool, db.external_effect_guard():
        pool.submit(operation).result(timeout=2)


def test_frozen_cloud_hash_ignores_local_metadata_but_detects_source_changes(context):
    _, db = context
    artifact = seed_artifact(db, text='')
    details = {**artifact['details'], 'cloud_vision_fallback': cloud('old synthetic page')}
    with db.connect() as c:
        c.execute('UPDATE capture_artifact SET details_json=? WHERE artifact_id=?', (json.dumps(details), artifact['artifact_id']))
        c.commit()
    add_revision(db, 'child-one', 'session-one', artifact['artifact_id'])
    with db.connect() as c: original = _hash(c, 'child-one', 'cloud_page', 'revision')
    metadata = {**details, 'trial_frame_id': 'frame', 'visual_capture': {'status': 'prepared'}, 'local_pipeline': {'engine': 'cpu'}}
    with db.connect() as c:
        c.execute('UPDATE capture_artifact SET details_json=? WHERE artifact_id=?', (json.dumps(metadata), artifact['artifact_id']))
        c.commit()
        assert _hash(c, 'child-one', 'cloud_page', 'revision') == original
    variants = [
        cloud('new synthetic page'),
        {**cloud('old synthetic page'), 'status': 'blocked'},
        {**cloud('old synthetic page'), 'privacy': {'original_frame_uploaded': True}},
        {**cloud('old synthetic page'), 'privacy': {'original_frame_uploaded': False, 'gate': 'new'}},
    ]
    for candidate in variants:
        with db.connect() as c:
            c.execute('UPDATE capture_artifact SET details_json=? WHERE artifact_id=?',
                      (json.dumps({**metadata, 'cloud_vision_fallback': candidate}), artifact['artifact_id']))
            c.commit()
            assert _hash(c, 'child-one', 'cloud_page', 'revision') != original


def test_legacy_full_details_hash_fails_closed(context):
    from home_study_agent.daily_report_frozen_dependencies import _CLOUD_PAGE, valid
    _, db = context
    artifact = seed_artifact(db, text='')
    with db.connect() as c:
        c.execute('UPDATE capture_artifact SET details_json=? WHERE artifact_id=?',
                  (json.dumps({'cloud_vision_fallback': cloud('old synthetic page')}), artifact['artifact_id']))
        c.commit()
    add_revision(db, 'child-one', 'session-one', artifact['artifact_id'])
    with db.connect() as c:
        c.execute('BEGIN')
        row = c.execute(_CLOUD_PAGE, ('child-one', 'revision')).fetchone()
        old_bytes = json.dumps(['cloud_page', 'child-one', list(row)], ensure_ascii=False,
                               separators=(',', ':'), allow_nan=False).encode()
        old = sha256(old_bytes).hexdigest()
        current = _hash(c, 'child-one', 'cloud_page', 'revision')
        assert old != current
        assert not valid(c, 'child-one', [('cloud_page', 'revision', old)])
        assert valid(c, 'child-one', [('cloud_page', 'revision', current)])


def test_privacy_value_type_change_is_guarded(context):
    from home_study_agent.document_report_source_guard import persist_monitor_details
    _, db = context
    artifact = seed_artifact(db, text='')
    previous = {**artifact['details'], 'cloud_vision_fallback': {
        **cloud('old synthetic page'), 'privacy': {'original_frame_uploaded': False, 'provider_verified': True}}}
    newer = {**artifact['details'], 'cloud_vision_fallback': {
        **cloud('old synthetic page'), 'privacy': {'original_frame_uploaded': False, 'provider_verified': 1}}}
    with db.connect() as c:
        c.execute('UPDATE capture_artifact SET details_json=? WHERE artifact_id=?', (json.dumps(previous), artifact['artifact_id']))
        c.commit()
    add_revision(db, 'child-one', 'session-one', artifact['artifact_id'])
    with db.connect() as c: digest = _hash(c, 'child-one', 'cloud_page', 'revision')
    with ThreadPoolExecutor(max_workers=1) as pool, db.external_effect_guard():
        future = pool.submit(persist_monitor_details, db, 'session-one', artifact['artifact_id'],
                             {'cloud_vision_fallback': newer['cloud_vision_fallback']}, baseline=previous)
        with pytest.raises(TimeoutError): future.result(timeout=.3)
        with db.connect() as c: assert _hash(c, 'child-one', 'cloud_page', 'revision') == digest
    future.result(timeout=5)
    with db.connect() as c: assert _hash(c, 'child-one', 'cloud_page', 'revision') != digest


def test_visual_capture_metadata_preserves_cloud_source_without_waiting(tmp_path):
    from home_study_agent.visual_capture_producer import VisualCaptureProducer, _Batch
    db, trial, image, fields = trial_fixture(tmp_path)
    image_hash = sha256(image).hexdigest()
    details = {'monitor_frame_sha256': image_hash, 'monitor_generation': fields['generation'],
               'cloud_vision_fallback': cloud('old synthetic page')}
    with db.connect() as c:
        c.execute("INSERT INTO capture_artifact VALUES('ocr','one','one-session','document_ocr',NULL,'',.4,?,'quality_warning',?,'ocr-action')",
                  (json.dumps(details), utc_now()))
        c.commit()
    add_revision(db, 'one', 'one-session', 'ocr')
    producer = VisualCaptureProducer(db, None, trial.monitor, detector=object(), clock=lambda: 0)
    batch = _Batch('one', 'one-session', 'ocr', image_hash, image, fields['generation'], (), None,
                   10, utc_now(), None)
    with db.connect() as c: digest = _hash(c, 'one', 'cloud_page', 'revision')
    with ThreadPoolExecutor(max_workers=1) as pool, db.external_effect_guard():
        result = pool.submit(producer.publish, batch).result(timeout=2)
        assert 'visual_capture' in result
        with db.connect() as c: assert _hash(c, 'one', 'cloud_page', 'revision') == digest
