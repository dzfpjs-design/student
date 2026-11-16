"""A later question replaces presentation, never the saved teaching history."""


def replaced_by_question(c, session_id, child_id, taught_at):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='diagnostic_prompt'").fetchone():
        return False
    return c.execute("""SELECT 1 FROM diagnostic_prompt WHERE origin_session_id=?
        AND child_id=? AND created_at>? LIMIT 1""", (session_id, child_id, taught_at)).fetchone() is not None
