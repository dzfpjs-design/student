"""Dependency-free database checks shared by local voice workflows."""


def scope_valid(connection, session_id, child_id, action_id, *, require_cloud=True):
    row = connection.execute(
        '''SELECT s.status,c.status AS child_status,p.cloud_model_text_allowed
        FROM daily_session s JOIN child_profile c ON c.child_id=s.child_id
        LEFT JOIN privacy_preference p ON p.singleton_id=1
        WHERE s.session_id=? AND s.child_id=?''',
        (session_id, child_id),
    ).fetchone()
    latest = connection.execute(
        '''SELECT client_action_id FROM learning_input_arrival
        WHERE session_id=? AND child_id=? ORDER BY rowid DESC LIMIT 1''',
        (session_id, child_id),
    ).fetchone()
    return bool(
        row
        and row['status'] == row['child_status'] == 'active'
        and (not require_cloud or row['cloud_model_text_allowed'])
        and latest
        and latest[0] == action_id
    )


def text_consent_version(connection):
    row = connection.execute(
        """SELECT consent_id,allowed,policy_version,source,occurred_at
        FROM consent_record WHERE scope='cloud_model_text_allowed'
        ORDER BY rowid DESC LIMIT 1"""
    ).fetchone()
    return tuple(row) if row else None
