"""Disposable local artifacts; synthetic transport fixtures are never outcomes."""

import hashlib
import json
import os
import sys

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from home_study_agent.pet_media_evidence import PetMediaEvidenceError, PetMediaEvidenceService, inspect_local_pcm
from home_study_agent.pet_media_files import write_new_managed_media, read_managed_media
from test_pet_media_evidence import media, CHILD, change, review
from test_pet_media_http import mounted


KEY = bytes(range(32))


@pytest.fixture
def supply(media, monkeypatch):
    db, _, sid, prompt, path, clock = media
    service = PetMediaEvidenceService(db, media_root=path.parent, runtime_epoch="supply", data_encryption_key=KEY)
    monkeypatch.setattr("home_study_agent.pet_material_supply.render_original_practice",
                        lambda text: (path.read_bytes(), "synthetic_test_only_not_child_effect"))
    return db, service, sid, prompt, path, clock


def materialize(supply, key="make"):
    return supply[1].materialize_listening(supply[2], prompt_id=supply[3]["prompt_id"],
        item_version=supply[3]["item_version"], client_action_id=key)


def bind(supply, material, key="bind-material"):
    return supply[1].begin_listening(supply[2], prompt_id=supply[3]["prompt_id"],
        item_version=supply[3]["item_version"], artifact_id=material["artifact_id"], client_action_id=key)


def test_encrypted_supply_review_delivery_restart_and_no_models(supply):
    material = materialize(supply)
    assert material["status"] == "completed" and not material["original_recording_verified"]
    assert material["origin"] == "local_synthesized_practice" and material["parent_review_required"]
    assert materialize(supply) == material
    row = bind(supply, material)
    assert not row["source_reviewed"] and not row["qualification"]["task_delivery_qualified"]
    with pytest.raises(PetMediaEvidenceError, match="not_reviewed"):
        supply[1].read_listening_audio(supply[2], media_id=row["media_id"], audio_sha256=row["audio"]["sha256"])
    assert supply[1].review_listening_audio(supply[2], media_id=row["media_id"], audio_sha256=row["audio"]["sha256"]) == supply[4].read_bytes()
    reviewed = review(supply, row)
    restarted = PetMediaEvidenceService(supply[0], media_root=supply[4].parent, runtime_epoch="restarted", data_encryption_key=KEY)
    assert restarted.read(supply[2], media_id=row["media_id"]) == reviewed
    assert restarted.read_listening_audio(supply[2], media_id=row["media_id"], audio_sha256=row["audio"]["sha256"]) == supply[4].read_bytes()
    with supply[0].connect() as c:
        artifact = c.execute("SELECT * FROM capture_artifact WHERE artifact_id=?", (material["artifact_id"],)).fetchone()
        content = read_managed_media(artifact["local_path"], media_root=supply[4].parent, max_bytes=24*1024*1024)
        assert content.startswith(b"HSC1") and b"RIFF" not in content[:16]
        assert c.execute("SELECT count(*) FROM model_call").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM diagnostic_attempt").fetchone()[0] == 0
    listed = restarted.list_for_prompt(supply[2], prompt_id=supply[3]["prompt_id"], item_version=supply[3]["item_version"])
    assert listed["materials"] == [material]


def test_missing_key_never_creates_plaintext_or_reservation(media):
    with pytest.raises(PetMediaEvidenceError, match="encryption_key_not_ready"):
        materialize(media)
    with media[0].connect() as c:
        assert c.execute("SELECT count(*) FROM capture_artifact").fetchone()[0] == 1


@pytest.mark.parametrize("key", [None, b"x"*32])
def test_encrypted_voice_and_artifact_require_actual_key(supply, key):
    material = materialize(supply)
    with supply[0].connect() as c:
        path = c.execute("SELECT local_path FROM capture_artifact WHERE artifact_id=?", (material["artifact_id"],)).fetchone()[0]
    assert inspect_local_pcm(path, media_root=supply[4].parent, data_encryption_key=key)["status"] == "unavailable_or_unsupported_audio"
    assert inspect_local_pcm(path, media_root=supply[4].parent, data_encryption_key=KEY)["status"] == "usable_signal"


