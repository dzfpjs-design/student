"""Synthetic voice/OCR, actual history, memory, report and approval consumers."""
from datetime import datetime
import json

import pytest

from home_study_agent import companion_memory, document_revisit
from home_study_agent.companion_memory import CompanionMemory
from home_study_agent.database import Database
from home_study_agent.document_revisit_memory import report_memory
from home_study_agent.parent_reports import ParentReportService
from home_study_agent.report_source_fingerprint import report_source_fingerprint
from home_study_agent.repository import StudyRepository, ReportNotReadyError
from home_study_agent.reports import build_daily_report
from test_diagnostic_prompt_http import environment, start
from test_document_goal_http import journey, BODY
from test_document_revisit import next_day, offer, say, feedback
from test_voice_runtime_binding import capture, interpret

CHILD = 'child-primary-4'
TEXT = '十二加八一共二十支，我把两种颜色合起来算'


def practice(j, monkeypatch, kind='answer'):
    next_day(j, monkeypatch)
    monkeypatch.setattr(companion_memory, '_now', lambda: datetime.fromisoformat(document_revisit.utc_now()))
    offer(j)
    say(j, monkeypatch, '我现在试试原来的题', 'start', 'memory-start')
    text = TEXT if kind == 'answer' else '我不明白两种颜色为什么要合起来，能提示我吗'
    turn = say(j, monkeypatch, text, kind, 'memory-attempt', execute=False)
    j['responses'].append(feedback(text))
    reply = j['client'].post('/v1/agent-turns/' + turn['agent_turn_id'] + '/execute')
    assert reply.status_code == 200 and 'tutor_turn' in reply.json()['execution'], reply.text
    j['answer_turn'] = turn
    j['feedback_id'] = reply.json()['execution']['tutor_turn']['turn_id']
    return turn


def finish(j):
    with j['db'].connect() as c:
        c.execute("UPDATE daily_session SET state='reflection' WHERE session_id=?", (j['sid'],))
        c.commit()
    repo = StudyRepository(j['db'])
    repo.complete_with_reflection(j['sid'], '', '', '', '', True, 'memory-finish')
    return repo


def test_original_practice_enters_spoken_memory_and_durable_daily_report(journey, monkeypatch):
    j = journey
    practice(j, monkeypatch)
    memory = CompanionMemory(j['db'])
    recall = memory.recall(CHILD, '原题回顾')
    assert len(recall['memories']) == 1
    item = recall['memories'][0]
    assert TEXT in item['content'] and '待验证' in item['content']
    assert item['mastery_update_allowed'] is False and item['verified_fact'] is False
    said = memory.handle_command(CHILD, j['sid'], '上次学了什么', 'memory-spoken-review')
    assert TEXT in said, said
    repo = finish(j)
    evidence = repo.report_evidence(j['sid'])
    assert len(evidence['document_revisit_practice']) == 1
    summary, coaching = build_daily_report(evidence)
    assert any(TEXT in fact and '不证明' in fact for fact in summary['facts'])
    daily = repo.save_daily_report(j['sid'], summary, coaching)
    reopened = StudyRepository(Database(j['db'].path)).get_daily_report(daily['report_id'])
    assert reopened['summary']['document_revisit_practice'] == evidence['document_revisit_practice']
    with j['db'].connect() as c:
        c.row_factory = None
        c.execute('PRAGMA query_only=ON')
        before = c.total_changes
        assert report_memory(c, CHILD, j['sid'])['items'] == evidence['document_revisit_practice']
        assert report_memory(c, 'child-junior-3', j['sid'])['items'] == []
        assert c.total_changes == before
        assert c.execute('SELECT count(*) FROM learning_evidence').fetchone()[0] == 0


def test_help_and_answer_after_feedback_stay_distinct(journey, monkeypatch):
    j = journey
    practice(j, monkeypatch, kind='help')
    turn = say(j, monkeypatch, TEXT, 'answer', 'after-help', execute=False)
    j['responses'].append(feedback(TEXT))
    assert j['client'].post('/v1/agent-turns/' + turn['agent_turn_id'] + '/execute').status_code == 200
    with j['db'].connect() as c:
        entries = report_memory(c, CHILD, j['sid'])['items']
    assert len(entries) == 2
    assert {(r['response_kind'], r['after_feedback']) for r in entries} == {('help', False), ('answer', True)}
    texts = [r['content'] for r in CompanionMemory(j['db']).recall(CHILD, '原题回顾')['memories']]
    assert any('求助时说' in t for t in texts) and any('提示后回答' in t for t in texts)


