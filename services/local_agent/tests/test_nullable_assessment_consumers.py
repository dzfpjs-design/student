"""Real saved attempts, published sources and current consumer projections."""
import json

import pytest

from home_study_agent.database import Database
from home_study_agent.exam_simulation import ExamSimulationService
from home_study_agent.learning_state import LearningStateService
from home_study_agent.repository import StudyRepository
from home_study_agent.specialized_program import SpecializedProgramService, _now
from home_study_agent.knowledge_graph.host_diagnostic_bridge import HostDiagnosticKnowledgeGraphBridge
from test_open_response_supply_http import CHILD, prepared, publish_open
from test_curriculum_objective_source import setup_source
from test_exam_simulation import _database


def guided_open(tmp_path):
    db,supply,spec,_,_ = prepared(tmp_path,'数学')
    item = publish_open(supply,spec)
    sid = StudyRepository(db).start_session(CHILD,'session')['session_id']
    service = SpecializedProgramService(db)
    service.ensure_schema()
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?",(sid,))
        node = c.execute('SELECT * FROM knowledge_node WHERE knowledge_node_id=(SELECT knowledge_node_id FROM diagnostic_item WHERE diagnostic_item_id=?)',(item['diagnostic_item_id'],)).fetchone()
        domain = service._mastery_domain('junior_exam',node)
        # Exact persisted queue fixture; this test does not claim producer selection.
        c.execute('''INSERT INTO specialized_assessment_run(run_id,child_id,session_id,track,catalog_track,subject,
            phase,item_queue_json,status,started_at,client_action_id)
            VALUES('open-guided',?,?,'junior_exam','school','数学','baseline',?,'active',?,'run')''',
            (CHILD,sid,json.dumps([{'diagnostic_item_id':item['diagnostic_item_id'],'domain_id':domain}]),_now()))
        c.commit()
    attempt = LearningStateService(db).submit_diagnostic(sid,item['diagnostic_item_id'],'原作答待评价','answer')
    assert attempt['correctness'] is None
    return db,service,supply,item,sid,attempt


@pytest.mark.parametrize('fault',['unknown','source_withdrawn','cancelled','legacy_cached_zero'])
def test_guided_unknown_and_invalid_sources_do_not_enter_score_sink(tmp_path,fault):
    db,service,supply,item,sid,attempt = guided_open(tmp_path)
    if fault=='source_withdrawn':
        supply.withdraw(CHILD,item['candidate_id'],actor='parent',note='Withdraw source',action_id='withdraw',expected_version=4)
    with db.connect() as c:
        if fault=='cancelled': c.execute("UPDATE specialized_assessment_run SET status='cancelled' WHERE run_id='open-guided'")
        if fault=='legacy_cached_zero':
            queue = json.loads(c.execute("SELECT item_queue_json FROM specialized_assessment_run WHERE run_id='open-guided'").fetchone()[0])
            c.execute("INSERT INTO specialized_assessment_run_attempt VALUES ('open-guided',?,?,?,0,?)",
                (attempt['attempt_id'],attempt['diagnostic_item_id'],queue[0]['domain_id'],attempt['created_at']))
        c.commit()
    result = service.capture_guided_attempt(sid,{**attempt,'correctness':1,'score_status':'verified_local'})
    if fault=='cancelled': assert result is None
    else:
        assert result['domain_results']=={} and result['reference_domain_results']=={}
        if fault=='unknown': assert result['capture_status']=='needs_review'
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM specialized_assessment_run_attempt').fetchone()[0]==int(fault=='legacy_cached_zero')
        assert c.execute('SELECT COUNT(*) FROM specialized_assessment_event').fetchone()[0]==0
    reopened = SpecializedProgramService(Database(db.path)).guided_status('open-guided')
    assert reopened['domain_results']=={} and reopened['qualified_items']==0
    assert HostDiagnosticKnowledgeGraphBridge(db).record_attempt(attempt['attempt_id'])['status']=='manual_confirmation_required'


