"""New publisher/base and exact builder-check controls, no compiler/product IO."""
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import create_payload_manifest as P
import offline_update as U


class PublisherBaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="synthetic-publisher-base-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.asar = self.root / "app.asar"
        self.agent = self.root / "home-study-local-agent.exe"
        self.asar.write_bytes(b"PUBLIC_SYNTHETIC_NEW_ASAR")
        self.agent.write_bytes(b"PUBLIC_SYNTHETIC_NEW_AGENT")
        self.base = {
            "app_asar_sha256": "1" * 64, "agent_sha256": "2" * 64,
            "release_info_sha256": "3" * 64,
        }
        self.base_path = self.root / "explicit-base.json"
        self.base_path.write_text(json.dumps(self.base))

    def author(self, output, base_path=None, expected=None):
        args = [
            "create_payload_manifest.py", "--release-id", "synthetic-r4",
            "--asar", str(self.asar), "--agent", str(self.agent),
            "--expected-asar-sha256", expected or U.fingerprint(self.asar)["sha256"],
            "--expected-agent-sha256", U.fingerprint(self.agent)["sha256"],
            "--output", str(output),
        ]
        if base_path is not None:
            args += ["--base-fingerprint", str(base_path)]
        with patch.object(sys, "argv", args), redirect_stdout(io.StringIO()):
            P.main()

    def test_default_known_r2_ignores_profile_environment(self):
        output = self.root / "default.json"
        with patch.dict(os.environ, {"APPDATA": "MUST_NOT_READ", "LOCALAPPDATA": "MUST_NOT_READ"}):
            self.author(output)
        self.assertEqual(U.load_manifest(output)["supported_bases"], [P.BASE])

    def test_explicit_sidecar_sha_or_none_adds_only_closed_supported_base(self):
        for sidecar in ("3" * 64, None):
            with self.subTest(sidecar=sidecar):
                self.base["release_info_sha256"] = sidecar
                original = json.dumps(self.base).encode()
                self.base_path.write_bytes(original)
                output = self.root / ("explicit-sha.json" if sidecar else "explicit-null.json")
                self.author(output, self.base_path)
                m = U.load_manifest(output)
                self.assertEqual(m["supported_bases"], [P.BASE, self.base])
                self.assertEqual(self.base_path.read_bytes(), original)
                self.assertEqual(self.asar.read_bytes(), b"PUBLIC_SYNTHETIC_NEW_ASAR")

    def test_unknown_missing_nonstring_uppercase_and_invalid_sha_reject(self):
        invalid = [
            {**self.base, "profile": "DO_NOT_READ"},
            {k: v for k, v in self.base.items() if k != "release_info_sha256"},
            {**self.base, "app_asar_sha256": None},
            {**self.base, "agent_sha256": True},
            {**self.base, "agent_sha256": "A" * 64},
            {**self.base, "release_info_sha256": False},
            {**self.base, "release_info_sha256": "0" * 63},
        ]
        for value in invalid:
            with self.subTest(value=value):
                self.base_path.write_text(json.dumps(value))
                with self.assertRaises(U.UpdateError):
                    P.supported_bases(self.base_path)

    def test_duplicate_json_key_and_alias_are_rejected(self):
        self.base_path.write_text('{"app_asar_sha256":"' + "1"*64 + '","app_asar_sha256":"' +
                                  "2"*64 + '","agent_sha256":"' + "2"*64 + '","release_info_sha256":null}')
        with self.assertRaises(U.UpdateError):
            P.supported_bases(self.base_path)
        self.base_path.write_text(json.dumps(self.base))
        alias = self.root / "base-link.json"
        alias.symlink_to(self.base_path)
        with self.assertRaisesRegex(U.UpdateError, "UNSAFE_PATH"):
            P.supported_bases(alias)

    def test_max16_and_exact_duplicate_deduplication(self):
        initial = [{"app_asar_sha256": f"{n:064x}", "agent_sha256": "a"*64,
                    "release_info_sha256": None} for n in range(16)]
        self.base_path.write_text(json.dumps(initial[4]))
        self.assertEqual(P.supported_bases(self.base_path, initial), initial)
        self.base_path.write_text(json.dumps(self.base))
        with self.assertRaises(U.UpdateError):
            P.supported_bases(self.base_path, initial)

    def test_existing_output_and_wrong_frozen_new_code_pin_are_not_overwritten(self):
        output = self.root / "frozen.json"
        output.write_bytes(b"FROZEN_AUTHOR_INPUT")
        with self.assertRaises(FileExistsError):
            self.author(output, self.base_path)
        self.assertEqual(output.read_bytes(), b"FROZEN_AUTHOR_INPUT")
        absent = self.root / "must-not-be-created.json"
        with self.assertRaisesRegex(SystemExit, "FROZEN_CODE_SHA_MISMATCH"):
            self.author(absent, self.base_path, "0"*64)
        self.assertFalse(absent.exists())

    def bridge(self, name, explicit, corrupt=False):
        source = Path(__file__).resolve().parents[1] / "scripts" / name
        text = source.read_text(encoding="utf-8-sig")
        # Execute the exact fixed Python code written by each PS script; do not
        # invoke PowerShell, compiler, helper, UI or backend product.
        code = text.split("$check=@'\n", 1)[1].split("\n'@", 1)[0]
        fresh = self.root / (name + ("-base" if explicit else "-default") + ("-bad" if corrupt else ""))
        fresh.mkdir()
        stage = fresh / "payload"
        stage.mkdir()
        original = fresh / "original-frozen-manifest.json"
        self.author(original)
        before = original.read_bytes()
        shutil.copyfile(original, stage / "manifest.json")
        shutil.copyfile(self.asar, stage / "app.asar")
        shutil.copyfile(self.agent, stage / "home-study-local-agent.exe")
        if corrupt:
            (stage / "app.asar").write_bytes(b"CORRUPT")
        check = fresh / "check-payload.py"
        check.write_text(code)
        tools = Path(__file__).resolve().parent
        completed = subprocess.run([
            sys.executable, str(check), str(tools / "offline_update.py"),
            str(stage), str(self.base_path) if explicit else "-",
        ], capture_output=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(original.read_bytes(), before)
        return completed, U.load_manifest(stage / "manifest.json"), before, stage

    def test_both_exact_builder_scripts_default_and_explicit_base_pass_through(self):
        for name in ("build-offline-update.ps1", "build-offline-update-iexpress.ps1"):
            for explicit in (False, True):
                with self.subTest(builder=name, explicit=explicit):
                    result, m, original, stage = self.bridge(name, explicit)
                    self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace")[-300:])
                    self.assertIn(b"CLOSED_PAYLOAD_VERIFIED", result.stdout)
                    self.assertEqual(m["supported_bases"], [P.BASE, self.base] if explicit else [P.BASE])
                    if not explicit:
                        self.assertEqual((stage / "manifest.json").read_bytes(), original)

    def test_both_exact_builder_scripts_reject_mismatched_payload_before_compile(self):
        for name in ("build-offline-update.ps1", "build-offline-update-iexpress.ps1"):
            with self.subTest(builder=name):
                result, _m, _original, _stage = self.bridge(name, True, corrupt=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b"PAYLOAD_SHA_MISMATCH", result.stderr)


if __name__ == "__main__":
    unittest.main()
