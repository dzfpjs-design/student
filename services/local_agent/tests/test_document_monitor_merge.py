"""Synthetic concurrent OCR publication preserves other durable evidence."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from hashlib import sha256
import json
import pytest

from home_study_agent.document_report_source_guard import persist_monitor_details
from home_study_agent.repository import utc_now
from test_document_trial_capture import fixture as trial_fixture
from test_document_report_source_effect_guard import add_revision, cloud


def setup_source(tmp_path):
    db, trial, image, fields = trial_fixture(tmp_path)
    frame = trial.capture('one-session', image, **{**fields, 'reason': 'ocr_capture'})
    digest = sha256(image).hexdigest()
    base = {'monitor_frame_sha256': digest, 'monitor_generation': fields['generation'],
            'cloud_vision_fallback': cloud('baseline'), 'other_evidence': {'preserve': 1}}
    with db.connect() as c:
        c.execute("INSERT INTO capture_artifact VALUES('ocr','one','one-session','document_ocr',NULL,'',.4,?,'quality_warning',?,'ocr-action')", (json.dumps(base), utc_now()))
        c.commit()
    add_revision(db, 'one', 'one-session', 'ocr')
    return db, trial, frame, digest, fields, base


def read_details(db):
    with db.connect() as c:
        return json.loads(c.execute("SELECT details_json FROM capture_artifact WHERE artifact_id='ocr'").fetchone()[0])


def test_trial_binding_and_other_details_survive_stale_monitor_publication(tmp_path):
    db, trial, frame, digest, fields, baseline = setup_source(tmp_path)
    trial.bind_ocr('one-session', frame['artifact_id'], 'ocr', digest)
    changes = {'monitor_frame_sha256': digest, 'monitor_generation': fields['generation'], 'monitor_capture_phase': 'study'}
    result = persist_monitor_details(db, 'one-session', 'ocr', changes, baseline=baseline)
    assert result['details']['trial_frame_id'] == frame['artifact_id']
    assert result['details']['other_evidence'] == {'preserve': 1}
    assert read_details(db)['cloud_vision_fallback'] == baseline['cloud_vision_fallback']
    replay = persist_monitor_details(db, 'one-session', 'ocr', changes, baseline=baseline)
    assert replay == result


def test_concurrent_cloud_conflict_preserves_current_and_is_repeatable(tmp_path):
    db, trial, frame, digest, fields, baseline = setup_source(tmp_path)
    newer = {**cloud('newer'), 'privacy': {'original_frame_uploaded': False, 'policy': 'newer'}}
    proposed = cloud('stale proposed')
    with db.connect() as c:
        c.execute("UPDATE capture_artifact SET details_json=? WHERE artifact_id='ocr'", (json.dumps({**baseline, 'cloud_vision_fallback': newer, 'new_metadata': 42}),))
        c.commit()
    changes = {'monitor_frame_sha256': digest, 'monitor_generation': fields['generation'], 'cloud_vision_fallback': proposed}
    result = persist_monitor_details(db, 'one-session', 'ocr', changes, baseline=baseline)
    assert result['cloud_candidate_publication']['status'] == 'existing_preserved_conflict'
    assert result['details']['cloud_vision_fallback'] == newer
    assert result['details']['new_metadata'] == 42
    assert result['details']['monitor_cloud_publication'] == result['cloud_candidate_publication']
    assert persist_monitor_details(db, 'one-session', 'ocr', changes, baseline=baseline) == result
    assert read_details(db)['cloud_vision_fallback'] == newer


def test_monitor_cannot_overwrite_trial_or_other_owned_fields(tmp_path):
    db, trial, frame, digest, fields, baseline = setup_source(tmp_path)
    trial.bind_ocr('one-session', frame['artifact_id'], 'ocr', digest)
    with pytest.raises(ValueError, match='DOCUMENT_OCR_MONITOR_FIELDS_INVALID'):
        persist_monitor_details(db, 'one-session', 'ocr', {'trial_frame_id': 'wrong-frame'}, baseline=baseline)
    assert read_details(db)['trial_frame_id'] == frame['artifact_id']


def test_conflict_after_wait_rechecks_cloud_and_preserves_trial_binding(tmp_path):
    db, trial, frame, digest, fields, baseline = setup_source(tmp_path)
    proposed = cloud('proposed')
    current = cloud('concurrent')
    changes = {'monitor_frame_sha256': digest, 'monitor_generation': fields['generation'], 'cloud_vision_fallback': proposed}
    with ThreadPoolExecutor(max_workers=2) as pool, db.external_effect_guard():
        future = pool.submit(persist_monitor_details, db, 'one-session', 'ocr', changes, baseline=baseline)
        try:
            future.result(timeout=.3)
            assert False, 'changing a frozen cloud source should wait for the sender'
        except TimeoutError:
            pass
        pool.submit(trial.bind_ocr, 'one-session', frame['artifact_id'], 'ocr', digest).result(timeout=2)
        with db.connect() as c:
            details = json.loads(c.execute("SELECT details_json FROM capture_artifact WHERE artifact_id='ocr'").fetchone()[0])
            details['cloud_vision_fallback'] = current
            c.execute("UPDATE capture_artifact SET details_json=? WHERE artifact_id='ocr'", (json.dumps(details),))
            c.commit()
    result = future.result(timeout=5)
    assert result['cloud_candidate_publication']['status'] == 'existing_preserved_conflict'
    assert result['details']['trial_frame_id'] == frame['artifact_id']
    assert result['details']['cloud_vision_fallback'] == current


def test_http_conflict_preserves_latest_candidate_and_same_action_replays(tmp_path, monkeypatch):
    import base64
    from test_app import advance_to_study, client_for
    from test_visual_capture_producer import LocalLines, configure
    from test_visual_material import frame as synthetic_image

    class WeakLines(LocalLines):
        def recognize(self, image, route):
            return [{'text': 'synthetic question', 'confidence': .1,
                     'box': [[110,110],[530,110],[530,140],[110,140]]}]

    with client_for(tmp_path, agent_runtime_enabled=True) as client:
        configure(client)
        client.app.state.local_ocr.engines = [WeakLines()]
        sid, _ = advance_to_study(client, child_id='child-primary-4')
        assert client.post(f'/v1/sessions/{sid}/document-monitor/start', json={'client_action_id': 'merge-start'}).status_code == 200
        db = client.app.state.document_monitoring.database
        with db.connect() as c:
            c.execute('UPDATE privacy_preference SET cloud_model_text_allowed=1 WHERE singleton_id=1')
            c.commit()
        calls = []
        current = {**cloud('concurrent answer'), 'privacy': {'original_frame_uploaded': False, 'gate': 'newer'}}
        def provider(_image, *, request_id, **kwargs):
            calls.append(request_id)
            artifact_id = request_id.removeprefix('artifact:')
            with db.connect() as c:
                details = json.loads(c.execute('SELECT details_json FROM capture_artifact WHERE artifact_id=?', (artifact_id,)).fetchone()[0])
                details.update(cloud_vision_fallback=current, trial_frame_id='concurrent-synthetic-frame')
                c.execute('UPDATE capture_artifact SET details_json=? WHERE artifact_id=?', (json.dumps(details), artifact_id))
                c.commit()
            return cloud('older response')
        monkeypatch.setattr(client.app.state.document_vision, 'analyze_page', provider)
        body = {'image_base64': base64.b64encode(synthetic_image()).decode(), 'client_action_id': 'merge-ocr'}
        route = f'/v1/sessions/{sid}/document-monitor/ocr'
        first = client.post(route, json=body)
        assert first.status_code == 200, first.text
        saved = first.json()
        assert saved['cloud_candidate_publication']['status'] == 'existing_preserved_conflict'
        assert saved['details']['cloud_vision_fallback'] == current
        assert saved['details']['trial_frame_id'] == 'concurrent-synthetic-frame'
        receipt = client.get(f'/v1/sessions/{sid}/document-monitor/receipts/ocr/merge-ocr')
        assert receipt.json()['status'] == 'saved'
        again = client.post(route, json=body)
        assert again.status_code == 200, again.text
        replay = again.json()
        assert replay['artifact_id'] == saved['artifact_id']
        assert replay['cloud_candidate_publication'] == saved['cloud_candidate_publication']
        assert replay['details']['cloud_vision_fallback'] == current
        assert calls == ['artifact:' + saved['artifact_id']]