def test_true_parent_http_create_audition_review_and_exact_audio(supply):
    client, token = mounted(supply)
    client.headers["X-HSC-Session"] = "local-test-token"
    base = f"/v1/sessions/{supply[2]}/pet-media"
    path = base + f"/prompt/{supply[3]['prompt_id']}/materialize"
    payload = dict(item_version=supply[3]["item_version"], client_action_id="http-make")
    assert client.post(path, json=payload).status_code == 401
    headers = {"X-HSC-Parent": token}
    assert client.post(path, json={**payload, "text": "HTTP may not choose stimulus"}, headers=headers).status_code == 422
    response = client.post(path, json=payload, headers=headers)
    assert response.status_code == 200, response.text
    material = response.json()["material"]
    assert client.post(path, json=payload, headers=headers).json() == response.json()
    row = bind(supply, material)
    url = base + f"/{row['media_id']}/review-audio?audio_sha256={row['audio']['sha256']}"
    assert client.get(url).status_code == 401
    assert client.get(url, headers=headers).content == supply[4].read_bytes()
    response = client.post(base + f"/{row['media_id']}/source-review", headers=headers,
        json=dict(audio_sha256=row["audio"]["sha256"], operation="approve", client_action_id="http-approve"))
    assert response.status_code == 200
    assert client.get(url.replace("review-audio", "audio")).content == supply[4].read_bytes()


@pytest.mark.parametrize("fault", ["withdraw", "cancel", "delete", "other_child", "content"])
def test_renderer_return_cannot_resurrect_changed_source_or_reservation(supply, monkeypatch, fault):
    def interrupted(text):
        if fault == "withdraw":
            change(supply[0], "UPDATE diagnostic_item SET status='inactive' WHERE diagnostic_item_id='diag-pet-listening-1'")
        elif fault == "cancel":
            DiagnosticPromptService(supply[0]).cancel(supply[2], prompt_id=supply[3]["prompt_id"], item_version=supply[3]["item_version"])
        elif fault == "delete":
            change(supply[0], "DELETE FROM capture_artifact WHERE client_action_id='pet-material:make'")
        elif fault == "other_child":
            change(supply[0], "UPDATE capture_artifact SET child_id='child-junior-3' WHERE client_action_id='pet-material:make'")
        else:
            change(supply[0], "UPDATE diagnostic_item SET stimulus_text='changed' WHERE diagnostic_item_id='diag-pet-listening-1'")
        return supply[4].read_bytes(), "interrupted-fixture"
    monkeypatch.setattr("home_study_agent.pet_material_supply.render_original_practice", interrupted)
    with pytest.raises(PetMediaEvidenceError):
        materialize(supply)
    assert not list(supply[4].parent.glob("*.enc"))


def test_idempotency_never_rebinds_or_regenerates_deleted_original(supply):
    materialize(supply)
    service = DiagnosticPromptService(supply[0])
    service.cancel(supply[2], prompt_id=supply[3]["prompt_id"], item_version=supply[3]["item_version"])
    new = service.prepare(supply[2], track="pet", preferred_item_id="diag-pet-listening-1", client_action_id="another")["prompt"]
    with pytest.raises(PetMediaEvidenceError, match="material_action_conflict"):
        supply[1].materialize_listening(supply[2], prompt_id=new["prompt_id"], item_version=new["item_version"], client_action_id="make")
    with pytest.raises(PetMediaEvidenceError, match="original_binding_conflict"):
        with supply[0].connect() as c:
            artifact_id = c.execute("SELECT artifact_id FROM capture_artifact WHERE client_action_id='pet-material:make'").fetchone()[0]
        supply[1].begin_listening(supply[2], prompt_id=new["prompt_id"], item_version=new["item_version"],
                                 artifact_id=artifact_id, client_action_id="reuse-original")


