"""Shape validation is not permission to absorb storage failures."""
import sqlite3

import pytest

from home_study_agent.agent_runtime import AgentRuntimeStore
from home_study_agent.learning_rubric import RubricEvidenceService
from home_study_agent.learning_outcome_projection import LearningOutcomeProjection
from home_study_agent.writing_followup_evidence import _object, validate_formal_followup_evaluation


@pytest.mark.parametrize('encoded', ['{broken', '[]', 'null', '42', '"text"'])
def test_followup_object_rejects_bad_or_nonobject_json_with_controlled_code(encoded):
    with pytest.raises(ValueError, match='^WRITING_FOLLOWUP_ORIGINAL_COMMAND_REQUIRED$'):
        _object(encoded, 'FOLLOWUP_ORIGINAL_COMMAND_REQUIRED')


@pytest.mark.parametrize('arguments,criteria', [('{broken', '[]'), ('[]', '[]'),
    ('null', '[]'), ('"text"', '[]'), ('{}', '{broken'), ('{}', '{}')])
def test_rubric_optional_source_shape_is_unavailable(arguments, criteria):
    with sqlite3.connect(':memory:') as c:
        c.row_factory = sqlite3.Row
        c.executescript('''
            CREATE TABLE agent_step(step_id TEXT,plan_id TEXT,arguments_json TEXT);
            CREATE TABLE agent_plan(plan_id TEXT,child_id TEXT,objective_id TEXT,session_id TEXT,agent_turn_id TEXT);
            CREATE TABLE agent_objective(objective_id TEXT,child_id TEXT,success_criteria_json TEXT);
            CREATE TABLE agent_turn(agent_turn_id TEXT,child_id TEXT,created_at TEXT);
            INSERT INTO agent_plan VALUES ('p','a','o','s','t');
            INSERT INTO agent_turn VALUES ('t','a','2026-09-09');
        ''')
        c.execute('INSERT INTO agent_step VALUES (?,?,?)', ('step', 'p', arguments))
        c.execute('INSERT INTO agent_objective VALUES (?,?,?)', ('o', 'a', criteria))
        assert RubricEvidenceService._source_current(c, {'step_id': 'step', 'child_id': 'a'},
            {'objective_id': 'o', 'session_id': 's'}) is False


def test_read_guards_do_not_convert_database_failures():
    class Broken:
        def execute(self, *args):
            raise sqlite3.OperationalError('synthetic storage failure')
        def connect(self):
            raise sqlite3.OperationalError('synthetic storage failure')
    with pytest.raises(sqlite3.OperationalError, match='synthetic storage failure'):
        RubricEvidenceService._source_current(Broken(), {'step_id': 'step', 'child_id': 'a'},
            {'objective_id': 'o', 'session_id': 's'})
    with pytest.raises(sqlite3.OperationalError, match='synthetic storage failure'):
        validate_formal_followup_evaluation(Broken(), child_id='a', session_id='s',
            prompt_id='q', item_version='v', attempt_id='attempt')
    store = object.__new__(AgentRuntimeStore)
    store.database = Broken()
    with pytest.raises(sqlite3.OperationalError, match='synthetic storage failure'):
        store.finalize_diagnostic_evaluation('s', 'e')
    with pytest.raises(sqlite3.OperationalError, match='synthetic storage failure'):
        LearningOutcomeProjection(None)._source_item(Broken(), {'evidence_refs_json':
            '[{"type":"child_response","agent_step_id":"step"}]', 'objective_id': 'o', 'child_id': 'a'}, '四年级')


@pytest.mark.parametrize('arguments', ['{broken', '[]', 'null', '"text"'])
def test_projection_bad_arguments_cannot_use_local_receipt_as_fallback(arguments, monkeypatch):
    def forbidden(*args):
        raise AssertionError('Invalid source must not consult a replacement qualification')
    monkeypatch.setattr(RubricEvidenceService, 'local_source_available', forbidden)
    with sqlite3.connect(':memory:') as c:
        c.row_factory = sqlite3.Row
        c.executescript('''
            CREATE TABLE agent_step(step_id TEXT,plan_id TEXT,arguments_json TEXT,capability_name TEXT);
            CREATE TABLE agent_plan(plan_id TEXT,child_id TEXT,objective_id TEXT,session_id TEXT);
            CREATE TABLE daily_session(session_id TEXT,child_id TEXT);
            INSERT INTO agent_plan VALUES ('p','a','o','s');
            INSERT INTO daily_session VALUES ('s','a');
        ''')
        c.execute('INSERT INTO agent_step VALUES (?,?,?,?)', ('step', 'p', arguments, 'learning.evaluate_response'))
        row = {'evidence_refs_json': '[{"type":"child_response","agent_step_id":"step"},'
               '{"type":"local_rubric","assessment_id":"r"}]',
               'outcome_id': 'outcome', 'objective_id': 'o', 'child_id': 'a',
               'outcome_level': 'candidate', 'confidence': 0, 'created_at': '2026-09-09', 'summary': ''}
        projected = LearningOutcomeProjection(None)._source_item(c, row, '四年级')
        assert projected['evidence_status'] == 'source_unavailable'
        assert projected['mapping'] is None
