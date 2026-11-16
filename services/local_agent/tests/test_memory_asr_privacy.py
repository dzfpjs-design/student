from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
import sqlite3
from types import SimpleNamespace

import pytest

from home_study_agent import companion_memory as memory_module
from home_study_agent.local_media_ai import LocalMediaAI
from test_companion_memory import command, items, memory, turn


def media_for(memory):
    return LocalMediaAI(memory.database, memory.database.path.parent, memory.database.path.parent,
                        cloud_asr_gateway=SimpleNamespace(context_supported=True))


def context_for(memory, child="a", session="a1"):
    return media_for(memory)._cloud_asr_context({"child_id": child, "session_id": session})


@pytest.mark.parametrize("state", ["active", "forgotten", "expired"])
def test_asr_omits_source_turn_after_forgetting_or_expiry(memory, monkeypatch, state):
    turn(memory, "这道几何题怎么画辅助线", response="先连接顶点。")
    source = turn(memory, "我喜欢画图讲解", response="我记下你的偏好。")
    memory.observe_turn(source)
    if state == "forgotten":
        memory.forget("a", items(memory)[0]["memory_id"], "asr-forget")
    elif state == "expired":
        later = memory_module._now() + timedelta(days=181)
        monkeypatch.setattr(memory_module, "_now", lambda: later)
        assert not items(memory)
    context = context_for(memory)
    assert "辅助线" in context["text"] and "连接顶点" in context["text"]
    assert "我喜欢画图讲解" not in context["text"] and "记下你的偏好" not in context["text"]
    assert context["recent_turn_count"] == 1


def test_asr_blocks_command_recall_and_unbound_action_source(memory):
    turn(memory, "普通代数问题", response="观察系数。")
    command(memory, "记住我喜欢画图讲解", "unbound-command")
    turn(memory, "记住我喜欢画图讲解", action_id="unbound-command", response="私密确认文本")
    turn(memory, "你记得我什么", intent="memory_command", response="你曾告诉我：我喜欢画图讲解。")
    context = context_for(memory)
    assert "普通代数问题" in context["text"]
    assert "画图" not in context["text"] and "私密" not in context["text"]
    assert context["recent_turn_count"] == 1


@pytest.mark.parametrize("action", [
    {"type": "none", "local_only": True},
    {"type": "none", "cloud_transmission_allowed": False},
    {"type": "none", "source": "local_companion_memory"},
])
def test_asr_honors_local_provenance_without_memory_schema(memory, action):
    turn(memory, "普通英语问题", response="先找主语。")
    turn(memory, "受限原话", action=action, response="受限回复")
    turn(memory, "旧本地召回", intent="memory_command", response="旧本地回复")
    context = context_for(memory)
    assert "普通英语问题" in context["text"]
    assert "受限" not in context["text"] and "旧本地" not in context["text"]
    with memory.database.connect() as connection:
        assert not connection.execute("SELECT 1 FROM sqlite_master WHERE name='companion_memory_source'").fetchone()


def test_asr_does_not_taint_unrelated_short_word_or_other_child(memory):
    memory.observe_turn(turn(memory, "我喜欢折纸", child="b", session="b1", response="孩子乙原文"))
    source = turn(memory, "我喜欢折纸", response="孩子甲本地偏好")
    memory.observe_turn(source)
    turn(memory, "折纸这个词怎么写", response="可以先看偏旁。")
    command(memory, "忘记折纸")
    context = context_for(memory)
    assert "折纸这个词怎么写" in context["text"]
    assert "孩子乙" not in context["text"] and "孩子甲本地偏好" not in context["text"]
    assert context_for(memory, child="b", session="a1")["text"] == ""


@pytest.mark.parametrize("intent", ["speech_echo_ignored", "repetition_confirmation"])
def test_asr_does_not_export_repeated_local_text_as_child_speech(memory, intent):
    turn(memory, "你曾告诉我：我喜欢画图讲解。", intent=intent, response="需要确认复述")
    assert context_for(memory)["recent_turn_count"] == 0


def test_asr_filter_is_read_only_with_an_existing_writer(memory, monkeypatch):
    memory.ensure_schema()
    turn(memory, "继续读普通题干", response="这次只看已知条件。")
    original = memory.database.connect
    denied = []
    writes = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
              sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_ALTER_TABLE,
              sqlite3.SQLITE_TRANSACTION}

    def authorize(operation, *args):
        if operation in writes:
            denied.append(operation)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    @contextmanager
    def read_only():
        with original() as connection:
            connection.set_authorizer(authorize)
            yield connection
            assert not connection.in_transaction

    with original() as writer:
        writer.execute("BEGIN IMMEDIATE")
        monkeypatch.setattr(memory.database, "connect", read_only)
        context = context_for(memory)
        assert "继续读普通题干" in context["text"]
    assert not denied


def test_personal_question_recall_http_never_calls_cloud(tmp_path):
    from test_app import client_for
    from test_companion_memory_integration import RecordingUnavailableGateway, say

    gateway = RecordingUnavailableGateway()
    with client_for(tmp_path, model_gateway=gateway, agent_runtime_enabled=True) as client:
        sid = client.app.state.repository.start_session("child-primary-4", "private-query-session")["session_id"]
        with client.app.state.agent_orchestrator.database.connect() as connection:
            connection.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
            connection.commit()
        for index, query in enumerate(("你记得我最喜欢哪种题目吗", "你记得我喜欢怎样讲解错题吗")):
            result = say(client, sid, query, "private-query-" + str(index))
            assert result["intent"] == "memory_command"
            assert result["action"]["local_only"] is True
            assert result["action"]["cloud_transmission_allowed"] is False
        assert not gateway.requests
