import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock
from types import SimpleNamespace
import sys

import numpy as np
import pytest

from home_study_agent.local_media_ai import ASR_MODEL_NAME, LocalCapabilityNotReadyError, LocalMediaAI


SAMPLES = np.zeros(1600, dtype=np.float32)


class DeferredNative:
    def __init__(self, phase):
        self.phase = phase
        self.entered = Event()
        self.release = Event()
        self.mutex = Lock()
        self.phase_entries = 0
        self.factory_calls = 0
        self.failure = None
        self.text = '公开合成识别'

    def visit(self, phase):
        if phase == self.phase:
            with self.mutex:
                self.phase_entries += 1
            self.entered.set()
            if not self.release.wait(3):
                raise AssertionError('synthetic native phase was not released')
        if phase == self.failure:
            raise RuntimeError('synthetic native phase failure')

    def from_transducer(self, **_kwargs):
        with self.mutex:
            self.factory_calls += 1
        self.visit('load')
        return self

    def create_stream(self):
        self.visit('create')
        native = self

        class Stream:
            def accept_waveform(self, rate, samples):
                assert rate == 16000 and samples is SAMPLES
                native.visit('accept')

            @property
            def result(self):
                native.visit('result')
                return SimpleNamespace(text=native.text)

        return Stream()

    def decode_stream(self, _stream):
        self.visit('decode')


def media(tmp_path, monkeypatch, native):
    model = tmp_path / ASR_MODEL_NAME
    for path in LocalMediaAI._asr_files(model).values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'public synthetic non-model fixture')
    monkeypatch.setitem(sys.modules, 'sherpa_onnx', SimpleNamespace(OfflineRecognizer=native))
    return LocalMediaAI(None, tmp_path, tmp_path)


@pytest.mark.parametrize('phase', ['load', 'create', 'accept', 'decode', 'result'])
def test_competing_real_thread_is_busy_until_the_entire_native_phase_finishes(tmp_path, monkeypatch, phase):
    native = DeferredNative(phase)
    instance = media(tmp_path, monkeypatch, native)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(instance._recognize_locally, SAMPLES, 16000)
        try:
            assert native.entered.wait(1)
            second = pool.submit(instance._recognize_locally, SAMPLES, 16000)
            with pytest.raises(LocalCapabilityNotReadyError) as caught:
                second.result(timeout=0.3)
            assert caught.value.code == 'LOCAL_ASR_BUSY'
            assert not native.release.is_set() and not first.done()
            assert native.phase_entries == 1
        finally:
            native.release.set()
        assert first.result(timeout=1) == '公开合成识别'
    assert instance._recognize_locally(SAMPLES, 16000) == '公开合成识别'
    assert native.factory_calls == 1


def test_async_http_wait_timeout_does_not_release_a_running_native_decode(tmp_path, monkeypatch):
    native = DeferredNative('decode')
    instance = media(tmp_path, monkeypatch, native)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(instance._recognize_locally, SAMPLES, 16000)
        try:
            assert native.entered.wait(1)

            async def request_wait():
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(asyncio.wrap_future(first), timeout=0.01)

            asyncio.run(request_wait())
            assert first.running() and not first.done() and not native.release.is_set()
            with pytest.raises(LocalCapabilityNotReadyError) as caught:
                pool.submit(instance._recognize_locally, SAMPLES, 16000).result(timeout=0.3)
            assert caught.value.code == 'LOCAL_ASR_BUSY' and native.phase_entries == 1
        finally:
            native.release.set()
        assert first.result(timeout=1) == '公开合成识别'
    assert instance._recognize_locally(SAMPLES, 16000) == '公开合成识别'


def test_native_exception_releases_the_slot_for_the_next_real_decode(tmp_path, monkeypatch):
    native = DeferredNative(None)
    native.failure = 'decode'
    instance = media(tmp_path, monkeypatch, native)
    with pytest.raises(RuntimeError, match='synthetic native phase failure'):
        instance._recognize_locally(SAMPLES, 16000)
    native.failure = None
    assert instance._recognize_locally(SAMPLES, 16000) == '公开合成识别'


def test_early_model_and_empty_speech_errors_do_not_leave_a_busy_slot(tmp_path, monkeypatch):
    native = DeferredNative(None)
    monkeypatch.setitem(sys.modules, 'sherpa_onnx', SimpleNamespace(OfflineRecognizer=native))
    instance = LocalMediaAI(None, tmp_path, tmp_path)
    with pytest.raises(LocalCapabilityNotReadyError) as caught:
        instance._recognize_locally(SAMPLES, 16000)
    assert caught.value.code == 'ASR_MODEL_NOT_READY'
    for path in LocalMediaAI._asr_files(tmp_path / ASR_MODEL_NAME).values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'public synthetic non-model fixture')
    native.text = ''
    with pytest.raises(LocalCapabilityNotReadyError) as caught:
        instance._recognize_locally(SAMPLES, 16000)
    assert caught.value.code == 'ASR_NO_SPEECH'
    native.text = '公开合成识别'
    assert instance._recognize_locally(SAMPLES, 16000) == '公开合成识别'
