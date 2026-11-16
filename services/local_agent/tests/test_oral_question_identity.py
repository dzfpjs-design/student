"""Frozen oral identity rejects incomplete proof instead of using mutable items."""
import json
import sqlite3

import pytest

from home_study_agent.learning_obligations import _source_question, question_identity


@pytest.mark.parametrize('corruption', [
    'missing_materials', 'missing_text', 'changed_text', 'missing_asset',
    'changed_asset', 'changed_prompt', 'invalid_source',
])
def test_incomplete_or_changed_frozen_oral_identity_is_not_accepted(corruption):
    original = dict(schema='pet_speaking_source_v1', subject='英语', grade='四年级',
        knowledge_node_id='PET.SPEAKING.READ_ALOUD', answer_type='read_aloud',
        assessment_mode='pet_reference_rubric', task_family='read_aloud',
        prompt='Read the passage aloud.', task_points=['Read the complete passage.'],
        stimulus_text='Ada and her teacher watered the plant.', stimulus_binding=None)
    material = dict(stimulus_text=original['stimulus_text'], stimulus_asset=None)
    proof = {'oral_materials': material}
    item = dict(diagnostic_item_id='published-oral', source_ref='parent_approved_supply:source-1',
                prompt=original['prompt'], **material)
    with sqlite3.connect(':memory:') as c:
        c.executescript('''CREATE TABLE specialized_supply_candidate
            (candidate_id TEXT, diagnostic_item_id TEXT, prompt TEXT);
            CREATE TABLE curriculum_supply_package
            (candidate_id TEXT, payload_json TEXT, source_snapshot_json TEXT);''')
        c.execute('INSERT INTO specialized_supply_candidate VALUES (?,?,?)',
                  ('source-1', item['diagnostic_item_id'], original['prompt']))
        c.execute('INSERT INTO curriculum_supply_package VALUES (?,?,?)',
                  ('source-1', json.dumps({'pet_oral': original}), json.dumps(proof)))
        assert question_identity(_source_question(c, item)) == question_identity(item)
        if corruption == 'missing_materials':
            proof.clear()
        elif corruption == 'missing_text':
            material.pop('stimulus_text')
        elif corruption == 'changed_text':
            material['stimulus_text'] = 'A different passage.'
        elif corruption == 'missing_asset':
            material.pop('stimulus_asset')
        elif corruption == 'changed_asset':
            material['stimulus_asset'] = 'different-image'
        elif corruption == 'changed_prompt':
            c.execute('UPDATE specialized_supply_candidate SET prompt=?', ('A different question.',))
        else:
            original['schema'] = 'unknown-oral-version'
        c.execute('UPDATE curriculum_supply_package SET payload_json=?,source_snapshot_json=?',
                  (json.dumps({'pet_oral': original}), json.dumps(proof)))
        with pytest.raises(ValueError):
            _source_question(c, item)
