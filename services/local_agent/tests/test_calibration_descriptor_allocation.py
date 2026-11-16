"""Source profile descriptors stay independently mutable without recursive copying."""
import pytest

from home_study_agent import open_response_profile as profiles


@pytest.mark.parametrize('subject,family,grade', [
    ('数学', 'mathematical_process', None),
    ('语文', 'communicative_writing', None),
    ('英语', 'textual_interpretation', None),
    ('物理', 'scientific_explanation', None),
    ('科学', 'scientific_explanation', '四年级'),
])
def test_profile_allocates_fresh_nested_descriptors_without_copy(subject, family, grade, monkeypatch):
    args = dict(task_family=family, response_mode='written', grade=grade)
    expected = profiles.profile_for(subject, **args)
    def redundant_copy(*_):
        pytest.fail('Profile already owns all nested mutable allocations')
    monkeypatch.setattr(profiles, 'deepcopy', redundant_copy, raising=False)
    first = profiles.profile_for(subject, **args)
    assert first == expected
    first['dimensions'][0]['score_anchors'].clear()
    first['dimensions'][0]['criterion'] = 'changed'
    first['dimensions'].pop()
    if 'required_origin_fields' in first:
        first['required_origin_fields'].clear()
    assert profiles.profile_for(subject, **args) == expected
