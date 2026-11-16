"""Parent wording may use its existing facts, never newly retained raw attempts."""
import asyncio
import json
import pytest

from home_study_agent.cloud_parent_reports import ParentReportGenerationAuthority, enrich_parent_coaching
from home_study_agent import reports
from test_diagnostic_prompt_http import environment
from test_document_goal_http import journey, BODY
from test_document_revisit_memory import practice, finish, TEXT


@pytest.mark.parametrize('practice_first', [False, True])
def test_actual_parent_model_projection_excludes_local_practice(journey, monkeypatch, practice_first):
    j = journey
    practice(j, monkeypatch)
    repo = finish(j)
    if practice_first:
        original = reports.build_daily_report
        def reordered(evidence):
            summary, baseline = original(evidence)
            summary['facts'].sort(key=lambda fact: TEXT not in fact)
            return summary, baseline
        # Display priority must not be a security boundary. Keep the actual
        # source/consent authority, changing only deterministic fact ordering.
        monkeypatch.setattr(reports, 'build_daily_report', reordered)
    version = ParentReportGenerationAuthority.capture_source(repo, j['sid'])
    evidence = repo.report_evidence(j['sid'])
    summary, baseline = reports.build_daily_report(evidence)
    assert any(TEXT in fact for fact in summary['facts'])
    authority = ParentReportGenerationAuthority(repo, evidence, summary, baseline,
        lambda: repo.report_evidence(j['sid']), version)
    assert authority.has_parent_consent
    count = len(j['calls'])
    j['responses'].append('{}')
    asyncio.run(enrich_parent_coaching(evidence=evidence, summary=summary, baseline=baseline,
        gateway=j['gateway'], repository=repo, settings=j['settings'], cloud_text_allowed=True, authority=authority))
    assert len(j['calls']) == count + 1
    transmitted = json.dumps(j['calls'][-1]['messages'], ensure_ascii=False)
    assert '计划' in transmitted
    assert TEXT not in transmitted and BODY not in transmitted
    assert '需要继续核对是否理解两类数量合并' not in transmitted
