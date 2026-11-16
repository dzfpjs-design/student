from __future__ import annotations

from copy import deepcopy
from typing import Any


CAMBRIDGE_B1_FORMAT_URL = (
    "https://www.cambridgeenglish.org/exams-and-tests/qualifications/preliminary/format/"
)
NANJING_2026_POLICY_URL = (
    "https://edu.nanjing.gov.cn/njsjyj/202604/t20260427_5830812.html"
)
NANJING_2026_SCHEDULE_URL = (
    "https://edu.nanjing.gov.cn/zb/gzzs/202604/W020260427633900865320.png"
)


def _section(
    section_id: str,
    title: str,
    duration_minutes: int,
    part_count: int | None,
    item_target: int | None,
    domain_prefixes: tuple[str, ...],
    max_score: float | None = None,
    weight_percent: float | None = None,
    delivery_mode: str = "paper_or_digital",
    item_target_unit: str | None = None,
    duration_variants: list[dict[str, Any]] | None = None,
    operational_duration_basis: str | None = None,
) -> dict[str, Any]:
    return {
        "section_id": section_id,
        "title": title,
        "duration_minutes": duration_minutes,
        "part_count": part_count,
        "item_target": item_target,
        "domain_prefixes": list(domain_prefixes),
        "max_score": max_score,
        "weight_percent": weight_percent,
        "delivery_mode": delivery_mode,
        "item_target_unit": item_target_unit,
        "duration_variants": deepcopy(duration_variants) if duration_variants else [],
        "operational_duration_basis": operational_duration_basis,
    }


_PET_2026 = {
    "blueprint_id": "cambridge-b1-preliminary-for-schools-current-v2",
    "track": "pet",
    "subject": "英语",
    "title": "Cambridge B1 Preliminary for Schools 当前卷型",
    "version": "current-v2",
    "effective_year": 2026,
    "authority": "Cambridge University Press & Assessment",
    "source_urls": [CAMBRIDGE_B1_FORMAT_URL],
    "source_verified_on": "2026-09-23",
    "fidelity": "strict_structure_reference",
    "strict_item_counts": True,
    "requires_subject": True,
    "score_boundary": (
        "覆盖官方四卷结构、时长、部分数和客观题数量；家庭题库没有官方等值校准，"
        "结果仅为能力参考百分比，不换算 Cambridge English Scale 或通过结论。"
    ),
    "sections": [
        _section(
            "reading", "Reading", 45, 6, 32, ("pet.reading.",),
            weight_percent=25, item_target_unit="questions",
        ),
        _section(
            "writing", "Writing", 45, 2, 2, ("pet.writing.",),
            weight_percent=25, item_target_unit="parts",
        ),
        _section(
            "listening", "Listening", 30, 4, 25, ("pet.listening.",),
            weight_percent=25, item_target_unit="questions",
        ),
        _section(
            "speaking", "Speaking", 12, 4, 4, ("pet.speaking.",),
            weight_percent=25,
            item_target_unit="parts",
            duration_variants=[
                {"candidate_count": 2, "min_minutes": 10, "max_minutes": 12, "label": "两人组"},
                {"candidate_count": 3, "min_minutes": 15, "max_minutes": 17, "label": "三人组"},
            ],
            operational_duration_basis="pair_upper_bound",
        ),
    ],
}


_NANJING_SUBJECTS = {
    "语文": (120, 120, "closed_book"),
    "数学": (120, 120, "closed_book"),
    "英语": (120, 90, "closed_book_with_separate_online_listening_speaking"),
    "物理": (100, 90, "closed_book"),
    "化学": (80, 60, "closed_book"),
    "道德与法治": (60, 100, "open_book_combined_with_history"),
    "历史": (60, 100, "open_book_combined_with_morality_and_law"),
}


def _nanjing_blueprint(subject: str, values: tuple[int, int, str]) -> dict[str, Any]:
    max_score, duration, delivery = values
    return {
        "blueprint_id": f"nanjing-2026-junior-exam-{subject}",
        "track": "junior_exam",
        "subject": subject,
        "title": f"南京市2026年中考{subject}考试框架",
        "version": "nanjing-2026",
        "effective_year": 2026,
        "authority": "南京市教育局",
        "source_urls": [NANJING_2026_POLICY_URL, NANJING_2026_SCHEDULE_URL],
        "source_verified_on": "2026-09-02",
        "fidelity": "official_administration_reference",
        "strict_item_counts": False,
        "requires_subject": True,
        "score_boundary": (
            "分值、文化考试时长及开闭卷形式来自南京市教育局2026年公开信息；"
            "本地题库未做市级等值校准，得分仅按完成题目折算为训练参考分。"
        ),
        "sections": [
            _section(
                subject,
                subject,
                duration,
                None,
                None,
                (f"junior_exam.{subject}.",),
                max_score=max_score,
                delivery_mode=delivery,
            )
        ],
    }


_COMPETITION = {
    "blueprint_id": "family-competition-math-ten-domain-v1",
    "track": "competition_math",
    "subject": "数学",
    "title": "家庭数学思维十能力域测评",
    "version": "family-v1",
    "effective_year": 2026,
    "authority": "family_defined",
    "source_urls": [],
    "source_verified_on": "2026-09-02",
    "fidelity": "family_defined_domain_coverage",
    "strict_item_counts": False,
    "requires_subject": True,
    "score_boundary": "家庭长期能力诊断，不对应任何竞赛官方卷型、奖项或分数线。",
    "sections": [
        _section("thinking", "数学思维", 45, 10, None, ("competition_math.",))
    ],
}


EXAM_BLUEPRINTS = {
    item["blueprint_id"]: item
    for item in (
        _PET_2026,
        _COMPETITION,
        *(_nanjing_blueprint(subject, values) for subject, values in _NANJING_SUBJECTS.items()),
    )
}


def list_exam_blueprints(track: str | None = None) -> list[dict[str, Any]]:
    values = [
        deepcopy(value)
        for value in EXAM_BLUEPRINTS.values()
        if not track or value["track"] == track
    ]
    return sorted(values, key=lambda item: (item["track"], item["subject"], item["version"]))


def resolve_exam_blueprint(
    track: str, subject: str, blueprint_id: str | None = None
) -> dict[str, Any]:
    if blueprint_id:
        blueprint = EXAM_BLUEPRINTS.get(blueprint_id)
        if not blueprint or blueprint["track"] != track:
            raise KeyError(blueprint_id)
        if subject and blueprint["subject"] != subject:
            raise KeyError(blueprint_id)
        return deepcopy(blueprint)
    matches = [
        value for value in EXAM_BLUEPRINTS.values()
        if value["track"] == track and (not subject or value["subject"] == subject)
    ]
    if len(matches) != 1:
        raise KeyError(f"{track}:{subject or 'subject-required'}")
    return deepcopy(matches[0])
