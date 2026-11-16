from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Thread
import hashlib
import json
from home_study_agent.database import Database
from home_study_agent.knowledge_graph.language_candidate_service import TextbookFocusCandidateService
from home_study_agent.knowledge_graph.application_service import KnowledgeGraphApplicationService
from home_study_agent.knowledge_graph.language_atoms import TextbookLanguageAtomProjector
from home_study_agent.knowledge_graph.schema import ensure_schema
from home_study_agent.knowledge_graph import schema as kg_schema
from home_study_agent.knowledge_graph.textbook_page_service import TextbookOcrPageInput, TextbookOcrPageService
from home_study_agent.knowledge_ingestion.document_evidence import DocumentCorpusSpec, LocalDocumentEvidenceIngestor
from home_study_agent.repository import utc_now

def test_ocr_candidates_are_idempotent_editable_and_confirmed_after_restart(tmp_path:Path)->None:
 d=Database(tmp_path/'d.db');d.initialize();root=tmp_path/'ocr';root.mkdir();(root/'page.txt').write_text('Unit 1 Page 6\nWords: Apple, apple\n“apple pie” 生字：春 多音字：行',encoding='utf-8')
 spec=DocumentCorpusSpec('book','教材','textbook',root,None,'book','user_local','ocr');ing=LocalDocumentEvidenceIngestor(d);ing.run(spec,ing.start(spec,idempotency_key='book'))
 with d.connect() as c:rid=c.execute('SELECT resource_id FROM kg_resource').fetchone()[0]
 service=TextbookFocusCandidateService(d);first=service.extract(rid);assert len(first)==4
 assert len(service.extract(rid))==4
 apple=next(x for x in first if x['normalized']=='apple')
 saved=service.save(apple['candidate_id'],term='apple',focus_kind='word',proposed={'part_of_speech':'noun','senses':['苹果']},status='confirmed')
 assert saved['status']=='confirmed'
 reopened=TextbookFocusCandidateService(Database(tmp_path/'d.db'))
 assert any(x['status']=='confirmed' for x in reopened.list(rid))
 phrase=next(x for x in first if x['focus_kind']=='phrase')
 service.save(phrase['candidate_id'],term='apple pie',focus_kind='phrase',proposed={'senses':['苹果派']},status='confirmed')
 line=next(x for x in first if x['term']=='行')
 service.save(line['candidate_id'],term='行',focus_kind='character',proposed={'pronunciations':['xíng','háng'],'compounds':['银行']},status='confirmed')
 spring=next(x for x in first if x['term']=='春')
 deleted=service.save(spring['candidate_id'],term='春',focus_kind='character',proposed={},status='deleted')
 assert deleted['status']=='deleted' and all(item['candidate_id']!=spring['candidate_id'] for item in service.list(rid))
 with d.connect() as c:
  assert c.execute("SELECT COUNT(*) FROM kg_language_lexeme WHERE normalized='apple'").fetchone()[0]==1
  assert c.execute("SELECT pronunciations_json FROM kg_language_lexeme WHERE normalized='行'").fetchone()[0]=='["xíng", "háng"]'
  assert c.execute("SELECT COUNT(*) FROM kg_language_lexeme WHERE normalized='apple pie' AND focus_kind='phrase'").fetchone()[0]==1

def test_parent_ocr_page_import_is_idempotent_and_keeps_teaching_location(tmp_path:Path)->None:
 d=Database(tmp_path/'d.db');d.initialize();pages=TextbookOcrPageService(d)
 request=TextbookOcrPageInput(title='四上英语 Unit 1',ocr_text='Unit 1 Page 6\nWords: Apple\n"Good morning". Apple.',grade_scope='四年级',subject_scope='英语',semester_scope='上',textbook_version='译林版',volume='四上',unit='Unit 1',lesson='Welcome',page_number=6,source_ref='capture:local-001',ocr_confidence=.91)
 first=pages.import_page(request);again=pages.import_page(request)
 assert first['resource_id']==again['resource_id'] and again['created_or_updated'] is False
 listed=pages.list();assert len(listed)==1 and listed[0]['textbook_version']=='译林版' and listed[0]['lesson']=='Welcome'
 candidates=TextbookFocusCandidateService(d).extract(first['resource_id'])
 phrase=next(item for item in candidates if item['term']=='Good morning')
 assert phrase['status']=='pending'
 with d.connect() as c:
  assert c.execute("SELECT COUNT(*) FROM kg_node WHERE canonical_key LIKE 'language:%'").fetchone()[0]==0

