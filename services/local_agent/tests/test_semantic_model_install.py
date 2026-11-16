"""Installer follows redirects, verifies pins and preserves old files on failure."""
import hashlib
import importlib.util
from pathlib import Path
import sys

import httpx
import pytest


@pytest.fixture
def installer(tmp_path,monkeypatch):
    path=Path(__file__).parents[1]/'scripts/download_semantic_model.py'
    spec=importlib.util.spec_from_file_location('test_model_installer',path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    data=b'synthetic-model-asset'
    monkeypatch.setattr(module,'FILES',{'tiny.onnx':(len(data),hashlib.sha256(data).hexdigest())})
    monkeypatch.setattr(sys,'argv',['download_semantic_model.py','--output',str(tmp_path),'--mirror'])
    return module,data,tmp_path/module.MODEL_DIR


def test_redirect_and_verified_reentry_do_not_duplicate_downloads(installer,monkeypatch):
    module,data,root=installer
    calls=[]
    def handle(request):
        calls.append(request.url.path)
        if request.url.path!='/asset':
            return httpx.Response(308,headers={'location':'/asset'})
        return httpx.Response(200,content=data)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(module.httpx,'stream',client.stream)
        module.main()
        assert (root/'tiny.onnx').read_bytes()==data
        module.main()
    assert len(calls)==2
    assert not list(root.glob('.download-*'))


def test_checksum_failure_retains_previous_file_and_cleans_staging(installer,monkeypatch):
    module,data,root=installer
    root.mkdir()
    previous=b'previous-asset'
    (root/'tiny.onnx').write_bytes(previous)
    with httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,content=b'x'*len(data)))) as client:
        monkeypatch.setattr(module.httpx,'stream',client.stream)
        with pytest.raises(RuntimeError,match='integrity'):
            module.main()
    assert (root/'tiny.onnx').read_bytes()==previous
    assert not list(root.glob('.download-*'))
