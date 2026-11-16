"""Synthetic paper pixels and real SQLite revisions; no model or skill claims."""
import hashlib
import json

import cv2
import numpy as np
import pytest

from home_study_agent.database import Database
from home_study_agent.local_supply import digest, LocalSupplyError
from test_document_monitoring import _service, _artifact
from test_document_vision import RegionEngine


def frame(diagonal=False):
    pixels = np.full((800, 800, 3), 50, dtype=np.uint8)
    cv2.rectangle(pixels, (80, 60), (720, 740), (255, 255, 255), -1)
    cv2.rectangle(pixels, (250, 240), (540, 480), (0, 0, 0), 3)
    if diagonal:
        cv2.line(pixels, (250, 240), (540, 480), (0, 0, 0), 3)
    return cv2.imencode('.png', pixels)[1].tobytes()


def visual_case(tmp_path, *, diagonal=False, grade='初三'):
    db, monitor = _service(tmp_path)
    monitor.start('study', 'start')
    raw = frame(diagonal)
    _artifact(db, 'page', 'Observe the figure.')
    with db.connect() as c:
        c.execute('UPDATE child_profile SET grade=?', (grade,))
        c.execute('UPDATE capture_artifact SET details_json=? WHERE artifact_id=?',
                  (json.dumps({'monitor_frame_sha256': hashlib.sha256(raw).hexdigest(),
                               'monitor_generation': monitor.status('study')['generation']}), 'page'))
        c.commit()
    rev = monitor.record_revision('study', 'page', 'same-ocr', 'revision')
    return db, monitor, raw, dict(child_id='child', session_id='study', artifact_id='page', revision_id=rev['revision_id'])


def registered(db, raw, scope):
    from home_study_agent.document_vision import DiagramRegionGate
    from home_study_agent.visual_material import record_region
    with db.connect() as c:
        names = tuple(r[0] for r in c.execute('SELECT nickname FROM child_profile'))
    prepared = DiagramRegionGate(RegionEngine()).prepare_region(raw,region_xyxy=[200,180,600,560],known_child_names=names)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        details = json.loads(c.execute("SELECT details_json FROM capture_artifact WHERE artifact_id='page'").fetchone()[0])
        result = record_region(c, **scope, prepared=prepared,
            region_id='figure', expected_details_version=digest(details))
        c.commit()
        return result


def confirmed(db, region, scope):
    from home_study_agent.visual_material import record_facts, confirm_facts
    facts = {'version': 'visual_facts_v1', 'observations': [
        {'id': 'boundary', 'kind': 'shape', 'description': 'A closed four-sided outline.',
         'bbox': [0.1, 0.1, 0.9, 0.9]}], 'uncertainties': []}
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        candidate = record_facts(c, **scope, region_id='figure', region_version=region['region_version'],
            facts=facts, expected_facts_version=None, client_action_id='facts')
        result = confirm_facts(c, **scope, region_id='figure', region_version=region['region_version'],
            facts_version=candidate['facts_version'], decision='confirm', client_action_id='review')
        c.commit()
        return result


def test_same_ocr_different_pixels_change_question_identity(tmp_path):
    from home_study_agent.visual_material import visual_question_identity
    regions = []
    for label, diagonal in [('plain', False), ('diagonal', True)]:
        childpath = tmp_path / label
        childpath.mkdir()
        db, _, raw, scope = visual_case(childpath, diagonal=diagonal)
        regions.append(registered(db, raw, scope))
    assert regions[0]['safe_png_sha256'] != regions[1]['safe_png_sha256']
    assert visual_question_identity('Observe the figure.', regions[0]) != visual_question_identity('Observe the figure.', regions[1])
    assert all(not r['cloud_transmission_allowed'] and not r['mastery_claim_allowed'] for r in regions)


def test_unconfirmed_then_exact_confirmed_survives_restart_without_score(tmp_path):
    from home_study_agent.visual_material import resolve_visual_material
    db, _, raw, scope = visual_case(tmp_path)
    region = registered(db, raw, scope)
    with db.connect() as c, pytest.raises(LocalSupplyError, match='CONFIRMATION_REQUIRED'):
        resolve_visual_material(c, child_id='child', binding=region['binding'])
    current = confirmed(db, region, scope)
    with Database(db.path).connect() as c:
        result = resolve_visual_material(c, child_id='child', binding=current['binding'])
        assert result['status'] == 'ready_local'
        assert result['image_availability'] == 'host_unverified'
        assert result['parent_confirmation_ready'] is False
        assert result['facts']['observations'][0]['kind'] == 'shape'
        assert not result['mastery_claim_allowed'] and not result['cloud_transmission_allowed']
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM learning_evidence').fetchone()[0] == 0