def test_parent_workbook_page_survives_restart_and_can_enter_candidate_flow(tmp_path:Path)->None:
 d=Database(tmp_path/'d.db');d.initialize();pages=TextbookOcrPageService(d)
 imported=pages.import_page(TextbookOcrPageInput(title='初三物理同步练习',ocr_text='知识点：欧姆定律。练习：电阻两端电压为 6V。',material_kind='workbook',grade_scope='九年级',subject_scope='物理',page_number=12,source_ref='capture:workbook-001'))
 assert imported['material_kind']=='workbook'
 reopened=TextbookOcrPageService(Database(tmp_path/'d.db'))
 listed=reopened.list_public()
 assert len(listed)==1 and listed[0]['material_kind']=='workbook'
 assert reopened.resource_id_for_ref(listed[0]['page_ref'])==imported['resource_id']

def test_candidates_do_not_turn_headers_or_unconfirmed_terms_into_atoms(tmp_path:Path)->None:
 d=Database(tmp_path/'d.db');d.initialize();page=TextbookOcrPageService(d).import_page(TextbookOcrPageInput(title='Unit 1 Page 6',ocr_text='Welcome to Unit One. Page 6. Words: apple.',subject_scope='英语',source_ref='capture:header-test'))
 service=TextbookFocusCandidateService(d);items=service.extract(page['resource_id'])
 assert [item['normalized'] for item in items]==['apple']
 with d.connect() as c: assert c.execute("SELECT COUNT(*) FROM kg_node WHERE canonical_key LIKE 'language:%'").fetchone()[0]==0
 # A parent cannot silently replace page evidence with an invented edited term.
 try:
  service.save(items[0]['candidate_id'],term='banana',focus_kind='word',proposed={},status='confirmed')
  assert False, 'missing page evidence should fail'
 except ValueError as error:
  assert 'evidenced' in str(error)

def test_confirmed_candidate_is_immutable_and_rescan_retires_missing_evidence(tmp_path:Path)->None:
 d=Database(tmp_path/'d.db');d.initialize();pages=TextbookOcrPageService(d)
 first=pages.import_page(TextbookOcrPageInput(title='四上英语',ocr_text='Words: apple, pear',grade_scope='四年级',subject_scope='英语',page_number=3,source_ref='capture:stable-page'))
 service=TextbookFocusCandidateService(d);items=service.extract(first['resource_id']);apple=next(item for item in items if item['term'].lower()=='apple');pear=next(item for item in items if item['term'].lower()=='pear')
 service.save(apple['candidate_id'],term='apple',focus_kind='word',proposed={'senses':['苹果']},status='confirmed')
 with d.connect() as c:
  apple_node=c.execute("SELECT node_id FROM kg_node WHERE canonical_key='language:english:word:apple:recognition'").fetchone()[0]
  plan={'items':[{'knowledge_node_id':apple_node,'title':'apple'}]}
  c.execute("INSERT INTO kg_review_plan(review_plan_id,child_id,status,algorithm_version,item_count,source_evidence_count,plan_json,idempotency_key,generated_at,updated_at) VALUES('active-plan','test-child','active','test',1,1,?,'active-plan-key',?,?)",(json.dumps(plan),utc_now(),utc_now()));c.commit()
 for altered in ({'language':'english'},{'senses':'not-a-list'}):
  try:
   service.save(pear['candidate_id'],term='pear',focus_kind='word',proposed=altered,status='confirmed')
   assert False, 'reserved or malformed proposed fields must fail'
  except ValueError: pass
 try:
  service.save(apple['candidate_id'],term='apple',focus_kind='word',proposed={},status='deleted')
  assert False, 'confirmed terms must not leave an active projection'
 except ValueError as error:
  assert 'immutable' in str(error)
 # Same parent-visible page identity, corrected OCR: apple vanishes and pear
 # was never confirmed. Both state changes survive a database reopen.
 rescanned=pages.import_page(TextbookOcrPageInput(title='四上英语',ocr_text='Words: orange',grade_scope='四年级',subject_scope='英语',page_number=3,source_ref='capture:stable-page'))
 assert rescanned['candidate_reconciliation']=={'stale':1,'conflicted':1}
 reopened=TextbookFocusCandidateService(Database(tmp_path/'d.db')).list(first['resource_id'])
 assert {item['term']:item['status'] for item in reopened if item['term'] in {'apple','pear'}}=={'apple':'conflicted','pear':'stale'}
 with d.connect() as c:
  node=c.execute("SELECT status FROM kg_node WHERE canonical_key='language:english:word:apple:recognition'").fetchone()
  mapping_count=c.execute("SELECT COUNT(*) FROM kg_resource_node WHERE node_id=(SELECT node_id FROM kg_node WHERE canonical_key='language:english:word:apple:recognition')").fetchone()[0]
  lexeme_pages=c.execute("SELECT COUNT(*) FROM kg_language_lexeme_page WHERE lexeme_id=(SELECT lexeme_id FROM kg_language_lexeme WHERE normalized='apple')").fetchone()[0]
  plan_status=c.execute("SELECT status FROM kg_review_plan WHERE review_plan_id='active-plan'").fetchone()[0]
 assert node['status']=='conflicted' and mapping_count==0 and lexeme_pages==0 and plan_status=='superseded'