@pytest.mark.parametrize('change', ['page', 'response', 'voice', 'feedback'])
def test_withdrawal_rebuilds_report_and_invalidates_approved_snapshot(journey, monkeypatch, change):
    j = journey
    practice(j, monkeypatch)
    repo = finish(j)
    summary, coaching = build_daily_report(repo.report_evidence(j['sid']))
    daily = repo.save_daily_report(j['sid'], summary, coaching)
    parents = ParentReportService(j['db'])
    snapshot = parents.create_from_daily(daily)
    parents.edit_draft(snapshot['snapshot_id'], parent_note='保留家长原批注')
    parents.approve(snapshot['snapshot_id'])
    with j['db'].connect() as c:
        raw = dict(c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?', (snapshot['snapshot_id'],)).fetchone())
        assert parents.snapshot_source_current(c, raw)
        before = report_source_fingerprint(c, j['sid'])
        if change == 'page':
            c.execute("UPDATE capture_artifact SET extracted_text='changed page' WHERE artifact_id=?", (j['source']['artifact_id'],))
        elif change == 'response':
            c.execute("UPDATE agent_turn SET transcript='different answer' WHERE agent_turn_id=?", (j['answer_turn']['agent_turn_id'],))
        elif change == 'voice':
            c.execute("UPDATE voice_input SET child_id='child-junior-3' WHERE voice_input_id=?", (j['answer_turn']['action']['voice_input_id'],))
        else:
            c.execute("UPDATE tutor_turn SET safety_status='blocked' WHERE turn_id=?", (j['feedback_id'],))
        c.commit()
        assert report_source_fingerprint(c, j['sid']) != before
        assert not parents.snapshot_source_current(c, raw)
    refreshed = StudyRepository(Database(j['db'].path)).get_daily_report(daily['report_id'])
    entries = refreshed['summary']['document_revisit_practice']
    assert (len(entries) == 1 and entries[0]['feedback'] is None) if change == 'feedback' else entries == []
    assert CompanionMemory(j['db']).recall(CHILD, '原题回顾')['memories'] == []
    assert parents.get(snapshot['snapshot_id'])['parent_edit']['parent_note'] == '保留家长原批注'


def test_forgetting_survives_backfill_and_does_not_delete_original_attempt(journey, monkeypatch):
    j = journey
    practice(j, monkeypatch)
    memory = CompanionMemory(j['db'])
    assert memory.recall(CHILD, '原题回顾')['memories']
    memory.handle_command(CHILD, j['sid'], '忘记原题回顾', 'forget-original-memory')
    with j['db'].connect() as c:
        c.execute("UPDATE companion_memory_checkpoint SET status='skipped' WHERE turn_id=?", (j['answer_turn']['agent_turn_id'],))
        c.commit()
    restored = CompanionMemory(Database(j['db'].path))
    restored.process_pending(500)
    assert restored.recall(CHILD, '原题回顾')['memories'] == []
    with j['db'].connect() as c:
        assert len(report_memory(c, CHILD, j['sid'])['items']) == 1
        assert c.execute("SELECT count(*) FROM companion_memory WHERE observation_kind='document_revisit' AND content IS NOT NULL").fetchone()[0] == 0


def test_source_change_at_final_report_write_is_not_silently_committed(journey, monkeypatch):
    j = journey
    practice(j, monkeypatch)
    repo = finish(j)
    summary, coaching = build_daily_report(repo.report_evidence(j['sid']))
    def changed(c):
        c.execute("UPDATE capture_artifact SET extracted_text='changed inside writer' WHERE artifact_id=?", (j['source']['artifact_id'],))
    with pytest.raises(ReportNotReadyError):
        repo.save_daily_report(j['sid'], summary, coaching, pre_persist=changed)
    with j['db'].connect() as c:
        assert c.execute('SELECT count(*) FROM daily_report WHERE session_id=?', (j['sid'],)).fetchone()[0] == 0
        assert len(report_memory(c, CHILD, j['sid'])['items']) == 1


def test_new_session_voice_recalls_prior_attempt_without_cloud(journey, monkeypatch):
    j = journey
    practice(j, monkeypatch)
    finish(j)
    sid = start(j['client'], action='memory-later-session')
    count = len(j['calls'])
    voice = capture(j['client'], j['settings'], monkeypatch, sid, '上次学了什么', key='recall-real-voice')
    turn = interpret(j['client'], sid, '上次学了什么', 'recall-real-turn', voice)
    assert turn['intent'] == 'memory_command', turn
    assert TEXT in turn['response_text'] and '待验证' in turn['response_text']
    assert len(j['calls']) == count


def test_legacy_skipped_turn_backfill_is_idempotent(journey, monkeypatch):
    j = journey
    with monkeypatch.context() as patch:
        patch.setattr(CompanionMemory, 'observe_turn', lambda *args: None)
        practice(j, monkeypatch)
    memory = CompanionMemory(j['db'])
    with j['db'].connect() as c:
        memory._checkpoint(c, j['answer_turn']['agent_turn_id'], CHILD, 'skipped', companion_memory._now())
        c.commit()
    assert memory.recall(CHILD, '原题回顾')['memories'] == []
    memory.process_pending(500)
    before = memory.recall(CHILD, '原题回顾')['memories']
    assert len(before) == 1
    memory.process_pending(500)
    assert memory.recall(CHILD, '原题回顾')['memories'] == before


def test_duplicate_or_corrupt_history_cannot_multiply_learning_records(journey, monkeypatch):
    j = journey
    practice(j, monkeypatch)
    with j['db'].connect() as c:
        before = report_memory(c, CHILD, j['sid'])
        row = c.execute('SELECT commitment_id,payload_json FROM agent_commitment').fetchone()
        payload = json.loads(row['payload_json'])
        payload['practice']['responses'] *= 2
        c.execute('UPDATE agent_commitment SET payload_json=? WHERE commitment_id=?', (json.dumps(payload), row[0]))
        c.commit()
        assert report_memory(c, CHILD, j['sid']) == before
        payload['practice'] = None
        c.execute('UPDATE agent_commitment SET payload_json=? WHERE commitment_id=?', (json.dumps(payload), row[0]))
        c.commit()
        assert report_memory(c, CHILD, j['sid'])['items'] == []
