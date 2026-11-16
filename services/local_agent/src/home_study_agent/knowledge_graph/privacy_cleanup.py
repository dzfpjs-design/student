"""Exact parent-captured textbook erasure in PrivacyLifecycle's transaction."""
import json
from hashlib import sha256

from ..privacy_evidence_cleanup import (
    PrivacyEvidenceCleanupError, _exists, _object, _transaction, parent_textbook_sources,
)
from ..repository import utc_now
from .language_candidate_service import TextbookFocusCandidateService


def _shared_lexeme(c, row):
    retained = c.execute('''SELECT f.*,d.page_number FROM kg_language_focus_candidate f
        JOIN kg_language_lexeme_page p ON p.resource_id=f.resource_id AND p.lexeme_id=?
        JOIN kg_document_page d ON d.resource_id=f.resource_id
        WHERE f.language=? AND f.normalized=? AND f.focus_kind=? AND f.status='confirmed'
        ORDER BY f.updated_at DESC,f.candidate_id''',
        (row['lexeme_id'], row['language'], row['normalized'], row['focus_kind'])).fetchall()
    if not retained:
        raise PrivacyEvidenceCleanupError('PRIVACY_TEXTBOOK_SHARED_LEXEME_REQUIRES_REVIEW')
    current = retained[0]
    proposed = _object(current['proposed_json'])
    values = {}
    for key in ('senses', 'collocations', 'confusables', 'pronunciations', 'compounds', 'common_errors'):
        value = proposed.get(key, [])
        if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
            raise PrivacyEvidenceCleanupError('PRIVACY_EVIDENCE_JSON_INVALID')
        values[key + '_json'] = json.dumps(value, ensure_ascii=False)
    for key in ('part_of_speech', 'phonics_or_ipa', 'radical', 'structure', 'stroke_order_ref'):
        value = proposed.get(key)
        if value is not None and not isinstance(value, str):
            raise PrivacyEvidenceCleanupError('PRIVACY_EVIDENCE_JSON_INVALID')
        values[key] = value
    values.update(source_ref=f"document_page:{current['resource_id']}:page:{current['page_number']}", updated_at=utc_now())
    c.execute('UPDATE kg_language_lexeme SET ' + ','.join(key + '=?' for key in values) + ' WHERE lexeme_id=?',
              (*values.values(), row['lexeme_id']))


def _guard_complex_sources(c, resource_ids):
    # Mixed atomic extraction batches contain frozen inputs for other resources.
    # Do not discard those records, or pretend deleting a FK erased their inputs.
    if _exists(c, 'kg_atomic_extraction_batch'):
        for row in c.execute('SELECT resource_ids_json FROM kg_atomic_extraction_batch'):
            try:
                ids = json.loads(row[0])
                if not isinstance(ids, list) or any(not isinstance(v, str) for v in ids):
                    raise ValueError()
            except (ValueError, TypeError) as error:
                raise PrivacyEvidenceCleanupError('PRIVACY_EVIDENCE_JSON_INVALID') from error
            if resource_ids.intersection(ids):
                raise PrivacyEvidenceCleanupError('PRIVACY_TEXTBOOK_ATOMIC_BATCH_REQUIRES_SCOPED_ERASURE')
    for rid in resource_ids:
        if _exists(c, 'kg_atomic_candidate') and c.execute("SELECT 1 FROM kg_atomic_candidate WHERE resource_id=? AND status IN ('confirmed','merged')", (rid,)).fetchone():
            raise PrivacyEvidenceCleanupError('PRIVACY_TEXTBOOK_ATOMIC_PROJECTION_REQUIRES_SCOPED_ERASURE')
        if _exists(c, 'kg_atomic_draft_evidence') and c.execute('SELECT 1 FROM kg_atomic_draft_evidence WHERE resource_id=?', (rid,)).fetchone():
            raise PrivacyEvidenceCleanupError('PRIVACY_TEXTBOOK_ATOMIC_DRAFT_REQUIRES_SCOPED_ERASURE')
        if c.execute("SELECT 1 FROM kg_language_focus_candidate WHERE resource_id=? AND status='projecting'", (rid,)).fetchone():
            raise PrivacyEvidenceCleanupError('PRIVACY_TEXTBOOK_PROJECTION_IN_PROGRESS')