def test_parent_withdrawal_retires_confirmed_projection_after_restart(tmp_path:Path)->None:
 d=Database(tmp_path/'d.db');d.initialize();page=TextbookOcrPageService(d).import_page(TextbookOcrPageInput(title='四上英语',ocr_text='Words: apple',grade_scope='四年级',subject_scope='英语',source_ref='capture:withdraw'))
 service=TextbookFocusCandidateService(d);apple=service.extract(page['resource_id'])[0]
 service.save(apple['candidate_id'],term='apple',focus_kind='word',proposed={},status='confirmed')
 assert service.withdraw_confirmation(apple['candidate_id'])['status']=='withdrawn'
 reopened=TextbookFocusCandidateService(Database(tmp_path/'d.db'))
 assert reopened.list(page['resource_id'])[0]['status']=='withdrawn'
 with d.connect() as c:
  mapping=c.execute("SELECT review_status FROM kg_resource_node WHERE source_ref LIKE 'textbook_focus_manifest_v1:candidate:%'").fetchall()
  node=c.execute("SELECT status FROM kg_node WHERE canonical_key='language:english:word:apple:recognition'").fetchone()[0]
  edges=c.execute("SELECT review_status FROM kg_edge WHERE source_ref LIKE 'textbook_focus_manifest_v1:candidate:%'").fetchall()
 assert [row['review_status'] for row in mapping]==['rejected','rejected','rejected'] and [row['review_status'] for row in edges]==['rejected','rejected','rejected'] and node=='conflicted'

def test_confirmation_publishes_candidate_and_approved_mapping_together_or_recovers(tmp_path:Path,monkeypatch)->None:
 d=Database(tmp_path/'d.db');d.initialize();page=TextbookOcrPageService(d).import_page(TextbookOcrPageInput(title='四上英语',ocr_text='Words: apple',grade_scope='四年级',subject_scope='英语',source_ref='capture:projection'))
 service=TextbookFocusCandidateService(d);apple=service.extract(page['resource_id'])[0]
 original=TextbookLanguageAtomProjector.project_page
 monkeypatch.setattr(TextbookLanguageAtomProjector,'project_page',lambda *args,**kwargs: (_ for _ in ()).throw(RuntimeError('simulated projection interruption')))
 try:
  service.save(apple['candidate_id'],term='apple',focus_kind='word',proposed={},status='confirmed')
  assert False, 'interrupted projection should not confirm a candidate'
 except RuntimeError: pass
 with d.connect() as c:
  assert c.execute("SELECT status FROM kg_language_focus_candidate WHERE candidate_id=?",(apple['candidate_id'],)).fetchone()[0]=='projecting'
  assert c.execute("SELECT COUNT(*) FROM kg_resource_node WHERE review_status='approved'").fetchone()[0]==0
 monkeypatch.setattr(TextbookLanguageAtomProjector,'project_page',original)
 resumed=TextbookFocusCandidateService(Database(tmp_path/'d.db')).save(apple['candidate_id'],term='apple',focus_kind='word',proposed={},status='confirmed')
 assert resumed['status']=='confirmed'
 with d.connect() as c:
  assert c.execute("SELECT COUNT(*) FROM kg_resource_node WHERE source_ref LIKE 'textbook_focus_manifest_v1:candidate:%' AND review_status='approved'").fetchone()[0]==3
  assert c.execute("SELECT COUNT(*) FROM kg_node WHERE canonical_key LIKE 'language:english:word:apple:%' AND review_status='approved'").fetchone()[0]==3
  assert c.execute("SELECT COUNT(*) FROM kg_edge WHERE source_ref LIKE 'textbook_focus_manifest_v1:candidate:%' AND review_status='approved'").fetchone()[0]==3

