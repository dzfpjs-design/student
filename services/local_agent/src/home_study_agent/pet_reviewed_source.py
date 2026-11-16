"""Typed original listening material for the existing curriculum review flow.

Parsing is not source approval, item eligibility or a score. The entire packet
must belong to one reviewed source excerpt and survive the existing independent
validation, review, publication and withdrawal lineage before consumers use it.
"""

import json
import re
import unicodedata

from .local_supply import LocalSupplyError, digest


SOURCE_SCHEMA = "pet_listening_source_v1"
SCHOOL_SOURCE_SCHEMA = 'school_listening_source_v1'
SOURCE_SCHEMA_V2 = 'pet_listening_source_v2'
SCHOOL_SOURCE_SCHEMA_V2 = 'school_listening_source_v2'
LISTENING_SCHEMAS = {SOURCE_SCHEMA,SCHOOL_SOURCE_SCHEMA,SOURCE_SCHEMA_V2,SCHOOL_SOURCE_SCHEMA_V2}
UNIT_FIELDS = {'unit_key','member_keys','original_audio_key','required_play_count'}


def stable_key(value):
    return isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,119}',value) is not None
VALIDATOR = "pet_listening_exact_source_v1"
_FIELDS = {"schema", "modality", "assessment_mode", "prompt", "stimulus_text", "instruction",
           "required_play_count", "expected_answer"}


def _require(value, code="PET_LISTENING_SOURCE_CONTRACT_INVALID"):
    if not value:
        raise LocalSupplyError(code)


