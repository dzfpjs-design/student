"""Local practice scope and current receipts, never official exam coverage.

Blueprints currently define sections but not individual parts. Existing typed
tasks can establish a practice skill, not an invented official part mapping.
"""
from datetime import datetime
import json

from .local_supply import LocalSupplyError, digest, item_snapshot, validate_item
from .family_clock import family_today

VERSION = 'goal_section_evidence_v1'


def attach_section_evidence(c, roadmap, *, as_of):
    # Parent read paths only. Do not change execution fingerprints or rescan
    # calibration/answer history during ordinary planning and voice admission.
    roadmap['section_evidence'] = build_section_evidence(c, roadmap['child_id'], roadmap['exam_rules'], as_of=as_of)
    return roadmap


def _time(value):
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def _scope(c, row, track):
    from .learning_state import NODES, SPECIALIZED_NODES, GRAPH_NODES
    from .specialized_program import NODE_DOMAIN_HINTS
    original = next((n for n in (*NODES, *SPECIALIZED_NODES) if n[0] == row['knowledge_node_id']), None)
    if original is None:
        graph = next((n for n in GRAPH_NODES if n[0] == row['knowledge_node_id']), None)
        if graph is not None:
            original = (graph[0], *graph[2:])
    if original is None or tuple(row[k] for k in ('track', 'subject', 'grade_band', 'code')) != original[1:5]:
        return None
    if track == 'junior_exam_readiness' and row['track'] in {'school', 'junior_exam'}:
        return row['subject'], 'canonical_subject_scope', None
    if track != 'pet_path' or row['track'] != 'pet':
        return None
    # Task modality matters: retelling has a reading node but an oral response.
    if row['answer_type'] in {'speaking', 'read_aloud'}:
        return 'speaking', 'validated_oral_response_scope', None
    if row['answer_type'] == 'writing':
        return 'writing', 'validated_written_response_scope', None
    domain = NODE_DOMAIN_HINTS.get(row['knowledge_node_id'])
    if domain and domain.startswith('pet.') and domain.split('.')[1] in {'reading', 'listening'}:
        return domain.split('.')[1], 'canonical_skill_scope', domain
    return None


def _visible_source(c, child_id, row):
    ref = row['source_ref'] or ''
    if ref.startswith('parent_approved_first:'):
        return bool(c.execute("SELECT 1 FROM sqlite_master WHERE name='curriculum_first_source'").fetchone()
            and c.execute('SELECT 1 FROM curriculum_first_source WHERE child_id=? AND diagnostic_item_id=? AND candidate_id=?',
                          (child_id,row['diagnostic_item_id'],ref.partition(':')[2])).fetchone())
    if not ref.startswith(('parent_approved_', 'local_verified_')):
        from .learning_state import ITEMS, EXTENDED_ITEMS, SPECIALIZED_ITEMS
        return any(item[0] == row['diagnostic_item_id'] for item in (*ITEMS, *EXTENDED_ITEMS, *SPECIALIZED_ITEMS))
    variant = ref.startswith(('parent_approved_variant:', 'local_verified_variant:'))
    table = 'variant_candidate' if variant else 'specialized_supply_candidate'
    if not c.execute('SELECT 1 FROM sqlite_master WHERE name=?', (table,)).fetchone():
        return False
    # Even a missing/unavailable entry must not disclose another child's item.
    return c.execute(f'SELECT 1 FROM {table} WHERE child_id=? AND diagnostic_item_id=? AND candidate_id=?',
        (child_id, row['diagnostic_item_id'], ref.partition(':')[2])).fetchone() is not None


def _source(c, child_id, row, as_of):
    item = validate_item(c, child_id, row['diagnostic_item_id'])
    ref = item['source_ref'] or ''
    proof = {'item': item_snapshot(item)}
    family = None
    if ref.startswith('parent_approved_first:'):
        from .curriculum_first_source import reviewed_baseline
        proof['first_source'] = reviewed_baseline(c,child_id,item,as_of)
        qualification = 'parent_reviewed_source'
    elif ref.startswith(('parent_approved_', 'local_verified_')):
        variant = ref.startswith(('parent_approved_variant:', 'local_verified_variant:'))
        table = 'variant_candidate' if variant else 'specialized_supply_candidate'
        candidate = dict(c.execute(f'SELECT * FROM {table} WHERE child_id=? AND diagnostic_item_id=?',
                                  (child_id, item['diagnostic_item_id'])).fetchone())
        reviewed_at = _time(candidate['reviewed_at'])
        if not reviewed_at or reviewed_at > as_of:
            raise LocalSupplyError('SECTION_SOURCE_NOT_REVIEWED_AT_REFERENCE_TIME')
        proof['candidate'] = candidate
        qualification = 'parent_reviewed_source' if ref.startswith('parent_approved_') else 'deterministically_validated_source'
        if not variant and ref.startswith('parent_approved_'):
            from .pet_oral_source import candidate_source
            oral = candidate_source(c, candidate)
            if oral:
                family = oral['task_family']
    else:
        qualification = 'shipped_reference_source'
    return {'diagnostic_item_id': item['diagnostic_item_id'], 'knowledge_node_id': item['knowledge_node_id'],
            'source_version': digest(proof), 'source_qualification': qualification,
            'answer_type': item['answer_type'], 'assessment_mode': item['assessment_mode'],
            'task_family': family, 'official_part_mapping': 'missing',
            'delivery_admission': 'not_checked', 'mastery_claim_allowed': False}


