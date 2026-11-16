"""Actual local daily-report stage content, evidence and source-bound execution."""
from copy import deepcopy
from datetime import timedelta
import json

import pytest

from home_study_agent.database import Database
from home_study_agent.reports import build_daily_report
from home_study_agent.repository import StudyRepository
from home_study_agent.specialized_program import SpecializedProgramService
from test_specialized_stages import case, baseline, answer, chain, progress, node, reviewed_variants, CHILD, OTHER, NODE, DOMAIN
from test_pet_media_evidence import media


def material(case, roadmaps):
    with case[0].connect() as c:
        child = dict(c.execute("SELECT * FROM child_profile WHERE child_id=?", (CHILD,)).fetchone())
    return {"child": child, "session": {"session_id": case[2]}, "reflection": {"skipped": True},
            "tasks": [], "recalls": [], "specialized_roadmaps": roadmaps}


def roadmap(case, track="competition_math"):
    return {"child_id": CHILD, "track": track, "reference_readiness": 100,
            "evidence_coverage": 1, "stage_progress": progress(case, track)}


def test_old_high_score_without_stage_is_only_unknown(case):
    summary, _ = build_daily_report(material(case, [{"track": "pet", "reference_readiness": 100, "evidence_coverage": 1}]))
    text = "\n".join(summary["facts"])
    assert "阶段证据未知" in text
    assert "准备度100" not in text
    assert summary["specialized_stage_reports"][0]["status"] == "unknown"
    assert summary["specialized_stage_reports"][0]["evidence_refs"] == []


def test_report_contains_current_stage_gaps_and_traceable_evidence(case):
    _, _, b, near, far = chain(case)
    summary, _ = build_daily_report(material(case, [roadmap(case)]))
    text = "\n".join(summary["facts"])
    assert "远迁移" in text and "延迟保持" in text
    assert "不代表考试通过" in text
    detail = summary["specialized_stage_reports"][0]
    assert {b["evidence_id"], near["evidence_id"], far["evidence_id"]} <= set(detail["evidence_refs"])
    assert detail["certification_claim_allowed"] is False
    assert any(row["observed_at"] for row in detail["observations"])
    assert b["evidence_id"] not in text  # IDs belong in local drill-down, not prose.
    assert "PRIVATE" not in text


def test_pet_report_names_missing_modality_and_scoring_not_child_inability(case):
    summary, _ = build_daily_report(material(case, [roadmap(case, "pet")]))
    text = "\n".join(summary["facts"])
    assert "独立基线" in text
    assert "听音交付回执" not in text
    assert "口语音频评分" not in text
    assert "绑定原稿的写作评分回执" in text
    assert "未知不表示孩子不会" in text and "已经掌握" not in text
    assert summary['specialized_stage_reports'][0]['evidence_refs'] == []


@pytest.mark.parametrize('modality,label', [('listening', '听音交付回执'), ('speaking', '口语音频评分')])
def test_attempted_pet_modalities_report_actual_missing_receipts(media, modality, label):
    from home_study_agent.diagnostic_prompt import DiagnosticPromptService
    from test_pet_audio_rubric_review import prepared
    db = Database(media[0].path, media_root=media[4].parent)
    if modality == 'listening':
        listened = DiagnosticPromptService(db).answer(media[2], prompt_id=media[3]['prompt_id'],
            item_version=media[3]['item_version'], answer='in front of the library', client_action_id='no-play-report')
        assert listened['attempt']['score_status'] != 'verified_local'
    else:
        prepared(media)  # Original spoken answer exists, but no parent audio review.
    stage = SpecializedProgramService(db).stage_progress(CHILD, 'pet')
    summary, _ = build_daily_report(material(media, [{'child_id': CHILD, 'track': 'pet', 'stage_progress': stage}]))
    text = '\n'.join(summary['facts'])
    assert label in text
    assert '未知不表示孩子不会' in text
    assert summary['specialized_stage_reports'][0]['evidence_refs'] == []


