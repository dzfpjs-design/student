"""Pure publisher authoring: explicit closed base fingerprints, no profile reads."""
import argparse
import hashlib
import json
from pathlib import Path

from offline_update import MAIN_NAME, PRODUCT, SHA, atomic_bytes, exact, fingerprint, json_bytes, load_manifest, require, strict_json

BASE = {
    "app_asar_sha256": "d24c1cefcf9c54dd20a95fd5e8cfc52d67c153e5796346451a1abae93703a1af",
    "agent_sha256": "884cc858a9fb5cf7971d08b35ecb0b355443b655d4fff16b995c3a64c9832bfd",
    "release_info_sha256": None,
}
ELECTRON = {
    "filename": MAIN_NAME, "bytes": 244441088,
    "sha256": "879984b7817d9e602194a2b521f2f15425764a986b1f46f62dcc5e2e23a00d29",
}

def base_fingerprint(value):
    row = exact(value, {"app_asar_sha256", "agent_sha256", "release_info_sha256"})
    for key in ("app_asar_sha256", "agent_sha256"):
        require(type(row[key]) is str and SHA.fullmatch(row[key]) is not None)
    require(row["release_info_sha256"] is None or (
        type(row["release_info_sha256"]) is str
        and SHA.fullmatch(row["release_info_sha256"]) is not None))
    return dict(row)


def supported_bases(base_path=None, initial=None):
    rows = [base_fingerprint(x) for x in ([BASE] if initial is None else initial)]
    require(1 <= len(rows) <= 16)
    if base_path is not None:
        row = base_fingerprint(strict_json(Path(base_path).absolute()))
        if row not in rows:
            rows.append(row)
    require(len(rows) <= 16)
    require(len({tuple(row[k] for k in sorted(row)) for row in rows}) == len(rows))
    return rows


def bind_fresh_payload_manifest(manifest_path, base_path=None):
    """Publisher fresh output only; callers never pass their frozen input file."""
    manifest_path = Path(manifest_path)
    value = load_manifest(manifest_path)
    if base_path is not None:
        value["supported_bases"] = supported_bases(base_path, value["supported_bases"])
        atomic_bytes(manifest_path, json_bytes(value))
        value = load_manifest(manifest_path)
    return value


def main():
    parser = argparse.ArgumentParser(description="Author an r2-based offline payload manifest")
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--asar", type=Path, required=True)
    parser.add_argument("--agent", type=Path, required=True)
    parser.add_argument("--expected-asar-sha256", required=True)
    parser.add_argument("--expected-agent-sha256", required=True)
    parser.add_argument("--base-fingerprint", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = {}
    for key, name, path, expected in (
        ("app_asar", "app.asar", args.asar, args.expected_asar_sha256),
        ("agent", "home-study-local-agent.exe", args.agent, args.expected_agent_sha256),
    ):
        if not SHA.fullmatch(expected):
            raise SystemExit("EXPECTED_FROZEN_SHA_REQUIRED")
        row = fingerprint(path.absolute())
        if row["sha256"] != expected:
            raise SystemExit("FROZEN_CODE_SHA_MISMATCH")
        rows[key] = {"filename": name, **row}
    value = {
        "schema": 1, "product": PRODUCT, "release_id": args.release_id,
        "data_schema_change": "none", "electron": ELECTRON,
        "files": rows, "supported_bases": supported_bases(args.base_fingerprint),
    }
    # Exclusive creation: no mutation of a previously frozen publisher manifest.
    with args.output.open("xb") as out:
        out.write((json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"))
    load_manifest(args.output.absolute())
    print(json.dumps({"status": "AUTHORED_NOT_PACKAGED",
                      "manifest_sha256": hashlib.sha256(args.output.read_bytes()).hexdigest()}))


if __name__ == "__main__":
    main()