def test_confirm_and_withdraw_commands_replay_after_response_loss_and_reject_reused_action(tmp_path:Path)->None:
 d=Database(tmp_path/'d.db');d.initialize();page=TextbookOcrPageService(d).import_page(TextbookOcrPageInput(title='四上英语',ocr_text='Words: apple, pear',source_ref='capture:actions'))
 service=TextbookFocusCandidateService(d); apple,pear=service.extract(page['resource_id'])
 command='candidate-confirm-001'
 first=service.save(apple['candidate_id'],term='apple',focus_kind='word',proposed={'senses':['苹果'],'collocations':['red apple']},status='confirmed',client_action_id=command)
 replay=TextbookFocusCandidateService(Database(tmp_path/'d.db')).save(apple['candidate_id'],term='apple',focus_kind='word',proposed={'collocations':['red apple'],'senses':['苹果']},status='confirmed',client_action_id=command)
 assert first['status']==replay['status']=='confirmed'
 try:
  service.save(pear['candidate_id'],term='pear',focus_kind='word',proposed={},status='confirmed',client_action_id=command)
  assert False, 'an action id cannot be reused across candidates'
 except Exception as error:
  assert 'already used' in str(error)
 withdrawn=service.withdraw_confirmation(apple['candidate_id'],client_action_id='candidate-withdraw-001')
 replay_withdrawn=TextbookFocusCandidateService(Database(tmp_path/'d.db')).withdraw_confirmation(apple['candidate_id'],client_action_id='candidate-withdraw-001')
 assert withdrawn['status']==replay_withdrawn['status']=='withdrawn'

def test_concurrent_double_click_same_confirm_command_is_one_durable_decision(tmp_path:Path)->None:
 d=Database(tmp_path/'d.db');d.initialize();page=TextbookOcrPageService(d).import_page(TextbookOcrPageInput(title='四上英语',ocr_text='Words: apple',source_ref='capture:double-click'))
 candidate=TextbookFocusCandidateService(d).extract(page['resource_id'])[0]
 def confirm(_: int) -> str:
  return TextbookFocusCandidateService(Database(tmp_path/'d.db')).save(candidate['candidate_id'],term='apple',focus_kind='word',proposed={},status='confirmed',client_action_id='candidate-double-click-001')['status']
 with ThreadPoolExecutor(max_workers=2) as pool:
  assert list(pool.map(confirm,range(2)))==['confirmed','confirmed']
 with d.connect() as c:
  assert c.execute("SELECT COUNT(*) FROM kg_language_focus_candidate_action WHERE client_action_id='candidate-double-click-001'").fetchone()[0]==1
  assert c.execute("SELECT COUNT(*) FROM kg_resource_node WHERE source_ref=? AND review_status='approved'",(TextbookFocusCandidateService._projection_source_ref(candidate['candidate_id']),)).fetchone()[0]==3

