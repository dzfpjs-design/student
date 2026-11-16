"""Shared OCR/context schema readiness, inspected before a managed upgrade."""


REQUIRED_COLUMNS = {
    'ocr_job': {'job_id', 'request_id', 'image_hash', 'preferred_route', 'status', 'created_at',
        'started_at', 'completed_at', 'result_json', 'error_code', 'child_id', 'session_id',
        'parent_artifact_id', 'artifact_id', 'owner_key', 'owner_epoch', 'monitor_generation',
        'pipeline_version', 'run_token', 'publication_started'},
    'ocr_cache': {'owner_key', 'image_hash', 'result_json', 'created_at'},
    'ocr_request_tombstone': {'request_digest', 'reason'},
    'ocr_cache_epoch': {'singleton', 'epoch'},
    'ocr_restore_fence': {'restore_id'},
    'ocr_legacy_reference': {'store_id', 'job_id', 'child_id', 'artifact_id', 'source_kind', 'image_hash'},
    'ocr_review_item': {'review_id', 'artifact_id', 'child_id', 'session_id', 'review_kind',
        'region_index', 'region_json', 'proposed_text', 'confidence', 'status', 'corrected_text',
        'created_at', 'updated_at'},
    'ocr_review_event': {'event_id', 'artifact_id', 'review_id', 'event_type', 'client_action_id',
        'payload_json', 'created_at'},
    'ocr_review_batch': {'client_action_id', 'child_id', 'request_json', 'response_json', 'created_at'},
    'ocr_review_legacy_import': {'source_path'},
    'kg_companion_graph_meta': {'key', 'value', 'created_at'},
    'kg_companion_graph_context_event': {'event_id', 'child_fingerprint', 'query_fingerprint',
        'grade_scope_json', 'subject_scope', 'result_count', 'approved_count', 'hypothesis_count',
        'selected_keys_json', 'build_label', 'created_at'},
    'kg_local_course_context_event': {'event_id', 'child_id', 'query_sha256',
        'selected_segments_json', 'created_at'},
}
REQUIRED_INDEXES = {
    'idx_ocr_owned_child', 'idx_ocr_review_child', 'idx_ocr_review_event_action',
    'idx_kg_companion_context_created',
}


def needs_upgrade(connection):
    for table, required in REQUIRED_COLUMNS.items():
        if not required <= {row[1] for row in connection.execute(f'PRAGMA table_info({table})')}:
            return True
    indexes = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    if not REQUIRED_INDEXES <= indexes:
        return True
    return (
        connection.execute('SELECT 1 FROM ocr_cache_epoch WHERE singleton=1').fetchone() is None
        or connection.execute("SELECT 1 FROM kg_companion_graph_meta WHERE key='privacy_salt'").fetchone() is None
    )


def ensure_schema(connection):
    from .local_ocr.cache_lifecycle import ensure_schema as cache
    from .local_ocr.review_schema import ensure_schema as review
    from .knowledge_graph.context_schema import ensure_schema as context
    cache(connection)
    review(connection)
    context(connection)