@pytest.mark.parametrize('fault', ['child', 'page', 'pixels', 'retract', 'facts'])
def test_exact_visual_binding_rejects_changed_sources(tmp_path, fault):
    from home_study_agent.visual_material import resolve_visual_material, confirm_facts
    db, _, raw, scope = visual_case(tmp_path)
    region = registered(db, raw, scope)
    result = confirmed(db, region, scope)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        binding = dict(result['binding'])
        child = 'other' if fault == 'child' else 'child'
        if fault == 'page': binding['revision_id'] = 'old-page'
        if fault in {'pixels', 'facts'}:
            details = json.loads(c.execute("SELECT details_json FROM capture_artifact WHERE artifact_id='page'").fetchone()[0])
            if fault == 'pixels': details['monitor_frame_sha256'] = '0' * 64
            else: details['visual_materials']['figure']['facts']['observations'][0]['description'] = 'changed'
            c.execute('UPDATE capture_artifact SET details_json=?', (json.dumps(details),))
        if fault == 'retract':
            confirm_facts(c, **scope, region_id='figure', region_version=region['region_version'],
                facts_version=result['binding']['facts_version'], decision='retract', client_action_id='retract')
        with pytest.raises(LocalSupplyError): resolve_visual_material(c, child_id=child, binding=binding)
        c.commit()


def test_late_visual_response_cannot_bind_new_page(tmp_path):
    from home_study_agent.visual_material import record_facts
    db, monitor, raw, scope = visual_case(tmp_path)
    region = registered(db, raw, scope)
    _artifact(db, 'next-page', 'New page.')
    monitor.record_revision('study', 'next-page', 'next', 'next-revision')
    with db.connect() as c, pytest.raises(LocalSupplyError, match='PAGE_NOT_CURRENT'):
        c.execute('BEGIN IMMEDIATE')
        record_facts(c, **scope, region_id='figure', region_version=region['region_version'],
            facts={'version': 'visual_facts_v1', 'observations': [], 'uncertainties': []},
            expected_facts_version=None, client_action_id='late')


def test_client_safe_flag_is_not_a_gate(tmp_path):
    from home_study_agent.visual_material import record_region
    db, _, raw, scope = visual_case(tmp_path)
    with db.connect() as c, pytest.raises(LocalSupplyError, match='PREPARED_REQUIRED'):
        c.execute('BEGIN IMMEDIATE')
        record_region(c, **scope, region_id='figure', expected_details_version='x', prepared={'safe': True})


def test_frame_cas_idempotency_and_borrowed_rollback(tmp_path):
    from home_study_agent.document_vision import DiagramRegionGate
    from home_study_agent.visual_material import record_region, record_facts, confirm_facts
    db, _, raw, scope = visual_case(tmp_path)
    gate = DiagramRegionGate(RegionEngine())
    good = gate.prepare_region(raw,region_xyxy=[200,180,600,560],known_child_names=('学生',))
    changed = gate.prepare_region(frame(True),region_xyxy=[200,180,600,560],known_child_names=('学生',))
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        before = c.execute('SELECT details_json FROM capture_artifact').fetchone()[0]
        kwargs = dict(**scope, region_id='figure',
                      expected_details_version=digest(json.loads(before)))
        with pytest.raises(LocalSupplyError, match='FRAME_CHANGED'):
            record_region(c, **kwargs, prepared=changed)
        assert c.execute('SELECT details_json FROM capture_artifact').fetchone()[0] == before
        region = record_region(c, **kwargs, prepared=good)
        assert record_region(c, **kwargs, prepared=good) == region
        with pytest.raises(LocalSupplyError, match='CAS_CONFLICT'):
            record_region(c, **{**kwargs, 'region_id':'another', 'expected_details_version':'old'}, prepared=good)
        c.rollback()
        assert c.execute('SELECT details_json FROM capture_artifact').fetchone()[0] == before
    region = registered(db, raw, scope)
    facts = dict(version='visual_facts_v1', observations=[dict(id='edge',kind='shape',description='Four edges.',bbox=[0,0,1,1])],uncertainties=[])
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        args = dict(**scope, region_id='figure',region_version=region['region_version'],facts=facts,
                    expected_facts_version=None,client_action_id='f')
        result = record_facts(c, **args)
        assert record_facts(c, **args) == result
        with pytest.raises(LocalSupplyError, match='REQUEST_CONFLICT'):
            record_facts(c, **{**args,'facts':{**facts,'uncertainties':['scale unclear']}})
        confirm_facts(c, **scope, region_id='figure',region_version=region['region_version'],
                      facts_version=result['facts_version'],decision='confirm',client_action_id='approve')
        assert c.execute('SELECT COUNT(*) FROM audit_event').fetchone()[0] == 1
        c.rollback()
        assert c.execute('SELECT COUNT(*) FROM audit_event').fetchone()[0] == 0
        assert 'facts' not in json.loads(c.execute('SELECT details_json FROM capture_artifact').fetchone()[0])['visual_materials']['figure']


