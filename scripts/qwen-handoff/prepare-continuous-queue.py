"""Publish the explicitly scoped, code-only first development queue."""
import argparse
import ast
import hashlib
import importlib.util
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location('continuous', Path(__file__).with_name('continuous-development.py'))
continuous = importlib.util.module_from_spec(spec)
spec.loader.exec_module(continuous)

SOURCE = 'services/local_agent/src/home_study_agent/'
TESTS = 'services/local_agent/tests/'
JOBS = [
    ('pending-resume-negation-v2',
     '''Fix permits_resume as a conservative denial veto, NOT an affirmative command whitelist.
Known failure: it currently accepts "先不处理刚才的回答" (must reject).
It incorrectly rejects "不用让我重答，把原来的回答接着处理完" (may pass the veto;
actual resume still requires the existing verified model receipt).
A prior candidate used punctuation as a negation boundary and incorrectly accepted
"先别，处理刚才的回答" and "不要，继续处理旧答案". Both must reject.
Quoted, hypothetical, withdrawn and ambiguous instructions must not become write authority.
Preserve binding, authorized(), model receipts and exact utterances. No new model/API calls.
Implement the precise correction and parameterized adversarial tests, including minimal pairs,
compound negation, hesitation punctuation, and statements about not re-answering vs not resuming.
Do not change scoring/ownership. Use the supplied HTTP tests as contract context.''',
     ['mistake_pending_answer.py'], ['test_mistake_pending_recovery_control_http.py']),
    ('pending-answer-withdrawal',
     '''Implement durable withdrawal of a saved but not yet scored mistake answer.
Current respond stores non-resume text in pending_response, but there is no complete explicit
withdrawal flow. Preserve the old answer as audit history; mark it withdrawn without scoring it.
Repeats must be idempotent; crash/restart and late original retry must not score the withdrawn answer.
Require a verified, current child/session/operation-bound control receipt or an unambiguous explicit
local cancellation; never infer withdrawal from generic dissatisfaction, quotes or "不是我的答案" alone.
Keep new answer replacement separate from withdrawal: do not silently substitute pending_response.
Preserve two-child isolation, source authorization and existing transaction boundaries. Supply code
and tests for rollback, replay and refusal. If orchestration requires files not supplied, name exact
integration contracts instead of pretending the whole voice path is connected. Do not modify
permits_resume; that is a separate task.''',
     ['mistake_dialogue.py', 'mistake_pending_answer.py'],
     ['test_mistake_pending_answer_http.py', 'test_mistake_pending_recovery_control_http.py']),
    ('tutor-playback-recovery',
     '''Audit and fix the real blackboard/voice playback checkpoint path for interruptions:
child says stop, a late segment-ended callback arrives, then resume or restart, and server restarts.
No late callback may advance a paused/cancelled/different-child/different-source lesson; a restart
must not manufacture playback completion or learning evidence. Preserve generation/revision fencing,
idempotent retry, and original presentation prefix. Inspect the current implementation before
claiming a gap; many guards already exist. Implement only concrete defects and add regression tests
at service/HTTP boundaries. If no defect is found, deliver meaningful missing tests and exact audit
findings rather than rewriting working code. Never mark playback finished as mastery.''',
     ['tutor_playback.py', 'tutor_playback_http.py'],
     ['test_tutor_playback.py', 'test_tutor_playback_http.py']),
    ('period-report-evidence-quality',
     '''Audit and correct weekly/monthly parent report aggregation and failure recovery.
The product must distinguish no PET/subject evidence, unverified/withdrawn evidence, and actual poor
performance; missing evidence is not zero ability. Report refresh must not let one child/source failure
erase another child's report. Replayed refresh/retraction/restoration must remain durable and scoped.
Inspect existing reliable-evidence and failure-isolation tests before changing code. Add targeted
tests for mixed evidence states and stale report regeneration. Fix demonstrated defects without
inventing scores, claiming PET certification, or replacing useful individualized advice with templates.
Document any missing source contract that prevents full classification; do not fabricate fields.''',
     ['period_reports.py'],
     ['test_period_report_reliable_evidence.py', 'test_period_report_failure_isolation_http.py']),
    ('strategy-feedback-retraction',
     '''Audit and fix teaching strategy adaptation when reliable source feedback is withdrawn,
revised, duplicated, or belongs to another child. Candidate execution, subjective feedback, independent
learning performance and causal improvement must remain distinct. Existing selection-basis invalidation
and rollback already exist: do not reimplement them from a stale PRD. Find concrete gaps in current
transactions, source freshness or aggregation and provide precise fixes plus regression tests.
Protect the child's active plan from unsupported promotion. No model-generated summary alone may
authorize mastery or strategy success. Supply complete candidate files and truthful unexecuted-test status.''',
     ['strategy_evolution.py'], ['test_strategy_evolution.py']),
    ('pending-evaluation-voice-control',
     '''Audit and fix non-answer voice controls while learning evaluation is pending or failed.
Stop speaking, pause cameras/document observation, ask evaluation status, and ask for a parent must
remain possible without implicitly resubmitting a saved answer or restoring withdrawn source consent.
Test same-key conflicts, other-child references, source deletion, and server restart. Existing safety
bindings and read-only controls already exist; preserve them and repair only demonstrated defects.
Do not broaden grading authority to make a control pass. Deliver code/tests with exact integration
points; acknowledge dependencies unavailable in this code-only packet.''',
     ['pending_evaluation_control.py'],
     ['test_pending_evaluation_control.py', 'test_pending_evaluation_priority_guards.py']),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    repo = args.repo.resolve(strict=True)
    args.root.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = args.root / 'queue.json'
    if destination.exists():
        raise ValueError('Refusing to overwrite an existing queue')
    jobs = []
    for identity, brief, modules, tests in JOBS:
        files = []
        for name in [SOURCE + n for n in modules] + [TESTS + n for n in tests]:
            content = (repo / name).read_bytes()
            ast.parse(content.decode())
            files.append({'path': name, 'sha256': hashlib.sha256(content).hexdigest()})
        job = {'id': identity, 'brief': brief, 'files': files}
        continuous.validate_job(job)
        continuous.source_packet(repo, job)
        jobs.append(job)
    continuous.save(destination, {'version': 1, 'jobs': jobs, 'automatic_integration': False,
                                 'full_product_scope_unchanged': True})
    print(json.dumps({'prepared_jobs': len(jobs), 'queue': str(destination)}))


if __name__ == '__main__':
    main()
