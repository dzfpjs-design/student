"""Real local original-PDF manual flow; controlled nodes never qualify real family work."""
from pathlib import Path
import base64, hashlib, json, os, shutil, socket, sqlite3
import pytest
from home_study_agent import manual_standard_scope as manual
from home_study_agent import app
ROOT = Path(__file__).resolve().parents[3]
def evidence_root(tmp_path):
    root = Path(os.environ.get('HSC_MANUAL_INTEGRATION_EVIDENCE_ROOT', str(tmp_path / 'manual-evidence')))
    root.mkdir(parents=True, exist_ok=True)
    return root
from fastapi.testclient import TestClient
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.repository import StudyRepository
from home_study_agent.curriculum_standards import CurriculumStandardService, InvalidCurriculumStandardSourceError
from home_study_agent.knowledge_base import KnowledgeBaseService
from home_study_agent.curriculum_supply import CurriculumSupplyService
from home_study_agent.local_supply import validate_item,LocalSupplyError
from test_curriculum_supply import prepared
from privacy_restore_operation import restore_operation
from synthetic_native_restore_request import KEY, signed_restore_post
CHILD='child-primary-4';NODE='synthetic-manual-math-skill'
BASE=f'/v1/parent/children/{CHILD}/manual-standard-requirements'
FLOW=f'/api/parent/children/{CHILD}/curriculum-supply'
ASSET=ROOT/'.agentic-engineering/operations/current-r90-real-math-scope-and-question-r1'
QUOTE='（3）在解决简单实际问题的过程中，理解四则运算的意义，能进行整数四则混合运算。'
STAGE='第二学段（3～4年级）'
class NoCloud:
    async def complete(self,*a,**k):raise AssertionError('CLOUD_FORBIDDEN')
@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*a,**k):raise AssertionError('NETWORK_FORBIDDEN')
    monkeypatch.setattr(socket.socket,'connect',denied)
@pytest.fixture(scope='session')
def official(tmp_path_factory):
    root=tmp_path_factory.mktemp('actual-official-pdf')
    shutil.copyfile(ASSET/'mathematics-2022-official.pdf',root/'mathematics.pdf')
    sha=hashlib.sha256((root/'mathematics.pdf').read_bytes()).hexdigest()
    manifest={'format':'hsc_official_curriculum_sources_v1','sources':[{'source_key':'moe-math-2022-manual-test','jurisdiction':'CN','source_type':'national_standard','subject':'数学','title':'义务教育数学课程标准（2022年版）','version':'2022年版（2022年5月9日以此为准）','official_page_url':'https://www.moe.gov.cn/srcsite/A26/s8001/202204/t20220420_619921.html','pdf_filename':'mathematics.pdf','pdf_sha256':sha,'page_count':189}]}
    (root/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False),encoding='utf-8')
    return root,sha

def client(tmp,db,*,data_encryption_key=None,native_launch_attestation_key=None):
    settings=Settings(data_dir=tmp,database_path=db.path,maintenance_enabled=False,session_token='isolated-manual-scope',data_encryption_key=data_encryption_key,native_launch_attestation_key=native_launch_attestation_key)
    return TestClient(app.create_app(settings,model_gateway=NoCloud()),client=('127.0.0.1',41190),headers={'X-HSC-Session':settings.session_token})
def auth(c,restart=False):
    r=c.post('/v1/parent/unlock' if restart else '/v1/parent/setup',json={'pin':'7391'});assert r.status_code==200,r.text
    c.headers['X-HSC-Parent']=r.json()['parent_token']
def post(c,path,body,status=200):
    r=c.post(path,json=body);assert r.status_code==status,r.text
    return r.json()
