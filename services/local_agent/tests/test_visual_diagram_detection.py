"""Discovery from real pixels; a candidate is not a semantic interpretation."""
import cv2
import numpy as np
import pytest

from home_study_agent.document_vision import DiagramRegionGate, is_prepared_diagram
from home_study_agent.visual_diagram_detection import MonochromeDiagramDetector
from test_document_vision import RegionEngine
from test_visual_material import frame


def encoded(pixels):
    return cv2.imencode('.png', pixels)[1].tobytes()


def detector(text=''):
    return MonochromeDiagramDetector(DiagramRegionGate(RegionEngine(text)))


@pytest.mark.parametrize('shape', ['rectangle', 'triangle', 'connected_rectangles'])
def test_pixels_select_actual_geometry_bounds_not_whole_page(shape):
    pixels = cv2.imdecode(np.frombuffer(frame(), np.uint8), cv2.IMREAD_COLOR)
    pixels[180:560, 180:610] = 255
    if shape == 'rectangle':
        cv2.rectangle(pixels, (290, 290), (560, 500), (0, 0, 0), 3)
    elif shape == 'triangle':
        cv2.polylines(pixels, [np.array([[300,230],[530,490],[200,490]])], True, (0,0,0), 3)
    else:
        cv2.rectangle(pixels, (210,270), (320,400), (0,0,0), 3)
        cv2.rectangle(pixels, (450,270), (560,400), (0,0,0), 3)
        cv2.line(pixels, (320,330), (450,330), (0,0,0), 3)
    result = detector().prepare(encoded(pixels), known_child_names=('Alice',))
    assert result.prepared, result.reason
    for prepared in result.prepared:
        assert is_prepared_diagram(prepared)
        assert prepared.png != encoded(pixels)
        assert prepared.width < 500 and prepared.height < 500
        assert prepared.region_xyxy[0] > 100 and prepared.region_xyxy[1] > 100


@pytest.mark.parametrize('fault', ['blank', 'text', 'color', 'room', 'photo', 'name', 'detector'])
def test_unsupported_or_unsafe_does_not_create_a_crop(fault, monkeypatch):
    pixels = cv2.imdecode(np.frombuffer(frame(), np.uint8), cv2.IMREAD_COLOR)
    if fault == 'blank':
        pixels[180:560,180:610] = 255
    elif fault == 'text':
        pixels[180:560,180:610] = 255
        cv2.putText(pixels, '123 ABC', (170,360), cv2.FONT_HERSHEY_SIMPLEX, 1, (0,0,0), 2)
    elif fault == 'color':
        cv2.rectangle(pixels, (250,240), (540,480), (0,0,255), 6)
    elif fault == 'room':
        pixels[:] = 50
        cv2.rectangle(pixels, (250,240), (540,480), (0,0,0), 3)
    elif fault == 'photo':
        rng = np.random.default_rng(17)
        pixels[240:480,250:540] = rng.integers(0,255,(240,290,3),dtype=np.uint8)
    elif fault == 'detector':
        class Missing:
            def empty(self): return True
        monkeypatch.setattr(cv2, 'CascadeClassifier', lambda *a: Missing())
    result = detector('Name: Alice' if fault == 'name' else '').prepare(encoded(pixels), known_child_names=('Alice',))
    assert result.prepared == ()
    assert result.reason


def test_face_detector_result_veto_remains_fail_closed(monkeypatch):
    class FoundFace:
        def empty(self): return False
        def detectMultiScale(self, *args, **kwargs): return np.array([[100,100,50,50]])
    monkeypatch.setattr(cv2, 'CascadeClassifier', lambda *args: FoundFace())
    result = detector().prepare(frame(), known_child_names=())
    assert not result.prepared
    assert result.reason
