from pathlib import Path
from home_study_agent.database import Database
from home_study_agent.knowledge_graph.language_atoms import LanguageFocus, TextbookLanguageAtomProjector
from home_study_agent.knowledge_graph.application_service import KnowledgeGraphApplicationService
from home_study_agent.knowledge_ingestion.document_evidence import DocumentCorpusSpec, LocalDocumentEvidenceIngestor

def test_page_focus_creates_assessable_word_and_character_atoms(tmp_path: Path) -> None:
 d=Database(tmp_path/'d.db');d.initialize();ocr=tmp_path/'ocr';ocr.mkdir();(ocr/'p1.txt').write_text('We read apple. 学生学习生字春。',encoding='utf-8')
 spec=DocumentCorpusSpec('book','教材','textbook',ocr,None,'local-book','user_local','local-ocr')
 ingest=LocalDocumentEvidenceIngestor(d);ingest.run(spec,ingest.start(spec,idempotency_key='book-v1'))
 with d.connect() as c: rid=c.execute('SELECT resource_id FROM kg_resource').fetchone()[0]
 result=TextbookLanguageAtomProjector(d).project_page(rid,[LanguageFocus('english','apple','word',part_of_speech='noun',senses=['苹果'],collocations=['an apple'],phonics_or_ipa='/ˈæpəl/'),LanguageFocus('chinese','春','character',pronunciations=['chūn'],radical='日',structure='上下结构',compounds=['春天'],common_errors=['日字底漏横']),LanguageFocus('english','missing','word')],review_status='approved')
 assert result['created_atoms']==6 and result['skipped'][0]['term']=='missing'
 with d.connect() as c:
  lexemes=c.execute('SELECT language,part_of_speech,phonics_or_ipa,radical,structure,compounds_json FROM kg_language_lexeme ORDER BY language').fetchall()
  apple=c.execute("SELECT node_id FROM kg_node WHERE canonical_key='language:english:word:apple:meaning_in_context'").fetchone()[0]
  c.execute("INSERT INTO child_profile(child_id,nickname,grade,curriculum_region,status,created_at,updated_at) VALUES('child','学生','四年级','江苏南京','active',datetime('now'),datetime('now'))");c.commit()
 by_language={row['language']:row for row in lexemes}
 assert by_language['english']['part_of_speech']=='noun' and by_language['english']['phonics_or_ipa']=='/ˈæpəl/'
 assert by_language['chinese']['radical']=='日' and by_language['chinese']['structure']=='上下结构'
 app=KnowledgeGraphApplicationService(d);app.record_mistake(child_id='child',node_id=apple,mistake_id='apple-context')
 plan=app.review_plan(child_id='child',action_id='language-atom-review',max_items=3)
 reopened=KnowledgeGraphApplicationService(Database(tmp_path/'d.db'))
 restored=reopened.learning_path('child',apple)
 assert restored['mastery']['evidence_count']==1 and restored['materials'] and plan['items']
