"""Synthetic public pages only: durable editing never grants source authority."""
import base64
from copy import deepcopy
from io import BytesIO
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
import pytest

from home_study_agent.database import Database
from home_study_agent.knowledge_graph.http_api import install_knowledge_graph_routes
from home_study_agent.knowledge_graph.parent_textbook_capture import ParentTextbookCaptureService, CaptureConflictError
from home_study_agent.knowledge_graph.parent_textbook_drafts import ParentTextbookDraftService
from test_parent_textbook_capture import Adapter, Auth
from test_parent_textbook_drafts import store, payload
from test_portable_textbook_draft_rekey import _store, OLD_KEY, NEW_KEY, PASSPHRASE


def capture_pair(database, child='child-4'):
    capture = ParentTextbookCaptureService(database, Adapter())
    pages = []
    for role, color in [('paper', 'white'), ('answer_key', 'yellow')]:
        raw = BytesIO()
        Image.new('RGB', (32, 32), color).save(raw, format='PNG')
        pages.append(capture.recognize_file(child, base64.b64encode(raw.getvalue()).decode(),
            {'title': 'public synthetic '+role, 'page_number': 1}, 'objective-ocr-'+child+'-'+role))
    return capture, pages


def objective(pages):
    return {'question_number': '6', 'full_question': '公开合成原题：十二加五等于多少？',
            'official_answers': ['17'], 'candidate_node_id': None,
            **{role: {'capture_ref': page['capture_ref'], 'source_version': page['source_version']}
               for role, page in zip(('paper', 'answer_key'), pages)}}


def no_authority(database):
    names = ('knowledge_source', 'diagnostic_item', 'diagnostic_attempt', 'review_task', 'paper_answer_authority')
    with database.connect() as c:
        return {name: c.execute('SELECT COUNT(*) FROM '+name).fetchone()[0]
                for name in names if c.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone()}


def save(service, value, action='objective-save-001', revision=0, child='child-4', generation=None, scope='capture'):
    return service.save(child, scope, revision, action, value,
                        expected_generation=generation or service.read(child, scope)['storage_generation'])


def test_objective_pair_encrypted_restart_is_editing_not_approval(store):
    database, service = store
    _, pages = capture_pair(database)
    before = no_authority(database)
    value = payload(objective_draft=objective(pages))
    saved = save(service, value)
    assert saved['payload'] == value and saved['stale_refs'] == []
    restarted = ParentTextbookDraftService(Database(database.path, media_root=database.path.parent,
                                                    data_encryption_key=b'0'*32))
    assert restarted.read('child-4', 'capture') == saved
    assert restarted.read('child-9', 'capture')['payload'] is None
    with database.connect() as c:
        encrypted = bytes(c.execute('SELECT encrypted_payload FROM kg_parent_textbook_form_draft').fetchone()[0])
        assert '公开合成原题'.encode() not in encrypted and b'official_answers' not in encrypted
    assert no_authority(database) == before
    assert all(page['status'] == 'needs_review' for page in pages)


@pytest.mark.parametrize('grade', ['四年级', '初三'])
def test_candidate_node_must_be_visible_to_this_child_without_granting_mastery(store, grade):
    database, service = store
    _, pages = capture_pair(database)
    with database.connect() as con:
        con.execute('''INSERT INTO knowledge_node
            (knowledge_node_id,node_type,track,subject,grade_band,code,title,description,source_ref,knowledge_version,status)
            VALUES('public-objective-node','skill','school','数学',?,'public-objective-node','公开合成加法','','public synthetic','v1','active')''', (grade,))
        con.commit()
    before = no_authority(database)
    value = payload(objective_draft={**objective(pages), 'candidate_node_id': 'public-objective-node'})
    if grade == '四年级':
        assert save(service, value)['payload'] == value
    else:
        with pytest.raises(CaptureConflictError): save(service, value)
        assert service.read('child-4', 'capture')['revision'] == 0
    assert no_authority(database) == before


@pytest.mark.parametrize('bad', ['foreign_child', 'stale_paper', 'stale_answer', 'same_page', 'approval',
                                'review_task', 'long_question', 'unknown_node', 'wrong_scope'])
def test_objective_rejects_wrong_sources_or_authority_fields(store, bad):
    database, service = store
    _, pages = capture_pair(database)
    draft = objective(pages)
    if bad == 'foreign_child':
        _, foreign = capture_pair(database, 'child-9'); draft['answer_key'] = objective(foreign)['answer_key']
    elif bad == 'stale_paper': draft['paper']['source_version'] = '0'*64
    elif bad == 'stale_answer': draft['answer_key']['source_version'] = '0'*64
    elif bad == 'same_page': draft['answer_key'] = deepcopy(draft['paper'])
    elif bad == 'approval': draft['approved_authority'] = True
    elif bad == 'review_task': draft['review_task_id'] = 'invented'
    elif bad == 'long_question': draft['full_question'] = 'x'*1201
    elif bad == 'unknown_node': draft['candidate_node_id'] = 'not-a-current-node'
    with pytest.raises(ValueError):
        save(service, payload(objective_draft=draft), scope='language' if bad == 'wrong_scope' else 'capture')
    assert service.read('child-4', 'capture')['revision'] == 0


