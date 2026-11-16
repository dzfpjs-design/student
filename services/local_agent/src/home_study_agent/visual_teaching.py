"""Local observation scaffolds; exact original evidence, never vision inference."""
import json
from hashlib import sha256

from .database import local_media_scope
from .diagnostic_prompt import DiagnosticPromptService
from .open_response_materials import resolve_source_materials
from .open_response_source import VISUAL_SCHEMA, candidate_source
from .teaching_question_target import resolve_question_target
from .tutor_request_reservation import (TutorRequestIdentity, TutorReservationConflictError,
    TutorReservationPendingError, client_request_hash, normalize_text)
from .visual_material_host import VisualMaterialHost, assert_host_visual_current

VERSION = 'visual_teaching_v1'
FLAGS = dict(local_only=True, cloud_transmission_allowed=False, mastery_claim_allowed=False)


class VisualTeachingUnavailable(ValueError):
    pass


def require(value):
    if not value:
        raise VisualTeachingUnavailable('TUTOR_VISUAL_UNAVAILABLE')


def digest(value):
    return sha256(json.dumps(value, ensure_ascii=True, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _envelope(row):
    value = json.loads(row['output_envelope_json'] or '{}')
    require(isinstance(value, dict) and isinstance(value.get('result', {}), dict))
    return value


def _host(c):
    scope = local_media_scope(c)
    require(scope.get('media_root') is not None)
    return VisualMaterialHost(None, data_dir=scope['media_root'],
        data_encryption_key=scope.get('data_encryption_key'))


def project(c, session_id, *, subject, question, prompt_id=None, item_version=None,
            parent_turn_id=None, ocr_confirmation_id=None, visual_host=None):
    """Borrow an existing snapshot without initializing, committing or connecting."""
    require(c.in_transaction)
    local_parent = False
    if parent_turn_id:
        parent = c.execute('SELECT * FROM tutor_turn WHERE turn_id=? AND session_id=?',
            (parent_turn_id, session_id)).fetchone()
        if parent:
            envelope = _envelope(parent)
            local_parent = bool(envelope.get('result', {}).get('local_visual'))
            if local_parent and prompt_id is None:
                binding = envelope['local_teaching_control']['question_target']['binding']
                prompt_id, item_version = binding['prompt_id'], binding['item_version']
    row = c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=? AND origin_session_id=?',
        (prompt_id, session_id)).fetchone() if prompt_id else None
    candidates = c.execute('SELECT * FROM specialized_supply_candidate WHERE child_id=? AND diagnostic_item_id=?',
        (row['child_id'], row['diagnostic_item_id'])).fetchall() if row else []
    sources = [(candidate, candidate_source(c, candidate)) for candidate in candidates]
    visual = [(candidate, source) for candidate, source in sources if source and source['schema'] == VISUAL_SCHEMA]
    if not visual and not local_parent:
        return None
    require(row is not None and len(visual) == 1)
    target = resolve_question_target(c, session_id, subject=subject, question=question,
        prompt_id=prompt_id, item_version=item_version, parent_turn_id=parent_turn_id,
        ocr_confirmation_id=ocr_confirmation_id)
    require(target['status'] == 'resolved')
    # An answered original is valid; a different live question is not its continuation.
    require(c.execute("""SELECT 1 FROM diagnostic_prompt WHERE child_id=? AND origin_session_id=?
        AND prompt_id<>? AND status IN ('waiting_answer','answer_saved') LIMIT 1""",
        (row['child_id'], session_id, prompt_id)).fetchone() is None)
    candidate, source = visual[0]
    require(candidate['child_id'] == target['child_id'])
    resolved = resolve_source_materials(c, child_id=target['child_id'], source=source,
        candidate_id=candidate['candidate_id'])
    require(1 <= len(resolved['materials']) <= 8)
    host = visual_host or _host(c)
    images, materials, observations = {}, [], []
    for material in resolved['materials']:
        binding = material['binding']['visual']
        assert_host_visual_current(c, target['child_id'], binding, require_managed=True)
        scope, _, region = host._scope(c, target['child_id'], binding['artifact_id'],
            binding['region_id'], binding['revision_id'], binding['region_version'])
        _, grant = host._grant(c, target['child_id'], binding['artifact_id'],
            binding['region_id'], binding['region_version'])
        require(grant and host._confirmed_grant(c, target['child_id'], binding['artifact_id'],
            binding['region_id'], region, grant))
        descriptor = dict(grant['image_descriptor'])
        require(descriptor == material['visual']['image_descriptor'])
        raw = host._asset(c, scope, binding['region_id'], binding['region_version'], grant)
        key = material['material_key']
        require(key not in images)
        images[key] = (descriptor['sha256'], raw)
        materials.append(dict(material_key=key, image_descriptor=descriptor))
        for observation in material['visual']['facts']['observations']:
            observations.append(dict(material_key=key, **observation))
    require(observations and source['task_points'])
    value = dict(version=VERSION, child_id=target['child_id'], session_id=session_id,
        prompt_id=prompt_id, item_version=item_version, materials=materials,
        teaching_scope='observation_scaffold', source_version=digest(dict(
            target=target['fingerprint'], source=source, materials=resolved)), **FLAGS)
    return dict(local_visual=value, target=target, images=images, observations=observations,
        task_points=source['task_points'])


def _lesson(projection, subject, level):
    require(type(level) is int and 1 <= level <= 6)
    observation = projection['observations'][(level - 1) % len(projection['observations'])]
    task = projection['task_points'][(level - 1) % len(projection['task_points'])]
    def excerpt(text):
        return text if len(text) <= 180 else text[:180] + '…'
    invitations = (
        '先在原图中找到这处标记。', '把图中看见的和还不知道的分开。',
        '先整理一条看得见的依据，再说还缺什么条件。', '回到题目的要求，逐项寻找图中的依据。',
        '换个整理顺序：先说题目要求，再指出原图依据。', '先把已有依据和待核对条件串起来，不急着下结论。')
    check = '对照原图，关于“' + excerpt(task) + '”，你能指出哪处标记？还有什么需要核对？'
    observed = '原图中已核对的描述摘录：“' + excerpt(observation['description']) + '”。'
    lines = [invitations[level - 1], observed, check]
    board = dict(version='tutor_blackboard_v1', source=VERSION, sync_mode='segment_estimate',
        subject=subject, title='一起看原题', segments=[dict(segment_id='visual-observation',
        narration=' '.join(lines), duration_hint_ms=0, blocks=[
            dict(kind='text', text=lines[0], tone='chalk'),
            dict(kind='text', text=observed, tone='cyan'),
            dict(kind='question', text=check, tone='yellow')])])
    return ' '.join(lines), board, dict(material_key=observation['material_key'],
        observation_id=observation['id'], task_point_index=(level - 1) % len(projection['task_points']))


def read_turn(c, session_id, turn_id, *, visual_host=None):
    require(c.in_transaction)
    row = c.execute('SELECT * FROM tutor_turn WHERE turn_id=? AND session_id=?',
        (turn_id, session_id)).fetchone()
    require(row is not None and row['status'] == 'completed' and row['safety_status'] == 'passed')
    envelope = _envelope(row)
    saved = envelope.get('result', {}).get('local_visual')
    require(isinstance(saved, dict) and saved.get('version') == VERSION)
    projection = project(c, session_id, subject=row['subject'], question=row['question'],
        prompt_id=saved['prompt_id'], item_version=saved['item_version'],
        parent_turn_id=row['parent_turn_id'], ocr_confirmation_id=row['ocr_confirmation_id'],
        visual_host=visual_host)
    require(projection and projection['local_visual'] == saved and saved['child_id'] == row['child_id'])
    answer, board, _ = _lesson(projection, row['subject'], row['hint_level'])
    require(row['provider'] == 'local' and row['model'] == VERSION and row['answer'] == answer
        and envelope['result']['blackboard'] == board and envelope.get('local_only') is True
        and envelope.get('cloud_transmission_allowed') is False)
    return dict(**saved, turn_id=turn_id), projection['images']


def local_speech_required(c, session_id, text):
    """Legacy TTS requests cannot upload saved local narration or its chunks.

    This is a stored-content backstop, not authorization to upload a paraphrase;
    current clients also retain the local-only policy with the playback journal.
    """
    spoken = normalize_text(text)
    if not spoken:
        return False
    rows = c.execute('SELECT answer,output_envelope_json FROM tutor_turn WHERE session_id=? AND model=?',
        (session_id, VERSION)).fetchall()
    for row in rows:
        fragments = [row['answer']]
        try:
            board = _envelope(row)['result']['blackboard']
            for segment in board['segments']:
                fragments.append(segment['narration'])
                fragments.extend(block.get('text', '') for block in segment['blocks'])
        except (ValueError, KeyError, TypeError, AttributeError):
            return True
        if any(spoken in normalize_text(value) or normalize_text(value) in spoken for value in fragments if value):
            return True
    return False


class VisualTeachingService:
    def __init__(self, database, repository, reservations, visual_host, *, safety_check):
        self.database, self.repository = database, repository
        self.reservations, self.visual_host = reservations, visual_host
        self.safety_check = safety_check

    def _project(self, session_id, payload):
        with self.database.connect() as c:
            c.execute('BEGIN')
            return project(c, session_id, subject=payload.subject, question=payload.question,
                prompt_id=payload.prompt_id, item_version=payload.item_version,
                parent_turn_id=payload.parent_turn_id, ocr_confirmation_id=payload.ocr_confirmation_id,
                visual_host=self.visual_host)

    def try_tutor(self, session_id, payload, *, authority=None):
        with self.database.external_effect_guard():
            existing = self.repository.get_tutor_turn_for_action(payload.client_action_id)
            projection = self._project(session_id, payload)
            if projection is None:
                require(not (existing and existing.get('model') == VERSION))
                return None
            request_hash = client_request_hash(session_id, payload.subject, payload.question,
                payload.hint_level, payload.parent_turn_id, payload.ocr_confirmation_id,
                prompt_id=payload.prompt_id, item_version=payload.item_version)
            if existing:
                if existing.get('tutor_request_hash') != request_hash:
                    raise TutorReservationConflictError()
                self.manifest(session_id, turn_id=existing['turn_id'])
                self.reservations.complete_from_saved_turn(existing, request_hash)
                return dict(existing, **FLAGS)
            claim = self.reservations.claim(TutorRequestIdentity(action_id=payload.client_action_id,
                session_id=session_id, child_id=projection['target']['child_id'], subject=payload.subject,
                question=normalize_text(payload.question), hint_level=payload.hint_level,
                parent_turn_id=payload.parent_turn_id, ocr_confirmation_id=payload.ocr_confirmation_id,
                request_hash=request_hash), payload.confirm_unknown_outcome_retry)
            if claim.status == 'completed':
                raise TutorReservationPendingError()
            try:
                answer, board, support = _lesson(projection, payload.subject, payload.hint_level)
                _, safety = self.safety_check(answer, min(payload.hint_level, 5))
                require(safety == 'passed')
                envelope = dict(request_id=payload.client_action_id, output_type='tutoring',
                    result=dict(content=answer, blackboard=board, local_visual=projection['local_visual'],
                        observation_support=support, **FLAGS),
                    local_teaching_control=dict(question_target=projection['target']),
                    reason_summary='local_confirmed_observation_scaffold', confidence=0.0,
                    evidence_refs=[], model_provider='local', model_version=VERSION,
                    prompt_version=VERSION, knowledge_version=projection['local_visual']['source_version'],
                    policy_version=VERSION, verification_status='unverified', safety_status='passed',
                    fallback_used=False, **FLAGS)

                def guard():
                    current = self._project(session_id, payload)
                    require(current and current['local_visual'] == projection['local_visual'])
                    if authority:
                        authority.assert_current()

                saved = self.repository.save_tutor_turn(session_id, None, payload.subject, payload.question,
                    answer, 'local', VERSION, payload.client_action_id, hint_level=payload.hint_level,
                    parent_turn_id=payload.parent_turn_id, output_envelope=envelope,
                    tutor_request_hash=request_hash, ocr_confirmation_id=payload.ocr_confirmation_id,
                    pre_persist=guard, reservation_attempt=claim.attempts,
                    on_persist=authority.attach if authority else None)
                return dict(saved, **FLAGS)
            except Exception:
                self.reservations.fail(payload.client_action_id, 'TUTOR_VISUAL_NOT_SAVED', expected_attempt=claim.attempts)
                raise

    def manifest(self, session_id, *, turn_id, connection=None):
        if connection is not None:
            return read_turn(connection, session_id, turn_id, visual_host=self.visual_host)[0]
        with self.database.external_effect_guard(), self.database.connect() as c:
            c.execute('BEGIN')
            return read_turn(c, session_id, turn_id, visual_host=self.visual_host)[0]

    def image(self, session_id, *, turn_id, material_key, sha256, connection=None):
        if connection is None:
            with self.database.external_effect_guard(), self.database.connect() as c:
                c.execute('BEGIN')
                return self.image(session_id, turn_id=turn_id, material_key=material_key, sha256=sha256, connection=c)
        _, images = read_turn(connection, session_id, turn_id, visual_host=self.visual_host)
        image = images.get(material_key)
        require(image is not None and image[0] == sha256)
        return image[1]
