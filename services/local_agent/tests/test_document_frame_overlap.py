import math
import numpy as np
import pytest
from home_study_agent.document_frame_overlap import reference_overlap

SQUARE = np.array([[0.,0.],[1.,0.],[1.,1.],[0.,1.]])

def test_nearly_axis_aligned_camera_fit_keeps_its_true_overlap():
    scale = np.array([math.sqrt(.5), math.sqrt(.5)])
    matrix = np.array([[1.00000690809796,8.46729337702427e-06,.04244907215095074],
                       [-8.46729337702427e-06,1.00000690809796,1.5913139286223752e-07]])
    projected = ((SQUARE*scale) @ matrix[:,:2].T + matrix[:,2])/scale
    assert .93 < reference_overlap(projected) < .95

@pytest.mark.parametrize('dx,dy', [(0,0),(.06,0),(-.06,.03),(.2,0),(.200001,0),(1,0),(2,0)])
def test_translation_matches_known_rectangle_intersection(dx,dy):
    assert reference_overlap(SQUARE+[dx,dy]) == pytest.approx(max(0,1-abs(dx))*max(0,1-abs(dy)),abs=1e-12)

@pytest.mark.parametrize('angle', [-1e-10,0,1e-10])
def test_overlap_is_continuous_near_parallel_edges(angle):
    matrix = np.array([[math.cos(angle),-math.sin(angle)],[math.sin(angle),math.cos(angle)]])
    assert reference_overlap(SQUARE@matrix.T+[.06,.01]) == pytest.approx(.94*.99,abs=1e-8)

def test_smaller_included_rectangle_keeps_absolute_area_not_iou():
    assert reference_overlap(SQUARE*.5+.2) == pytest.approx(.25,abs=1e-12)

def test_rotated_square_has_the_known_octagonal_intersection():
    angle = math.pi / 4
    matrix = np.array([[math.cos(angle),-math.sin(angle)],[math.sin(angle),math.cos(angle)]])
    assert reference_overlap((SQUARE-.5)@matrix.T+.5) == pytest.approx(2*math.sqrt(2)-2,abs=1e-12)

def test_camera_aspect_normalization_preserves_absolute_reference_area():
    scale = np.array([.8,.6])
    projected = (SQUARE*scale+[.04,.03])/scale
    assert reference_overlap(projected) == pytest.approx(.95*.95,abs=1e-12)

def test_eighty_percent_gate_is_not_relaxed():
    assert reference_overlap(SQUARE+[.199999,0]) >= .80
    assert reference_overlap(SQUARE+[.200001,0]) < .80

@pytest.mark.parametrize('bad', [np.full((4,2),np.nan),np.full((4,2),np.inf),np.zeros((4,2)),
                                SQUARE[[0,2,1,3]],SQUARE[::-1],np.zeros((3,2)), SQUARE*1e-10])
def test_unsupported_or_degenerate_projection_cannot_authorize_overlap(bad):
    assert reference_overlap(bad) == 0
