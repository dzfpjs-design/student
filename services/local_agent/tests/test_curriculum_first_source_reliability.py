import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from home_study_agent.curriculum_first_source import CurriculumFirstSourceService, VALIDATOR, ensure_schema
from home_study_agent.curriculum_supply import CurriculumSupplyService
from home_study_agent.curriculum_supply_http import create_curriculum_supply_router
from home_study_agent.database import Database
from home_study_agent.learning_obligations import qualified_exact_score
from home_study_agent.local_supply import validate_item, LocalSupplyError
from home_study_agent.parent_auth import ParentAuth
from test_curriculum_first_source_http import first_source_fixture, publish, BASE, CHILD, NODE


def client_for(db):
    auth = ParentAuth(db)
    token = auth.setup('7391')
    app = FastAPI()
    app.include_router(create_curriculum_supply_router(CurriculumSupplyService(db), auth))
    return TestClient(app, client=('127.0.0.1',41200), headers={'X-HSC-Parent':token})


@pytest.mark.parametrize('mutation', ['source_withdrawn','source_revised','excerpt_changed','node_changed','scope_changed','standard_unreviewed','expired'])
def test_current_qualification_drops_without_rewriting_answer_or_other_child(tmp_path, mutation):
    db, source, spec, _ = first_source_fixture(tmp_path)
    client = client_for(db)
    packet = publish(client,spec)
    item = packet['diagnostic_item_id']
    with db.connect() as c:
        other_before = [tuple(r) for r in c.execute("SELECT * FROM diagnostic_item WHERE knowledge_node_id LIKE 'node-p4-%'")]
        if mutation == 'source_withdrawn':
            c.execute("UPDATE knowledge_source SET review_status='quarantined' WHERE source_id=?",(source['source_id'],))
        elif mutation == 'source_revised':
            c.execute('UPDATE knowledge_source SET current_version=0 WHERE source_id=?',(source['source_id'],))
        elif mutation == 'excerpt_changed':
            c.execute("UPDATE knowledge_excerpt SET content_text='changed' WHERE excerpt_id=?",(spec['excerpt_id'],))
        elif mutation == 'node_changed':
            c.execute("UPDATE knowledge_node SET title='changed target' WHERE knowledge_node_id=?",(NODE,))
        elif mutation == 'scope_changed':
            c.execute('UPDATE curriculum_supply_selection SET version=version+1 WHERE child_id=?',(CHILD,))
        elif mutation == 'standard_unreviewed':
            c.execute("UPDATE curriculum_standard_requirement SET maturity='model_drafted' WHERE requirement_id='req'")
        else:
            c.execute("UPDATE knowledge_source SET effective_to='2000-01-01' WHERE source_id=?",(source['source_id'],))
        c.commit()
    resumed = CurriculumFirstSourceService(Database(db.path))
    assert not resumed.get(CHILD,packet['candidate_id'])['eligible']
    with db.connect() as c:
        assert qualified_exact_score(c,CHILD,item,'2,3') is None
        assert other_before == [tuple(r) for r in c.execute("SELECT * FROM diagnostic_item WHERE knowledge_node_id LIKE 'node-p4-%'")]
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0


def test_draft_is_not_a_seed_or_scoring_record_and_stage_cannot_be_relabelled(tmp_path):
    db, _, spec, _ = first_source_fixture(tmp_path)
    client = client_for(db)
    body = dict(action_id='draft',expected_version=0,expected_scope_version=1,source=spec)
    response = client.post(BASE+'/first-sources',json=body)
    assert response.status_code == 200, response.text
    packet = response.json()
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_item WHERE knowledge_node_id=?',(NODE,)).fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM specialized_supply_candidate').fetchone()[0] == 0
    assert client.post(BASE+'/first-sources',json={**body,'source':{**spec,'stage':'near_transfer'}}).status_code == 422
    assert client.post(BASE+'/first-sources',json={**body,'actor':'parent'}).status_code == 422
    checked = client.post(BASE+f"/{packet['candidate_id']}/validate",json=dict(action_id='v',expected_version=1))
    assert checked.json()['state'] == 'validated'
    service = CurriculumFirstSourceService(db)
    with pytest.raises(LocalSupplyError,match='BASELINE_ONLY'):
        service.transition(CHILD,packet['candidate_id'],'review',actor='parent',action_id='r',expected_version=2,
                           values=dict(decision='approve',note='不可伪称迁移',stage_review={'stage':'near_transfer'}))


def test_publish_rollback_then_exact_key_retry_and_concurrent_replay(tmp_path):
    db, _, spec, _ = first_source_fixture(tmp_path)
    service = CurriculumFirstSourceService(db)
    cid = service.generate(CHILD,spec,action_id='d',expected_scope_version=1)['candidate_id']
    service.transition(CHILD,cid,'validate',actor=VALIDATOR,action_id='v',expected_version=1)
    service.transition(CHILD,cid,'review',actor='parent',action_id='r',expected_version=2,values=dict(decision='approve',note='完整原题及课标已核对'))
    with db.connect() as c:
        c.execute("CREATE TRIGGER fail_first_publish BEFORE INSERT ON curriculum_first_source_event WHEN NEW.kind='publish' BEGIN SELECT RAISE(ABORT,'injected write failure'); END")
        c.commit()
    def attempt():
        return CurriculumFirstSourceService(Database(db.path)).transition(CHILD,cid,'publish',actor='parent',action_id='p',expected_version=3)
    with pytest.raises(sqlite3.IntegrityError,match='injected'):
        attempt()
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_item WHERE knowledge_node_id=?',(NODE,)).fetchone()[0] == 0
        assert c.execute('SELECT state,version,diagnostic_item_id FROM curriculum_first_source').fetchone()[:] == ('reviewed',3,None)
        c.execute('DROP TRIGGER fail_first_publish')
        c.commit()
    with ThreadPoolExecutor(max_workers=2) as pool:
        a,b = list(pool.map(lambda _: attempt(),range(2)))
    assert a == b and a['eligible']
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_item WHERE knowledge_node_id=?',(NODE,)).fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM curriculum_first_source_event WHERE kind='publish'").fetchone()[0] == 1


