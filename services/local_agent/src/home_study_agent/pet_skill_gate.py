"""One read-only modality veto for score admission AND historical recomputation.

Consumers must AND skill_scoring_allowed with their existing source/score checks.
It is not a scorer, an assessment, or permission to bypass other learning guards.
No calls into mastery or media projections: safe to use inside qualified_exact_score
without recursively asking that scorer for qualification. Speaking reads only the
original rubric journal and source validator, never a derived mastery projection.
"""

from .local_supply import LocalSupplyError, validate_item
from .pet_listening_requirement import listening_requirement


def pet_skill_gate(connection, child_id, diagnostic_item_id, *, session_id=None, prompt_id=None,
                   item_version=None, attempt_id=None, evidence_id=None, media_root=None,
                   data_encryption_key=None, as_of=None, answer=None):
    """Use the real node's track/code plus item type, never question keywords.

    Audited original instructions can specify playback counts, but delivery alone
    cannot supply modality-specific assessment. Legacy speaking rubrics assess text only;
    the separate versioned human-audio profile must verify its original source.
    A whole-clip receipt (even repeated twice) cannot remove those missing gates.
    """
    result = {"version": "pet_audio_modality_gate_v1", "required": True, "modality": "unknown",
              "status": "unknown", "skill_scoring_allowed": False,
              "task_delivery_qualified": False, "required_play_count": None,
              "blockers": ["source_or_owner_unavailable"],
              "local_only": True, "cloud_transmission_allowed": False,
              "scope": "modality_veto_only_not_a_score_or_mastery_grant"}
    domain = connection.execute("""SELECT kn.*,di.answer_type FROM diagnostic_item di
        JOIN knowledge_node kn USING(knowledge_node_id) WHERE di.diagnostic_item_id=?""",
        (diagnostic_item_id,)).fetchone()
    # Non-PET source/grade/reviewer authority belongs to the existing consumer.
    # This modality veto must not impose a new general diagnostic eligibility rule.
    from .listening_modality import school_listening_node, listening_node
    if domain is not None and domain["track"] != "pet" and not school_listening_node(domain):
        return {**result, "required": False, "modality": None, "status": "not_applicable",
                "skill_scoring_allowed": True, "blockers": []}
    row = connection.execute("""SELECT di.answer_type,kn.track,kn.code,kn.subject,kn.grade_band,cp.grade
        FROM diagnostic_item di JOIN knowledge_node kn USING(knowledge_node_id)
        JOIN child_profile cp ON cp.child_id=? AND cp.status='active'
        WHERE di.diagnostic_item_id=? AND di.status='active' AND kn.status='active'""",
        (child_id, diagnostic_item_id)).fetchone()
    if row is None or not row["grade_band"].startswith(row["grade"]):
        return result
    try:
        validate_item(connection, child_id, diagnostic_item_id)
    except (LocalSupplyError, KeyError, ValueError, TypeError):
        return result
    code = row["code"].upper()
    if listening_node(row, row['answer_type']):
        requirement = listening_requirement(connection, child_id, diagnostic_item_id)
        from .pet_listening_scope import scoped_listening_gate
        try:
            qualified = scoped_listening_gate(connection, child_id, diagnostic_item_id, requirement,
                session_id=session_id, prompt_id=prompt_id, item_version=item_version, attempt_id=attempt_id,
                evidence_id=evidence_id, media_root=media_root, data_encryption_key=data_encryption_key, as_of=as_of, answer=answer)
        except (ValueError, TypeError, KeyError):
            qualified = {"status": "unknown", "skill_scoring_allowed": False, "blockers": ["listening_scope_invalid"]}
        return {**result, **qualified, "modality": "listening",
                "required_play_count": requirement["required_play_count"], "delivery_requirement": requirement}
    if code.startswith("PET.SPEAKING.") or row["answer_type"] in {"speaking", "read_aloud"}:
        from .pet_audio_rubric import scoped_speaking_gate
        qualified = scoped_speaking_gate(connection, child_id, diagnostic_item_id, session_id=session_id,
            prompt_id=prompt_id, item_version=item_version, attempt_id=attempt_id, evidence_id=evidence_id,
            as_of=as_of, answer=answer, media_root=media_root, data_encryption_key=data_encryption_key)
        return {**result, **qualified, "modality": "speaking"}
    return {**result, "required": False, "modality": None, "status": "not_applicable",
            "skill_scoring_allowed": True, "blockers": []}
