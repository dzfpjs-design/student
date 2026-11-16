"""Synthetic pixels, not physical-camera or handwriting recognition evidence."""
from __future__ import annotations

import cv2
import numpy as np
import pytest

from home_study_agent.local_media_ai import LocalMediaAI
from home_study_agent.local_ocr.service import _prepare_page


def paper(*, script=False, border=True, background=255, ink=25, sparse=False):
    image = np.full((800, 640, 3), background, np.uint8)
    if border:
        image[:] = 55
        image[20:780, 20:620] = background
    font = cv2.FONT_HERSHEY_SCRIPT_SIMPLEX if script else cv2.FONT_HERSHEY_SIMPLEX
    lines = ['Dear Ben, our concert was on Friday.',
             'I enjoyed the music and the violin.',
             'Please join me at the next concert.',
             'We can meet at the school gate.', 'Best wishes, Mia.']
    if sparse:
        lines = ['Mia']
    for i, line in enumerate(lines):
        cv2.putText(image, line, (45, 100 + i * 50), font, .75,
                    (ink, ink, ink), 2, cv2.LINE_AA)
    return image


def clipped_page(*, script=False, background=255, soft=False):
    image = paper(script=script, background=background)
    if soft:
        yy, xx = np.mgrid[:800, :640]
        light = 300 * np.exp(-((xx - 330) / 90) ** 4 - ((yy - 163) / 100) ** 4)
        image = np.clip(image.astype(float) + light[..., None], 0, 255).astype(np.uint8)
    else:
        # A real destructive pixel replacement, not an injected quality result.
        image[70:219, 277:393] = 255
    return image


def qualities(image):
    _, pipeline, _ = _prepare_page(image)
    capture = LocalMediaAI._image_quality(image)
    return pipeline, capture, LocalMediaAI._merge_ocr_quality(capture, pipeline)


@pytest.mark.parametrize('script', [False, True], ids=['printed', 'script-strokes'])
@pytest.mark.parametrize('border,background,sparse', [
    (True, 255, False), (False, 255, False), (True, 245, False), (False, 255, True),
])
def test_pristine_white_paper_is_not_content_loss(script, border, background, sparse):
    for quality in qualities(paper(script=script, border=border, background=background, sparse=sparse)):
        assert quality['passed'], quality
        assert 'glare' not in quality['issues']


@pytest.mark.parametrize('script', [False, True])
@pytest.mark.parametrize('background,soft', [(255, False), (240, False), (255, True)])
def test_local_clipped_patch_across_text_keeps_glare_review(script, background, soft):
    image = clipped_page(script=script, background=background, soft=soft)
    assert np.count_nonzero(image != paper(script=script, background=background)) > 100
    pipeline, capture, merged = qualities(image)
    for quality in (pipeline, capture, merged):
        assert not quality['passed'], quality
        assert 'glare' in quality['issues'], quality


@pytest.mark.parametrize('border', [False, True])
def test_blank_white_page_is_unverifiable_not_proven_glare(border):
    image = np.full((800, 640, 3), 255, np.uint8)
    if border:
        image[:20] = image[-20:] = 55
        image[:, :20] = image[:, -20:] = 55
    for quality in qualities(image):
        assert not quality['passed']
        assert 'content_unverifiable' in quality['issues'], quality
        assert 'glare' not in quality['issues'], quality


def test_faint_pencil_stays_reviewable_even_with_sharp_page_border():
    for quality in qualities(paper(ink=237)):
        assert not quality['passed']
        assert 'low_contrast' in quality['issues'], quality


@pytest.mark.parametrize('kind', ['dark', 'blur'])
def test_dark_and_blurred_content_still_require_review(kind):
    image = paper()
    if kind == 'dark':
        image = (image.astype(float) * .12).astype(np.uint8)
    else:
        image = cv2.GaussianBlur(image, (31, 31), 9)
    assert all(not quality['passed'] for quality in qualities(image))


def test_preexisting_capture_warning_is_never_cleared_by_pipeline():
    _, good, _ = _prepare_page(paper())
    merged = LocalMediaAI._merge_ocr_quality(
        {'passed': False, 'issues': ['possible_hand_occlusion'], 'messages': []}, good)
    assert not merged['passed']
    assert 'possible_hand_occlusion' in merged['issues']


@pytest.mark.parametrize('script', [False, True])
def test_bright_patch_in_unused_margin_does_not_imply_text_loss(script):
    image = paper(script=script, background=240)
    image[440:620, 270:395] = 255
    assert all(quality['passed'] for quality in qualities(image))


def test_legitimate_columns_are_uncertain_not_proven_glare():
    image = np.full((800, 640, 3), 255, np.uint8)
    for y in (100, 150, 200):
        for x in (40, 350):
            cv2.putText(image, 'Mia Ben', (x, y), cv2.FONT_HERSHEY_SIMPLEX, .7, (25, 25, 25), 2)
    for quality in qualities(image):
        assert not quality['passed']
        assert 'glare' not in quality['issues']
        assert 'content_integrity_uncertain' in quality['issues']


