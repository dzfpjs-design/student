import base64
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

import cv2
import numpy as np
import pytest

from home_study_agent.local_ocr.service import RapidOcrEngine, _marked_question_candidates, _segment_questions
from home_study_agent.local_ocr.text_geometry import build
from test_app import advance_to_study
from test_visual_capture_producer import capture, configure, local_client, revise


def worksheet(*, blue=True):
    image = np.full((800, 800, 3), 245, dtype=np.uint8)
    cv2.putText(image, '1. Find the discriminant', (110, 134), cv2.FONT_HERSHEY_SIMPLEX,
                .8, (20, 20, 20), 2)
    cv2.putText(image, '= 9', (112, 182), cv2.FONT_HERSHEY_SIMPLEX,
                .9, (235, 45, 15) if blue else (20, 20, 20), 2)
    cv2.line(image, (173, 158), (193, 180), (10, 25, 245), 3)
    cv2.line(image, (193, 158), (173, 180), (10, 25, 245), 3)
    cv2.putText(image, '2. Another question', (110, 320), cv2.FONT_HERSHEY_SIMPLEX,
                .8, (20, 20, 20), 2)
    lines = [
        {'text': text, 'confidence': .98, 'box': [[x,y], [end,y], [end,bottom], [x,bottom]]}
        for text, x, y, end, bottom in (
            ('1. 求一元二次方程根的判别式', 108, 108, 420, 136),
            ('= 9', 108, 156, 164, 184),
            ('X', 170, 154, 196, 184),
            ('2. 求另一道题', 108, 291, 386, 324),
        )
    ]
    raw = cv2.imencode('.png', image)[1].tobytes()
    blocks = _segment_questions(lines, 800, 800)
    marks = _marked_question_candidates(image, blocks, lines=lines)
    return raw, lines, blocks, marks


def add_ocr_facts(client, sid, action_id, *, blue):
    raw, lines, blocks, marks = worksheet(blue=blue)
    assert marks and ('strict_capture_facts' in marks[0]) is blue
    artifact, _ = capture(client, sid, action_id, raw=raw)
    database = client.app.state.document_monitoring.database
    with database.connect() as connection:
        stored = connection.execute('SELECT details_json FROM capture_artifact WHERE artifact_id=?',
                                    (artifact['artifact_id'],)).fetchone()[0]
        details = json.loads(stored)
        details['lines'] = lines
        details['local_pipeline'].update(
            question_blocks=blocks, correction_mark_candidates=marks, wrong_question_candidates=marks,
            quality={'orientation_evidence': {'ambiguous': False}},
            text_geometry=build(lines, frame_sha256=sha256(raw).hexdigest(), frame_size=[800,800],
                                prepared_size=[800,800], frame_to_prepared=np.eye(3).tolist(), degrees=0),
        )
        connection.execute('''UPDATE capture_artifact SET extracted_text=?,confidence=.98,status='completed',details_json=?
                              WHERE artifact_id=?''', ('\n'.join(line['text'] for line in lines),
                                                        json.dumps(details), artifact['artifact_id']))
        connection.commit()
    return artifact


def test_wrong_question_automatically_saves_crop_in_same_revision(tmp_path):
    with local_client(tmp_path) as client:
        configure(client)
        sid, _ = advance_to_study(client)
        root = f'/v1/sessions/{sid}/document-monitor'
        assert client.post(root + '/start', json={'client_action_id':'start-auto'}).status_code == 200
        artifact = add_ocr_facts(client, sid, 'page-with-graded-answer', blue=True)
        response = revise(client, sid, artifact)
        assert response.status_code == 200, response.text
        revision = response.json()
        assert revision['queued_mistake_candidate_count'] == 0
        mistake, = revision['auto_saved_mistakes']
        mistake_id = mistake['mistake_id']
        url = f'/v1/children/child-junior-3/mistakes/{mistake_id}/question-crop'
        crop_response = client.get(url)
        assert crop_response.status_code == 200
        assert crop_response.headers['cache-control'] == 'no-store'
        from home_study_agent.mistake_learning import _source
        with client.app.state.document_monitoring.database.connect() as connection:
            source_revision = _source(connection, 'child-junior-3', mistake_id)[1]
        assert client.get(url, params={'source_revision': source_revision}).json() == crop_response.json()
        stale = client.get(url, params={'source_revision': '0' * 64})
        assert stale.status_code == 409
        assert stale.json()['detail']['code'] == 'MISTAKE_LEARNING_SOURCE_CHANGED'
        assert stale.headers['cache-control'] == 'no-store'
        assert client.get(url.replace('child-junior-3', 'child-primary-4')).status_code == 404
        raw = base64.b64decode(crop_response.json()['image_base64'])
        crop = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
        assert crop.shape == (76, 312, 3)
        assert client.get(root).json()['latest_revision']['auto_saved_mistakes'] == [mistake]
        assert revise(client, sid, artifact).json()['auto_saved_mistakes'] == [mistake]
        repeat = add_ocr_facts(client, sid, 'new-frame-same-question', blue=True)
        assert revise(client, sid, repeat, 'repeat-revision').json()['auto_saved_mistakes'] == []
        with client.app.state.document_monitoring.database.connect() as connection:
            cases = connection.execute('SELECT * FROM mistake_case').fetchall()
            assert len(cases) == 1 and cases[0]['status'] == 'diagnosing'
            assert cases[0]['submitted_answer'] == '9'
            assert cases[0]['prompt_summary'] == '1. 求一元二次方程根的判别式'
            assert connection.execute('SELECT COUNT(*) FROM paper_scoring_decision').fetchone()[0] == 1
            stored = connection.execute('SELECT encrypted_jpeg FROM mistake_question_crop').fetchone()[0]
            assert stored != raw and raw not in stored