def _object(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def _text(value, maximum):
    return (isinstance(value, str) and 0 < len(value.strip()) <= maximum
            and not any(ord(char) < 32 and char not in "\n\r\t" for char in value))


def parse_listening_source(content_text):
    """Read exact original metadata; never infer a play count or a correct answer.

    No paths, permissions, scores, review identities or alternate question IDs
    are accepted. Caller-supplied packets are still unapproved original drafts.
    """
    _require(isinstance(content_text, str) and len(content_text) <= 16000,
             "PET_LISTENING_TYPED_SOURCE_REQUIRED")
    try:
        source = json.loads(content_text, object_pairs_hook=_object)
    except (TypeError, ValueError) as error:
        raise LocalSupplyError("PET_LISTENING_TYPED_SOURCE_REQUIRED") from error
    school = isinstance(source, dict) and source.get('schema') in {SCHOOL_SOURCE_SCHEMA,SCHOOL_SOURCE_SCHEMA_V2}
    v2 = isinstance(source,dict) and source.get('schema') in {SOURCE_SCHEMA_V2,SCHOOL_SOURCE_SCHEMA_V2}
    _require(isinstance(source, dict) and set(source) == _FIELDS | ({'subject', 'grade', 'knowledge_node_id'} if school else set()) | ({'question_key','delivery_unit'} if v2 else set()))
    _require(source["schema"] in LISTENING_SCHEMAS and source["modality"] == "listening"
             and source["assessment_mode"] == "objective", "PET_LISTENING_OBJECTIVE_SOURCE_REQUIRED")
    _require(_text(source["prompt"], 1200) and _text(source["stimulus_text"], 3000)
             and _text(source["instruction"], 1200))
    if school:
        _require(source['subject'] == '英语' and source['grade'] == '初三'
                 and _text(source['knowledge_node_id'], 240), 'SCHOOL_LISTENING_SCOPE_REQUIRED')
        _require(source['stimulus_text'].strip().casefold() not in source['prompt'].casefold(),
                 'SCHOOL_LISTENING_TRANSCRIPT_IN_QUESTION')
    _require(type(source["required_play_count"]) is int and 1 <= source["required_play_count"] <= 3,
             "PET_LISTENING_ORIGINAL_PLAY_COUNT_REQUIRED")
    if v2:
        unit=source['delivery_unit']
        _require(stable_key(source['question_key']) and isinstance(unit,dict) and set(unit)==UNIT_FIELDS
            and stable_key(unit['unit_key']) and stable_key(unit['original_audio_key'])
            and isinstance(unit['member_keys'],list) and 1 <= len(unit['member_keys']) <= 50
            and all(stable_key(key) for key in unit['member_keys'])
            and len(set(unit['member_keys']))==len(unit['member_keys'])
            and source['question_key'] in unit['member_keys']
            and type(unit['required_play_count']) is int
            and unit['required_play_count']==source['required_play_count'],
            'LISTENING_ORIGINAL_UNIT_DECLARATION_INVALID')
        _require(source['stimulus_text'].strip().casefold() not in source['prompt'].casefold(),
            'LISTENING_TRANSCRIPT_IN_PUBLIC_QUESTION')
    rubric = source["expected_answer"]
    _require(isinstance(rubric, dict) and set(rubric) == {"local_rule", "exact_answers"}
             and rubric["local_rule"] == "local_exact_v1", "PET_LISTENING_EXACT_REFERENCE_REQUIRED")
    answers = rubric["exact_answers"]
    _require(isinstance(answers, list) and 1 <= len(answers) <= 24
             and all(_text(answer, 200) for answer in answers), "PET_LISTENING_EXACT_REFERENCE_REQUIRED")
    _require(len({answer.strip().casefold() for answer in answers}) == len(answers),
             "PET_LISTENING_EXACT_REFERENCE_DUPLICATE")
    return source


def validate_listening_units(sources):
    """Validate complete declarations within one actual imported original source."""
    packets=[s for s in sources if s.get('schema') in {SOURCE_SCHEMA_V2,SCHOOL_SOURCE_SCHEMA_V2}]
    keys=[s['question_key'] for s in packets]
    _require(len(set(keys))==len(keys),'LISTENING_ORIGINAL_QUESTION_KEY_DUPLICATE')
    units={}
    for packet in packets:
        unit=packet['delivery_unit']
        identity={k:packet.get(k) for k in ('schema','subject','grade','knowledge_node_id','stimulus_text','instruction','required_play_count')}
        identity['delivery_unit']=unit
        prior=units.setdefault(unit['unit_key'],identity)
        _require(prior==identity,'LISTENING_ORIGINAL_UNIT_DECLARATION_CONFLICT')
    for key,identity in units.items():
        members={s['question_key'] for s in packets if s['delivery_unit']['unit_key']==key}
        _require(members==set(identity['delivery_unit']['member_keys']),'LISTENING_ORIGINAL_UNIT_MEMBERS_MISSING')
    return units


def published_unit_requirement(connection,child_id,diagnostic_item_id,source):
    """One source/child-scoped requirement shared by all declared member keys."""
    _require(source.get('schema') in {SOURCE_SCHEMA_V2,SCHOOL_SOURCE_SCHEMA_V2})
    row=connection.execute('''SELECT ks.*,ke.excerpt_id FROM specialized_supply_candidate sc
        JOIN curriculum_supply_package cp USING(candidate_id)
        JOIN knowledge_excerpt ke ON ke.excerpt_id=json_extract(cp.payload_json,'$.excerpt_id')
        JOIN knowledge_source ks ON ks.source_id=ke.source_id AND ks.child_id=sc.child_id
        WHERE sc.child_id=? AND sc.diagnostic_item_id=? AND cp.child_id=sc.child_id
            AND cp.state='published' AND sc.status='promoted' ''',(child_id,diagnostic_item_id)).fetchone()
    _require(row is not None,'LISTENING_ORIGINAL_UNIT_SOURCE_REQUIRED')
    packets=[]
    for excerpt in connection.execute('SELECT content_text FROM knowledge_excerpt WHERE source_id=? ORDER BY sequence',(row['source_id'],)):
        try:
            content=json.loads(excerpt[0])
        except (ValueError,TypeError):
            continue
        if isinstance(content,dict) and content.get('schema') in LISTENING_SCHEMAS:
            packets.append(parse_listening_source(excerpt[0]))
    units=validate_listening_units(packets)
    _require(any(p==source for p in packets),'LISTENING_ORIGINAL_UNIT_SOURCE_CHANGED')
    unit=source['delivery_unit']
    identity=dict(child_id=child_id,source_id=row['source_id'],source_version=row['current_version'],
        source_hash=row['content_hash'],grade=row['grade_band'],subject=row['subject'],
        declaration=units[unit['unit_key']])
    return {**unit,'status':'known','version':'listening_unit_instruction_v2:'+digest(identity),
        'basis':'independently_reviewed_original_unit_metadata','local_only':True,
        'cloud_transmission_allowed':False}


def source_payload(content_text):
    """Fields derived from one original excerpt, never supplied by generate HTTP."""
    source = parse_listening_source(content_text)
    return {"prompt": source["prompt"], "expected_answer": None, "pet_listening": source}


def normalize_pet_exact_text(value):
    """Keep word boundaries and negation; do not reuse spoken-math whitespace removal."""
    _require(isinstance(value, str), "PET_LISTENING_EXACT_TEXT_REQUIRED")
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split()).strip(".,!?。！？").strip()