@pytest.mark.parametrize('changed', [0, 1])
def test_either_page_changed_rejects_even_retry_but_keeps_saved_work(store, changed):
    database, service = store
    capture, pages = capture_pair(database)
    value = payload(objective_draft=objective(pages)); saved = save(service, value)
    capture.review('child-4', pages[changed]['capture_ref'], 'explicit corrected public page', 'objective-page-review')
    assert service.read('child-4', 'capture')['payload'] == value
    assert pages[changed]['capture_ref'] in service.read('child-4', 'capture')['stale_refs']
    with pytest.raises(CaptureConflictError): save(service, value)
    with pytest.raises(CaptureConflictError): save(service, value, 'objective-edit-001', saved['revision'])
    assert service.read('child-4', 'capture')['revision'] == saved['revision']


def test_actual_asgi_save_lost_response_readback_and_retry_are_one_write(store):
    database, _ = store; _, pages = capture_pair(database)
    app = FastAPI(); app.state.ocr_workflow = SimpleNamespace(adapter=Adapter())
    install_knowledge_graph_routes(app, database, Auth())
    path = '/v1/parent/knowledge-graph/textbook-capture/child-4/drafts/capture'
    with TestClient(app) as client:
        assert client.get(path).status_code == 401
        headers = {'X-HSC-Parent': 'ok'}
        generation = client.get(path, headers=headers).json()['draft']['storage_generation']
        body = dict(expected_revision=0, expected_generation=generation, client_action_id='objective-http-action',
                    payload=payload(objective_draft=objective(pages)))
        before = no_authority(database)
        # Simulate loss after the real server commits, not an alternative fake save.
        response = client.put(path, json=body, headers=headers)
        assert response.status_code == 200, response.text
        readback = client.get(path, headers=headers).json()['draft']
        assert readback['revision'] == 1 and readback['payload'] == body['payload']
        assert client.put(path, json=body, headers=headers).json()['draft'] == readback
        assert no_authority(database) == before
        assert client.get(path.replace('child-4', 'child-9'), headers=headers).json()['draft']['payload'] is None


@pytest.mark.parametrize('raw_answers', [None, '测'*201, '\n'.join(['17']*25)])
def test_objective_portable_restore_rekeys_sources_and_rejects_old_generation(tmp_path, raw_answers):
    db, drafts, privacy = _store(tmp_path/'source', OLD_KEY)
    original, pages = capture_pair(db, 'public-child-a'); value = payload(objective_draft=objective(pages))
    if raw_answers is not None:
        value['objective_draft']['raw_editing'] = {'full_question': value['objective_draft']['full_question'], 'official_answers_text': raw_answers}
        value['objective_draft']['official_answers'] = []
    saved = save(drafts, value, child='public-child-a'); before = no_authority(db)
    raw_pages = [original.source_image('public-child-a', page['capture_ref'])[0] for page in pages]
    archive = privacy.create_portable_backup(PASSPHRASE)['path']
    target_db, target_drafts, target_privacy = _store(tmp_path/'target', NEW_KEY)
    assert target_privacy.restore_portable_backup(archive, PASSPHRASE, '确认迁移恢复')['status'] == 'restored'
    restored = target_drafts.read('public-child-a', 'capture')
    assert restored['payload'] == value and restored['revision'] == saved['revision'] and restored['stale_refs'] == []
    assert restored['storage_generation'] != saved['storage_generation']
    with pytest.raises(CaptureConflictError):
        save(target_drafts, value, child='public-child-a', generation=saved['storage_generation'])
    target_capture = ParentTextbookCaptureService(target_db, Adapter())
    assert [target_capture.source_image('public-child-a', page['capture_ref'])[0] for page in pages] == raw_pages
    changed = deepcopy(value); changed['objective_draft']['full_question'] += '（人工未审核草稿）'
    if raw_answers is not None: changed['objective_draft']['raw_editing']['full_question'] = changed['objective_draft']['full_question']
    new = save(target_drafts, changed, 'objective-after-restore', 1, child='public-child-a')
    restarted = ParentTextbookDraftService(Database(target_db.path, media_root=tmp_path/'target', data_encryption_key=NEW_KEY))
    assert restarted.read('public-child-a', 'capture') == new
    assert no_authority(target_db) == before


@pytest.mark.parametrize('bad', ['stale_answer_projection', 'stale_question_projection', 'raw_null', 'raw_limit'])
def test_raw_editing_cannot_smuggle_a_stale_candidate_or_exceed_bounded_storage(store, bad):
    database, service = store; _, pages = capture_pair(database)
    value = payload(objective_draft=objective(pages)); draft = value['objective_draft']
    draft['raw_editing'] = {'full_question': draft['full_question'], 'official_answers_text': '17'}
    if bad == 'stale_answer_projection': draft['raw_editing']['official_answers_text'] = '测'*201
    elif bad == 'stale_question_projection': draft['raw_editing']['full_question'] = '不同的原始题干'
    elif bad == 'raw_null': draft['raw_editing'] = None
    elif bad == 'raw_limit': draft['raw_editing']['full_question'] = '😀'*100001; draft['full_question'] = ''
    with pytest.raises(ValueError): save(service, value)
    assert service.read('child-4', 'capture')['revision'] == 0
