"""Shared read-only current-textbook filter for curriculum catalog queries.

With no selection, legacy catalog browsing stays available; it is not proof of
current textbook alignment. Once a child selects a curriculum, queries may not
silently fall back to another edition/term or to a withdrawn textbook source.
"""


def current_curriculum_predicate(connection, child_id, alias):
    if alias not in {"cc", "ct", "cr"}:
        raise ValueError("unsupported catalog alias")
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='curriculum_supply_selection'").fetchone():
        return "1", []
    if not connection.execute("SELECT 1 FROM curriculum_supply_selection WHERE child_id=?", (child_id,)).fetchone():
        return "1", []
    return f"""EXISTS (
        SELECT 1 FROM curriculum_supply_selection selected_scope
        JOIN knowledge_source selected_source ON selected_source.source_id=
            json_extract(selected_scope.scope_json, '$.textbook_source_id')
        WHERE selected_scope.child_id=?
          AND selected_scope.version=(SELECT MAX(latest.version) FROM curriculum_supply_selection latest
              WHERE latest.child_id=selected_scope.child_id AND latest.subject=selected_scope.subject)
          AND selected_scope.subject=REPLACE(REPLACE({alias}.subject_title,'小学',''),'初中','')
          AND json_extract(selected_scope.scope_json,'$.grade')={alias}.grade_title
          AND json_extract(selected_scope.scope_json,'$.edition')={alias}.version_title
          AND json_extract(selected_scope.scope_json,'$.semester')=CASE {alias}.semester
              WHEN '上册' THEN '上学期' WHEN '下册' THEN '下学期' ELSE {alias}.semester END
          AND selected_source.child_id=selected_scope.child_id AND selected_source.review_status='approved'
          AND selected_source.license_status IN ('public','family_owned_index')
          AND selected_source.source_type IN ('textbook_index','school_material','curriculum')
          AND selected_source.subject=selected_scope.subject
          AND selected_source.grade_band={alias}.grade_title
          AND selected_source.version_label={alias}.version_title
          AND (selected_source.effective_from IS NULL OR selected_source.effective_from<=date('now','localtime'))
          AND (selected_source.effective_to IS NULL OR selected_source.effective_to>=date('now','localtime'))
    )""", [child_id]
