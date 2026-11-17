import json
from pathlib import Path
import unittest
from unittest.mock import patch

from recovery_fixture import Fixture, Rejected, expected, manifest, V1, V2


class RecoveryFixtureTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)

    def test_backup_restore_and_prelaunch_rollback_preserve_data(self):
        before_data = manifest(self.f.data)
        before_registry = manifest(self.f.registry)
        self.f.upgrade()
        self.assertEqual(manifest(self.f.live), expected(V2))
        self.f.rollback_before_start()
        self.assertEqual(manifest(self.f.live), expected(V1))
        self.assertEqual(manifest(self.f.data), before_data)
        self.assertEqual(manifest(self.f.registry), before_registry)
        self.assertEqual(manifest(self.f.restored / 'data'), before_data)

    def test_all_rename_boundaries_recover_old_files_before_launch(self):
        for point in ('MOVE_OLD_INTENT_BEFORE', 'MOVE_OLD_INTENT_AFTER',
                      'MOVE_NEW_INTENT_BEFORE', 'MOVE_NEW_INTENT_AFTER'):
            with self.subTest(point=point):
                f = Fixture()
                try:
                    with self.assertRaisesRegex(Rejected, 'INJECTED_STOP'):
                        f.upgrade(fail_at=point)
                    f.rollback_before_start()
                    self.assertEqual(manifest(f.live), expected(V1))
                    self.assertEqual(manifest(f.data), manifest(f.backup / 'data'))
                finally:
                    f.close()

    def test_no_database_rollback_after_start_even_after_reinitialization(self):
        self.f.upgrade()
        self.f.mark_started()
        self.f.launched = False  # simulated process state loss; durable journal remains
        with self.assertRaisesRegex(Rejected, 'DATABASE_MAY_HAVE_CHANGED'):
            self.f.rollback_before_start()
        self.assertEqual(manifest(self.f.live), expected(V2))

    def test_unknown_version_or_incomplete_manifest_rejected(self):
        (self.f.live / 'app.bin').write_bytes(b'unknown')
        with self.assertRaisesRegex(Rejected, 'OLD_VERSION_OR_MANIFEST_MISMATCH'):
            self.f.upgrade()
        self.assertFalse(self.f.journal.exists())

    def test_candidate_manifest_mismatch_fails_closed(self):
        (self.f.source / 'unexpected').write_bytes(b'not allowed')
        with self.assertRaisesRegex(Rejected, 'NEW_VERSION_OR_MANIFEST_MISMATCH'):
            self.f.upgrade()
        self.assertEqual(manifest(self.f.live), expected(V1))

    def test_data_changed_before_rollback_fails_closed(self):
        self.f.upgrade()
        (self.f.data / 'new-record').write_text('synthetic newer state')
        with self.assertRaisesRegex(Rejected, 'DATA_CHANGED_SINCE_BACKUP'):
            self.f.rollback_before_start()
        self.assertEqual(manifest(self.f.live), expected(V2))

    def test_inconsistent_journal_and_tampered_stage_fail_closed(self):
        self.f.upgrade()
        self.f.write_journal('MOVE_OLD_INTENT')
        with self.assertRaisesRegex(Rejected, 'AMBIGUOUS_STATE'):
            self.f.rollback_before_start()
        self.assertEqual(manifest(self.f.live), expected(V2))

    def test_unknown_journal_phase_fails_closed(self):
        self.f.upgrade()
        self.f.journal.write_text(json.dumps({'phase': 'unknown'}))
        with self.assertRaisesRegex(Rejected, 'UNKNOWN_JOURNAL_PHASE'):
            self.f.rollback_before_start()
        self.assertEqual(manifest(self.f.live), expected(V2))

    def test_symlink_and_live_wal_rejected(self):
        (self.f.data / 'untrusted-link').symlink_to(self.f.root / 'missing')
        with self.assertRaisesRegex(Rejected, 'UNSUPPORTED_FILE'):
            self.f.upgrade()
        other = Fixture()
        try:
            (other.data / 'sample.db-wal').write_bytes(b'synthetic wal')
            with self.assertRaisesRegex(Rejected, 'LIVE_SQLITE_NOT_SUPPORTED'):
                other.upgrade()
        finally:
            other.close()

    def test_backup_corruption_prevents_upgrade(self):
        import recovery_fixture
        original = recovery_fixture.shutil.copytree
        def copy_and_corrupt(src, dst, *args, **kwargs):
            result = original(src, dst, *args, **kwargs)
            if Path(dst) == self.f.backup / 'registry':
                (Path(dst) / 'registry.json').write_text('corrupt')
            return result
        with patch.object(recovery_fixture.shutil, 'copytree', side_effect=copy_and_corrupt):
            with self.assertRaisesRegex(Rejected, 'BACKUP_MISMATCH'):
                self.f.upgrade()
        self.assertEqual(manifest(self.f.live), expected(V1))
        self.assertFalse(self.f.journal.exists())


if __name__ == '__main__':
    unittest.main()
