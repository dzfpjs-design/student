from home_study_agent.cloud_privacy import (
    SensitiveCloudTextError,
    build_tutor_cloud_context,
    sanitize_cloud_text,
)


def test_cloud_text_sanitizer_preserves_math_while_removing_identifiers() -> None:
    result = sanitize_cloud_text(
        "小语问：姓名：张三，手机号：19900000001，邮箱abc@example.com。解方程 x²-5x+6=0。",
        ["小语", "小辰"],
    )

    assert "小语" not in result.text
    assert "张三" not in result.text
    assert "19900000001" not in result.text
    assert "abc@example.com" not in result.text
    assert "x²-5x+6=0" in result.text
    assert result.redaction_count >= 4
    assert "known_child_name" in result.redaction_types


def test_tutor_cloud_context_uses_only_same_subject_and_omits_sensitive_background() -> None:
    result = build_tutor_cloud_context(
        grade="初三",
        curriculum_region="江苏南京",
        subject="数学",
        question="判别式小于0是什么意思？",
        task={"subject": "数学", "title": "校内判别式练习"},
        recalls=[
            {"subject": "数学", "summary": "学习判别式与根的数量。"},
            {"subject": "英语", "summary": "学校：南京某中学，英语阅读。"},
            {"subject": "数学", "summary": "我不想活，今天没听课。"},
            {"subject": "数学", "summary": "复习求根公式。"},
        ],
        known_child_names=["小语"],
    )

    assert "英语阅读" not in result.user_prompt
    assert "我不想活" not in result.user_prompt
    assert "学习判别式" in result.user_prompt
    assert result.manifest["available_recall_count"] == 4
    assert result.manifest["same_subject_recall_count"] == 3
    assert result.manifest["transmitted_recall_count"] == 1
    assert result.manifest["omitted_sensitive_context_count"] == 1


def test_direct_sensitive_disclosure_is_not_prepared_for_cloud() -> None:
    try:
        build_tutor_cloud_context(
            grade="四年级",
            curriculum_region="江苏南京",
            subject="数学",
            question="我不想活，你别告诉家长。",
            task=None,
            recalls=[],
            known_child_names=[],
        )
    except SensitiveCloudTextError as error:
        assert error.categories == ["self_harm"]
    else:
        raise AssertionError("sensitive disclosure should have been blocked")