def _host_source_guard(c, refs):
    for table in ('diagnostic_item', 'knowledge_source', 'knowledge_node'):
        if _exists(c, table):
            for row in c.execute(f'SELECT source_ref FROM {table}'):
                if row[0] in refs:
                    raise PrivacyEvidenceCleanupError('PRIVACY_TEXTBOOK_HOST_EVIDENCE_REQUIRES_SCOPED_ERASURE')
    def linked(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {'resource_id','source_ref','segment_id','textbook_resource_id','source_id'} and isinstance(item, str) and item in refs:
                    return True
                if key in {'resource_ids','source_refs','segment_ids'} and isinstance(item, list) and any(isinstance(v, str) and v in refs for v in item):
                    return True
                if linked(item):
                    return True
        elif isinstance(value, list):
            return any(linked(v) for v in value)
        return False
    for table, column in [('rubric_assessment','payload_json'), ('curriculum_supply_package','source_snapshot_json'),
                          ('curriculum_supply_package','payload_json'), ('specialized_supply_candidate','payload_json')]:
        if _exists(c, table) and column in {r[1] for r in c.execute(f'PRAGMA table_info({table})')}:
            for row in c.execute(f'SELECT {column} FROM {table}'):
                if linked(_object(row[0])):
                    raise PrivacyEvidenceCleanupError('PRIVACY_TEXTBOOK_HOST_EVIDENCE_REQUIRES_SCOPED_ERASURE')


def _retained_mapping(c, node, *, approved):
    return c.execute('''SELECT m.source_ref,r.resource_id,r.grade_scope,r.subject_scope,d.page_number
        FROM kg_resource_node m JOIN kg_resource r USING(resource_id) LEFT JOIN kg_document_page d USING(resource_id)
        WHERE (m.node_id=? OR m.node_id IN (SELECT target_node_id FROM kg_edge WHERE source_node_id=? AND relation_type='contains'))
        AND (?=0 OR m.review_status='approved') ORDER BY r.resource_id LIMIT 1''', (node, node, int(approved))).fetchone()


def purge_imported(c, child_id, sources, *, affected_scopes=None):
    """Callback for purge_parent_textbook_sources; no schema init/connection/commit.

Revalidates all child/source/resource identities. FTS and journals without FKs
are erased explicitly; page-owned FKs cascade. Existing child mastery evidence
and nodes shared with other pages are never deleted to make cleanup pass.
"""
    _transaction(c)
    if not c.execute('PRAGMA foreign_keys').fetchone()[0]:
        raise PrivacyEvidenceCleanupError('PRIVACY_EVIDENCE_FOREIGN_KEYS_REQUIRED')
    actual = parent_textbook_sources(c, child_id, artifact_ids=[s['artifact_id'] for s in sources])
    by_id = {s['artifact_id']: s for s in actual}
    if any(s != by_id.get(s['artifact_id']) for s in sources):
        raise PrivacyEvidenceCleanupError('PRIVACY_EVIDENCE_SOURCE_NOT_OWNED')
    resource_ids = {rid for s in actual for rid in s['resource_ids']}
    if not resource_ids:
        return {'kg_resource': 0}
    _guard_complex_sources(c, resource_ids)
    page_refs = {f"document_page:{r['resource_id']}:page:{r['page_number']}" for rid in resource_ids
                 for r in c.execute('SELECT resource_id,page_number FROM kg_document_page WHERE resource_id=?', (rid,))}
    segment_refs = {r[0] for rid in resource_ids for r in c.execute('SELECT segment_id FROM kg_segment WHERE resource_id=?', (rid,))}
    native_refs = resource_ids | page_refs | segment_refs | {s['source_ref'] for s in sources}
    counts = {'kg_resource': 0, 'kg_segment_fts': 0, 'kg_mapping_review_event': 0,
              'kg_mapping_audit_item': 0, 'kg_candidate_mapping_failure': 0, 'kg_node_redacted': 0}
    c.execute('SAVEPOINT privacy_imported_textbook')
    native_scopes = {} if affected_scopes is not None else None
    try:
        from ..privacy_native_sources import purge_native_sources
        counts.update(purge_native_sources(c, child_id, native_refs, affected_scopes=native_scopes))
        _host_source_guard(c, native_refs)
        lexemes, nodes, projection_refs, packs = {}, set(), set(), set()
        original_nodes, original_edges = {}, {}
        for rid in resource_ids:
            for row in c.execute('SELECT n.* FROM kg_node n JOIN kg_resource_node m USING(node_id) WHERE m.resource_id=?', (rid,)):
                original_nodes[row['node_id']] = dict(row)
            for row in c.execute('SELECT e.* FROM kg_edge e JOIN kg_resource_node m ON m.node_id=e.target_node_id WHERE m.resource_id=?', (rid,)):
                original_edges[row['edge_id']] = dict(row)
        for ref in page_refs:
            for row in c.execute('SELECT * FROM kg_node WHERE source_ref=?', (ref,)):
                original_nodes[row['node_id']] = dict(row)
        nodes.update(original_nodes)
        for rid in sorted(resource_ids):
            for row in c.execute('''SELECT l.* FROM kg_language_lexeme l
                                    JOIN kg_language_lexeme_page p USING(lexeme_id) WHERE p.resource_id=?''', (rid,)):
                lexemes[row['lexeme_id']] = dict(row)
            nodes.update(r[0] for r in c.execute('SELECT node_id FROM kg_resource_node WHERE resource_id=?', (rid,)))
            for row in c.execute('SELECT * FROM kg_language_focus_candidate WHERE resource_id=?', (rid,)).fetchall():
                _object(row['proposed_json'])
                projection_refs.add(TextbookFocusCandidateService._projection_source_ref(row['candidate_id']))
                TextbookFocusCandidateService._retire_projection(c, rid, dict(row))
            if _exists(c, 'kg_mapping_audit_item'):
                packs.update(r[0] for r in c.execute('SELECT audit_pack_id FROM kg_mapping_audit_item WHERE resource_id=?', (rid,)))
            for table in ('kg_mapping_review_event', 'kg_mapping_audit_item', 'kg_candidate_mapping_failure'):
                if _exists(c, table):
                    counts[table] += c.execute(f'DELETE FROM {table} WHERE resource_id=?', (rid,)).rowcount
            if _exists(c, 'kg_segment_fts'):
                counts['kg_segment_fts'] += c.execute('DELETE FROM kg_segment_fts WHERE segment_id IN (SELECT segment_id FROM kg_segment WHERE resource_id=?)', (rid,)).rowcount
            counts['kg_resource'] += c.execute('DELETE FROM kg_resource WHERE resource_id=?', (rid,)).rowcount
        for lid, row in lexemes.items():
            if c.execute('SELECT 1 FROM kg_language_lexeme_page WHERE lexeme_id=?', (lid,)).fetchone():
                _shared_lexeme(c, row)
            else:
                c.execute('DELETE FROM kg_language_lexeme WHERE lexeme_id=?', (lid,))
        for node in nodes:
            retained = _retained_mapping(c, node, approved=True)
            if not retained:
                c.execute("UPDATE kg_node SET status='conflicted',review_status='unreviewed',updated_at=? WHERE node_id=?", (utc_now(), node))
                c.execute("""UPDATE kg_review_plan SET status='superseded',updated_at=? WHERE status='active'
                    AND EXISTS(SELECT 1 FROM json_each(kg_review_plan.plan_json,'$.items') WHERE json_extract(value,'$.knowledge_node_id')=?)""", (utc_now(), node))
            else:
                original = original_nodes[node]
                c.execute("UPDATE kg_node SET status=?,review_status=?,updated_at=? WHERE node_id=?", (original['status'], original['review_status'], utc_now(), node))
            original = original_nodes[node]
            if original['source_ref'] in page_refs and original['canonical_key'].startswith(('language:', 'language-lexeme:')):
                preserved = retained or _retained_mapping(c, node, approved=False)
                if preserved:
                    scope = {'grade':preserved['grade_scope'],'subject':preserved['subject_scope'],
                             'page':preserved['page_number'],'atom_family':'language'}
                    c.execute('UPDATE kg_node SET source_ref=?,scope_json=?,description=? WHERE node_id=?',
                        (f"document_page:{preserved['resource_id']}:page:{preserved['page_number']}", json.dumps(scope, ensure_ascii=False),
                         '语言条目；资格以保留来源的审核记录为准。', node))
                else:
                    # Keep opaque FK identity for historical child evidence, not
                    # the erased page's word, description or provenance text.
                    c.execute("UPDATE kg_node SET canonical_key=?,title='来源已删除',description='',scope_json='{}',source_ref='privacy_deleted_source' WHERE node_id=?",
                              ('privacy-deleted:' + sha256(node.encode()).hexdigest(), node))
                    counts['kg_node_redacted'] += 1
            for edge in c.execute('SELECT edge_id,source_ref FROM kg_edge WHERE target_node_id=?', (node,)).fetchall():
                if edge['source_ref'] not in projection_refs:
                    continue
                if retained:
                    original_status = original_edges[edge['edge_id']]['review_status']
                    c.execute("UPDATE kg_edge SET source_ref=?,review_status=?,updated_at=? WHERE edge_id=?", (retained['source_ref'], original_status, utc_now(), edge['edge_id']))
                else:
                    c.execute('DELETE FROM kg_edge WHERE edge_id=?', (edge['edge_id'],))
        for pack in packs:
            c.execute("""UPDATE kg_mapping_audit_pack SET status='superseded',item_count=(SELECT COUNT(*) FROM kg_mapping_audit_item WHERE audit_pack_id=?),updated_at=? WHERE audit_pack_id=?""", (pack, utc_now(), pack))
        c.execute('RELEASE SAVEPOINT privacy_imported_textbook')
    except BaseException:
        c.execute('ROLLBACK TO SAVEPOINT privacy_imported_textbook')
        c.execute('RELEASE SAVEPOINT privacy_imported_textbook')
        raise
    if affected_scopes is not None:
        for owner, nodes in native_scopes.items():
            affected_scopes.setdefault(owner, set()).update(nodes)
    return counts