def test_slow_or_crashed_projection_keeps_command_processing_until_lease_takeover(tmp_path:Path,monkeypatch)->None:
 d=Database(tmp_path/'d.db');d.initialize();page=TextbookOcrPageService(d).import_page(TextbookOcrPageInput(title='四上英语',ocr_text='Words: apple',source_ref='capture:lease'))
 candidate=TextbookFocusCandidateService(d).extract(page['resource_id'])[0]; original=TextbookLanguageAtomProjector.project_page
 started=Event(); release=Event()
 def slow(*args,**kwargs):
  started.set(); release.wait(5); return original(*args,**kwargs)
 monkeypatch.setattr(TextbookLanguageAtomProjector,'project_page',slow)
 first_result: list[dict]=[]
 thread=Thread(target=lambda: first_result.append(TextbookFocusCandidateService(Database(tmp_path/'d.db')).save(candidate['candidate_id'],term='apple',focus_kind='word',proposed={},status='confirmed',client_action_id='candidate-slow-001')))
 thread.start(); assert started.wait(2)
 waiting=TextbookFocusCandidateService(Database(tmp_path/'d.db')).save(candidate['candidate_id'],term='apple',focus_kind='word',proposed={},status='confirmed',client_action_id='candidate-slow-001')
 assert waiting['status']=='projecting'
 release.set();thread.join(5);assert first_result[0]['status']=='confirmed'
 # A killed owner leaves its durable claim.  It is visible as processing until
 # the lease expires, then the same command can safely take over.
 monkeypatch.setattr(TextbookLanguageAtomProjector,'project_page',lambda *args,**kwargs: (_ for _ in ()).throw(SystemExit('simulated process death')))
 pear_page=TextbookOcrPageService(d).import_page(TextbookOcrPageInput(title='四上英语二',ocr_text='Words: pear',source_ref='capture:lease-pear'))
 pear=TextbookFocusCandidateService(d).extract(pear_page['resource_id'])[0]
 try: TextbookFocusCandidateService(d).save(pear['candidate_id'],term='pear',focus_kind='word',proposed={},status='confirmed',client_action_id='candidate-crash-001')
 except SystemExit: pass
 with d.connect() as c:
  c.execute("UPDATE kg_language_focus_candidate_action SET projection_lease_expires_at='2000-01-01T00:00:00+00:00' WHERE client_action_id='candidate-crash-001'");c.commit()
 monkeypatch.setattr(TextbookLanguageAtomProjector,'project_page',original)
 assert TextbookFocusCandidateService(Database(tmp_path/'d.db')).save(pear['candidate_id'],term='pear',focus_kind='word',proposed={},status='confirmed',client_action_id='candidate-crash-001')['status']=='confirmed'

def test_projection_failure_is_queryable_and_same_durable_command_recovers_after_restart(tmp_path:Path,monkeypatch)->None:
 d=Database(tmp_path/'d.db');d.initialize();page=TextbookOcrPageService(d).import_page(TextbookOcrPageInput(title='英语',ocr_text='Words: apple',source_ref='capture:status-retry'))
 candidate=TextbookFocusCandidateService(d).extract(page['resource_id'])[0]; original=TextbookLanguageAtomProjector.project_page
 monkeypatch.setattr(TextbookLanguageAtomProjector,'project_page',lambda *args,**kwargs: (_ for _ in ()).throw(RuntimeError('disk full')))
 try: TextbookFocusCandidateService(d).save(candidate['candidate_id'],term='apple',focus_kind='word',proposed={},status='confirmed',client_action_id='candidate-status-001')
 except RuntimeError: pass
 status=TextbookFocusCandidateService(Database(tmp_path/'d.db')).action_status(candidate['candidate_id'],'candidate-status-001')
 assert status['action_status']=='failed_recoverable' and status['item']['status']=='projecting' and status['retryable'] is True
 monkeypatch.setattr(TextbookLanguageAtomProjector,'project_page',original)
 recovered=TextbookFocusCandidateService(Database(tmp_path/'d.db')).save(candidate['candidate_id'],term='apple',focus_kind='word',proposed={},status='confirmed',client_action_id='candidate-status-001')
 assert recovered['status']=='confirmed'

def test_candidate_projection_is_scoped_and_does_not_promote_sibling_mapping(tmp_path:Path)->None:
 d=Database(tmp_path/'d.db');d.initialize();pages=TextbookOcrPageService(d)
 first=pages.import_page(TextbookOcrPageInput(title='一页',ocr_text='Words: apple',source_ref='capture:one'))
 second=pages.import_page(TextbookOcrPageInput(title='二页',ocr_text='Words: apple',source_ref='capture:two'))
 service=TextbookFocusCandidateService(d); one=service.extract(first['resource_id'])[0]; two=service.extract(second['resource_id'])[0]
 service.save(one['candidate_id'],term='apple',focus_kind='word',proposed={},status='confirmed',client_action_id='candidate-scope-001')
 with d.connect() as c:
  assert c.execute("SELECT COUNT(*) FROM kg_resource_node WHERE source_ref=? AND review_status='approved'",(service._projection_source_ref(one['candidate_id']),)).fetchone()[0]==3
  assert c.execute("SELECT COUNT(*) FROM kg_resource_node WHERE source_ref=?",(service._projection_source_ref(two['candidate_id']),)).fetchone()[0]==0