def listening_exact_rubric(source):
    answers = [normalize_pet_exact_text(value) for value in source["expected_answer"]["exact_answers"]]
    _require(all(answers) and len(set(answers)) == len(answers), "PET_LISTENING_EXACT_REFERENCE_DUPLICATE")
    return {"local_rule": "local_exact_v1", "answer_format": "pet_exact_text_v1", "exact_answers": answers}


def check_listening_payload(payload, seed):
    """Structural/source equality check, not independent pedagogical approval."""
    _require(payload.get("validator") == VALIDATOR)
    _require(payload.get("stage") == "parallel", "PET_LISTENING_STAGE_REVIEW_UNAVAILABLE")
    source = parse_listening_source(json.dumps(payload.get("pet_listening")))
    _require(payload.get("prompt") == source["prompt"] and payload.get("expected_answer") is None,
             "PET_LISTENING_SOURCE_CONTENT_CHANGED")
    _require(payload["prompt"] != seed["prompt"], "PET_LISTENING_DUPLICATE_QUESTION")
    return {"validator": VALIDATOR, "computed_answer": source["expected_answer"]["exact_answers"][0],
            "question_hash": digest(payload["prompt"]), "payload_hash": digest(payload),
            "expected_answer": listening_exact_rubric(source), "pet_listening": source,
            "qualification": "original_reference_checked_not_computed_or_skill_certified"}


def candidate_listening_source(connection, candidate):
    """Material for equality/publication; caller must validate review lineage too."""
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='curriculum_supply_package'").fetchone():
        return None
    package = connection.execute("SELECT payload_json FROM curriculum_supply_package WHERE candidate_id=? AND child_id=?",
                                 (candidate["candidate_id"], candidate["child_id"])).fetchone()
    if not package:
        return None
    payload = json.loads(package["payload_json"])
    if payload.get("validator") != VALIDATOR:
        return None
    source = parse_listening_source(json.dumps(payload.get("pet_listening")))
    from .listening_modality import listening_source_matches_node
    node = connection.execute('''SELECT kn.* FROM diagnostic_item di JOIN knowledge_node kn USING(knowledge_node_id)
        WHERE di.diagnostic_item_id=?''', (candidate['seed_item_id'],)).fetchone()
    _require(node is not None and listening_source_matches_node(source, node)
             and candidate['track'] == ('junior_exam' if node['track'] == 'school' else node['track'])
             and candidate["prompt"] == source["prompt"]
             and json.loads(candidate["expected_answer_json"]) == listening_exact_rubric(source),
             "PET_LISTENING_PUBLISHED_CONTENT_CHANGED")
    return source


def published_listening_source(connection, child_id, diagnostic_item_id):
    """Current authority, not a source-label permission or seed allowlist.

    Uses the existing curriculum package and review events, and never writes a
    score. Safe after validate_item; not called by the curriculum validator.
    """
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='curriculum_supply_package'").fetchone():
        return None
    candidate = connection.execute("""SELECT sc.* FROM specialized_supply_candidate sc
        JOIN curriculum_supply_package cp USING(candidate_id)
        WHERE sc.child_id=? AND cp.child_id=sc.child_id AND sc.diagnostic_item_id=?
          AND sc.status='promoted' AND cp.state='published'""", (child_id, diagnostic_item_id)).fetchone()
    if not candidate:
        return None
    source = candidate_listening_source(connection, candidate)
    if source is None:
        return None
    from .curriculum_supply import validate_curriculum_candidate
    validate_curriculum_candidate(connection, candidate)
    if source.get('schema') in {SOURCE_SCHEMA_V2,SCHOOL_SOURCE_SCHEMA_V2}:
        published_unit_requirement(connection,child_id,diagnostic_item_id,source)
    item = connection.execute("""SELECT di.*,kn.track,kn.code,kn.grade_band,kn.subject FROM diagnostic_item di
        JOIN knowledge_node kn USING(knowledge_node_id) WHERE diagnostic_item_id=?
        AND di.status='active' AND kn.status='active'""", (diagnostic_item_id,)).fetchone()
    from .listening_modality import listening_source_matches_node
    _require(item is not None and listening_source_matches_node(source, item)
             and item["source_ref"] == "parent_approved_specialized:" + candidate["candidate_id"]
             and item["prompt"] == source["prompt"] and item["answer_type"] == "listening_text"
             and item["assessment_mode"] == "objective" and item["stimulus_text"] == source["stimulus_text"]
             and not item["stimulus_asset"] and json.loads(item["expected_answer_json"]) == listening_exact_rubric(source),
             "PET_LISTENING_PUBLISHED_CONTENT_CHANGED")
    return source
