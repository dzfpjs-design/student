"""Punctuation in prose is not by itself evidence of a formula region."""
import numpy as np
import pytest

from home_study_agent.local_ocr.service import _formula_candidate_regions


def regions(text):
    return _formula_candidate_regions(np.full((160, 640), 255, dtype=np.uint8), [{
        'text': text, 'confidence': .99,
        'box': [[30, 40], [610, 40], [610, 75], [30, 75]],
    }])


@pytest.mark.parametrize('text', [
    'We joined an after-school club.',
    'My well-known friend came too.',
    'Mia (my friend) plays the violin.',
    'Please bring a pen and/or pencil.',
    '[Note] Please read the next paragraph.',
    '(1) Describe your favourite school activity.',
    'The title is music(Mia).',
    '学校活动 (音乐会) 很精彩。',
    'I (Mia) went to school.',
    'Choose a (cat) picture for your story.',
])
def test_plain_prose_punctuation_does_not_create_formula_work(text):
    assert regions(text) == []


@pytest.mark.parametrize('text', [
    '12 + 8 = 20', '35 - 17', '-3', '-x', 'x-y', 'a/b', '1/2',
    '3.5-1.2', '6 × 7', '24 ÷ 6', 'x^2', '√9', 'x ≤ 3',
    'f(x)', 'sin(x)', '(3, 4)', '2(x)', '[x+2]', 'x+y=5',
    'sin(theta)', '[1, 2]', '{a, b}', '(x)',
    'f (x)', 'sin (theta)', '2(xy)', '(x)(y)',
])
def test_explicit_mathematical_evidence_keeps_review_region(text):
    selected = regions(text)
    assert len(selected) == 1
    assert selected[0]['review_eligible'] is True
    assert selected[0]['provider_eligible'] is True
