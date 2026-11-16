"""Create actual local arrival proof for tests of private planning helpers."""


def capture_dispatch_guard(orchestrator, session_id, child_id, transcript, action_id='planner-input'):
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    PrivacyLifecycleService(orchestrator.database, orchestrator.database.path.parent).initialize()
    with orchestrator.database.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        orchestrator.learning_input_revisions.capture_arrival(session_id, action_id, transcript, connection=c)
        consent = orchestrator.voice_control_intent.text_consent_version(c)
        guard = orchestrator._input_dispatch_guard(c, session_id, child_id, action_id, transcript, None, consent)
        c.commit()
    return guard