def test_exact_node_child_auth_and_unsupported_science_do_not_create_records(tmp_path):
    db, _, spec, _ = first_source_fixture(tmp_path)
    client = client_for(db)
    body = dict(action_id='draft',expected_version=0,expected_scope_version=1,source=spec)
    assert client.post(BASE.replace(CHILD,'child-primary-4')+'/first-sources',json=body).status_code == 404
    assert client.post(BASE+'/first-sources',json={**body,'source':{**spec,'knowledge_node_id':'node-j3-math-discriminant'}}).status_code == 422
    client.headers.pop('X-HSC-Parent')
    assert client.post(BASE+'/first-sources',json=body).status_code == 401
    with db.connect() as c:
        assert not c.execute("SELECT 1 FROM knowledge_node WHERE grade_band='四年级' AND subject='科学'").fetchone()
        assert c.execute('SELECT COUNT(*) FROM diagnostic_item WHERE knowledge_node_id=?',(NODE,)).fetchone()[0] == 0


def test_schema_borrows_transaction_and_plain_sqlite_backup_preserves_review(tmp_path):
    c = sqlite3.connect(':memory:')
    c.execute('BEGIN IMMEDIATE')
    ensure_schema(c)
    assert c.in_transaction
    c.rollback()
    assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='curriculum_first_source'").fetchone()
    c.close()
    db, _, spec, _ = first_source_fixture(tmp_path)
    packet = publish(client_for(db),spec)
    with sqlite3.connect(db.path) as original, sqlite3.connect(tmp_path/'snapshot.db') as backup:
        original.backup(backup)
    assert CurriculumFirstSourceService(Database(tmp_path/'snapshot.db')).get(CHILD,packet['candidate_id'])['eligible']


def test_same_grade_other_child_cannot_read_or_select_first_root_or_change_stage_fingerprint(tmp_path):
    from datetime import datetime, timezone, timedelta
    from home_study_agent.diagnostic_prompt import DiagnosticPromptService
    from home_study_agent.specialized_stages import _item_source, project_specialized_stages
    from home_study_agent.goal_section_coverage import _visible_source
    from home_study_agent.mistake_book import MistakeBookService
    db, _, spec, _ = first_source_fixture(tmp_path)
    with db.connect() as c:
        c.execute("UPDATE child_profile SET grade='初三' WHERE child_id='child-primary-4'")
        c.commit()
    now = datetime.now(timezone.utc)
    with db.connect() as c:
        before = project_specialized_stages(c,'child-primary-4','junior_exam',as_of=now.isoformat())
    packet = publish(client_for(db),spec)
    with db.connect() as c:
        item = validate_item(c,CHILD,packet['diagnostic_item_id'])
        assert DiagnosticPromptService._item(c,'child-primary-4',item['diagnostic_item_id']) is None
        assert not _visible_source(c,'child-primary-4',item)
        clauses, params = MistakeBookService._item_scope(c,'child-primary-4')
        assert not c.execute('SELECT 1 FROM diagnostic_item di WHERE di.diagnostic_item_id=? '+clauses,
                             (item['diagnostic_item_id'],*params)).fetchone()
        later = datetime.now(timezone.utc)
        projected, error = _item_source(c,CHILD,item,later)
        assert not error and projected['stage'] == 'baseline'
        assert projected['source_root_item_id'] == item['diagnostic_item_id']
        assert _item_source(c,CHILD,item,now-timedelta(days=1))[0] is None
        assert project_specialized_stages(c,'child-primary-4','junior_exam',as_of=now.isoformat()) == before


@pytest.mark.parametrize('modality', ['LISTENING','SPEAKING','WRITING','PHONICS','textbook_chapter'])
def test_seedless_entry_does_not_bypass_existing_objective_modality_boundary(tmp_path, modality):
    db, source, spec, _ = first_source_fixture(tmp_path)
    with db.connect() as c:
        # A catalog revision to a modality node cannot inherit an exact-text validator.
        if modality == 'textbook_chapter':
            c.execute('UPDATE knowledge_node SET node_type=? WHERE knowledge_node_id=?', (modality,NODE))
        else:
            c.execute('UPDATE knowledge_node SET code=? WHERE knowledge_node_id=?', ('J3.MATH.'+modality,NODE))
        c.commit()
    client = client_for(db)
    options = client.get(BASE+f"/sources/{source['source_id']}/options").json()
    assert options['first_sources'] == []
    result = client.post(BASE+'/first-sources',json=dict(action_id='d',expected_version=0,expected_scope_version=1,source=spec))
    assert result.status_code in (409,422), result.text
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM curriculum_first_source').fetchone()[0] == 0
