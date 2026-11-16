"""Exact inverse of the actual crop/rotation, not a guessed bounding rectangle."""
from hashlib import sha256
from copy import deepcopy

import cv2
import numpy as np
import pytest

from home_study_agent.local_ocr.service import _perspective_crop, _rotate_page
from home_study_agent.local_ocr.text_geometry import build,validate,rotation


@pytest.mark.parametrize('degrees',[0,90,180,270])
@pytest.mark.parametrize('perspective',[False,True])
def test_actual_crop_rotation_inverse_preserves_original_pixels(degrees,perspective):
    raw=np.full((900,1100,3),230,np.uint8)
    corners=np.asarray([[100,80],[950,100],[1000,790],[60,820]] if perspective else
                       [[100,80],[1000,80],[1000,820],[100,820]],np.float32)
    original=np.asarray([[230,250],[740,250],[740,310],[230,310]],np.float32)
    cv2.fillConvexPoly(raw,original.astype(np.int32),(0,0,0))
    cropped,transform=_perspective_crop(raw,corners,return_transform=True)
    oriented=_rotate_page(cropped,degrees)
    matrix=rotation(degrees,cropped.shape[1],cropped.shape[0]) @ transform
    points=cv2.perspectiveTransform(original.reshape(1,4,2).astype(float),matrix)[0]
    center=points.mean(axis=0).astype(int)
    assert oriented[center[1],center[0],0]==0
    lines=[dict(text='This is my own answer.',box=points.tolist())]
    frame_hash=sha256(raw.tobytes()).hexdigest()
    result=build(lines,frame_sha256=frame_hash,frame_size=[1100,900],
        prepared_size=[cropped.shape[1],cropped.shape[0]],frame_to_prepared=transform.tolist(),degrees=degrees)
    assert np.allclose(result['lines'][0]['frame_polygon'],original,atol=.001)
    assert validate(result,lines,frame_sha256=frame_hash,text=lines[0]['text'])==result
    for field,value in [('frame_to_ocr',np.eye(3).tolist()),('geometry_sha256','0'*64),('text_sha256','0'*64),
                        ('frame_size',[1,1]),('orientation_degrees',45)]:
        bad=deepcopy(result);bad[field]=value
        with pytest.raises(ValueError):validate(bad,lines,frame_sha256=frame_hash,text=lines[0]['text'])


def test_invalid_missing_or_nonfinite_polygons_never_gain_geometry():
    for points in (None,[],[[0,0],[0,0],[0,0],[0,0]],[[0,0],[5,0],[5,float('nan')],[0,5]]):
        with pytest.raises((ValueError,TypeError)):
            build([dict(text='Answer',box=points)],frame_sha256='0'*64,frame_size=[100,100],
                prepared_size=[100,100],frame_to_prepared=np.eye(3).tolist(),degrees=0)
