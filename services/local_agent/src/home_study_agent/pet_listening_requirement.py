"""Audited original listening instructions, not a blanket PET repetition rule."""

import hashlib
import json

from .local_supply import LocalSupplyError, validate_item


POLICY_VERSION = "pet_listening_instruction_v1"
# These prefixes were checked against the shipped original, not inferred from a
# domain label or from the catalog's non-task editorial comment about two plays.
CATALOG_REQUIREMENTS = {
    "diag-pet-listening-1": ("听两遍短消息：", 2),
    "diag-pet-listening-2": ("听两遍通知：", 2),
}


def listening_requirement(connection, child_id, diagnostic_item_id):
    unknown = {"status": "unknown", "version": None, "required_play_count": None,
               "basis": "original_instruction_not_verified", "local_only": True}
    try:
        item = validate_item(connection, child_id, diagnostic_item_id)
    except (LocalSupplyError, ValueError, TypeError, KeyError):
        return unknown
    if item["source_ref"].startswith("parent_approved_specialized:"):
        from .pet_reviewed_source import published_listening_source
        try:
            source = published_listening_source(connection, child_id, diagnostic_item_id)
        except (LocalSupplyError, ValueError, TypeError, KeyError):
            return unknown
        if source is None:
            return unknown
        if source.get('schema') in {'pet_listening_source_v2','school_listening_source_v2'}:
            from .pet_reviewed_source import published_unit_requirement
            return published_unit_requirement(connection,child_id,diagnostic_item_id,source)
        digest = hashlib.sha256(json.dumps({"policy": "pet_reviewed_listening_instruction_v1",
            "item": item, "source": source}, sort_keys=True, ensure_ascii=False,
            separators=(",", ":")).encode()).hexdigest()
        return {"status": "known", "version": "pet_reviewed_listening_instruction_v1:" + digest,
                "required_play_count": source["required_play_count"],
                "basis": "independently_reviewed_original_metadata", "local_only": True}
    declaration = CATALOG_REQUIREMENTS.get(diagnostic_item_id)
    if not declaration or not item["source_ref"].startswith("hsc_original"):
        return unknown
    prefix, count = declaration
    if (type(count) is not int or not 1 <= count <= 3 or not item["prompt"].startswith(prefix)
            or not item["stimulus_text"] or item["answer_type"] != "listening_text"):
        return unknown
    digest = hashlib.sha256(json.dumps({"policy": POLICY_VERSION, "item": item,
        "instruction_prefix": prefix, "required_play_count": count}, sort_keys=True,
        ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    return {"status": "known", "version": POLICY_VERSION + ":" + digest,
            "required_play_count": count, "basis": "audited_original_instruction", "local_only": True}


def listening_unit_requirement(connection,child_id,diagnostic_item_id):
    """V2 uses audited shared keys; legacy v1 remains a single item unit."""
    result=listening_requirement(connection,child_id,diagnostic_item_id)
    if result['status']!='known' or 'unit_key' in result:
        return result
    return {**result,'unit_key':'legacy-item:'+diagnostic_item_id,
        'member_keys':[diagnostic_item_id],'original_audio_key':'legacy-item:'+diagnostic_item_id,
        'cloud_transmission_allowed':False}
