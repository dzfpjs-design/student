"""Bounded SQLite evidence for an explicitly owned disposable stability profile."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile


def probe(root, owner):
    root = Path(root).resolve()
    if root.parent != Path(tempfile.gettempdir()).resolve() or not root.name.startswith("hsc-runtime-stability-"):
        raise ValueError("not a stability temporary root")
    marker = json.loads((root / "owner.json").read_text(encoding="utf-8"))
    if marker != {"owner": owner, "synthetic_only": True}:
        raise ValueError("temporary ownership mismatch")
    database = root / "profile" / "family-data" / "stability.db"
    if database.resolve() != database:
        raise ValueError("database escaped temporary profile")
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=3)) as connection:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        counts = {name: connection.execute('SELECT COUNT(*) FROM "' + name + '"').fetchone()[0]
                  for name in ("daily_session", "homework_item", "review_task", "agent_turn", "capture_artifact", "model_call") if name in tables}
        children = [list(row) for row in connection.execute(
            "SELECT child_id,status,COUNT(*) FROM daily_session GROUP BY child_id,status ORDER BY child_id,status")]
        result = {"quick_check": connection.execute("PRAGMA quick_check").fetchone()[0],
                  "counts": counts, "sessions_by_child": children,
                  "database_bytes": database.stat().st_size,
                  "wal_bytes": Path(str(database) + "-wal").stat().st_size if Path(str(database) + "-wal").exists() else 0}
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--owner", required=True)
    args = parser.parse_args()
    print(json.dumps(probe(args.root, args.owner), separators=(",", ":")))