def test_legacy_exam_reference_scores_are_not_verified_totals(tmp_path):
    db,sid = _database(tmp_path/'legacy-reference.db')
    SpecializedProgramService(db).ensure_schema()
    exam = ExamSimulationService(db)
    run = exam.start(sid,'competition_math','数学','practice',2,20,'start')
    while run['status']=='active':
        with db.connect() as c:
            expected = json.loads(c.execute('SELECT expected_answer_json FROM diagnostic_item WHERE diagnostic_item_id=?',
                (run['current_item']['diagnostic_item_id'],)).fetchone()[0])
        answer = ' '.join(expected.get('keywords',[])) or '原思路'
        response = exam.submit_answer(run['run_id'],answer,'answer-'+str(run['current_position']))
        assert response['attempt']['score_status']=='needs_review'
        run = response['exam']
    assert run['result']['score'] is None and run['result']['score_status']=='needs_review'
    assert run['result']['verified_items']==0 and run['result']['domain_results']=={}
    with db.connect() as c:
        assert all(row[0] is None for row in c.execute('SELECT correctness FROM specialized_exam_item'))
        assert c.execute('SELECT COUNT(*) FROM specialized_assessment_event').fetchone()[0]==0


@pytest.mark.parametrize('fault',['source_withdrawn','wrong_child','answer_changed'])
def test_exam_current_result_and_roadmap_drop_withdrawn_exact_source(tmp_path,fault):
    db,supply,spec,source,child = setup_source(tmp_path,'语文')
    published = publish_open(supply,spec)
    sid = StudyRepository(db).start_session(child,'session')['session_id']
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?",(sid,))
        c.commit()
    exam = ExamSimulationService(db)
    run = exam.start(sid,'junior_exam','语文','practice',100,30,'exam-start')
    seen = set()
    while run['status']=='active':
        current = run['current_item']['diagnostic_item_id']
        seen.add(current)
        answer = source['expected_answer']['exact_answers'][0] if current==published['diagnostic_item_id'] else '不会'
        run = exam.submit_answer(run['run_id'],answer,'answer-'+str(run['current_position']))['exam']
    assert published['diagnostic_item_id'] in seen
    assert run['result']['verified_items']==1 and run['result']['domain_results']
    with db.connect() as c:
        recorded = c.execute('SELECT result_json FROM specialized_exam_run WHERE run_id=?',(run['run_id'],)).fetchone()[0]
        assessment_id = c.execute('SELECT assessment_id FROM specialized_assessment_event WHERE source_ref=?',(run['run_id'],)).fetchone()[0]
    if fault=='source_withdrawn':
        supply.withdraw(child,published['candidate_id'],actor='parent',note='Withdraw original source',action_id='withdraw',expected_version=4)
    else:
        with db.connect() as c:
            if fault=='wrong_child':
                c.execute("UPDATE diagnostic_attempt SET child_id='child-primary-4' WHERE diagnostic_item_id=?",(published['diagnostic_item_id'],))
            else:
                c.execute("UPDATE diagnostic_attempt SET submitted_answer='Changed original response' WHERE diagnostic_item_id=?",(published['diagnostic_item_id'],))
            c.commit()
    reopened = ExamSimulationService(Database(db.path)).status(run['run_id'])
    assert reopened['result']['verified_items']==0 and reopened['result']['domain_results']=={}
    assert reopened['result']['score'] is None and reopened['result']['score_status']=='needs_review'
    roadmap = SpecializedProgramService(db).roadmap(child,'junior_exam')
    events = [row for row in roadmap['assessment_history'] if row['assessment_id']==assessment_id]
    assert events and all(row['current_evidence_status']=='invalidated' for row in events)
    with db.connect() as c:
        assert c.execute('SELECT result_json FROM specialized_exam_run WHERE run_id=?',(run['run_id'],)).fetchone()[0]==recorded


def test_exam_real_null_open_attempt_remains_unknown_after_restart(tmp_path):
    db,supply,spec,_,_ = prepared(tmp_path,'物理')
    published = publish_open(supply,spec)
    sid = StudyRepository(db).start_session(CHILD,'session')['session_id']
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?",(sid,))
        c.commit()
    exam = ExamSimulationService(db)
    run = exam.start(sid,'junior_exam','物理','practice',100,30,'start')
    original = None
    while run['status']=='active':
        response = exam.submit_answer(run['run_id'],'说明变量与结论边界','answer-'+str(run['current_position']))
        if response['attempt']['diagnostic_item_id']==published['diagnostic_item_id']:
            original = response['attempt']
            assert original['correctness'] is None
        run = response['exam']
    assert original is not None
    reopened = ExamSimulationService(Database(db.path)).status(run['run_id'])
    assert reopened['result']['score'] is None and reopened['result']['verified_items']==0
    with db.connect() as c:
        row = c.execute('''SELECT sei.correctness,da.correctness FROM specialized_exam_item sei
            JOIN diagnostic_attempt da USING(attempt_id) WHERE da.attempt_id=?''',(original['attempt_id'],)).fetchone()
        assert tuple(row)==(None,None)
