from home_study_agent.tutor_blackboard import (
    build_degraded_presentation,
    parse_tutor_blackboard_response,
    presentation_visible_text,
)


def test_parses_structured_multimodal_tutor_presentation() -> None:
    answer, board = parse_tutor_blackboard_response(
        '''{
          "blackboard": {
            "title": "判别式与根的数量",
            "segments": [
              {
                "narration": "先看判别式这个量。",
                "blocks": [
                  {"kind": "heading", "text": "判别式", "tone": "cyan"},
                  {"kind": "formula", "latex": "\\\\Delta=b^2-4ac", "text": "先算它的符号"}
                ]
              },
              {
                "narration": "当它等于零时，两个根会重合。你能说出有几个不同的实数根吗？",
                "blocks": [
                  {"kind": "relation", "nodes": ["判别式等于0", "两个根重合", "1个不同实数根"]},
                  {"kind": "question", "text": "有几个不同的实数根？", "tone": "yellow"}
                ]
              }
            ]
          }
        }''',
        "数学",
        3,
    )

    assert answer.startswith("先看判别式")
    assert board["source"] == "model_structured"
    assert len(board["segments"]) == 2
    assert board["segments"][0]["blocks"][1]["kind"] == "formula"
    assert "两个根重合" in presentation_visible_text(board)


def test_invalid_model_shape_degrades_to_bounded_local_timeline() -> None:
    answer, board = parse_tutor_blackboard_response(
        "先圈出已知条件。然后找出题目要求什么？",
        "数学",
        1,
    )

    assert answer == "先圈出已知条件。然后找出题目要求什么？"
    assert board["source"] == "local_degraded"
    assert board["degraded_reason"] == "invalid_json"
    assert len(board["segments"]) == 2
    assert board["segments"][1]["blocks"][0]["kind"] == "question"


def test_degraded_board_never_accepts_empty_content() -> None:
    board = build_degraded_presentation("", "语文", 2)
    assert board["segments"][0]["narration"]
    assert board["segments"][0]["blocks"][0]["text"]


def test_repairs_common_model_schema_drift_without_losing_the_spoken_timeline() -> None:
    answer, board = parse_tutor_blackboard_response(
        '''这是板书：
        {"blackboard":{"segments":[
          {"speech":"先看判别式的符号。","blocks":[
            {"kind":"equation","formula":"\\\\Delta=b^2-4ac","label":"判别式"},
            {"kind":"bullet","items":["大于零","有两个不等实根"]}
          ]},
          {"narration":"你能说说为什么吗？","blocks":[{"kind":"question","text":"为什么？"}]}
        ]}}
        ```''',
        "数学",
        6,
    )

    assert answer == "先看判别式的符号。你能说说为什么吗？"
    assert board["source"] == "model_structured_repaired"
    assert board["title"] == "第6级讲解"
    assert board["segments"][0]["blocks"][0]["kind"] == "formula"
    assert "json_wrapper_removed" in board["repair_warnings"]
    assert "block_normalized" in board["repair_warnings"]
