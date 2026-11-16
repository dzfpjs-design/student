"""Synthetic original media producers and fresh consumers for portable restoration.

No cloud, microphone, OCR, native-device issuer, or DPAPI credit is claimed.
"""
from contextlib import contextmanager
import hashlib, io, json, math, struct, wave
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.document_trial_capture import FRAME_TYPE
from home_study_agent.knowledge_graph.parent_textbook_capture import ParentTextbookCaptureService
from home_study_agent.pet_media_evidence import inspect_local_pcm
from test_native_privacy_permit_http import certificate, signed_header, KEY
from test_privacy_operation_recovery_http import identity

KEY_BYTES = b'o' * 32
H = lambda raw: hashlib.sha256(raw).hexdigest()


@contextmanager
def client(root,database):
    settings=Settings(data_dir=root,database_path=database,data_encryption_key=KEY_BYTES,
        native_launch_attestation_key=KEY,session_token='synthetic-asset-session',maintenance_enabled=False)
    with TestClient(create_app(settings),headers={'X-HSC-Session':'synthetic-asset-session'})as c:yield c

def auth(c,setup=False):
    result=c.post('/v1/parent/'+('setup'if setup else'unlock'),json={'pin':'4286'})
    assert result.status_code==200,result.text
    return {'X-HSC-Parent':result.json()['parent_token']}

def payloads():
    stream=io.BytesIO()
    with wave.open(stream,'wb')as w:
        w.setparams((1,2,16000,0,'NONE','not compressed'))
        w.writeframes(b''.join(struct.pack('<h',int(1000*math.sin(2*math.pi*220*n/16000)))for n in range(16000)))
    image=np.full((40,80,3),255,np.uint8);image[5:30,5:15]=0
    ok,encoded=cv2.imencode('.png',image);assert ok
    return stream.getvalue(),encoded.tobytes()

def seed_media(c,child,index):
    session=c.post('/v1/sessions',json={'child_id':child,'client_action_id':'asset-fixture-session-'+str(index)})
    assert session.status_code==201,session.text
    sid=session.json()['session_id'];db=c.app.state.privacy_lifecycle.database
    media=c.app.state.media_ai;page=ParentTextbookCaptureService(db,None)
    audio,png=payloads();voice='synthetic-voice-'+str(index);frame='synthetic-frame-'+str(index);book='synthetic-book-'+str(index)
    voice_path=media._store_raw(child,sid,voice,'.wav',audio)
    frame_path=media._store_raw(child,sid,frame,'.png',png)
    book_path=page._store_raw_page(child,book,png)
    context=H(b'public synthetic page context');action='synthetic-book-action-'+str(index);fileid='synthetic-file-'+str(index)
    with db.connect()as d:
        d.execute("INSERT INTO voice_input(voice_input_id,child_id,session_id,purpose,transcript,local_path,model_version,duration_ms,status,created_at,client_action_id)VALUES(?,?,?,'synthetic-fixture','public synthetic',?,'fixture-not-ASR',1000,'recognized',datetime('now'),?)",(voice,child,sid,voice_path,voice+'-action'))
        d.execute("INSERT INTO capture_artifact(artifact_id,child_id,session_id,artifact_type,local_path,extracted_text,confidence,details_json,status,created_at,client_action_id)VALUES(?,?,?,?,?,'public synthetic',1,?,'completed',datetime('now'),?)",(frame,child,sid,FRAME_TYPE,frame_path,json.dumps({'sha256':H(png),'observed_at':'2026-10-20T00:00:00+00:00','reason':'synthetic-original-frame','width':80,'height':40}),frame+'-action'))
        d.execute("INSERT INTO kg_parent_textbook_file_source(file_source_id,child_id,image_sha256,page_context_sha256,client_action_id,request_sha256,created_at)VALUES(?,?,?,?,?,?,datetime('now'))",(fileid,child,H(png),context,action,H(b'public synthetic file request')))
        d.execute("INSERT INTO kg_parent_textbook_ocr_artifact(artifact_id,child_id,source_kind,file_source_id,page_context_json,extracted_text,confidence,details_json,image_sha256,page_context_sha256,raw_image_path,raw_image_media_type,raw_image_byte_length,raw_image_encryption_version,status,created_at,updated_at,client_action_id)VALUES(?,?,'file',?,'{\"title\":\"Public synthetic page\"}','public synthetic',1,'{}',?,?,?,'image/png',?,'hsc-raw-media-v1','ready',datetime('now'),datetime('now'),?)",(book,child,fileid,H(png),context,book_path,len(png),action))
        d.execute("UPDATE daily_session SET status='completed',state='completed'WHERE session_id=?",(sid,))
        d.commit()
    return {'child':child,'session':sid,'voice':voice,'frame':frame,'book':book,'audio':audio,'png':png,'paths':[Path(voice_path),Path(frame_path),Path(book_path)]}

def assert_consumers(c,item):
    db=c.app.state.privacy_lifecycle.database;root=c.app.state.settings.data_dir/'media'
    with db.connect()as d:
        path=d.execute('SELECT local_path FROM voice_input WHERE voice_input_id=?',(item['voice'],)).fetchone()[0]
    audio=inspect_local_pcm(path,media_root=root,data_encryption_key=KEY_BYTES)
    assert audio['status']=='usable_signal'and audio['sha256']==H(item['audio'])
    assert c.app.state.document_trial_capture.image(item['child'],item['frame'])==item['png']
    review=c.app.state.document_trial_capture.review(item['child'],item['session'],persist=False)
    original=[f for f in review['frames']if f['artifact_id']==item['frame']]
    assert len(original)==1 and original[0]['image_available']and original[0]['sha256']==H(item['png'])
    assert(original[0]['width'],original[0]['height'])==(80,40)
    page=ParentTextbookCaptureService(db,None)
    assert page._read_raw_page(item['child'],item['book'])==item['png']
    other='child-junior-3'if item['child']=='child-primary-4'else'child-primary-4'
    with pytest.raises(Exception):page._read_raw_page(other,item['book'])

def restore(c,headers,saved):
    op=identity(c,headers);body={'backup_id':saved['backup_id'],'confirmation':'确认恢复','operation':op}
    proof=certificate('/v1/parent/backups/restore',body,op,kind='restore',child_id=None,scope=None)
    r=c.post('/v1/parent/backups/restore',headers={**headers,**signed_header(proof)},json=body)
    assert r.status_code==200,r.text
    return proof

def backup(c,headers):
    r=c.post('/v1/parent/backups',headers=headers);assert r.status_code==200,r.text;return r.json()

