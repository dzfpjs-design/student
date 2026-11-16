import json

from home_study_agent.database import Database
from home_study_agent.learning_state import LearningStateService
from home_study_agent.local_supply_delivery import refresh_local_question_supply
from home_study_agent.specialized_supply import SpecializedDiagnosticSupplyService
from home_study_agent.variant_supply import VariantSupplyService
from test_specialized_supply import prepared


def test_qualified_supply_enters_child_diagnostics_and_survives_restart(tmp_path):
    db = prepared(tmp_path)
    specialized, variants = SpecializedDiagnosticSupplyService(db), VariantSupplyService(db)
    result = refresh_local_question_supply(db, specialized, variants)
    assert result['qualified'] > 0
    # One maintenance pass intentionally handles at most 50 jobs.
    refresh_local_question_supply(db, specialized, variants)
    with db.connect() as connection:
        rows = connection.execute("SELECT * FROM diagnostic_item WHERE source_ref LIKE 'local_verified_specialized:%'").fetchall()
        assert rows
        assert connection.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0
        assert connection.execute('SELECT count(*) FROM mastery_state').fetchone()[0] == 0
    learning = LearningStateService(Database(db.path))
    for row in rows:
        child = 'child-junior-3' if row['knowledge_node_id'].startswith('node-j3') else 'child-primary-4'
        other = 'child-primary-4' if child == 'child-junior-3' else 'child-junior-3'
        track = 'school' if child == 'child-junior-3' else ('pet' if 'pet' in row['knowledge_node_id'] else 'competition_math')
        assert learning.next_diagnostic(child, track, preferred_item_id=row['diagnostic_item_id'])
        assert learning.next_diagnostic(other, track, preferred_item_id=row['diagnostic_item_id']) is None
        answer = json.loads(row['expected_answer_json'])['exact_answers'][0]
        assert learning._score_answer(row, answer)[0] == 1
        assert learning._score_answer(row, answer+'或者999')[0] == 0
    assert refresh_local_question_supply(db, specialized, variants)['qualified'] == 0
    with db.connect() as connection:
        item = rows[0]
        connection.execute("UPDATE diagnostic_item SET prompt=prompt||'tampered' WHERE diagnostic_item_id=?", (item['diagnostic_item_id'],))
        connection.commit()
    assert learning.next_diagnostic('child-junior-3', 'school', preferred_item_id=item['diagnostic_item_id']) is None


def test_school_selection_cannot_choose_other_grade(tmp_path):
    db = prepared(tmp_path)
    learning = LearningStateService(db)
    for child, grade in [('child-primary-4','四年级'),('child-junior-3','初三')]:
        item = learning.next_diagnostic(child, 'school')
        assert item
        with db.connect() as connection:
            assert connection.execute('SELECT grade_band FROM knowledge_node WHERE knowledge_node_id=?', (item['knowledge_node_id'],)).fetchone()[0] == grade
