from __future__ import annotations

from dataclasses import dataclass
import re


POLICY_VERSION = "cloud_text_privacy_v1"

SENSITIVE_DISCLOSURE_PATTERNS = {
    "self_harm": re.compile(r"不想活|想死|自杀|伤害自己|不想存在"),
    "sexual_harm": re.compile(r"性侵|猥亵|裸照|被(?:摸|碰)(?:隐私部位|身体)"),
    "family_violence": re.compile(r"家暴|爸爸打我|妈妈打我|家里人打我|被家长打"),
}

LABELED_PRIVATE_VALUE = re.compile(
    r"(?P<label>姓名|学校|校名|班级|住址|家庭住址|地址|手机号|手机|电话|身份证号|身份证|邮箱)"
    r"\s*[:：]\s*[^\s，,；;。]{2,80}"
)
MOBILE_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
LANDLINE_PHONE = re.compile(r"(?<!\d)(?:0\d{2,3}[- ]?)?\d{7,8}(?!\d)")
CHINESE_ID = re.compile(r"(?<![0-9A-Za-z])\d{17}[0-9Xx](?![0-9A-Za-z])")
# A final dot may be sentence punctuation; domain dots are consumed above.
EMAIL = re.compile(r"(?<![\w.-])[\w.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![\w-])")
CLASS_NAME = re.compile(r"(?:小学|初中|高中)?(?:[一二三四五六九1-9]年级|初[一二三123]|高[一二三123])\s*[（(]?\d{1,2}[)）]?班")


@dataclass(frozen=True)
class SanitizedText:
    text: str
    redaction_count: int
    redaction_types: tuple[str, ...]


@dataclass(frozen=True)
class TutorCloudContext:
    user_prompt: str
    sanitized_question: str
    manifest: dict


class SensitiveCloudTextError(Exception):
    def __init__(self, categories: list[str]):
        super().__init__(",".join(categories))
        self.categories = categories


def sensitive_categories(text: str) -> list[str]:
    return sorted(
        category
        for category, pattern in SENSITIVE_DISCLOSURE_PATTERNS.items()
        if pattern.search(text)
    )


def sanitize_cloud_text(text: str, known_child_names: list[str] | tuple[str, ...] = ()) -> SanitizedText:
    value = text
    actions: list[str] = []
    count = 0

    def replace(pattern: re.Pattern[str], replacement: str, action: str) -> None:
        nonlocal value, count
        value, replacements = pattern.subn(replacement, value)
        if replacements:
            count += replacements
            actions.append(action)

    replace(LABELED_PRIVATE_VALUE, lambda match: f"{match.group('label')}：[已脱敏]", "labeled_private_value")
    replace(CHINESE_ID, "[身份证已脱敏]", "government_id")
    replace(MOBILE_PHONE, "[手机号已脱敏]", "mobile_phone")
    replace(EMAIL, "[邮箱已脱敏]", "email")
    replace(CLASS_NAME, "[班级已脱敏]", "class_name")

    # Landline matching is intentionally restricted to numbers containing a dash or area code.
    for match in list(LANDLINE_PHONE.finditer(value)):
        candidate = match.group(0)
        if "-" not in candidate and not candidate.startswith("0"):
            continue
        value = value.replace(candidate, "[电话已脱敏]", 1)
        count += 1
        actions.append("landline_phone")

    for name in sorted({item.strip() for item in known_child_names if len(item.strip()) >= 2}, key=len, reverse=True):
        replacements = value.count(name)
        if not replacements:
            continue
        value = value.replace(name, "[孩子称呼已脱敏]")
        count += replacements
        actions.append("known_child_name")

    return SanitizedText(value, count, tuple(sorted(set(actions))))


def build_tutor_cloud_context(
    *,
    grade: str,
    curriculum_region: str,
    subject: str,
    question: str,
    task: dict | None,
    recalls: list[dict],
    known_child_names: list[str],
    knowledge_context: list[dict] | None = None,
) -> TutorCloudContext:
    blocked = sensitive_categories(question)
    if blocked:
        raise SensitiveCloudTextError(blocked)

    redaction_count = 0
    redaction_types: set[str] = set()
    omitted_sensitive_context = 0

    def sanitized(text: str) -> str:
        nonlocal redaction_count
        result = sanitize_cloud_text(text, known_child_names)
        redaction_count += result.redaction_count
        redaction_types.update(result.redaction_types)
        return result.text

    safe_subject = sanitized(subject[:40])
    safe_question = sanitized(question[:4000])
    context_lines = [
        f"年级：{grade[:40]}；地区：{curriculum_region[:40]}",
    ]
    if task:
        task_title = str(task.get("title") or "")
        if sensitive_categories(task_title):
            omitted_sensitive_context += 1
            context_lines.append(f"当前任务：{safe_subject} - [本地敏感任务标题未上传]")
        else:
            context_lines.append(f"当前任务：{sanitized(str(task.get('subject') or safe_subject)[:40])} - {sanitized(task_title[:240])}")
    else:
        context_lines.append("当前任务：未记录")

    selected_recalls = []
    same_subject = [item for item in recalls if str(item.get("subject") or "") == subject]
    for item in same_subject[:2]:
        summary = str(item.get("summary") or "")[:240]
        if sensitive_categories(summary):
            omitted_sensitive_context += 1
            continue
        selected_recalls.append(f"{sanitized(str(item.get('subject') or '')[:40])}：{sanitized(summary)}")
    if selected_recalls:
        context_lines.append("今天孩子确认的同学科课堂回忆：" + "；".join(selected_recalls))
    selected_knowledge = []
    for item in (knowledge_context or [])[:2]:
        excerpt = str(item.get("excerpt") or "")[:520]
        if sensitive_categories(excerpt):
            omitted_sensitive_context += 1
            continue
        selected_knowledge.append(
            f"课程《{sanitized(str(item.get('title') or '')[:120])}》"
            f"（{sanitized(str(item.get('unit_path') or '')[:180])}）：{sanitized(excerpt)}"
        )
    if selected_knowledge:
        context_lines.append(
            "本地检索到的公开课程参考（转录可能有识别误差，只作讲解线索，不视为标准答案）："
            + "；".join(selected_knowledge)
        )
    context_lines.append(f"孩子的问题（{safe_subject}）：{safe_question}")

    manifest = {
        "policy_version": POLICY_VERSION,
        "data_categories": [
            "child_grade_region",
            "active_task_summary",
            "same_subject_classroom_recall_summary",
            "public_course_reference_excerpt",
            "tutor_question_text",
        ],
        "raw_audio_or_video": False,
        "redaction_count": redaction_count,
        "redaction_types": sorted(redaction_types),
        "available_recall_count": len(recalls),
        "same_subject_recall_count": len(same_subject),
        "transmitted_recall_count": len(selected_recalls),
        "transmitted_knowledge_excerpt_count": len(selected_knowledge),
        "omitted_sensitive_context_count": omitted_sensitive_context,
        "context_limits": {"recalls": 2, "recall_chars_each": 240, "knowledge_excerpts": 2, "knowledge_chars_each": 520, "task_title_chars": 240},
        "external_provider_terms_apply": True,
    }
    return TutorCloudContext("\n".join(context_lines), safe_question, manifest)
