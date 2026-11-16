"""Spawn-only fixture used to verify formula-worker process recovery in tests."""
from __future__ import annotations

from time import sleep
from pathlib import Path
import os

from .formula_recognition import FormulaPrediction


class TestSupportFormulaProvider:
    name = "test-support-formula-worker"

    def __init__(self, mode: str, pid_file: str | None = None):
        self.mode = mode
        self.pid_file = pid_file

    def health(self) -> dict:
        return {"ready": True, "cpu_only": True, "implicit_network_download": False}

    def recognize(self, image_bytes: bytes) -> FormulaPrediction:
        if self.pid_file:
            Path(self.pid_file).write_text(str(os.getpid()), encoding="utf-8")
        if self.mode == "block":
            sleep(60)
        if self.mode == "crash":
            raise RuntimeError("test_support_crash")
        return FormulaPrediction(latex="x=2", latency_seconds=.001, provider=self.name)


def make_provider(values: dict) -> TestSupportFormulaProvider:
    return TestSupportFormulaProvider(str(values.get("mode", "success")), str(values["pid_file"]) if values.get("pid_file") else None)
