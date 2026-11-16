# Real HTTP Safe Response Samples

All seven JSON files are verbatim copies of one synthetic SQLite journey from
`/private/tmp/hsc-c07-section-20260909/http-samples-complete/`.
Do not mix these with the earlier four-sample export: their synthetic IDs differ.

Producer: `test_goal_section_typed_tutor_http.py[original-only]`.
Evidence: `section-retraction-http.log`, 1 passed / 1 deselected / 26.85s.
Backend owner manifest: `section-cross-test-manifest.json`.

The flow uses real create_app HTTP, source authoring/publication, controlled
synthetic PCM and local recognition, saved original answer, original-audio human
review, full app restart, review retraction, another app restart, then source
withdrawal. Original attempt rows are asserted unchanged. No paid model, family
media, or real speech-quality calibration is involved.

Only safe outer roadmap identity fields and the original section_evidence are
exported; there is no prompt, answer, task-point or media payload. The mounted
test stubs transport with these real bodies while exercising the production
parent API adapter. It does not claim to run a live backend from Vitest.

Expected speaking levels (source / response / qualified / independent correct):

- published_reference: 8 / 0 / 0 / 0
- answer_pending_review: 8 / 1 / 0 / 0
- reviewed and restarted: 8 / 1 / 1 / 1
- review_retracted and review_retracted_restart: 8 / 1 / 0 / 0
- withdrawn: 7 / 0 / 0 / 0, with one preserved unattributed historical response

The first answer also changes the original roadmap source version. The final UI
test rejects that fresh response under the old roadmap, then receives the new
parent roadmap before displaying it. Review and retraction of that answer do not
change the roadmap version in these samples; their current section counts still
update. No frontend code changes the backend hash.
