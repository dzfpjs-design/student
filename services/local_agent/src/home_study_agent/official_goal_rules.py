"""Versioned public references, distinct from a family's target and registration."""
from copy import deepcopy
from datetime import date
import hashlib
import json

from . import exam_blueprints


VERSION = 'official_goal_rules_v1'
REFERENCES = {
    'pet_path': {
        'authority': 'Cambridge University Press & Assessment',
        'url': exam_blueprints.CAMBRIDGE_B1_FORMAT_URL,
        'verified_on': '2026-09-23',
        'document_sha256': '7d5945bd4c8385ea8d70cfbb40f4a4b0711809e7d7d8add7823c473ceafc93d2',
        'reference_year': None, 'region': None,
    },
    'junior_exam_readiness': {
        'authority': '南京市教育局',
        'url': exam_blueprints.NANJING_2026_POLICY_URL,
        'verified_on': '2026-09-08',
        'document_sha256': '00085a0f1bf56539621cdd88dec5a28678f491e312124dee36c93f0a9e6782bf',
        'reference_year': 2026, 'region': '南京',
    },
}


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False, separators=(',', ':')).encode()).hexdigest()


def rule_snapshot(track):
    reference = REFERENCES.get(track)
    if reference is None:
        return None
    blueprints = exam_blueprints.list_exam_blueprints(
        'pet' if track == 'pet_path' else 'junior_exam')
    value = dict(version=VERSION, track=track, reference=deepcopy(reference),
                 blueprint_snapshots=blueprints)
    value['snapshot_hash'] = _hash(value)
    return value


def target_rule_context(track, horizon, *, as_of_date=None, curriculum_region=None):
    current = rule_snapshot(track)
    bound = horizon.get('rule_binding')
    try:
        target_year = date.fromisoformat(horizon.get('target_date')).year
    except (ValueError, TypeError):
        target_year = None
    status = ('event_rules_unverified' if track == 'competition_math_path' else
              'rules_unverified' if current is None else
              'target_year_unverified' if track == 'junior_exam_readiness' and target_year != 2026 else
              'verified_reference_snapshot')
    if current and as_of_date is not None and as_of_date < date.fromisoformat(current['reference']['verified_on']):
        status = 'not_verified_at_reference_date'
    if (track == 'junior_exam_readiness' and status == 'verified_reference_snapshot'
            and curriculum_region not in {'江苏南京', '江苏省南京市', '南京', '南京市'}):
        status = 'target_region_unverified'
    binding = ('unbound' if not isinstance(bound, dict) else
               'current' if current is not None and bound == current else 'source_changed')
    sections = []
    if current and status == 'verified_reference_snapshot':
        for blueprint in current['blueprint_snapshots']:
            for section in blueprint['sections']:
                sections.append({**deepcopy(section), 'blueprint_id': blueprint['blueprint_id'],
                    'coverage_status': 'not_audited', 'qualified_evidence_count': None})
    return dict(version=VERSION, public_rule_status=status, binding_status=binding,
        current_snapshot=current, bound_snapshot=deepcopy(bound) if isinstance(bound, dict) else None,
        target_year=target_year, reference_year=current['reference']['reference_year'] if current else None,
        region=current['reference']['region'] if current else None,
        curriculum_region=curriculum_region, location_basis='family_profile_not_registration',
        target_date_basis='family_target_not_registration' if horizon.get('target_date') else 'not_provided',
        applicability='reference_snapshot_not_future_rule_guarantee',
        registration_status='not_verified', required_sections=sections,
        coverage_status='not_audited_against_official_requirements',
        exam_readiness_claim_allowed=False, ordinary_learning_allowed=True,
        local_only=True, cloud_transmission_allowed=False)
