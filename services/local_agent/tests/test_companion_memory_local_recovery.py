from datetime import datetime

import pytest

from home_study_agent.companion_memory import CompanionMemory
from test_companion_memory import memory, turn


@pytest.mark.parametrize('marker', [
    {'version': 'local_voice_recovery_v1', 'mode': 'safety_only'}, None, {},
])
def test_recovery_turn_is_never_a_memory_source(memory, monkeypatch, marker):
    memory.ensure_schema()
    recovery = turn(memory, '我喜欢画图讲解', intent='local_recovery_clarification',
                    action={'type': 'none', 'local_recovery_control': marker})
    with memory.database.connect() as c:
        row = c.execute('SELECT * FROM agent_turn WHERE agent_turn_id=?', (recovery,)).fetchone()
        assert not memory._eligible(row)
    calls = []
    monkeypatch.setattr(memory, '_prune', lambda *args: calls.append('prune'))
    memory.observe_turn(recovery)
    assert calls == []
    with memory.database.connect() as c:
        for table in ('companion_memory_source', 'companion_memory_checkpoint'):
            assert c.execute(f'SELECT count(*) FROM {table} WHERE turn_id=?', (recovery,)).fetchone()[0] == 0


def test_recovery_backfill_does_not_starve_ordinary_memory_after_restart(memory):
    recoveries = [turn(memory, '我喜欢画图讲解', intent='local_recovery_clarification',
        action={'type': 'none', 'local_recovery_control': {'version': 'local_voice_recovery_v1', 'mode': 'safety_only'}})
        for _ in range(3)]
    ordinary = turn(memory, '我喜欢慢一点讲解', created=datetime.fromisoformat('2026-09-04T12:00:01+00:00'))
    replacement = CompanionMemory(memory.database)
    assert replacement.process_pending(1)['processed'] == 1
    with memory.database.connect() as c:
        assert c.execute('SELECT status FROM companion_memory_checkpoint WHERE turn_id=?', (ordinary,)).fetchone()[0] == 'observed'
        assert c.execute('SELECT count(*) FROM companion_memory_checkpoint WHERE turn_id IN (?,?,?)', recoveries).fetchone()[0] == 0
    assert replacement.process_pending(1)['processed'] == 0
