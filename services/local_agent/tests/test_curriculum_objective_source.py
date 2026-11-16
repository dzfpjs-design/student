"""Reviewed whole-answer sources, not generated answers or all-subject mastery."""
import json

import pytest

from home_study_agent.curriculum_supply import CurriculumSupplyService
from home_study_agent.database import Database
from home_study_agent.local_supply import LocalSupplyError, score_local_item, validate_item
from test_curriculum_supply import prepared, CHILD


def setup_source(tmp_path, subject='数学'):
    db, service, old = prepared(tmp_path)
    child = CHILD if subject == '数学' else 'child-junior-3'
    grade = '四年级' if subject == '数学' else '初三'
    question, reference = {
        '数学': ('把8/10写成小数，只回答一个数。', '0.8'),
        '英语': ("Read: 'The meeting is in Room 207.' Which room is the meeting in? Copy the complete room name.", 'Room 207'),
        '语文': ('阅读：雨停后，父亲把伞向我倾了倾。请原样摘录表现父亲关心我的完整动作。', '把伞向我倾了倾'),
    }[subject]
    with db.connect() as c:
        if subject == '数学':
            seed_id = 'diag-p4-fraction-decimal-closed-1'
        else:
            seed_id = c.execute("""SELECT di.diagnostic_item_id FROM diagnostic_item di
                JOIN knowledge_node kn USING(knowledge_node_id) WHERE kn.subject=? AND kn.grade_band=?
                AND di.answer_type='text' ORDER BY di.diagnostic_item_id LIMIT 1""", (subject, grade)).fetchone()[0]
        node = c.execute('SELECT knowledge_node_id FROM diagnostic_item WHERE diagnostic_item_id=?', (seed_id,)).fetchone()[0]
        packet = {'schema': 'curriculum_objective_source_v1', 'subject': subject, 'grade': grade,
            'knowledge_node_id': node, 'assessment_mode': 'objective', 'answer_type': 'text',
            'prompt': question,
            'expected_answer': {'local_rule': 'local_exact_v1', 'exact_answers': [reference]}}
        c.execute('UPDATE knowledge_excerpt SET content_text=? WHERE excerpt_id=?', (json.dumps(packet), old['excerpt_id']))
        c.execute('UPDATE knowledge_source SET subject=?,grade_band=?,child_id=?', (subject, grade, child))
        c.execute('UPDATE curriculum_standard_requirement SET subject_title=?', (subject,))
        c.execute('UPDATE curriculum_standard_source SET subject_title=?', (subject,))
        selected = c.execute('SELECT scope_json FROM curriculum_supply_selection').fetchone()[0]
        c.execute('UPDATE curriculum_supply_selection SET child_id=?,subject=?,scope_json=?',
                  (child, subject, json.dumps({**json.loads(selected), 'subject': subject, 'grade': grade})))
        c.commit()
    spec = {key: value for key, value in old.items() if key not in {'prompt', 'expected_answer'}}
    spec.update(seed_item_id=seed_id, validator='curriculum_objective_exact_source_v1')
    return db, service, spec, packet, child