def test_uncertain_candidate_is_available_without_parent_but_never_scores(tmp_path):
    from home_study_agent.visual_material import read_visual_candidate, record_facts, confirm_facts
    db, _, raw, scope = visual_case(tmp_path)
    region = registered(db, raw, scope)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        candidate = record_facts(c, **scope,region_id='figure',region_version=region['region_version'],
            facts=dict(version='visual_facts_v1',observations=[dict(id='edge',kind='shape',
                description='Possibly four sides.',bbox=[0,0,1,1])],uncertainties=['Check the corner mark.']),
            expected_facts_version=None,client_action_id='candidate')
        result = read_visual_candidate(c,child_id='child',artifact_id='page',revision_id=scope['revision_id'],
            region_id='figure',region_version=region['region_version'])
        assert result['can_explain_uncertain'] and result['status'] == 'needs_clarification'
        assert result['parent_confirmation_ready'] is False and result['image_availability'] == 'host_unverified'
        assert not result['can_score'] and not result['can_update_mastery'] and not result['cloud_transmission_allowed']
        with pytest.raises(LocalSupplyError, match='UNCERTAINTY_REQUIRES_REVIEW'):
            confirm_facts(c, **scope,region_id='figure',region_version=region['region_version'],
                facts_version=candidate['facts_version'],decision='confirm',client_action_id='blind-approve')
        c.commit()
    with Database(db.path).connect() as c:
        assert c.execute('SELECT COUNT(*) FROM audit_event').fetchone()[0] == 0


@pytest.mark.parametrize('fault', ['room', 'photo', 'background', 'full_page', 'name', 'detector'])
def test_real_pixel_gate_refuses_unsafe_or_unproven_regions(fault, monkeypatch):
    from home_study_agent.document_vision import DiagramRegionGate, DocumentVisionError
    raw, bounds = frame(), [200, 180, 600, 560]
    ocr = RegionEngine('Name: Alice' if fault=='name' else 'visible mark')
    if fault == 'room':
        pixels = np.full((800,800,3),50,dtype=np.uint8)
        cv2.line(pixels,(200,180),(600,560),(0,0,0),5)
        raw = cv2.imencode('.png',pixels)[1].tobytes()
    if fault == 'photo':
        pixels = cv2.imdecode(np.frombuffer(raw,np.uint8),cv2.IMREAD_COLOR)
        pixels[200:540,220:580] = np.tile(np.arange(360,dtype=np.uint16)*255//359,(340,1))[...,None].astype(np.uint8)
        raw = cv2.imencode('.png',pixels)[1].tobytes()
    if fault == 'background': bounds = [50,180,600,560]
    if fault == 'full_page': bounds = [95,75,705,725]
    if fault == 'detector':
        class MissingDetector:
            def empty(self): return True
        monkeypatch.setattr(cv2,'CascadeClassifier',lambda path:MissingDetector())
    with pytest.raises(DocumentVisionError):
        DiagramRegionGate(ocr).prepare_region(raw,region_xyxy=bounds)


def test_detected_face_veto_is_not_a_redaction_permission(monkeypatch):
    """Injected detector result tests veto, not real-world face recall."""
    from home_study_agent.document_vision import DiagramRegionGate, DocumentVisionError
    class Detector:
        def empty(self): return False
        def detectMultiScale(self,*args,**kwargs): return [(10,10,40,40)]
    monkeypatch.setattr(cv2,'CascadeClassifier',lambda path:Detector())
    with pytest.raises(DocumentVisionError,match='FACE_DETECTED'):
        DiagramRegionGate(RegionEngine()).prepare_region(frame(),region_xyxy=[200,180,600,560])


def test_broken_detector_is_controlled_unavailable_not_a_safe_image(monkeypatch):
    from home_study_agent.document_vision import DiagramRegionGate, DocumentVisionError
    class Broken:
        def empty(self): return False
        def detectMultiScale(self,*args,**kwargs): raise cv2.error('isolated broken detector')
    monkeypatch.setattr(cv2,'CascadeClassifier',lambda path:Broken())
    with pytest.raises(DocumentVisionError,match='DETECTOR_UNAVAILABLE'):
        DiagramRegionGate(RegionEngine()).prepare_region(frame(),region_xyxy=[200,180,600,560])


def test_new_gate_preserves_non_ocr_geometry_but_does_not_relax_text_v3():
    from home_study_agent.document_vision import DiagramRegionGate, PageOnlySafetyGate, DocumentVisionError, PRIVACY_GATE_VERSION
    class NoText:
        def recognize(self,*args): return []
    prepared = DiagramRegionGate(NoText()).prepare_region(frame(True),region_xyxy=[200,180,600,560])
    decoded = cv2.imdecode(np.frombuffer(prepared.png,np.uint8),cv2.IMREAD_GRAYSCALE)
    assert decoded.shape == (380,400) and decoded[60,50] == 0 and decoded[299,339] == 0
    assert PRIVACY_GATE_VERSION == 'rectified_full_page_binary_ink.v3'
    with pytest.raises(DocumentVisionError): PageOnlySafetyGate(NoText()).prepare(frame(True))