def test_process_crash_after_file_write_can_retry_original_reservation(supply, monkeypatch):
    import home_study_agent.pet_material_supply as module
    original = module.write_new_managed_media
    def crash(*args, **kwargs):
        original(*args, **kwargs)
        raise SystemExit("fixture process crash")
    monkeypatch.setattr(module, "write_new_managed_media", crash)
    with pytest.raises(SystemExit):
        materialize(supply)
    monkeypatch.setattr(module, "write_new_managed_media", original)
    restarted = PetMediaEvidenceService(supply[0], media_root=supply[4].parent, data_encryption_key=KEY, runtime_epoch="second-process")
    result = materialize((supply[0], restarted, *supply[2:]))
    assert result["status"] == "completed" and len(list(supply[4].parent.glob("*.enc"))) == 1


@pytest.mark.skipif(sys.platform != "darwin", reason="Requires installed macOS offline voice, not a child outcome")
def test_actual_local_original_text_renderer_to_encrypted_artifact(media):
    service = PetMediaEvidenceService(media[0], media_root=media[4].parent, data_encryption_key=KEY)
    actual = (media[0], service, *media[2:])
    result = materialize(actual)
    row = bind(actual, result)
    assert result["engine"] == "macos_installed_daniel_v1"
    assert row["audio"]["frames"] > 16000 and row["audio"]["status"] == "usable_signal"
    assert not row["qualification"]["skill_qualified"] and not row["source_reviewed"]


def test_exclusive_encrypted_writer_does_not_follow_symlinks_or_overwrite(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    data = b"HSC1" + os.urandom(44)
    name = "a"*32 + ".wav.enc"
    path = write_new_managed_media(data, media_root=root, filename=name)
    assert read_managed_media(path, media_root=root, max_bytes=100) == data
    with pytest.raises(FileExistsError):
        write_new_managed_media(data, media_root=root, filename=name)
    link = tmp_path / "link"
    link.symlink_to(root, target_is_directory=True)
    with pytest.raises((OSError, ValueError)):
        write_new_managed_media(data, media_root=link, filename="b"*32 + ".wav.enc")


@pytest.mark.parametrize("bad_root", [False, True])
def test_windows_encrypted_writer_holds_verified_parent_handles(monkeypatch, bad_root):
    from pathlib import PureWindowsPath
    from types import SimpleNamespace
    import home_study_agent.pet_media_files as files
    from test_pet_media_files import Kernel
    kernel = Kernel()
    written = []
    def create(path):
        assert len(kernel.live) == 2
        return kernel.open(path, directory=False)
    def write(handle, data):
        assert len(kernel.live) == 3
        written.append(data)
    kernel.create, kernel.write, kernel.flush = create, write, lambda handle: None
    if bad_root:
        original = kernel.info
        kernel.info = lambda handle: {**original(handle), "attributes": 0x410} if handle == 2 else original(handle)
    monkeypatch.setattr(files, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(files, "Path", PureWindowsPath)
    monkeypatch.setattr(files, "_Win32", lambda: kernel)
    data = b"HSC1" + bytes(40)
    if bad_root:
        with pytest.raises(OSError):
            files.write_new_managed_media(data, media_root=r"C:\media", filename="a"*32 + ".wav.enc")
        assert not written
    else:
        files.write_new_managed_media(data, media_root=r"C:\media", filename="a"*32 + ".wav.enc")
        assert written == [data]
    assert kernel.live == []


def test_windows_offline_adapter_uses_audio_stream_and_wraps_real_pcm_format(monkeypatch):
    import base64
    import io
    import wave
    from types import SimpleNamespace
    import home_study_agent.pet_local_audio as local
    def run(arguments, **kwargs):
        script = base64.b64decode(arguments[-1]).decode("utf-16-le")
        assert "SetOutputToAudioStream($stream, $format)" in script
        assert "SetOutputToWaveStream" not in script and "Microsoft David Desktop" in script
        assert kwargs["input"] == b"Public original practice."
        assert "Public original practice." not in script
        return SimpleNamespace(stdout=base64.b64encode(bytes(32000)))
    monkeypatch.setattr(local, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(local.subprocess, "run", run)
    content, engine = local.render_original_practice("Public original practice.")
    with wave.open(io.BytesIO(content), "rb") as audio:
        assert (audio.getnframes(), audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) == (16000, 16000, 1, 2)
    assert engine == "windows_installed_system_speech_v1"
