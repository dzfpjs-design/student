"""One question's grant cannot silently authorize its external materials."""
from copy import deepcopy

import pytest

from home_study_agent import writing_question_authorization as authorization


LICENSE = {'license_id': 'original_author', 'reference': 'synthetic author declaration',
           'evidence': 'Synthetic isolated author explicitly permits processing this exact text.'}


def material_scope():
    return {'purpose_version': 'writing_question_material_formative_text_v2',
            'material_bindings': [{'material_key': 'passage', 'source_version': 's1', 'text_hash': 't1'},
                                  {'material_key': 'table', 'source_version': 's2', 'text_hash': 't2'}]}


def bundle():
    return {'question': deepcopy(LICENSE), 'materials': [
        {**entry, 'license_basis': deepcopy(LICENSE)} for entry in material_scope()['material_bindings']]}


def test_each_material_needs_its_own_exact_current_basis():
    value = bundle()
    assert authorization._licenses_for_scope(value, material_scope()) == value


@pytest.mark.parametrize('fault', ['missing', 'extra', 'duplicate', 'order', 'source_version', 'text_hash',
                                  'license', 'extra_field', 'question_only', 'private_reference'])
def test_material_license_bundle_rejects_scope_changes(fault):
    value = bundle()
    if fault == 'missing': value['materials'].pop()
    elif fault == 'extra': value['materials'].append(deepcopy(value['materials'][0]))
    elif fault == 'duplicate': value['materials'][1] = deepcopy(value['materials'][0])
    elif fault == 'order': value['materials'].reverse()
    elif fault in {'source_version', 'text_hash'}: value['materials'][0][fault] = 'changed'
    elif fault == 'license': value['materials'][0]['license_basis']['license_id'] = 'public'
    elif fault == 'extra_field': value['materials'][0]['cloud_transmission_allowed'] = True
    elif fault == 'question_only': value = deepcopy(LICENSE)
    elif fault == 'private_reference': value['materials'][0]['license_basis']['reference'] = '/private/source.pdf'
    with pytest.raises(ValueError, match='WRITING_QUESTION_'):
        authorization._licenses_for_scope(value, material_scope())


def test_v1_license_is_unchanged_and_cannot_accept_material_bundle():
    scope = {'purpose_version': authorization.VERSION}
    assert authorization._licenses_for_scope(LICENSE, scope) == LICENSE
    with pytest.raises(ValueError, match='WRITING_QUESTION_'):
        authorization._licenses_for_scope(bundle(), scope)
