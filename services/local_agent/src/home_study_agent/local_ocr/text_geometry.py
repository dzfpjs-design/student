"""Local provenance for OCR character spans, never a semantic/ownership score."""
from hashlib import sha256
import json

import cv2
import numpy as np

VERSION = 'ocr_text_geometry_v1'


def fingerprint(value):
    return sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def rotation(degrees, width, height):
    return np.asarray({0:[[1,0,0],[0,1,0],[0,0,1]],
        90:[[0,-1,height-1],[1,0,0],[0,0,1]],
        180:[[-1,0,width-1],[0,-1,height-1],[0,0,1]],
        270:[[0,1,0],[-1,0,width-1],[0,0,1]]}[degrees],dtype=float)


def horizontal_mirror(width):
    return np.asarray([[-1, 0, width - 1], [0, 1, 0], [0, 0, 1]], dtype=float)


def polygon(points, matrix, size):
    points=np.asarray(points,dtype=float)
    if points.shape!=(4,2) or not np.isfinite(points).all():raise ValueError('OCR_GEOMETRY_POLYGON_INVALID')
    homogeneous=np.c_[points,np.ones(4)] @ matrix.T
    if not np.isfinite(homogeneous).all() or np.any(np.abs(homogeneous[:,2])<1e-9):raise ValueError('OCR_GEOMETRY_TRANSFORM_INVALID')
    result=homogeneous[:,:2]/homogeneous[:,2:]
    if np.any(result < -.01) or np.any(result > np.asarray(size)-1+.01):raise ValueError('OCR_GEOMETRY_OUT_OF_FRAME')
    if not cv2.isContourConvex(result.astype(np.float32)) or abs(cv2.contourArea(result.astype(np.float32)))<1:
        raise ValueError('OCR_GEOMETRY_POLYGON_INVALID')
    return result.tolist()


def build(lines, *, frame_sha256, frame_size, prepared_size, frame_to_prepared, degrees,
          mirror_horizontal=False):
    if (type(degrees) is not int or degrees not in (0,90,180,270)
        or any(not isinstance(size,(list,tuple)) or len(size)!=2
            or any(type(v) is not int or not 80<=v<=32768 for v in size) for size in (frame_size,prepared_size))
        or not isinstance(lines,list) or not 1<=len(lines)<=4096):
        raise ValueError('OCR_GEOMETRY_SHAPE_INVALID')
    mirror = horizontal_mirror(prepared_size[0]) if mirror_horizontal else np.eye(3)
    matrix=rotation(degrees,*prepared_size) @ mirror @ np.asarray(frame_to_prepared,dtype=float)
    if matrix.shape!=(3,3) or not np.isfinite(matrix).all() or abs(np.linalg.det(matrix))<1e-12:
        raise ValueError('OCR_GEOMETRY_TRANSFORM_INVALID')
    inverse=np.linalg.inv(matrix)
    ocr_size=prepared_size[::-1] if degrees in (90,270) else prepared_size
    records=[];offset=0
    for index,line in enumerate(lines):
        text=line.get('text')
        if not isinstance(text,str) or not text.strip():raise ValueError('OCR_GEOMETRY_TEXT_INVALID')
        points=polygon(line.get('box'),np.eye(3),ocr_size)
        records.append(dict(line_index=index,start=offset,end=offset+len(text),
            text_sha256=sha256(text.encode()).hexdigest(),ocr_polygon=points,
            frame_polygon=polygon(points,inverse,frame_size)))
        offset+=len(text)+1
    if not records:raise ValueError('OCR_GEOMETRY_TEXT_INVALID')
    result=dict(version=VERSION,frame_sha256=frame_sha256,frame_size=list(frame_size),
        prepared_size=list(prepared_size),orientation_degrees=degrees,frame_to_prepared=frame_to_prepared,
        frame_to_ocr=matrix.tolist(),text_sha256=sha256('\n'.join(l['text'] for l in lines).encode()).hexdigest(),lines=records)
    if mirror_horizontal:
        result['mirror_horizontal'] = True
    return {**result,'geometry_sha256':fingerprint(result)}


def validate(value, lines, *, frame_sha256, text):
    if not isinstance(value,dict) or value.get('version')!=VERSION:raise ValueError('OCR_GEOMETRY_REQUIRED')
    try:
        if value['text_sha256']!=sha256(text.encode()).hexdigest():raise ValueError('OCR_GEOMETRY_TEXT_CHANGED')
        expected=build(lines,frame_sha256=frame_sha256,frame_size=value['frame_size'],
            prepared_size=value['prepared_size'],frame_to_prepared=value['frame_to_prepared'],degrees=value['orientation_degrees'],
            mirror_horizontal=value.get('mirror_horizontal') is True)
        if value!=expected:raise ValueError('OCR_GEOMETRY_CHANGED')
    except (TypeError,KeyError,np.linalg.LinAlgError,OverflowError) as error:
        raise ValueError('OCR_GEOMETRY_INVALID') from error
    return value