def register(c,db,official):
    root,sha=official
    r=post(c,'/v1/parent/curriculum-standards/import',{'root_path':str(root)})
    assert r=={'status':'imported','source_count':1,'page_count':0}
    with db.connect()as con:src=dict(con.execute("SELECT * FROM curriculum_standard_source WHERE source_key='moe-math-2022-manual-test'").fetchone())
    assert src['page_count']==189 and src['status']=='downloaded' and src['extracted_page_count']==0
    return dict(action_id='manual-draft-1',expected_version=0,source_id=src['source_id'],source_pdf_sha256=sha,source_version=src['version_label'],knowledge_node_id=NODE,stage_scope='第二学段',page_number=28,stage_page_number=27,page_text=QUOTE,stage_page_text=STAGE,title='四则运算与实际问题（人工单条审核）',quote=QUOTE,observable_outcome='在简单实际问题中说明整数四则运算及经过时间的计算。')
def decide(c,cid,version,kind='approve',action=None):
    return post(c,BASE+f'/{cid}/'+('withdraw'if kind=='withdraw'else'review'),dict(action_id=action or 'manual-'+kind,expected_version=version,note='已逐页核对服务生成的官方原页、准确摘录、版次及当前学校节点；仅隔离合成范围。',decision='reject'if kind=='reject'else'approve'))
def setup(tmp,official):
    db,_,_=prepared(tmp)
    # Explicit unseeded test-only node. This fixture never establishes a real
    # school alignment, model result, family approval, or published child scope.
    with db.connect() as c:
        c.execute("INSERT INTO knowledge_node VALUES(?,'skill','school','数学','四年级','TEST.MANUAL.MATH','隔离合成整数运算技能','仅HTTP契约测试','isolated:manual-scope','fixture-v1','active')",(NODE,))
        c.commit()
    return db

def test_actual_official_two_pages_draft_images_recreate_without_real_node(tmp_path,official):
    db=Database(tmp_path/'real-pages.db');db.initialize();StudyRepository(db).seed_family()
    with client(tmp_path,db)as c:
        body=register_after_auth(c,db,official);body['knowledge_node_id']=None
        with db.connect()as con: node_count_before=con.execute('SELECT COUNT(*) FROM knowledge_node').fetchone()[0]
        r=post(c,BASE,body);cid=r['candidate_id'];assert r['state']=='draft'and not r['eligible']
        img=c.get(BASE+f'/{cid}?images=true').json()
        assert len(img['pages'])==2 and img['source']['page_count']==189 and img['source']['status']=='downloaded'
        for p in img['pages']:
            raw=base64.b64decode(p['image_data_url'].split(',',1)[1]);assert hashlib.sha256(raw).hexdigest()==p['image_sha256']
        assert post(c,BASE+f'/{cid}/review',dict(action_id='real-no-node-review',expected_version=1,note='不可造真实节点',decision='approve'),409)['detail']['code']=='MANUAL_CURRENT_SCHOOL_NODE_REQUIRED'
        assert post(c,BASE,{**body,'image_sha256':'a'*64},422)['detail']
        assert post(c,BASE,body)['candidate_id']==cid
    with client(tmp_path,Database(db.path))as c:
        auth(c,True);assert c.get(BASE+f'/{cid}?images=true').json()==img
        assert c.get(BASE.replace(CHILD,'child-junior-3')+f'/{cid}').status_code==404
    with db.connect()as con:
        assert con.execute('SELECT COUNT(*) FROM curriculum_standard_requirement').fetchone()[0]==0
        assert con.execute('SELECT COUNT(*) FROM curriculum_standard_page').fetchone()[0]==0
        assert con.execute('SELECT COUNT(*) FROM knowledge_node').fetchone()[0]==node_count_before
        assert con.execute('SELECT COUNT(*) FROM review_task').fetchone()[0]==0
    evidence={'status':'ACTUAL_OFFICIAL_TWO_SERVER_RENDERED_PAGES_DRAFT_AND_RECREATE_EXACT','pages':[{k:v for k,v in p.items()if k!='image_data_url'}for p in img['pages']],'source_sha256':body['source_pdf_sha256'],'actual_full_pdf_pages':189,'approved_requirement_count':0,'current_school_node_missing':True,'source_status':'downloaded','parent_auth_required':True,'network_forbidden':True,'no_ocr_or_model_claim':True}
    (evidence_root(tmp_path)/'actual-public-pages.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n')
    save_snapshot(db,'real-draft')

