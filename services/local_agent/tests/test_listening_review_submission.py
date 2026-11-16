"""Original premature speech is retained, revocable, and never auto-submitted."""
import pytest
from test_pet_media_evidence import media
from test_listening_review_journey import test_day_summary_to_real_voice_review_waits_for_new_original_audio as journey


@pytest.mark.parametrize('fault', ['speech_cancel', 'cancel', 'new_question'])
def test_deferred_original_does_not_follow_cancelled_capture_or_new_question(media, tmp_path, monkeypatch, fault):
    journey(media, tmp_path, monkeypatch, False, False, False, early_fault=fault)