def _rubric_receipts(c, child_id, eligible, attempts, as_of):
    from .learning_rubric import RubricEvidenceService
    from .pet_audio_rubric import is_speaking, current_audio_review
    service = RubricEvidenceService(None)
    if not service.available(c):
        return []
    receipts, candidates = {}, []
    for row in c.execute('SELECT * FROM rubric_assessment WHERE child_id=? ORDER BY rowid', (child_id,)):
        try:
            payload = json.loads(row['payload_json'])
            if not isinstance(payload, dict):
                continue
            source_id = payload.get('source_evidence_id')
            if source_id not in attempts or attempts[source_id]['diagnostic_item_id'] not in eligible:
                continue
            latest = c.execute('SELECT operation,created_at FROM rubric_review_event WHERE assessment_id=? ORDER BY revision DESC LIMIT 1',
                               (row['assessment_id'],)).fetchone()
            if latest:
                stamp = _time(latest['created_at'])
                if not stamp or stamp > as_of:
                    continue
            elif isinstance(payload.get('calibration_binding'), dict):
                stamp = _time(row['created_at'])
                if not stamp or stamp > as_of:
                    continue
            else:
                continue
            candidates.append((stamp, row['assessment_id'], source_id, dict(row), dict(latest) if latest else None))
        except (ValueError, TypeError, KeyError):
            continue
    from .rubric_prior_index import RubricPriorIndex
    # Synchronous borrowed reads only; the index cannot escape this transaction.
    with RubricPriorIndex(c, child_id) as prior_index:
        for _, _, source_id, row, latest in sorted(candidates):
            # A later explicit rejection/retraction cannot reveal an older score
            # of the same answer. Keep the actual response opportunity.
            receipts.pop(source_id, None)
            try:
                if latest and latest['operation'] != 'approve':
                    continue
                item_id = attempts[source_id]['diagnostic_item_id']
                if is_speaking(c, item_id):
                    current = service.get(child_id, row['assessment_id'], connection=c)
                    if not latest or not current_audio_review(c, child_id, current):
                        continue
                receipt = (service.reviewed_receipt(c, child_id, row['step_id'],
                    assessment_id=row['assessment_id'], now=as_of,
                    _prior_index=prior_index) if latest else service.calibrated_receipt(c, child_id, row['step_id'],
                        assessment_id=row['assessment_id'], now=as_of))
                if receipt and receipt.get('diagnostic_item_id') == attempts[source_id]['diagnostic_item_id']:
                    receipts[source_id] = receipt
            except (ValueError, TypeError, KeyError):
                # Malformed optional history cannot break an unrelated route.
                # SQLite/storage failures are not converted to missing.
                continue
    return list(receipts.items())