def register_after_auth(c,db,official):
    auth(c);return register(c,db,official)
def save_snapshot(db,name):
    requested = os.environ.get('HSC_MANUAL_INTEGRATION_EVIDENCE_ROOT')
    if not requested:
        return
    dest=Path(requested)/name;dest.mkdir(parents=True,exist_ok=True)
    with db.connect() as origin:
        target=sqlite3.connect(dest/'source.sqlite')
        origin.backup(target)
        assert target.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        target.close()

def objective(db):
    value=dict(schema='curriculum_objective_source_v1',subject='数学',grade='四年级',knowledge_node_id=NODE,assessment_mode='objective',answer_type='text',prompt='隔离合成原题：求18乘5与18乘3之差。',expected_answer=dict(local_rule='local_exact_v1',exact_answers=['36']))
    source=KnowledgeBaseService(db).import_source(CHILD,dict(source_type='school_material',title='隔离合成算式，非真实华杯资格',subject='数学',grade_band='四年级',version_label='隔离合成2026',source_ref='isolated:manual-standard',license_status='family_owned_index',parent_reviewed=True,content_text=json.dumps(value,ensure_ascii=False)))
    with db.connect()as con:excerpt=con.execute('SELECT excerpt_id FROM knowledge_excerpt WHERE source_id=?',(source['source_id'],)).fetchone()[0]
    CurriculumSupplyService(db).select_curriculum(CHILD,dict(grade='四年级',subject='数学',edition='隔离合成2026',semester='上学期',standard_scope='第二学段',textbook_source_id=source['source_id']),actor='parent',action_id='manual-select',expected_version=1)
    return source,excerpt

