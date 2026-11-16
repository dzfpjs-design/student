"""Email sentence punctuation must not disable outbound text redaction."""
import pytest

from home_study_agent.cloud_privacy import sanitize_cloud_text, build_tutor_cloud_context


@pytest.mark.parametrize('address', ['synthetic@example.test', 'name+lesson@sub.example.co.uk'])
@pytest.mark.parametrize('suffix', ['.', '...', '. Next sentence.', '。', ',', ')', '!'])
def test_email_at_sentence_boundary_keeps_punctuation(address, suffix):
    result = sanitize_cloud_text('Contact ' + address + suffix)
    assert result.text == 'Contact [邮箱已脱敏]' + suffix
    assert result.redaction_count == 1 and result.redaction_types == ('email',)
    assert sanitize_cloud_text(result.text).redaction_count == 0


def test_multiple_email_sentence_endings_preserve_math_and_domain_name():
    result = sanitize_cloud_text('synthetic@example.test. second@school.example. x=1.25; example.test')
    assert result.text == '[邮箱已脱敏]. [邮箱已脱敏]. x=1.25; example.test'
    assert result.redaction_count == 2


def test_actual_tutor_context_redacts_email_at_end_of_question_and_recall():
    result = build_tutor_cloud_context(grade='四年级', curriculum_region='南京', subject='英语',
        question='Please reply to synthetic@example.test.', task=None,
        recalls=[{'subject':'英语', 'summary':'Email second@school.example.'}], known_child_names=[])
    assert '@' not in result.user_prompt and '@' not in result.sanitized_question
    assert result.manifest['redaction_count'] == 2
