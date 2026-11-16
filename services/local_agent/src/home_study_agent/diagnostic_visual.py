"""Exact local original figures, separate from prompt/cloud/scoring payloads."""
from contextlib import contextmanager

from .diagnostic_prompt import DiagnosticPromptService, _version
from .open_response_materials import resolve_source_materials
from .open_response_source import VISUAL_SCHEMA, candidate_source
from .visual_material_host import assert_host_visual_current


class DiagnosticVisualUnavailable(ValueError):
    pass


def _require(condition):
    if not condition:
        raise DiagnosticVisualUnavailable('DIAGNOSTIC_VISUAL_UNAVAILABLE')


class DiagnosticVisualService:
    def __init__(self, database, visual_host):
        self.database = database
        self.visual_host = visual_host

    @contextmanager
    def _snapshot(self, connection):
        if connection is not None:
            _require(connection.in_transaction)
            yield connection
        else:
            # Keep destructive file cleanup outside the entire read/decrypt interval.
            with self.database.external_effect_guard(), self.database.connect() as c:
                c.execute('BEGIN')
                yield c

    def _read(self, c, session_id, prompt_id, item_version):
        session = DiagnosticPromptService._session(c, session_id)
        _require(session['status'] == 'active' and session['child_status'] == 'active'
            and session['state'] not in {'evolving', 'completed'})
        row = DiagnosticPromptService._owned(c, session, prompt_id)
        _require(row['status'] in {'waiting_answer', 'answer_saved'} and row['item_version'] == item_version)
        _require(DiagnosticPromptService._scheduled_source_reason(c, row) is None)
        item = DiagnosticPromptService._item(c, session['child_id'], row['diagnostic_item_id'])
        _require(item is not None and _version(item) == item_version)
        if row['review_task_id']:
            review = DiagnosticPromptService._review(c, session['child_id'], row['review_task_id'])
            _require(review is not None and _version(review) == row['review_version'])
        result = dict(version='diagnostic_visual_v1', child_id=session['child_id'], session_id=session_id,
            prompt_id=prompt_id, item_version=item_version, materials=[], local_only=True,
            cloud_transmission_allowed=False, mastery_claim_allowed=False)
        candidates = c.execute('SELECT * FROM specialized_supply_candidate WHERE child_id=? AND diagnostic_item_id=?',
            (session['child_id'], row['diagnostic_item_id'])).fetchall()
        if not candidates:
            return result, {}
        _require(len(candidates) == 1)
        candidate = candidates[0]
        source = candidate_source(c, candidate)
        if source is None or source['schema'] != VISUAL_SCHEMA:
            return result, {}
        resolved = resolve_source_materials(c, child_id=session['child_id'], source=source,
            candidate_id=candidate['candidate_id'])
        _require(1 <= len(resolved['materials']) <= 8)
        images = {}
        for material in resolved['materials']:
            binding = material['binding']['visual']
            assert_host_visual_current(c, session['child_id'], binding, require_managed=True)
            scope, _, region = self.visual_host._scope(c, session['child_id'], binding['artifact_id'],
                binding['region_id'], binding['revision_id'], binding['region_version'])
            _, grant = self.visual_host._grant(c, session['child_id'], binding['artifact_id'],
                binding['region_id'], binding['region_version'])
            _require(grant and self.visual_host._confirmed_grant(c, session['child_id'], binding['artifact_id'],
                binding['region_id'], region, grant))
            descriptor = dict(grant['image_descriptor'])
            _require(descriptor == material['visual']['image_descriptor'])
            raw = self.visual_host._asset(c, scope, binding['region_id'], binding['region_version'], grant)
            key = material['material_key']
            _require(key not in images)
            images[key] = (descriptor['sha256'], raw)
            result['materials'].append(dict(material_key=key, image_descriptor=descriptor))
        return result, images

    def manifest(self, session_id, *, prompt_id, item_version, connection=None):
        with self._snapshot(connection) as c:
            result, _ = self._read(c, session_id, prompt_id, item_version)
            return result

    def image(self, session_id, *, prompt_id, item_version, material_key, sha256, connection=None):
        with self._snapshot(connection) as c:
            _, images = self._read(c, session_id, prompt_id, item_version)
            image = images.get(material_key)
            _require(image is not None and image[0] == sha256)
            return image[1]
