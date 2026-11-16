"""OCR review schema; the caller owns the connection and transaction."""


def ensure_schema(connection):
    connection.execute('''CREATE TABLE IF NOT EXISTS ocr_review_item(
        review_id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL, child_id TEXT NOT NULL,
        session_id TEXT, review_kind TEXT NOT NULL, region_index INTEGER NOT NULL,
        region_json TEXT, proposed_text TEXT NOT NULL, confidence REAL NOT NULL,
        status TEXT NOT NULL, corrected_text TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')
    connection.execute('CREATE INDEX IF NOT EXISTS idx_ocr_review_child ON ocr_review_item(child_id,status,created_at)')
    connection.execute('''CREATE TABLE IF NOT EXISTS ocr_review_event(
        event_id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL, review_id TEXT,
        event_type TEXT NOT NULL, client_action_id TEXT, payload_json TEXT NOT NULL, created_at TEXT NOT NULL)''')
    connection.execute('''CREATE TABLE IF NOT EXISTS ocr_review_batch(
        client_action_id TEXT PRIMARY KEY, child_id TEXT NOT NULL,
        request_json TEXT NOT NULL, response_json TEXT NOT NULL, created_at TEXT NOT NULL)''')
    columns = {row[1] for row in connection.execute('PRAGMA table_info(ocr_review_event)')}
    if 'client_action_id' not in columns:
        connection.execute('ALTER TABLE ocr_review_event ADD COLUMN client_action_id TEXT')
    connection.execute('''CREATE UNIQUE INDEX IF NOT EXISTS idx_ocr_review_event_action
        ON ocr_review_event(review_id,client_action_id) WHERE review_id IS NOT NULL''')
    connection.execute('''CREATE TABLE IF NOT EXISTS ocr_review_legacy_import(
        source_path TEXT PRIMARY KEY)''')