def test_withdrawal_removes_child_access_when_only_unreviewed_shared_page_evidence_remains(tmp_path:Path)->None:
 d=Database(tmp_path/'d.db');d.initialize();pages=TextbookOcrPageService(d)
 with d.connect() as c:
  c.execute("INSERT INTO child_profile(child_id,nickname,grade,curriculum_region,status,created_at,updated_at) VALUES('child-4','小四','四年级','江苏南京','active',?,?)",(utc_now(),utc_now()));c.commit()
 one=pages.import_page(TextbookOcrPageInput(title='第一页',ocr_text='Words: apple',grade_scope='四年级',subject_scope='英语',source_ref='capture:shared-one'))
 two=pages.import_page(TextbookOcrPageInput(title='第二页',ocr_text='Words: apple',grade_scope='四年级',subject_scope='英语',source_ref='capture:shared-two'))
 service=TextbookFocusCandidateService(d); first=service.extract(one['resource_id'])[0]; second=service.extract(two['resource_id'])[0]
 service.save(first['candidate_id'],term='apple',focus_kind='word',proposed={},status='confirmed',client_action_id='shared-first-001')
 with d.connect() as c: atom=c.execute("SELECT node_id FROM kg_node WHERE canonical_key='language:english:word:apple:recognition'").fetchone()[0]
 facade=KnowledgeGraphApplicationService(d);assert facade.learning_path('child-4',atom)['materials']
 service.withdraw_confirmation(first['candidate_id'],client_action_id='shared-withdraw-001')
 try:
  facade.learning_path('child-4',atom);assert False, 'unreviewed sibling evidence must not keep child access alive'
 except ValueError as error: assert 'approved' in str(error) or 'evidence' in str(error)
 service.save(second['candidate_id'],term='apple',focus_kind='word',proposed={},status='confirmed',client_action_id='shared-second-001')
 assert KnowledgeGraphApplicationService(d).learning_path('child-4',atom)['materials']

def test_unreviewed_shared_projection_never_downgrades_existing_approved_atom(tmp_path:Path,monkeypatch)->None:
 d=Database(tmp_path/'d.db');d.initialize();pages=TextbookOcrPageService(d)
 first=pages.import_page(TextbookOcrPageInput(title='已批准页',ocr_text='Words: apple',grade_scope='四年级',subject_scope='英语',source_ref='capture:approved'))
 second=pages.import_page(TextbookOcrPageInput(title='新候选页',ocr_text='Words: apple',grade_scope='四年级',subject_scope='英语',source_ref='capture:new'))
 service=TextbookFocusCandidateService(d); approved=service.extract(first['resource_id'])[0]; pending=service.extract(second['resource_id'])[0]
 service.save(approved['candidate_id'],term='apple',focus_kind='word',proposed={},status='confirmed',client_action_id='shared-approved-001')
 with d.connect() as c:
  before=c.execute("SELECT review_status FROM kg_node WHERE canonical_key='language:english:word:apple:recognition'").fetchone()[0]
 assert before=='approved'
 original=TextbookLanguageAtomProjector.project_page
 def partially_fail(self,*args,**kwargs):
  original(self,*args,**kwargs)
  raise RuntimeError('second candidate interrupted')
 monkeypatch.setattr(TextbookLanguageAtomProjector,'project_page',partially_fail)
 try: service.save(pending['candidate_id'],term='apple',focus_kind='word',proposed={},status='confirmed',client_action_id='shared-new-001')
 except RuntimeError: pass
 with d.connect() as c:
  assert c.execute("SELECT review_status FROM kg_node WHERE canonical_key='language:english:word:apple:recognition'").fetchone()[0]=='approved'
 monkeypatch.setattr(TextbookLanguageAtomProjector,'project_page',original)
 # A restart/retry followed by withdrawing only the second source keeps the first evidence usable.
 assert TextbookFocusCandidateService(Database(tmp_path/'d.db')).save(pending['candidate_id'],term='apple',focus_kind='word',proposed={},status='confirmed',client_action_id='shared-new-001')['status']=='confirmed'
 assert service.withdraw_confirmation(pending['candidate_id'],client_action_id='shared-new-withdraw-001')['status']=='withdrawn'
 with d.connect() as c:
  row=c.execute("SELECT review_status,status FROM kg_node WHERE canonical_key='language:english:word:apple:recognition'").fetchone()
 assert dict(row)=={'review_status':'approved','status':'active'}

