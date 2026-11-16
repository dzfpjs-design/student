import pytest

from home_study_agent.learning_system_facts import detect_learning_system_query


@pytest.mark.parametrize(
    "utterance,history,scope,track",
    [
        ("你有我要学的派特英语题库吗", [], "resources", "pet"),
        ("或者学习计划吗", [{"content": "刚才问的是 PET 题库"}], "plan", "pet"),
        ("我之前喂给你的", [{"content": "派特英语的题库还在吗"}], "resources", "pet"),
        ("知识库还在吗", [{"content": "我们在说 PET"}], "resources", "pet"),
        ("你还记得我的长期记录吗", [], "memory", "all"),
        ("我的奥数薄弱点是什么", [], "progress", "competition_math"),
        ("今天的校内学习安排是什么", [], "plan", "school"),
    ],
)
def test_detects_product_owned_facts_across_followups(utterance, history, scope, track):
    query = detect_learning_system_query(utterance, history)
    assert query is not None
    assert (query.scope, query.track) == (scope, track)


@pytest.mark.parametrize(
    "utterance",
    ["为什么两个负数相乘是正数", "给我讲个故事", "帮我看看这道题怎么做", "我们聊聊怎样整理学习计划"],
)
def test_does_not_capture_learning_content_as_system_inventory(utterance):
    assert detect_learning_system_query(utterance) is None


@pytest.mark.parametrize(
    "utterance",
    [
        "按今天的安排开始这项推理练习",
        "按照今天的计划做第一项",
        "开始今天安排好的数学练习",
    ],
)
def test_does_not_override_requests_to_execute_the_daily_plan(utterance):
    assert detect_learning_system_query(utterance) is None