@pytest.mark.parametrize("mutation", ["other_child", "other_track", "wrong_version", "missing_fingerprint"])
def test_foreign_or_incomplete_stage_snapshot_does_not_support_report(case, mutation):
    data = roadmap(case)
    stage = data["stage_progress"]
    if mutation == "other_child":
        stage["child_id"] = OTHER
    elif mutation == "other_track":
        stage["track"] = "pet"
    elif mutation == "wrong_version":
        stage["version"] = "future_unknown"
    else:
        stage.pop("evidence_fingerprint")
    summary, _ = build_daily_report(material(case, [data]))
    assert summary["specialized_stage_reports"][0]["status"] == "unknown"
    assert summary["specialized_stage_reports"][0]["evidence_refs"] == []


def recheck(case, binding, *, child_id=CHILD):
    from home_study_agent.specialized_stages import revalidate_stage_action
    with case[0].connect() as c:
        return revalidate_stage_action(c, child_id, "competition_math", binding=binding, as_of=case[3][0].isoformat())


def test_bound_action_naturally_becomes_due_without_rebinding_or_stale_hash_rejection(case):
    chain(case)
    binding = deepcopy(node(case)["next_action"])
    assert recheck(case, binding)["status"] == "waiting_due"
    original = binding["diagnostic_item_id"]
    case[3][0] += timedelta(days=7)
    result = recheck(case, binding)
    assert result["status"] == "ready"
    assert result["action"]["diagnostic_item_id"] == original
    assert binding["execution_status"] == "waiting_due"


def test_bound_action_rejects_source_withdrawal_cross_child_and_different_stage(case):
    answer(case, baseline(case))
    reviewed_variants(case)
    binding = deepcopy(node(case)["next_action"])
    assert recheck(case, binding)["status"] == "ready"
    assert recheck(case, binding, child_id=OTHER)["status"] == "blocked"
    wrong = deepcopy(binding)
    wrong["stage_constraint"]["current_stage"] = "far_transfer"
    assert recheck(case, wrong)["status"] == "blocked"
    with case[0].connect() as c:
        c.execute("UPDATE diagnostic_item SET status='withdrawn' WHERE diagnostic_item_id=?", (binding["diagnostic_item_id"],))
        c.commit()
    result = recheck(case, binding)
    assert result["status"] == "blocked" and result["action"] is None


def test_recheck_old_question_after_answer_never_returns_new_question(case):
    answer(case, baseline(case))
    reviewed_variants(case)
    binding = deepcopy(node(case)["next_action"])
    case[3][0] += timedelta(minutes=1)
    answer(case, binding["diagnostic_item_id"])
    assert node(case)["current_stage"] == "far_transfer"
    assert recheck(case, binding)["status"] == "blocked"


def test_recheck_detects_same_stage_changed_prerequisite(case):
    answer(case, baseline(case))
    reviewed_variants(case)
    binding = deepcopy(node(case)["next_action"])
    corrupted = deepcopy(binding)
    corrupted["stage_constraint"]["prerequisite_version"] = "stale-parent-evidence"
    assert recheck(case, corrupted)["reason_code"] == "stage_prerequisites_changed"


def test_actual_report_save_restart_and_withdrawal_rebuild(case):
    item = baseline(case)
    receipt = answer(case, item)
    repo = StudyRepository(case[0])
    with case[0].connect() as c:
        c.execute("UPDATE daily_session SET state='reflection' WHERE session_id=?", (case[2],))
        c.commit()
    repo.complete_with_reflection(case[2], "", "", "", "", True, "stage-report-complete")
    summary, coaching = build_daily_report(repo.report_evidence(case[2]))
    saved = repo.save_daily_report(case[2], summary, coaching)
    refs = [ref for entry in saved["summary"]["specialized_stage_reports"] for ref in entry["evidence_refs"]]
    assert receipt["evidence_id"] in refs
    reopened = StudyRepository(Database(case[0].path)).get_daily_report(saved["report_id"])
    assert reopened["summary"]["specialized_stage_reports"] == saved["summary"]["specialized_stage_reports"]
    with case[0].connect() as c:
        c.execute("UPDATE diagnostic_item SET status='withdrawn' WHERE diagnostic_item_id=?", (item,))
        c.commit()
    rebuilt = StudyRepository(Database(case[0].path)).get_daily_report(saved["report_id"])
    assert receipt["evidence_id"] not in [ref for entry in rebuilt["summary"]["specialized_stage_reports"] for ref in entry["evidence_refs"]]
    assert rebuilt["summary"]["specialized_reference_fingerprint"] != saved["summary"]["specialized_reference_fingerprint"]