def test_mark_without_strict_answer_binding_remains_pending(tmp_path):
    with local_client(tmp_path) as client:
        configure(client)
        sid, _ = advance_to_study(client)
        root = f'/v1/sessions/{sid}/document-monitor'
        assert client.post(root + '/start', json={'client_action_id':'start-pending'}).status_code == 200
        artifact = add_ocr_facts(client, sid, 'uncertain-answer', blue=False)
        response = revise(client, sid, artifact)
        assert response.status_code == 200, response.text
        revision = response.json()
        assert revision['auto_saved_mistakes'] == []
        assert revision['queued_mistake_candidate_count'] == 1
        focused = add_ocr_facts(client, sid, 'clearer-answer', blue=True)
        revised = revise(client, sid, focused, 'clearer-revision').json()
        assert revised['queued_mistake_candidate_count'] == 0 and len(revised['auto_saved_mistakes']) == 1
        with client.app.state.document_monitoring.database.connect() as connection:
            candidates = connection.execute('SELECT status FROM mistake_capture_candidate').fetchall()
            assert [row['status'] for row in candidates] == ['saved']


def test_failed_crop_write_rolls_back_score_case_and_revision(tmp_path):
    with local_client(tmp_path) as client:
        configure(client)
        sid, _ = advance_to_study(client)
        root = f'/v1/sessions/{sid}/document-monitor'
        assert client.post(root + '/start', json={'client_action_id':'start-atomic'}).status_code == 200
        artifact = add_ocr_facts(client, sid, 'atomic-crop', blue=True)
        database = client.app.state.document_monitoring.database
        with database.connect() as connection:
            connection.execute('''CREATE TRIGGER simulated_crop_failure BEFORE INSERT ON mistake_question_crop
                BEGIN SELECT RAISE(ABORT,'synthetic crop write failed'); END''')
        with pytest.raises(sqlite3.IntegrityError, match='synthetic crop write failed'):
            revise(client, sid, artifact)
        with database.connect() as connection:
            for table in ('document_page_revision','mistake_capture_candidate','paper_scoring_request',
                          'paper_scoring_decision','mistake_case','visual_answer_evidence_promotion'):
                assert connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0, table
            connection.execute('DROP TRIGGER simulated_crop_failure')
        response = revise(client, sid, artifact)
        assert response.status_code == 200 and len(response.json()['auto_saved_mistakes']) == 1


def test_real_rapidocr_qualifies_synthetic_marked_worksheet(tmp_path):
    if not RapidOcrEngine().health()['ready']:
        pytest.skip('Local RapidOCR model assets are unavailable')
    raw = (Path(__file__).parent / 'fixtures' / 'marked-answer-auto-capture.png').read_bytes()
    with local_client(tmp_path) as client:
        sid, _ = advance_to_study(client)
        root = f'/v1/sessions/{sid}/document-monitor'
        assert client.post(root + '/start', json={'client_action_id':'real-ocr-start'}).status_code == 200
        artifact, _ = capture(client, sid, 'real-ocr-frame', raw=raw)
        assert artifact['details']['local_pipeline']['engine'] == 'rapidocr-onnx'
        response = revise(client, sid, artifact, 'real-ocr-revision')
        assert response.status_code == 200, response.text
        mistake, = response.json()['auto_saved_mistakes']
        crop_url = f"/v1/children/child-junior-3/mistakes/{mistake['mistake_id']}/question-crop"
        crop = client.get(crop_url).json()
        assert 0 < crop['width'] < 1200 and 0 < crop['height'] < 1050
    with local_client(tmp_path) as reopened:
        assert reopened.get(crop_url).json() == crop
        reopened.app.state.privacy_lifecycle.delete_capture_artifact_raw_media(
            artifact['artifact_id'], '确认删除', 'delete-synthetic-marked-source',
        )
        assert reopened.get(crop_url).status_code == 404
