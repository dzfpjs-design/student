"""Format-gate tests only; authority stubs grant no integration credit."""
import hashlib,io,wave
from types import SimpleNamespace
import pytest
from home_study_agent import pet_multipa_candidate as candidate,pet_oral_source


def pcm(frames):
    stream=io.BytesIO()
    with wave.open(stream,'wb')as w:w.setparams((1,2,16000,0,'NONE',''));w.writeframes(b'\0\0'*frames)
    return stream.getvalue()


def input_packet(monkeypatch,frames,family):
    content=pcm(frames);packet={k:'PUBLIC_SYNTHETIC_'+k for k in candidate.FIELDS}
    packet.update(task_family=family,child_id='PUBLIC_CHILD',diagnostic_item_id='PUBLIC_ITEM',item_version='PUBLIC_VERSION',audio=dict(sha256=hashlib.sha256(content).hexdigest(),frames=frames,sample_rate=16000))
    monkeypatch.setattr(candidate,'prepare_speaking_audio_review',lambda *a,**k:packet)
    monkeypatch.setattr(candidate,'read_speaking_review_audio',lambda *a,**k:content)
    monkeypatch.setattr(pet_oral_source,'resolve_read_aloud_reference',lambda *a:({'text_sha256':hashlib.sha256(b'Public reference').hexdigest()},'Public reference'))
    return content


def test_current_read_aloud_audio_preparation_admits_bounded_30s_original_pcm(monkeypatch):
    raw=input_packet(monkeypatch,480000,'read_aloud')
    binding,content=candidate.prepare_input(SimpleNamespace(in_transaction=True),'PUBLIC_SESSION',media_id='PUBLIC_MEDIA',assessment_id='PUBLIC_ASSESSMENT',media_root=None,data_encryption_key=None)
    assert content==raw and binding['audio']['frames']==480000


@pytest.mark.parametrize('frames',[240000,480000])
def test_spoken_response_short_contract_still_rejects_long_audio(monkeypatch,frames):
    input_packet(monkeypatch,frames,'spoken_response')
    with pytest.raises(ValueError,match='PCM16_MONO_SHORT_SEGMENT_REQUIRED'):
        candidate.prepare_input(SimpleNamespace(in_transaction=True),'PUBLIC_SESSION',media_id='PUBLIC_MEDIA',assessment_id='PUBLIC_ASSESSMENT',media_root=None,data_encryption_key=None)


@pytest.mark.parametrize('frames',[9600000,9600001])
def test_original_600second_ceiling_is_not_raised(monkeypatch,frames):
    input_packet(monkeypatch,frames,'read_aloud')
    if frames==9600000:
        binding,content=candidate.prepare_input(SimpleNamespace(in_transaction=True),'PUBLIC_SESSION',media_id='PUBLIC_MEDIA',assessment_id='PUBLIC_ASSESSMENT',media_root=None,data_encryption_key=None)
        assert binding['audio']['frames']==frames
    else:
        with pytest.raises(ValueError):candidate.prepare_input(SimpleNamespace(in_transaction=True),'PUBLIC_SESSION',media_id='PUBLIC_MEDIA',assessment_id='PUBLIC_ASSESSMENT',media_root=None,data_encryption_key=None)
