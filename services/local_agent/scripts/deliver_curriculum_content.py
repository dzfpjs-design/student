"""Build/import an existing, text-only curriculum package; never extracts atoms."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from home_study_agent.knowledge_graph.content_bundle import export_content, import_content


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    export = sub.add_parser("export")
    export.add_argument("--source", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    load = sub.add_parser("import")
    load.add_argument("--bundle", type=Path, required=True)
    load.add_argument("--target", type=Path, required=True)
    load.add_argument("--sha256", required=True)
    args = parser.parse_args()
    if args.action == "export":
        result = export_content(args.source, args.output)
        digest = hashlib.sha256()
        with args.output.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""): digest.update(block)
        result["bundle_sha256"] = digest.hexdigest()
        result["bytes"] = args.output.stat().st_size
    else:
        result = import_content(args.bundle, args.target, expected_sha256=args.sha256)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