@pytest.mark.parametrize('subject', ['数学', '英语', '语文'])
def test_review_publish_exact_score_restart_and_withdraw(tmp_path, subject):
    db, service, spec, packet, child = setup_source(tmp_path, subject)
    draft = service.generate_draft(child, spec, author='author', action_id='draft')
    assert draft['state'] == 'draft', draft['blockers']
    cid = draft['candidate_id']
    checked = service.validate(child, cid, actor='validator', action_id='validate', expected_version=1)
    assert checked['state'] == 'validated', checked['blockers']
    service.review(child, cid, decision='approve', actor='parent', note='核对完整原题和原始参考答案，不宣称独立复算', action_id='review', expected_version=2)
    published = service.publish(child, cid, actor='parent', action_id='publish', expected_version=3)
    item_id = published['diagnostic_item_id']
    with db.connect() as c:
        item = validate_item(c, child, item_id)
        reference = packet['expected_answer']['exact_answers'][0]
        assert score_local_item(item, reference)[0] == 1
        for answer in ('incorrect', 'not ' + reference, reference + ' or something else'):
            assert score_local_item(item, answer)[0] == 0
        assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0
    restarted = CurriculumSupplyService(Database(db.path))
    assert restarted.get(child, cid)['eligible']
    assert restarted.publish(child, cid, actor='parent', action_id='publish', expected_version=3)['diagnostic_item_id'] == item_id
    with pytest.raises(KeyError):
        restarted.get(CHILD if child != CHILD else 'child-junior-3', cid)
    restarted.withdraw(child, cid, actor='parent', note='原题撤回', action_id='withdraw', expected_version=4)
    with db.connect() as c, pytest.raises(LocalSupplyError):
        validate_item(c, child, item_id)


@pytest.mark.parametrize('field,value', [('subject', '英语'), ('grade', '初三'),
    ('knowledge_node_id', 'node-p4-math-arithmetic'), ('answer_type', 'writing'),
    ('assessment_mode', 'subjective'), ('expected_answer', {'keywords': ['here']}), ('cloud_allowed', True)])
def test_invalid_source_cannot_be_certified(tmp_path, field, value):
    db, service, spec, packet, _ = setup_source(tmp_path)
    packet[field] = value
    with db.connect() as c:
        c.execute('UPDATE knowledge_excerpt SET content_text=? WHERE excerpt_id=?', (json.dumps(packet), spec['excerpt_id']))
        c.commit()
    try:
        result = service.generate_draft(CHILD, spec, author='author', action_id='bad')
    except LocalSupplyError:
        return
    assert result['state'] == 'blocked'


@pytest.mark.parametrize('fault', ['withdrawn_source', 'changed_original', 'changed_node', 'changed_answer', 'changed_mode'])
def test_published_source_changes_invalidate_current_qualification(tmp_path, fault):
    db, service, spec, packet, child = setup_source(tmp_path)
    cid = service.generate_draft(child, spec, author='author', action_id='draft')['candidate_id']
    service.validate(child, cid, actor='validator', action_id='validate', expected_version=1)
    service.review(child, cid, decision='approve', actor='parent', note='核对原题参考', action_id='review', expected_version=2)
    item_id = service.publish(child, cid, actor='parent', action_id='publish', expected_version=3)['diagnostic_item_id']
    with db.connect() as c:
        if fault == 'withdrawn_source':
            c.execute("UPDATE knowledge_source SET review_status='needs_review'")
        elif fault == 'changed_original':
            c.execute("UPDATE knowledge_excerpt SET content_text='withdrawn original'")
        elif fault == 'changed_node':
            c.execute("UPDATE knowledge_node SET title='changed source target' WHERE knowledge_node_id=?", (packet['knowledge_node_id'],))
        elif fault == 'changed_answer':
            c.execute("UPDATE diagnostic_item SET expected_answer_json='{}' WHERE diagnostic_item_id=?", (item_id,))
        else:
            c.execute("UPDATE diagnostic_item SET answer_type='speaking' WHERE diagnostic_item_id=?", (item_id,))
        c.commit()
    assert not CurriculumSupplyService(Database(db.path)).get(child, cid)['eligible']
    with db.connect() as c, pytest.raises(LocalSupplyError):
        validate_item(c, child, item_id)


def test_exact_text_does_not_use_keywords_or_remove_word_boundaries():
    from home_study_agent.curriculum_objective_source import exact_rubric
    item = {'expected_answer_json': json.dumps(exact_rubric({'expected_answer': {'exact_answers': ['now here']}})),
            'explanation': 'Original complete answer'}
    assert score_local_item(item, 'NOW  HERE')[0] == 1
    for answer in ('nowhere', 'not now here', 'now here or there', 'here'):
        assert score_local_item(item, answer)[0] == 0
