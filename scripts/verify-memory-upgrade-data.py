"""Read-only post-upgrade checks; emit counts, never family record contents."""
import argparse
import json
from pathlib import Path
import sqlite3


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", required=True)
    parser.add_argument("--backup", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    report = {"status": "passed", "read_only": True, "preserved_tables": {}}
    with sqlite3.connect(Path(args.live).resolve().as_uri() + "?mode=ro", uri=True) as live, \
            sqlite3.connect(Path(args.backup).resolve().as_uri() + "?mode=ro", uri=True) as prior:
        assert live.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        for table in ("child_profile", "agent_turn", "mistake_case", "diagnostic_attempt", "daily_session"):
            columns = prior.execute(f"PRAGMA table_info({table})").fetchall()
            assert columns, f"Missing source table: {table}"
            keys = [row[1] for row in sorted(columns, key=lambda item: item[5]) if row[5]]
            assert keys, f"Missing primary key: {table}"
            sql = f"SELECT {','.join(keys)} FROM {table}"
            before = set(prior.execute(sql).fetchall())
            after = set(live.execute(sql).fetchall())
            assert before <= after, f"Original records missing: {table}"
            report["preserved_tables"][table] = {"before": len(before), "after": len(after)}
        tables = {row[0] for row in live.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {"companion_memory", "companion_memory_source", "companion_memory_evidence", "companion_memory_audit", "companion_memory_checkpoint", "companion_memory_suppression"}
        assert required <= tables, "Memory schema not initialized"
        report["memory_schema_present"] = True
        report["integrity"] = "ok"
    Path(args.output).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("Upgrade data verification passed: original record IDs retained, memory schema ready")


if __name__ == "__main__":
    main()