def test_controlled_manual_review_strict_first_source_publish_reopen_withdraw(tmp_path,official):
    db=setup(tmp_path,official)
    with client(tmp_path,db)as c:
        body=register_after_auth(c,db,official)
        without=c.headers.pop('X-HSC-Parent');assert c.post(BASE,json=body).status_code==401;c.headers['X-HSC-Parent']=without
        d=post(c,BASE,body);cid=d['candidate_id']
        a=decide(c,cid,1);assert a['eligible']and a['state']=='approved'and a['version']==2
        assert decide(c,cid,1)==a # exact original replay, not a second grant
        assert post(c,BASE+f'/{cid}/review',dict(action_id='stale-review-action',expected_version=1,note='stale',decision='approve'),409)['detail']['code']=='MANUAL_VERSION_CONFLICT'
        source,excerpt=objective(db)
        opts=c.get(FLOW+f"/sources/{source['source_id']}/options").json()
        assert any(o['requirement_id']==a['requirement_id']and o['available']and o['quote']==QUOTE for o in opts['standards'])
        spec=dict(knowledge_node_id=NODE,excerpt_id=excerpt,requirement_id=a['requirement_id'],standard_page=28,standard_quote=QUOTE,explanation='隔离答案：90减54为36，不授真实孩子或真题信用。',difficulty=1)
        out=post(c,FLOW+'/first-sources',dict(source=spec,action_id='manual-first-source',expected_version=0,expected_scope_version=2))
        assert out['state']=='draft'and out['payload']['seed_item_id']is None
        for ver,op in enumerate(('validate','review','publish'),1):
            b=dict(action_id='manual-first-'+op,expected_version=ver)
            if op=='review':b.update(decision='approve',note='显式审核仅隔离合成原题和官方页，非真实华杯current qualification。')
            out=post(c,FLOW+f"/{out['candidate_id']}/{op}",b)
        assert out['eligible']and out['state']=='published'
        item=out['diagnostic_item_id']
        with db.connect()as con:
            snapshot=json.loads(con.execute('SELECT source_snapshot_json FROM curriculum_first_source WHERE candidate_id=?',(out['candidate_id'],)).fetchone()[0])
            assert snapshot['manual_requirement_review']['origin']==manual.ORIGIN
            assert snapshot['standard']['status'] == 'downloaded'
        truncated = official[0] / 'manual-two-page-ocr.jsonl'
        truncated.write_text(json.dumps({'page':27,'text':STAGE},ensure_ascii=False)+'\n'+json.dumps({'page':28,'text':QUOTE},ensure_ascii=False)+'\n')
        claimed = json.loads((official[0]/'manifest.json').read_text())['sources'][0]
        claimed.update(ocr_status='completed',ocr_pages_filename=truncated.name)
        with pytest.raises(InvalidCurriculumStandardSourceError,match='OCR页数与官方PDF不一致'):
            CurriculumStandardService(db)._import_source(official[0].resolve(), claimed)
        with db.connect()as con:
            assert con.execute('SELECT status,page_count,extracted_page_count FROM curriculum_standard_source WHERE source_id=?',(body['source_id'],)).fetchone()[:]==('downloaded',189,2)
    with client(tmp_path,Database(db.path))as c:
        auth(c,True)
        assert c.get(FLOW+f"/{out['candidate_id']}").json()==out
        w=decide(c,cid,2,'withdraw');assert not w['eligible']and w['state']=='withdrawn'
        changed=c.get(FLOW+f"/{out['candidate_id']}").json();assert not changed['eligible']
        options=c.get(FLOW+f"/sources/{source['source_id']}/options").json()
        assert not next(o for o in options['standards']if o['requirement_id']==a['requirement_id'])['available']
    with db.connect()as con:
        with pytest.raises(LocalSupplyError):validate_item(con,CHILD,item)
        assert con.execute('SELECT COUNT(*) FROM review_task').fetchone()[0]==0
        assert con.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==0
        assert con.execute('SELECT COUNT(*) FROM curriculum_manual_requirement_event WHERE candidate_id=?',(cid,)).fetchone()[0]==3
    save_snapshot(db,'controlled-withdrawn')
    (evidence_root(tmp_path)/'controlled-publish.json').write_text(json.dumps({'status':'CONTROLLED_SCOPE_HTTP_MANUAL_APPROVE_FIRST_SOURCE_PUBLISH_RECREATE_WITHDRAW','boundary':'synthetic_current_node/source; official PDF real; not actual family qualification','source_full_pages':189,'manual_pages':2,'source_status':'downloaded','whole_import_not_ready':True,'full_import_two_pages_cannot_claim_whole_pdf_completed':True,'manual_review_origin':manual.ORIGIN,'recreated_packet_exact':True,'withdraw_deauthorizes_existing_item':True,'learning_history_deleted':False,'review_task_count':0,'attempt_count':0},indent=2)+'\n')

