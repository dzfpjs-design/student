from home_study_agent.database import Database
from home_study_agent.paper_scoring import PaperScoringService
from home_study_agent.paper_scoring_source_authority import PaperScoringSourceAuthorityService
from home_study_agent.paper_scoring_source_match import PaperScoringSourceMatchService
from home_study_agent.knowledge_graph.textbook_page_service import TextbookOcrPageInput, TextbookOcrPageService
from test_paper_scoring_source_authority import prepared


def test_formal_rejudging_stays_unscored_and_preserves_audit_across_restart(tmp_path):
    fixture, request, _ = prepared(tmp_path, runtime_mode='formal')
    database = Database(fixture.path)
    pages = TextbookOcrPageService(database)
    page = dict(title='合成练习册', material_kind='workbook', grade_scope='初三', subject_scope='数学',
                source_ref='local_parent_textbook_capture:formal-gate', ocr_confidence=.95)
    pages.import_page(TextbookOcrPageInput(**page, ocr_text=f"{request['question_text']} 参考答案：90"))
    assert PaperScoringSourceMatchService(database).process_next()['status'] == 'candidate'
    first = PaperScoringSourceAuthorityService(database).process_next()
    assert first['correctness'] == 0
    pages.import_page(TextbookOcrPageInput(**page, ocr_text=f"{request['question_text']} 参考答案：80"))
    assert PaperScoringSourceMatchService(database).process_next()['status'] == 'candidate'
    next_source = PaperScoringSourceAuthorityService(database).process_next()
    assert next_source['decision_id'] is None and next_source['correctness'] is None
    for _ in range(2):
        restarted = PaperScoringService(Database(database.path))
        assert restarted.resolve(request['request_id'], 'child', 'formal-repeat-action') is None
        current = restarted.for_candidate(request['candidate_id'], 'child')
        assert current['decision_id'] is None
        assert current['authority_supply']['status'] == 'blocked_source'
    with database.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM paper_scoring_decision_revision').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM paper_scoring_decision').fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM paper_scoring_supply_event WHERE reason='FORMAL_SCORE_REVISION_RELEASE_GATE'").fetchone()[0] == 1
