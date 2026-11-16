from pathlib import Path
from fastapi import FastAPI,HTTPException
from fastapi.testclient import TestClient
from home_study_agent.database import Database
from home_study_agent.knowledge_graph.http_api import install_knowledge_graph_routes
from home_study_agent.knowledge_ingestion.document_evidence import DocumentCorpusSpec,LocalDocumentEvidenceIngestor

class Auth:
 def require(self,t):
  if t!='ok':raise HTTPException(status_code=401,detail='no')

def test_parent_can_import_extract_edit_confirm_and_delete_ocr_language_candidate(tmp_path:Path)->None:
 d=Database(tmp_path/'d.db');d.initialize()
 app=FastAPI();install_knowledge_graph_routes(app,d,Auth())
 with TestClient(app) as client:
  assert client.post('/v1/parent/knowledge-graph/textbook-pages',json={'title':'四上英语','ocr_text':'Apple apple. 多音字：行','page_number':2}).status_code==401
  page=client.post('/v1/parent/knowledge-graph/textbook-pages',headers={'X-HSC-Parent':'ok'},json={'title':'四上英语','ocr_text':'Unit 1 Page 2\nWords: Apple, apple\n多音字：行','grade_scope':'四年级','subject_scope':'英语','textbook_version':'译林版','page_number':2}).json()['page'];page_ref=page['page_ref']
  assert 'resource_id' not in page
  assert client.get('/v1/parent/knowledge-graph/textbook-pages',headers={'X-HSC-Parent':'ok'}).json()['items'][0]['textbook_version']=='译林版'
  rows=client.post(f'/v1/parent/knowledge-graph/textbook-pages/{page_ref}/language-candidates/extract',headers={'X-HSC-Parent':'ok'}).json()['items'];apple=next(item for item in rows if item['normalized']=='apple')
  assert 'candidate_id' not in apple and 'resource_id' not in apple
  saved=client.put(f"/v1/parent/knowledge-graph/language-candidates/{apple['candidate_ref']}",headers={'X-HSC-Parent':'ok'},json={'term':'apple','focus_kind':'word','proposed':{'part_of_speech':'noun'},'status':'confirmed','client_action_id':'candidate-http-confirm-001'})
  assert saved.status_code==200 and saved.json()['item']['status']=='confirmed'
  line=next(item for item in rows if item['term']=='行')
  changed=client.put(f"/v1/parent/knowledge-graph/language-candidates/{line['candidate_ref']}",headers={'X-HSC-Parent':'ok'},json={'term':'行','focus_kind':'character','proposed':{'pronunciations':['xíng','háng']},'status':'confirmed','client_action_id':'candidate-http-confirm-002'})
  assert changed.status_code==200
  deleted=client.put(f"/v1/parent/knowledge-graph/language-candidates/{line['candidate_ref']}",headers={'X-HSC-Parent':'ok'},json={'term':'行','focus_kind':'character','proposed':{},'status':'deleted','client_action_id':'candidate-http-delete-001'})
  assert deleted.status_code==422 and 'immutable' in deleted.json()['detail']['message']

def test_candidate_action_http_replay_is_safe_and_reuse_is_a_conflict(tmp_path:Path)->None:
 d=Database(tmp_path/'d.db');d.initialize();app=FastAPI();install_knowledge_graph_routes(app,d,Auth())
 with TestClient(app) as client:
  headers={'X-HSC-Parent':'ok'}
  page=client.post('/v1/parent/knowledge-graph/textbook-pages',headers=headers,json={'title':'英语','ocr_text':'Words: apple, pear'}).json()['page']
  rows=client.post(f"/v1/parent/knowledge-graph/textbook-pages/{page['page_ref']}/language-candidates/extract",headers=headers).json()['items']
  apple=next(item for item in rows if item['term'].lower()=='apple'); pear=next(item for item in rows if item['term'].lower()=='pear')
  payload={'term':'apple','focus_kind':'word','proposed':{},'status':'confirmed','client_action_id':'candidate-http-replay-001'}
  assert client.put(f"/v1/parent/knowledge-graph/language-candidates/{apple['candidate_ref']}",headers=headers,json=payload).status_code==200
  assert client.put(f"/v1/parent/knowledge-graph/language-candidates/{apple['candidate_ref']}",headers=headers,json=payload).json()['item']['status']=='confirmed'
  state=client.get(f"/v1/parent/knowledge-graph/language-candidates/{apple['candidate_ref']}/actions/candidate-http-replay-001",headers=headers)
  assert state.status_code==200 and state.json()['action_status']=='confirmed' and 'candidate_id' not in state.json()['item']
  conflict=client.put(f"/v1/parent/knowledge-graph/language-candidates/{pear['candidate_ref']}",headers=headers,json={**payload,'term':'pear'})
  assert conflict.status_code==409 and conflict.json()['detail']['code']=='LANGUAGE_CANDIDATE_ACTION_CONFLICT'
  withdraw={'client_action_id':'candidate-http-withdraw-001'}
  assert client.post(f"/v1/parent/knowledge-graph/language-candidates/{apple['candidate_ref']}/withdraw",headers=headers,json=withdraw).json()['item']['status']=='withdrawn'
  assert client.post(f"/v1/parent/knowledge-graph/language-candidates/{apple['candidate_ref']}/withdraw",headers=headers,json=withdraw).json()['item']['status']=='withdrawn'