def test_unreviewed_cross_grade_projection_preserves_approved_scope_and_provenance(tmp_path:Path)->None:
 d=Database(tmp_path/'d.db');d.initialize();graph=TextbookLanguageAtomProjector(d).graph
 node=graph.upsert_node(canonical_key='language:english:word:apple:recognition',node_type='knowledge_point',title='apple 认读',scope={'grade':'四年级','subject':'英语'},source_ref='approved-page',version_label='four-v1',license_boundary='parent_textbook',confidence=.91,review_status='approved')
 graph.upsert_node(canonical_key='language:english:word:apple:recognition',node_type='knowledge_point',title='apple 认读（初三）',scope={'grade':'初三','subject':'英语'},source_ref='unreviewed-page',version_label='nine-v1',license_boundary='parent_textbook',confidence=.2,review_status='unreviewed')
 with d.connect() as c: row=c.execute('SELECT title,scope_json,source_ref,version_label,license_boundary,confidence,review_status FROM kg_node WHERE node_id=?',(node,)).fetchone()
 assert dict(row)=={'title':'apple 认读','scope_json':'{"grade": "四年级", "subject": "英语"}','source_ref':'approved-page','version_label':'four-v1','license_boundary':'parent_textbook','confidence':.91,'review_status':'approved'}

def test_retry_reconciles_after_a_committed_page_update_and_rejects_graph_key_syntax(tmp_path:Path)->None:
 d=Database(tmp_path/'d.db');d.initialize();pages=TextbookOcrPageService(d)
 original=TextbookOcrPageInput(title='四上英语',ocr_text='Words: apple',subject_scope='英语',page_number=4,source_ref='capture:recovery')
 page=pages.import_page(original);service=TextbookFocusCandidateService(d);apple=service.extract(page['resource_id'])[0]
 service.save(apple['candidate_id'],term='apple',focus_kind='word',proposed={},status='confirmed')
 # Simulate a process death after the committed OCR/page update but before the
 # separate reconciliation transaction.  No candidate state is touched here.
 replacement='Words: orange'; digest=hashlib.sha256(replacement.encode()).hexdigest()
 with d.connect() as c:
  c.execute("UPDATE kg_resource SET transcript_sha256=? WHERE resource_id=?",(digest,page['resource_id']))
  c.execute("UPDATE kg_segment SET text=?,text_sha256=? WHERE resource_id=?",(replacement,digest,page['resource_id']))
  c.commit()
 retry=pages.import_page(TextbookOcrPageInput(title='四上英语',ocr_text=replacement,subject_scope='英语',page_number=4,source_ref='capture:recovery'))
 assert retry['created_or_updated'] is False and retry['candidate_reconciliation']=={'stale':0,'conflicted':1}
 try:
  service.create(page['resource_id'],language='english',term='apple%_:',focus_kind='word',proposed={})
  assert False, 'wildcard/control syntax must not reach graph-key matching'
 except ValueError: pass
 try:
  service.create(page['resource_id'],language='chinese',term='行_',focus_kind='character',proposed={})
  assert False, 'non-CJK character syntax must fail'
 except ValueError: pass