def test_single_line_hole_keeps_uncertainty_without_ocr_vote():
    image = paper()
    image[70:110, 277:393] = 255
    assert all(not quality['passed'] for quality in qualities(image))


def test_pixel_analysis_is_bounded_at_large_camera_resolution():
    from home_study_agent.local_ocr.service import _page_content_quality
    gray = cv2.cvtColor(cv2.resize(paper(), (3840, 4800)), cv2.COLOR_BGR2GRAY)
    _, metrics = _page_content_quality(gray)
    assert metrics['analysis_pixels'] <= 960 * 960


def test_thin_diagonal_does_not_crash_band_analysis():
    image = np.full((800, 640, 3), 255, np.uint8)
    for i in range(30):
        image[100+i, 100+i] = 25
    assert all(not quality['passed'] for quality in qualities(image))


def test_old_quality_job_is_not_reused_or_silently_reapproved(tmp_path, monkeypatch):
    import home_study_agent.local_ocr.service as module
    from home_study_agent.local_ocr import LocalOcrService
    from home_study_agent.local_ocr.cache_lifecycle import OcrSourceInvalid
    from test_local_ocr_pipeline import FakeEngine

    _, encoded = cv2.imencode('.png', paper())
    payload = encoded.tobytes()
    with monkeypatch.context() as context:
        context.setattr(module, 'OCR_PIPELINE_VERSION', 5)
        old = LocalOcrService(tmp_path / 'old-quality.db', [FakeEngine()])
        saved = old.run(old.enqueue(payload, 'old-action')['job_id'], payload)
    current = LocalOcrService(old.path, [FakeEngine()])
    with pytest.raises(OcrSourceInvalid, match='OCR_PIPELINE_VERSION_INVALIDATED'):
        current.enqueue(payload, 'old-action')
    queued = current.enqueue(payload, 'explicit-new-action')
    assert queued['status'] == 'queued'
    result = current.run(queued['job_id'], payload)['result']
    assert result['pipeline_version'] == module.OCR_PIPELINE_VERSION
    assert result['pipeline_version'] != saved['result']['pipeline_version']


@pytest.mark.parametrize('prepared', [False, True], ids=['direct-capture', 'pipeline'])
@pytest.mark.parametrize('kind', ['pristine', 'glare', 'blank', 'faint'])
def test_quality_cap_revision_and_same_key_survive_restart(tmp_path, prepared, kind):
    import json
    from home_study_agent.database import Database
    from home_study_agent.document_monitoring import DocumentMonitoringService
    from home_study_agent.writing_ocr_authorization import _document
    from test_document_monitoring import _service

    db, monitor = _service(tmp_path)
    monitor.start('study', 'start')
    image = {'pristine': paper, 'glare': clipped_page,
             'blank': lambda: np.full((800, 640, 3), 255, np.uint8),
             'faint': lambda: paper(ink=237)}[kind]()
    ok, encoded = cv2.imencode('.png', image)
    assert ok
    calls = []
    class HighConfidenceEngine:
        name = 'controlled-not-real-ocr'
        def recognize(self, image, route):
            calls.append(route)
            return [{'text': 'This deliberately confident OCR cannot clear image warnings.',
                     'confidence': .999, 'box': [[45, 70], [590, 70], [590, 101], [45, 101]]}]
    media = LocalMediaAI(db, tmp_path, tmp_path / 'no-models')
    media._ocr_engine = HighConfidenceEngine()
    result = None
    if prepared:
        _, quality, _ = _prepare_page(image)
        result = {'quality': quality, 'lines': HighConfidenceEngine().recognize(image, 'test'),
                  'engine': HighConfidenceEngine.name}
    artifact = media.ocr('study', encoded.tobytes(), 'original-ocr', False, result)
    revision = monitor.record_revision('study', artifact['artifact_id'], 'frame-signature', 'revision')
    expected_review = kind != 'pristine'
    assert artifact['status'] == ('quality_warning' if expected_review else 'completed')
    assert artifact['confidence'] == (.49 if expected_review else .999)
    assert revision['review_required'] is expected_review
    assert len(calls) == 1

    restarted = Database(tmp_path / 'study.db')
    resumed = LocalMediaAI(restarted, tmp_path, tmp_path / 'no-models')
    assert resumed.ocr('study', encoded.tobytes(), 'original-ocr', False)['artifact_id'] == artifact['artifact_id']
    assert DocumentMonitoringService(restarted).status('study')['latest_revision']['revision_id'] == revision['revision_id']
    with restarted.connect() as c:
        row = c.execute('SELECT * FROM capture_artifact').fetchone()
        assert json.loads(row['details_json'])['ocr_confidence'] == .999
        assert c.execute('SELECT COUNT(*) FROM capture_artifact').fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM document_page_revision').fetchone()[0] == 1
        if expected_review:
            with pytest.raises(ValueError, match='DOCUMENT_REVIEW_REQUIRED'):
                _document(c, 'child', 'study', artifact['artifact_id'], revision['revision_id'], 0, 4)
    assert not (tmp_path / 'media').exists()
