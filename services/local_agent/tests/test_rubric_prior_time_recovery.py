"""Recover only an owned prior answer time, never old scoring authority."""
from datetime import timedelta
import json

import pytest

from home_study_agent.learning_rubric import RubricEvidenceService
from test_learning_outcome_mastery_gate import gate, flow, CHILD, SECOND
from test_learning_rubric_review import review


def corrupt_prior(c, assessment_id, payload='{broken'):
    guard = c.execute("SELECT sql FROM sqlite_master WHERE name='rubric_assessment_no_update'").fetchone()[0]
    c.execute('DROP TRIGGER rubric_assessment_no_update')
    c.execute('UPDATE rubric_assessment SET payload_json=? WHERE assessment_id=?',(payload,assessment_id))
    c.execute(guard)


@pytest.mark.parametrize('days', [1, 2])
def test_old_owned_answer_with_broken_payload_does_not_permanently_block_new_independence(gate, days):
    first, evidence, _ = gate.run(local=True)
    prior = first['evaluation']['rubric_assessment']
    current, _, _ = gate.run(local=True,item=SECOND,answer='42',days=days)
    snapshot = current['evaluation']['rubric_assessment']
    service = RubricEvidenceService(gate.db)
    review(service,snapshot)
    with gate.db.connect() as c:
        c.execute('BEGIN')
        before = service.reviewed_receipt(c,CHILD,snapshot['step_id'],now=gate.clock[0])
        assert before and before['correctness']==1 and before['independent']
        # The original source version and actual answer persist independently
        # of the corrupt presentation payload. No chronology is backfilled.
        assert service._diagnostic_source_version(c,CHILD,evidence['evidence_id']) == prior['source_evidence_version_hash']
        step = c.execute('SELECT arguments_json FROM agent_step WHERE step_id=?',(prior['step_id'],)).fetchone()
        assert json.loads(step[0])['evidence_id']==evidence['evidence_id']
        corrupt_prior(c,prior['assessment_id'])
        c.commit()
    with gate.db.connect() as c:
        c.execute('BEGIN')
        c.execute('PRAGMA query_only=ON')
        receipt = service.reviewed_receipt(c,CHILD,snapshot['step_id'],now=gate.clock[0])
        assert receipt and receipt['correctness']==1
        assert receipt['independent'], 'One corrupt old payload permanently vetoed a new independent answer'
        assert c.execute('SELECT payload_json FROM rubric_assessment WHERE assessment_id=?',
            (prior['assessment_id'],)).fetchone()[0]=='{broken'


@pytest.mark.parametrize('fault', ['missing_evidence', 'bad_arguments', 'response_changed', 'foreign_plan',
    'foreign_turn', 'naive_time', 'time_mismatch', 'future_time', 'not_answer', 'within_window'])
def test_incomplete_owned_time_proof_does_not_restore_independence(gate, fault):
    first, evidence, _ = gate.run(local=True)
    prior = first['evaluation']['rubric_assessment']
    current, _, _ = gate.run(local=True,item=SECOND,answer='42',days=.5 if fault=='within_window' else 2)
    snapshot = current['evaluation']['rubric_assessment']
    service = RubricEvidenceService(gate.db)
    review(service,snapshot)
    with gate.db.connect() as c:
        c.execute('BEGIN')
        assert service.reviewed_receipt(c,CHILD,snapshot['step_id'],now=gate.clock[0])['independent']
        corrupt_prior(c,prior['assessment_id'])
        if fault in {'missing_evidence','response_changed'}:
            key,value = ('evidence_id','missing') if fault=='missing_evidence' else ('response','not the saved answer')
            c.execute('UPDATE agent_step SET arguments_json=json_set(arguments_json,?,?) WHERE step_id=?',
                ('$.'+key,value,prior['step_id']))
        elif fault=='bad_arguments':
            c.execute("UPDATE agent_step SET arguments_json='[]' WHERE step_id=?",(prior['step_id'],))
        elif fault=='foreign_plan':
            c.execute("UPDATE agent_plan SET child_id='child-junior-3' WHERE plan_id=(SELECT plan_id FROM agent_step WHERE step_id=?)",(prior['step_id'],))
        elif fault=='foreign_turn':
            c.execute("UPDATE agent_turn SET child_id='child-junior-3' WHERE agent_turn_id=(SELECT p.agent_turn_id FROM agent_plan p JOIN agent_step s USING(plan_id) WHERE s.step_id=?)",(prior['step_id'],))
        elif fault=='naive_time':
            c.execute("UPDATE learning_evidence SET occurred_at='2020-01-01 00:00:00' WHERE evidence_id=?",(evidence['evidence_id'],))
        elif fault=='time_mismatch':
            c.execute('UPDATE diagnostic_attempt SET created_at=? WHERE evidence_id=?',
                ((gate.clock[0]-timedelta(days=5)).isoformat(),evidence['evidence_id']))
        elif fault=='future_time':
            future=(gate.clock[0]+timedelta(days=5)).isoformat()
            c.execute('UPDATE diagnostic_attempt SET created_at=? WHERE evidence_id=?',(future,evidence['evidence_id']))
            c.execute('UPDATE learning_evidence SET occurred_at=? WHERE evidence_id=?',(future,evidence['evidence_id']))
        elif fault=='not_answer':
            c.execute("UPDATE learning_evidence SET evidence_type='self_report' WHERE evidence_id=?",(evidence['evidence_id'],))
        c.commit()
    with gate.db.connect() as c:
        c.execute('BEGIN')
        c.execute('PRAGMA query_only=ON')
        receipt=service.reviewed_receipt(c,CHILD,snapshot['step_id'],now=gate.clock[0])
        assert receipt and receipt['correctness']==1
        assert not receipt['independent']


def test_prior_time_recovery_does_not_hide_sqlite_errors(gate):
    import sqlite3
    from home_study_agent.learning_rubric import _prior_answer_outside_window
    first, _, _=gate.run(local=True)
    with gate.db.connect() as c:
        c.execute('BEGIN')
        c.set_authorizer(lambda op,table,*_: sqlite3.SQLITE_DENY
            if op==sqlite3.SQLITE_READ and table=='agent_step' else sqlite3.SQLITE_OK)
        with pytest.raises(sqlite3.DatabaseError):
            _prior_answer_outside_window(c,CHILD,first['evaluation']['rubric_assessment']['step_id'],gate.clock[0])
