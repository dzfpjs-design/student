from home_study_agent.agent_capability_executor import AgentCapabilityExecutor

def test_blocked_exam_never_reads_listening_transcript_or_claims_execution():
    value = {"status": "active", "delivery_status": "blocked",
             "delivery_blockers": ["EXAM_LISTENING_PLAYBACK_SCOPE_UNSUPPORTED"],
             "current_item": {"prompt": "选择答案", "stimulus_text": "不得朗读的听力正文"}}
    reply = AgentCapabilityExecutor._exam_prompt(value)
    assert "原音播放与作答核验还没有接通" in reply
    assert "不得朗读" not in reply
    assert "选择答案" not in reply

def test_legacy_listening_modality_cannot_bypass_delivery_block():
    value = {"status": "paused", "current_item": {"exam_audio_modality": "listening",
             "stimulus_text": "不得朗读的听力正文"}}
    assert "暂时不能开始或继续" in AgentCapabilityExecutor._exam_prompt(value)
