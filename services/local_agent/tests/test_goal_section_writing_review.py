"""Original manuscript/reviewer protocol; not a claim of calibrated exam quality."""
from datetime import datetime, timezone

from home_study_agent.database import Database
from home_study_agent.family_clock import family_today
from home_study_agent.goal_section_coverage import build_section_evidence
from home_study_agent.learning_rubric import RubricEvidenceService
from home_study_agent.official_goal_rules import target_rule_context
from test_goal_section_evidence import section
from test_writing_stage_reviews import writing_chain, reviewed_answer
from test_writing_response_origin import CHILD


def writing_section(db):
    now = datetime.now(timezone.utc)
    rules = target_rule_context('pet_path', {'target_date':'2026-12-01'},
        as_of_date=family_today(now), curriculum_region='南京')
    with db.connect() as c:
        c.execute('BEGIN')
        c.execute('PRAGMA query_only=ON')
        return section(build_section_evidence(c, CHILD, rules, as_of=now), 'writing')


def test_original_manuscript_review_receipt_reaches_section_and_retraction_survives_restart(writing_chain):
    db, sid, prompts, originals, _, near, _ = writing_chain
    item = near['diagnostic_item_id']
    text = ('Dear Ben, our school concert was in the hall on Friday. I enjoyed the violin performance '
            'because the music was cheerful. Would you like to come with me to our next concert in June? '
            'Best wishes, Mia.')
    assessment, scored, _ = reviewed_answer(db, sid, prompts, originals, item, text, 'section-original')
    current = writing_section(db)
    matching = [r for r in current['qualified_evidence'] if r['diagnostic_item_id'] == item]
    assert len(matching) == 1
    assert matching[0]['source_evidence_id'] == scored['attempt']['evidence_id']
    assert matching[0]['score_status'] == 'reviewed_human'
    assert matching[0]['independent'] and matching[0]['correctness'] == 1
    assert current['official_part_mapping'] == 'missing'
    assert not current['mastery_claim_allowed']
    assert writing_section(Database(db.path)) == current
    RubricEvidenceService(db).review(CHILD, assessment['assessment_id'], expected_revision=1,
        client_action_id='section-retract-original', reviewer_id='parent', reviewer_role='parent',
        operation='retract', reason='Withdraw the synthetic review, not the original manuscript')
    retracted = writing_section(Database(db.path))
    assert not any(r['diagnostic_item_id'] == item for r in retracted['qualified_evidence'])
    assert any(r['diagnostic_item_id'] == item for r in retracted['opportunities'])
    with db.connect() as c:
        assert c.execute('SELECT submitted_answer FROM diagnostic_attempt WHERE evidence_id=?',
            (scored['attempt']['evidence_id'],)).fetchone()[0] == text
