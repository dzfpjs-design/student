"""Explicitly acquire and inventory official Pix2Text MFR-1.5 assets.

This is the *only* networked acquisition path.  The runtime provider uses a
verified local directory and does not invoke this script or Hugging Face.
Never choose an output inside the git worktree: the model weights must not be
version controlled.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from home_study_agent.local_ocr.model_assets import build_model_manifest


REPO_ID = "breezedeus/pix2text-mfr-1.5"
LICENSE = "MIT"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True, help="Dedicated local model-cache directory; never a git worktree")
    parser.add_argument("--manifest-output", type=Path, required=True, help="JSON inventory with hashes; safe to commit, contains no weights")
    parser.add_argument("--revision", help="Optional immutable Hugging Face commit. Omit only during acquisition to resolve the current commit once.")
    parser.add_argument("--offline-existing", action="store_true", help="Do not contact a host; inventory an already complete cache at --output")
    args = parser.parse_args()
    output = args.output.expanduser().resolve()
    manifest_output = args.manifest_output.expanduser().resolve()
    _reject_worktree_output(output)
    if args.offline_existing:
        if not args.revision or not output.is_dir():
            raise SystemExit("--offline-existing requires --revision and a pre-existing model directory")
        revision = args.revision
    else:
        try:
            from huggingface_hub import HfApi, snapshot_download
        except ImportError as error:
            raise SystemExit("huggingface_hub_not_installed; install it only in the isolated model-acquisition environment") from error
        revision = args.revision or HfApi().model_info(REPO_ID).sha
        snapshot_download(repo_id=REPO_ID, revision=revision, local_dir=output)
    manifest = build_model_manifest(output, repo_id=REPO_ID, revision=revision, license_name=LICENSE)
    manifest_output.parent.mkdir(parents=True, exist_ok=True)
    manifest_output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "downloaded_and_verified", "repo_id": REPO_ID, "revision": revision, "file_count": manifest["file_count"], "total_bytes": manifest["total_bytes"], "manifest": str(manifest_output)}, ensure_ascii=False))


def _reject_worktree_output(output: Path) -> None:
    worktree = Path(__file__).parents[3].resolve()
    if output == worktree or worktree in output.parents:
        raise SystemExit("model_output_must_be_outside_git_worktree")


if __name__ == "__main__":
    main()