def test_pdf_version_renderer_and_saved_page_tamper_fail_closed(tmp_path,official,monkeypatch):
    db=setup(tmp_path,official)
    with client(tmp_path,db)as c:
        body=register_after_auth(c,db,official)
        with monkeypatch.context()as m:
            m.setattr(manual,'pdf_renderer_available',lambda:False)
            err=post(c,BASE,body,503);assert err['detail']['code']=='MANUAL_PDF_RENDERER_UNAVAILABLE'
        d=post(c,BASE,body);cid=d['candidate_id']
        number=d['pages'][0]['pdf_page']
        with db.connect() as con:
            raw=con.execute('SELECT image FROM curriculum_manual_requirement_page WHERE candidate_id=? AND pdf_page=?',(cid,number)).fetchone()[0]
            con.execute('UPDATE curriculum_manual_requirement_page SET image=? WHERE candidate_id=? AND pdf_page=?',(raw+b'tamper',cid,number));con.commit()
        assert post(c,BASE+f'/{cid}/review',dict(action_id='tampered-page-approval',expected_version=1,note='not allowed',decision='approve'),409)['detail']['code']=='MANUAL_GENERATED_PAGE_CHANGED'
        with db.connect() as con:
            con.execute('UPDATE curriculum_manual_requirement_page SET image=? WHERE candidate_id=? AND pdf_page=?',(raw,cid,number));con.commit()
        with db.connect()as con:con.execute("UPDATE curriculum_standard_source SET version_label='modified' WHERE source_id=?",(body['source_id'],));con.commit()
        assert post(c,BASE+f'/{cid}/review',dict(action_id='changed-source-approval',expected_version=1,note='not allowed',decision='approve'),409)['detail']['code']=='MANUAL_SOURCE_VERSION_CHANGED'
        with db.connect()as con:assert con.execute('SELECT COUNT(*) FROM curriculum_standard_requirement WHERE source_id=?',(body['source_id'],)).fetchone()[0]==0

def test_current_scope_quote_and_manual_requirement_tamper_are_not_legacy_fallback(tmp_path,official):
    db=setup(tmp_path,official)
    with client(tmp_path,db)as c:
        body=register_after_auth(c,db,official);d=post(c,BASE,body);a=decide(c,d['candidate_id'],1)
        with db.connect()as con:
            with pytest.raises(manual.ManualScopeError,match='MANUAL_APPROVED_PAGE_QUOTE_REQUIRED'):manual.manual_requirement(con,CHILD,a['requirement_id'],28,QUOTE+'改',knowledge_node_id=NODE)
            with pytest.raises(manual.ManualScopeError,match='MANUAL_REQUIREMENT_NOT_CURRENT'):manual.manual_requirement(con,'child-junior-3',a['requirement_id'],28,QUOTE,knowledge_node_id=NODE)
            con.execute('UPDATE curriculum_standard_requirement SET title=? WHERE requirement_id=?',('tampered',a['requirement_id']));con.commit()
        assert c.get(BASE+f"/{d['candidate_id']}").json()['blockers']==['MANUAL_REQUIREMENT_CHANGED']
        with db.connect()as con:
            con.execute('UPDATE curriculum_standard_requirement SET title=? WHERE requirement_id=?',(body['title'],a['requirement_id']))
            con.execute("UPDATE knowledge_node SET knowledge_version='changed' WHERE knowledge_node_id=?",(NODE,));con.commit()
        assert c.get(BASE+f"/{d['candidate_id']}").json()['blockers']==['MANUAL_CURRENT_SCHOOL_SCOPE_CHANGED']


