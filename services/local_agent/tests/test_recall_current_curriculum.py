import json

from home_study_agent.curriculum_supply import CurriculumSupplyService
from home_study_agent.repository import StudyRepository
from test_curriculum_catalog_reliability import CHILD, catalog


def test_recall_respects_selected_term_and_preserves_original_candidates(catalog):
    db, kb, _, _, _ = catalog
    source = kb.import_source(CHILD, {
        'source_type': 'school_material', 'title': 'Textbook selection', 'subject': '数学',
        'grade_band': '四年级', 'version_label': '苏教版', 'source_ref': 'fixture:selection',
        'license_status': 'family_owned_index', 'parent_reviewed': True, 'content_text': '目录',
    })
    supply = CurriculumSupplyService(db)
    scope = dict(grade='四年级', subject='数学', edition='苏教版', semester='下学期',
                 standard_scope='第二学段', textbook_source_id=source['source_id'])
    supply.select_curriculum(CHILD, scope, actor='parent', action_id='scope', expected_version=0)
    repo = StudyRepository(db)
    sid = repo.start_session(CHILD, 'recall-session')['session_id']
    repo.add_recall(sid, '数学', '今天讲小数加法怎样把数位对齐', 'matching-term')
    with db.connect() as c:
        first = c.execute("SELECT * FROM classroom_recall WHERE client_action_id='matching-term:candidate'").fetchone()
        assert json.loads(first['candidate_nodes_json'])[0]['knowledge_node_id'].startswith('curriculum:')
    supply.select_curriculum(CHILD, {**scope, 'semester': '上学期'}, actor='parent', action_id='new-term', expected_version=1)
    repo.add_recall(sid, '数学', '今天讲小数加法怎样把数位对齐', 'nonmatching-term')
    with db.connect() as c:
        current = c.execute("SELECT * FROM classroom_recall WHERE client_action_id='nonmatching-term:candidate'").fetchone()
        assert json.loads(current['candidate_nodes_json']) == []
        assert current['confirmation_status'] == 'unknown'
        assert c.execute('SELECT candidate_nodes_json FROM classroom_recall WHERE recall_id=?',
                         (first['recall_id'],)).fetchone()[0] == first['candidate_nodes_json']