def build_section_evidence(c, child_id, rules, *, as_of):
    if not c.in_transaction:
        raise RuntimeError('section evidence requires a read transaction')
    if not isinstance(as_of, datetime) or as_of.tzinfo is None:
        raise ValueError('timezone-aware reference time required')
    child = c.execute("SELECT * FROM child_profile WHERE child_id=? AND status='active'", (child_id,)).fetchone()
    if not child:
        raise KeyError(child_id)
    snapshot = rules.get('current_snapshot') or {}
    sections = [{k: definition[k] for k in ('blueprint_id', 'section_id', 'title', 'part_count', 'item_target')}
                for definition in rules['required_sections']]
    for section in sections:
        section.update(sources=[], opportunities=[], qualified_evidence=[],
            official_part_mapping='missing', coverage_status='missing_reviewed_part_mapping',
            scope_status='missing', mastery_claim_allowed=False)
    by_section = {s['section_id']: s for s in sections}
    eligible, unavailable, unmapped = {}, [], []
    if sections:
        rows = c.execute('''SELECT di.*,kn.track,kn.subject,kn.grade_band,kn.code FROM diagnostic_item di
            JOIN knowledge_node kn USING(knowledge_node_id) WHERE kn.status='active' AND di.status='active'
            ORDER BY di.diagnostic_item_id''').fetchall()
        for raw in rows:
            row = dict(raw)
            if not str(row['grade_band']).startswith(child['grade']):
                continue
            if not _visible_source(c, child_id, row):
                continue
            scope = _scope(c, row, snapshot.get('track'))
            if not scope or scope[0] not in by_section:
                unmapped.append(row['diagnostic_item_id'])
                continue
            try:
                source = _source(c, child_id, row, as_of)
            except (LocalSupplyError, ValueError, TypeError, KeyError):
                unavailable.append(row['diagnostic_item_id'])
                continue
            source.update(scope_basis=scope[1], domain_id=scope[2])
            by_section[scope[0]]['sources'].append(source)
            eligible[row['diagnostic_item_id']] = scope[0]
    attempts, unmapped_opportunities = {}, 0
    for row in c.execute('''SELECT da.* FROM diagnostic_attempt da JOIN daily_session ds USING(session_id)
        WHERE da.child_id=? AND ds.child_id=da.child_id ORDER BY da.created_at,da.attempt_id''', (child_id,)):
        occurred = _time(row['created_at'])
        if not occurred or occurred > as_of:
            continue
        target = eligible.get(row['diagnostic_item_id'])
        if not target:
            unmapped_opportunities += 1
            continue
        attempts[row['evidence_id']] = dict(row)
        by_section[target]['opportunities'].append({'attempt_id': row['attempt_id'],
            'diagnostic_item_id': row['diagnostic_item_id'], 'occurred_at': row['created_at'],
            'qualification': 'response_saved_not_score_or_mastery'})
    receipts = {}
    if attempts:
        from .learning_obligations import project_child_mastery_evidence
        projections = project_child_mastery_evidence(c, child_id, as_of=as_of.isoformat())
        for projection in projections.values():
            for receipt in projection['effective_evidence']:
                if receipt['evidence_id'] in attempts and receipt['diagnostic_item_id'] in eligible:
                    receipts[receipt['evidence_id']] = {**receipt, 'score_status': 'verified_local'}
        # One actual answer is one observation even if it later receives review.
        receipts.update(_rubric_receipts(c, child_id, eligible, attempts, as_of))
    for source_id, receipt in receipts.items():
        target = eligible[receipt['diagnostic_item_id']]
        safe = {k: receipt.get(k) for k in ('evidence_id', 'diagnostic_item_id', 'score_status',
            'assessment_id', 'review_revision', 'question_identity', 'occurred_at', 'correctness', 'hint_level')}
        safe.update(source_evidence_id=source_id, independent=bool(receipt['independent'] and receipt['hint_level'] == 0))
        by_section[target]['qualified_evidence'].append(safe)
    for section in sections:
        evidence = section['qualified_evidence']
        section.update(scope_status='practice_scope_available' if section['sources'] else 'missing',
            available_source_count=len(section['sources']),
            reviewed_source_count=sum(s['source_qualification']=='parent_reviewed_source' for s in section['sources']),
            opportunity_count=len(section['opportunities']), qualified_evidence_count=len(evidence),
            independent_correct_count=sum(bool(e['independent'] and e['correctness']==1) for e in evidence),
            distinct_qualified_questions=len({e['question_identity'] for e in evidence}),
            distinct_independent_correct_questions=len({e['question_identity'] for e in evidence if e['independent'] and e['correctness']==1}))
    result = {'version': VERSION, 'child_id': child_id, 'as_of_date': family_today(as_of).isoformat(),
        'time_basis': 'current_source_and_receipt_at_read_not_end_of_day',
        'rule_snapshot_hash': snapshot.get('snapshot_hash'), 'rule_binding_status': rules['binding_status'],
        'public_rule_status': rules['public_rule_status'], 'sections': sections,
        'status': 'scope_inventory_not_official_coverage' if sections else 'rule_applicability_unverified',
        'unavailable_item_ids': unavailable, 'unmapped_item_ids': unmapped,
        'unmapped_or_unavailable_opportunity_count': unmapped_opportunities,
        'exam_readiness_claim_allowed': False, 'local_only': True, 'cloud_transmission_allowed': False}
    result['context_version'] = digest(result)
    return result