def verify_complete_pdf_backup_and_portable(tmp_path, official, *, switch_to_packaged=None):
    # A real copied official PDF is registered via HTTP; the original location
    # is then removed. No source path rebinding or new approval is manufactured.
    local_root = tmp_path / 'registered-official'
    shutil.copytree(official[0], local_root)
    copied = (local_root, official[1])
    db = setup(tmp_path, copied)
    with client(tmp_path, db, data_encryption_key=b's' * 32, native_launch_attestation_key=KEY) as c:
        body = register_after_auth(c, db, copied)
        draft = post(c, BASE, body)
        approved = decide(c, draft['candidate_id'], 1)
        exact = c.get(BASE + f"/{draft['candidate_id']}?images=true").json()
        assert exact['eligible'] and len(exact['pages']) == 2
        from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
        life = PrivacyLifecycleService(db, tmp_path, data_encryption_key=b's' * 32)
        life.initialize()
        backup = life.create_backup(prune=False)
        portable = life.create_portable_backup('synthetic-manual-backup-only')
        if switch_to_packaged is not None:
            switch_to_packaged()
        (local_root / 'mathematics.pdf').unlink()
        with db.connect() as con:
            con.execute('DELETE FROM curriculum_manual_requirement_page')
            con.execute('DELETE FROM curriculum_manual_pdf_blob')
            con.commit()
        assert manual.ManualStandardService(db).get(CHILD, draft['candidate_id'])['blockers']
        restored = signed_restore_post(c, headers={}, json={
            'backup_id': backup['backup_id'], 'confirmation': '确认恢复',
            'operation': restore_operation(c)})
        assert restored.status_code == 200, restored.text
        result = manual.ManualStandardService(Database(db.path)).get(CHILD, draft['candidate_id'], images=True)
        assert result == exact
    other_dir = tmp_path / 'fresh-portable-target'
    other_dir.mkdir()
    other_db = Database(other_dir / 'fresh.sqlite'); other_db.initialize()
    other_life = PrivacyLifecycleService(other_db, other_dir, data_encryption_key=b'n' * 32)
    other_life.initialize()
    with client(other_dir, other_db, data_encryption_key=b'n' * 32) as fresh:
        auth(fresh)
        restored = fresh.post('/v1/parent/backups/portable/restore', json={
            'path': portable['path'], 'passphrase': 'synthetic-manual-backup-only',
            'confirmation': '确认迁移恢复', 'operation': restore_operation(fresh)})
        assert restored.status_code == 200, restored.text
    after = manual.ManualStandardService(other_db).get(CHILD, draft['candidate_id'], images=True)
    assert after == exact and after['version'] == 2 and after['eligible']
    with other_db.connect() as con:
        record = con.execute('SELECT * FROM curriculum_manual_pdf_blob').fetchone()
        assert record['page_count'] == 189
        assert len(record['content']) == 40123943
        assert hashlib.sha256(record['content']).hexdigest() == official[1]
        assert con.execute('SELECT COUNT(*) FROM curriculum_manual_pdf_blob').fetchone()[0] == 1
        assert con.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert not con.execute('PRAGMA foreign_key_check').fetchall()
        assert con.execute('SELECT status,extracted_page_count FROM curriculum_standard_source WHERE source_id=?', (body['source_id'],)).fetchone()[:] == ('downloaded', 2)
        manual.purge_manual_requirements(con, CHILD)
        assert con.execute('SELECT COUNT(*) FROM curriculum_manual_requirement_page').fetchone()[0] == 0
        assert con.execute('SELECT COUNT(*) FROM curriculum_manual_requirement_event').fetchone()[0] == 0
        assert con.execute('SELECT COUNT(*) FROM curriculum_manual_requirement').fetchone()[0] == 0
        con.commit()
    (evidence_root(tmp_path) / ('actual-backup-portable-pdfium-transition.json' if switch_to_packaged is not None else 'actual-backup-portable.json')).write_text(json.dumps({
        'status': 'ACTUAL_SQLITE_BACKUP_AND_FRESH_PORTABLE_RESTORE_ORIGINAL_PDF_PAGES_AND_APPROVAL_EXACT',
        'boundary': 'official PDF real; current node and approval controlled synthetic fixture; no Windows or family credit',
        'original_external_pdf_removed_before_restores': True, 'pdfium_only_after_original_page_approval': switch_to_packaged is not None, 'complete_pdf_pages': 189,
        'complete_pdf_bytes': 40123943, 'complete_pdf_sha256': official[1], 'saved_page_sha256': [p['image_sha256'] for p in exact['pages']],
        'full_packet_exact': True, 'original_approval_version': 2, 'new_approval_or_path_rebinding': False,
        'whole_pdf_status': 'downloaded', 'manual_registered_pages': 2,
        'portable_new_database_integrity': 'ok', 'foreign_key_errors': 0,
        'manual_child_transcript_events_and_page_blobs_purge': True,
        'network_forbidden': True, 'model_calls': 0
    }, indent=2) + '\n')


