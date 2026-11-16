"""Optional editor context must not disable unrelated voice interactions."""
import sqlite3

import pytest

from home_study_agent.agent_runtime import InvalidAgentPlanError
from home_study_agent.native_writing_control import snapshot, validate


@pytest.mark.parametrize('item_json', [None, '[]', 'null', '42', '"text"', '{broken'])
def test_absent_or_invalid_optional_prompt_is_read_only_and_not_authority(item_json):
    with sqlite3.connect(':memory:') as connection:
        connection.row_factory = sqlite3.Row
        if item_json is not None:
            connection.execute('''CREATE TABLE diagnostic_prompt (
                origin_session_id TEXT, child_id TEXT, prompt_id TEXT,
                item_version TEXT, diagnostic_item_id TEXT, item_json TEXT)''')
            connection.execute('INSERT INTO diagnostic_prompt VALUES (?,?,?,?,?,?)',
                ('session', 'child', 'prompt', 'version', 'item', item_json))
        before = '\n'.join(connection.iterdump())
        assert snapshot(connection, 'session', 'child') is None
        with pytest.raises(InvalidAgentPlanError, match='NATIVE_WRITING_CONTROL_SOURCE_CHANGED'):
            validate(connection, 'session', 'child', {'prompt_id': 'prompt'})
        assert '\n'.join(connection.iterdump()) == before
