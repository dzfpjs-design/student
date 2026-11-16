"""Resolve exact child ownership separately from shared catalog retention."""
import json

from .privacy_evidence_cleanup import PrivacyEvidenceCleanupError, _exists, _ids
from .privacy_source_withdrawal import require_schema


class NativeOwnerScope:
    def __init__(self, c, child_id, source_ids):
        require_schema(c)
        self.c, self.child_id = c, child_id
        requested = _ids(source_ids)
        all_sources = self.all_rows('knowledge_source')
        self.sources = [r for r in all_sources if r['source_id'] in requested]
        if any(r['child_id'] != child_id for r in self.sources):
            raise PrivacyEvidenceCleanupError('PRIVACY_EVIDENCE_SOURCE_NOT_OWNED')
        missing = requested - {r['source_id'] for r in self.sources}
        erased = {r[0] for r in c.execute('SELECT source_id FROM privacy_source_erasure WHERE child_id=?', (child_id,))}
        if missing - erased:
            raise PrivacyEvidenceCleanupError('PRIVACY_EVIDENCE_SOURCE_NOT_OWNED')
        self.source_ids = {r['source_id'] for r in self.sources}
        self.exact_lineage = self.source_ids | {r['excerpt_id'] for r in self.all_rows('knowledge_excerpt')
                                               if r['source_id'] in self.source_ids}
        self.refs = {r['source_ref'] for r in self.sources if isinstance(r['source_ref'], str) and r['source_ref'].strip()}
        self.retained_refs = {r['source_ref'] for r in all_sources if r['source_id'] not in self.source_ids}
        self._plans = {r['plan_id'] for r in self.all_rows('agent_plan') if r['child_id'] == child_id}
        self._steps = {r['step_id'] for r in self.all_rows('agent_step') if r['plan_id'] in self._plans}

    def all_rows(self, table):
        return [dict(r) for r in self.c.execute(f'SELECT * FROM {table}')] if _exists(self.c, table) else []

    def rows(self, c, table):
        if table == 'knowledge_source':
            return self.sources
        if not _exists(c, table):
            return []
        columns = {r[1] for r in c.execute(f'PRAGMA table_info({table})')}
        if 'child_id' in columns:
            return [dict(r) for r in c.execute(f'SELECT * FROM {table} WHERE child_id=?', (self.child_id,))]
        rows = self.all_rows(table)
        if table == 'agent_step':
            return [r for r in rows if r['plan_id'] in self._plans]
        if table == 'learning_evaluation_execution':
            return [r for r in rows if r['step_id'] in self._steps]
        return rows

    def retained_dependency(self, item_id):
        # A foreign child's malformed snapshot cannot authorize global erasure.
        # Keep the catalog entity instead of parsing it as this child's evidence.
        for table in ('diagnostic_attempt', 'mistake_attempt', 'mistake_case', 'diagnostic_prompt',
                      'specialized_supply_candidate', 'variant_candidate', 'curriculum_supply_package',
                      'specialized_supply_job', 'variant_generation_job', 'rubric_assessment',
                      'agent_plan', 'agent_turn', 'agent_step', 'learning_evaluation_execution'):
            for row in self.all_rows(table):
                if row.get('child_id') == self.child_id:
                    continue
                if table == 'agent_step' and row['plan_id'] in self._plans:
                    continue
                if table == 'learning_evaluation_execution' and row['step_id'] in self._steps:
                    continue
                if row.get('diagnostic_item_id') == item_id or row.get('seed_item_id') == item_id:
                    return True
                for key, raw in row.items():
                    if not key.endswith('_json') or raw is None:
                        continue
                    try:
                        value = json.loads(raw)
                    except (ValueError, TypeError):
                        return True
                    from .privacy_native_sources import _linked
                    if _linked(value, {item_id}):
                        return True
        return False

    def erasable_items(self, items):
        result = set()
        foreign_candidates = self.all_rows('specialized_supply_candidate') + self.all_rows('variant_candidate')
        for iid, row in items.items():
            if (row['source_ref'] not in self.exact_lineage and not any(
                    r.get('diagnostic_item_id') == iid and r['child_id'] == self.child_id
                    for r in foreign_candidates)):
                # A same-named import does not own a global catalog question.
                continue
            if row['source_ref'] in self.retained_refs:
                continue
            if any(r.get('diagnostic_item_id') == iid and r['child_id'] != self.child_id for r in foreign_candidates):
                continue
            if not self.retained_dependency(iid):
                result.add(iid)
        return result

    def erasable_nodes(self, nodes, erased_items):
        result = set()
        all_items = self.all_rows('diagnostic_item')
        evidence = self.all_rows('learning_evidence')
        edges = self.all_rows('knowledge_edge')
        for row in nodes:
            nid = row['knowledge_node_id']
            if row['source_ref'] not in self.exact_lineage:
                continue
            if row['source_ref'] in self.retained_refs:
                continue
            if any(r['knowledge_node_id'] == nid and r['diagnostic_item_id'] not in erased_items for r in all_items):
                continue
            if any(r['knowledge_node_id'] == nid and r['child_id'] != self.child_id for r in evidence):
                continue
            if any(nid in (r['source_node_id'], r['target_node_id']) for r in edges):
                continue
            result.add(nid)
        return result