def test_v8_candidate_state_migrates_without_losing_pending_row(tmp_path:Path)->None:
 d=Database(tmp_path/'d.db');d.initialize();page=TextbookOcrPageService(d).import_page(TextbookOcrPageInput(title='四上英语',ocr_text='Words: apple',page_number=5,source_ref='capture:migration'))
 service=TextbookFocusCandidateService(d);candidate=service.extract(page['resource_id'])[0]
 with d.connect() as c:
  row=c.execute("SELECT * FROM kg_language_focus_candidate WHERE candidate_id=?",(candidate['candidate_id'],)).fetchone()
  c.execute("DROP TABLE kg_language_focus_candidate")
  c.execute("""CREATE TABLE kg_language_focus_candidate (
    candidate_id TEXT PRIMARY KEY, resource_id TEXT NOT NULL REFERENCES kg_resource(resource_id) ON DELETE CASCADE,
    language TEXT NOT NULL, term TEXT NOT NULL, normalized TEXT NOT NULL, focus_kind TEXT NOT NULL, proposed_json TEXT NOT NULL DEFAULT '{{}}',
    status TEXT NOT NULL CHECK(status IN ('pending','confirmed','deleted')), source_start INTEGER, source_end INTEGER,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(resource_id,language,normalized,focus_kind))""")
  c.execute("INSERT INTO kg_language_focus_candidate VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",tuple(row));ensure_schema(c,utc_now());c.commit()
  migrated=c.execute("SELECT status FROM kg_language_focus_candidate WHERE candidate_id=?",(candidate['candidate_id'],)).fetchone()[0]
  sql=c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='kg_language_focus_candidate'").fetchone()[0]
 assert migrated=='pending' and "'conflicted'" in sql and "'withdrawn'" in sql

def test_candidate_migration_recovers_old_and_legacy_rows_after_each_interruption(tmp_path:Path)->None:
 def prepare(path:Path):
  d=Database(path);d.initialize();page=TextbookOcrPageService(d).import_page(TextbookOcrPageInput(title='英语',ocr_text='Words: apple',source_ref='capture:migration-fault'))
  candidate=TextbookFocusCandidateService(d).extract(page['resource_id'])[0]
  with d.connect() as c:
   row=tuple(c.execute("SELECT * FROM kg_language_focus_candidate WHERE candidate_id=?",(candidate['candidate_id'],)).fetchone())
   c.execute("DROP TABLE kg_language_focus_candidate_action")
   c.execute("DROP TABLE kg_language_focus_candidate")
   old="""CREATE TABLE {name} (candidate_id TEXT PRIMARY KEY, resource_id TEXT NOT NULL REFERENCES kg_resource(resource_id) ON DELETE CASCADE,
    language TEXT NOT NULL, term TEXT NOT NULL, normalized TEXT NOT NULL, focus_kind TEXT NOT NULL, proposed_json TEXT NOT NULL DEFAULT '{{}}',
    status TEXT NOT NULL CHECK(status IN ('pending','confirmed','deleted')), source_start INTEGER, source_end INTEGER,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(resource_id,language,normalized,focus_kind))"""
   c.execute(old.format(name='kg_language_focus_candidate'));c.execute("INSERT INTO kg_language_focus_candidate VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",row)
   c.execute(old.format(name='kg_language_focus_candidate_legacy'))
   legacy=list(row);legacy[0]='legacy-candidate';legacy[3]='pear';legacy[4]='pear';c.execute("INSERT INTO kg_language_focus_candidate_legacy VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",legacy);c.commit()
  return d,candidate['candidate_id']
 for index, step in enumerate(('rename_active','create_current','copy_active','copy_legacy','drop_active','drop_legacy')):
  d,candidate_id=prepare(tmp_path/f'fault-{index}.db')
  kg_schema._CANDIDATE_MIGRATION_FAULT_AFTER=step
  with d.connect() as c:
   try:
    ensure_schema(c,utc_now());assert False, step
   except Exception as error:
    assert 'injected candidate migration interruption' in str(error)
   kg_schema._CANDIDATE_MIGRATION_FAULT_AFTER=None
   ensure_schema(c,utc_now());c.commit()
   assert {item[0] for item in c.execute("SELECT candidate_id FROM kg_language_focus_candidate")}=={candidate_id,'legacy-candidate'}
   assert c.execute("SELECT COUNT(*) FROM sqlite_master WHERE name IN ('kg_language_focus_candidate_legacy','kg_language_focus_candidate_upgrade_active','kg_language_focus_candidate_action_upgrade_legacy')").fetchone()[0]==0
   assert list(c.execute('PRAGMA foreign_key_check'))==[]
 kg_schema._CANDIDATE_MIGRATION_FAULT_AFTER=None
