"""Explicit synthetic-only TCP fixture for the mounted parent journey."""
import argparse
import base64
from io import BytesIO
import json
from pathlib import Path
import socket
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from PIL import Image
import uvicorn

from home_study_agent.database import Database
from home_study_agent.knowledge_graph.http_api import install_knowledge_graph_routes


class SyntheticOcr:
    def enqueue_ocr(self, image, request_id):
        return {'task':{'task_id':request_id}}
    def claim_ocr(self, scheduled, snapshot):
        return scheduled['task']
    def run_ocr_claim(self, claim, image):
        return {'status':'completed','result':{'confidence':.6,'engine':'synthetic-only',
            'quality':{'passed':False,'issues':['synthetic-review']},'lines':[{'text':'Words: apple'}],
            'manual_review':{'required':True,'reasons':['synthetic-review'],'handwriting':'review_required','math':'review_required'}}}
    def cancel_ocr(self, scheduled, reason):
        pass


class SyntheticParentAuth:
    def require(self, token):
        if token != 'synthetic-parent':
            raise HTTPException(status_code=401,detail='synthetic parent authentication required')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-root',required=True)
    args = parser.parse_args()
    root = Path(args.data_root).resolve()
    if not root.is_dir() or not root.name.startswith('hsc-parent-file-test-'):
        raise ValueError('only an explicit isolated fixture directory is allowed')
    db = Database(root/'fixture.db',media_root=root,data_encryption_key=b'0'*32)
    db.initialize()
    with db.connect() as c:
        c.execute("""INSERT OR IGNORE INTO child_profile(child_id,nickname,grade,curriculum_region,status,created_at,updated_at)
            VALUES('synthetic-child-4','合成四年级','四年级','江苏','active',datetime('now'),datetime('now'))""")
        c.execute("""INSERT OR IGNORE INTO child_profile(child_id,nickname,grade,curriculum_region,status,created_at,updated_at)
            VALUES('synthetic-child-9','合成初三','初三','江苏','active',datetime('now'),datetime('now'))""")
        c.commit()
    app = FastAPI(); app.state.ocr_workflow = SimpleNamespace(adapter=SyntheticOcr())
    install_knowledge_graph_routes(app,db,SyntheticParentAuth())
    sock = socket.socket(); sock.bind(('127.0.0.1',0))
    raw = BytesIO(); Image.new('RGB',(100,80),'white').save(raw,format='PNG')
    print(json.dumps({'port':sock.getsockname()[1],'synthetic_image':base64.b64encode(raw.getvalue()).decode()}),flush=True)
    uvicorn.Server(uvicorn.Config(app,log_level='error')).run(sockets=[sock])


if __name__ == '__main__':
    main()
