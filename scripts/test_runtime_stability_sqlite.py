import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("stability_probe", Path(__file__).with_name("runtime-stability-sqlite.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ProbeTests(unittest.TestCase):
    def test_read_only_connections_close_even_when_a_query_fails(self):
        with tempfile.TemporaryDirectory(prefix="hsc-runtime-stability-") as directory:
            root = Path(directory).resolve()
            (root / "owner.json").write_text(json.dumps({"owner": "synthetic", "synthetic_only": True}))
            data = root / "profile" / "family-data"
            data.mkdir(parents=True)
            original = sqlite3.connect
            connection = original(data / "stability.db")
            connection.execute("CREATE TABLE daily_session(child_id TEXT,status TEXT)")
            connection.close()
            handles = []

            def tracked(*args, **kwargs):
                connection = original(*args, **kwargs)
                handles.append(connection)
                return connection

            with patch.object(module.sqlite3, "connect", tracked):
                self.assertEqual(module.probe(root, "synthetic")["quick_check"], "ok")
                connection = original(data / "stability.db")
                connection.execute("DROP TABLE daily_session")
                connection.close()
                with self.assertRaises(sqlite3.OperationalError):
                    module.probe(root, "synthetic")
            for handle in handles:
                with self.assertRaises(sqlite3.ProgrammingError):
                    handle.execute("SELECT 1")
            with self.assertRaises(ValueError):
                module.probe(root, "wrong-owner")

    def test_arbitrary_profile_is_rejected_before_connect(self):
        with tempfile.TemporaryDirectory(prefix="unrelated-") as directory:
            with patch.object(module.sqlite3, "connect") as connection:
                with self.assertRaises(ValueError):
                    module.probe(directory, "synthetic")
                connection.assert_not_called()


if __name__ == "__main__":
    unittest.main()