def test_manual_scope_is_exact_child_grade_node_and_action_receipt(tmp_path, official):
    db = setup(tmp_path, official)
    with client(tmp_path, db) as c:
        body = register_after_auth(c, db, official)
        context = c.get(BASE + '/context').json()
        assert context['grade'] == '四年级' and context['child_id'] == CHILD
        assert any(n['knowledge_node_id'] == NODE for n in context['nodes'])
        assert c.get(BASE.replace(CHILD, 'child-junior-3') + '/context').status_code == 409
        assert post(c, BASE.replace(CHILD, 'child-junior-3'), body, 409)['detail']['code'] == 'MANUAL_CURRENT_SCHOOL_SCOPE_REQUIRED'
        d = post(c, BASE, body); a = decide(c, d['candidate_id'], 1)
        assert c.get(BASE + '/actions/manual-approve').json()['candidate_id'] == d['candidate_id']
        assert c.get(BASE.replace(CHILD, 'child-junior-3') + '/actions/manual-approve').status_code == 404
        with db.connect() as con:
            with pytest.raises(manual.ManualScopeError, match='MANUAL_APPROVED_SCHOOL_NODE_REQUIRED'):
                manual.manual_requirement(con, CHILD, a['requirement_id'], 28, QUOTE, knowledge_node_id='node-p4-math-arithmetic')
        changed = {**body, 'quote': QUOTE + 'changed'}
        assert post(c, BASE, changed, 409)['detail']['code'] == 'MANUAL_ACTION_CONFLICT'


def test_original_restore_guard_refuses_new_manual_write_without_rows(tmp_path, official):
    db = setup(tmp_path, official)
    with client(tmp_path, db) as c:
        body = register_after_auth(c, db, official)
        with db.external_effect_guard(timeout=0):
            response = c.post(BASE, json=body)
            assert response.status_code == 409
            assert response.json()['code'] == 'RESTORE_EXTERNAL_EFFECT_BUSY'
        with db.connect() as con:
            assert con.execute('SELECT COUNT(*) FROM curriculum_manual_requirement').fetchone()[0] == 0
            assert con.execute('SELECT COUNT(*) FROM curriculum_manual_pdf_blob').fetchone()[0] == 0
        d = post(c, BASE, body)
        with db.external_effect_guard(timeout=0):
            response = c.post(BASE + f"/{d['candidate_id']}/review", json=dict(action_id='restore-held-approval',expected_version=1,note='禁止恢复中新增审批',decision='approve'))
            assert response.status_code == 409
            assert response.json()['code'] == 'RESTORE_EXTERNAL_EFFECT_BUSY'
        assert c.get(BASE + f"/{d['candidate_id']}").json()['state'] == 'draft'
        with db.connect() as con:
            con.execute("UPDATE child_profile SET status='archived' WHERE child_id=?", (CHILD,));con.commit()
        assert c.get(BASE + '/context').status_code == 404


def test_actual_complete_pdf_and_original_pages_survive_backup_and_portable_without_external_pdf(tmp_path, official):
    verify_complete_pdf_backup_and_portable(tmp_path, official)


def test_actual_saved_poppler_pages_and_approval_survive_pdfium_only_restore_without_external_pdf(tmp_path, official, monkeypatch):
    import multiprocessing as mp
    from home_study_agent.pdf_renderer import pdf_renderer_available
    from home_study_agent.knowledge_ingestion.textbook_pdf import TextbookPdfOcrPipeline
    # Initial original PNGs are produced by the existing Poppler backend.
    # Subsequent validation uses the actual packaged worker, never a mocked PNG.
    assert TextbookPdfOcrPipeline._pdf_page_count(official[0] / 'mathematics.pdf') == 189
    def switch():
        monkeypatch.setattr('home_study_agent.knowledge_ingestion.textbook_pdf.shutil.which', lambda _: None)
        assert pdf_renderer_available()
    verify_complete_pdf_backup_and_portable(tmp_path, official, switch_to_packaged=switch)
    assert not mp.active_children()
