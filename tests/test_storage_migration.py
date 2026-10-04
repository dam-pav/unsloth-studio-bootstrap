"""Real filesystem and SQLite regression tests for the v1-to-v2 migration."""

import importlib.util
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    "migration", Path(__file__).resolve().parents[1] / "scripts/migrate-storage.py"
)
assert spec is not None and spec.loader is not None
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


class StorageMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.legacy = self.root / "legacy"
        self.home = self.legacy / "home"
        self.shared = self.home / "studio-state"
        self.release = self.home / "releases/1"
        self.studio = self.root / "studio"
        self.projects = self.root / "projects"
        self.shared.mkdir(parents=True)
        self.release.mkdir(parents=True)
        (self.home / "current").symlink_to("releases/1")
        self.environment = patch.dict(os.environ, {
            "UNSLOTH_LEGACY_HOME_PATH": "home",
            "UNSLOTH_LEGACY_WORK_PATH": "work",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def write(self, path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def database(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE data (value TEXT)")
        connection.execute("INSERT INTO data VALUES ('saved history')")
        connection.commit()
        return connection

    def run_migration(self):
        migration.migrate(self.legacy, self.studio, self.projects)

    def test_shared_state_absolute_links_assets_projects_and_tokens(self):
        self.database(self.shared / "studio.db").close()
        self.write(self.shared / "auth/password", "saved password")
        (self.release / "auth").symlink_to("/home/unsloth/studio-state/auth")
        self.write(self.release / "library/document", "uploaded document")
        self.write(self.home / "Documents/Unsloth Studio/Projects/demo/file", "project")
        self.write(self.home / "Documents/Unsloth Studio/Accounts/alice/Projects/file", "alice")
        self.write(self.legacy / "work/.cache/huggingface/token", "saved token")
        self.write(self.release / "cache/uv/download", "discardable")
        self.write(self.home / "llama.cpp/binary", "discardable")
        self.run_migration()
        self.assertEqual((self.studio / "auth/password").read_text(), "saved password")
        self.assertEqual((self.studio / "library/document").read_text(), "uploaded document")
        self.assertEqual((self.projects / "demo/file").read_text(), "project")
        self.assertEqual((self.projects / "Accounts/alice/Projects/file").read_text(), "alice")
        self.assertEqual((self.studio / "huggingface/token").read_text(), "saved token")
        self.assertFalse((self.studio / "cache").exists())
        self.assertFalse((self.studio / "llama.cpp").exists())
        self.assertTrue((self.shared / "studio.db").exists())
        self.assertTrue((self.release / "auth").is_symlink())

    def test_pre_shared_layout_and_absolute_current_link(self):
        (self.home / "current").unlink()
        (self.home / "current").symlink_to("/home/unsloth/releases/1")
        self.database(self.release / "studio.db").close()
        self.write(self.release / "auth/password", "original")
        self.run_migration()
        self.assertEqual((self.studio / "auth/password").read_text(), "original")
        with sqlite3.connect(self.studio / "studio.db") as db:
            self.assertEqual(db.execute("SELECT value FROM data").fetchone()[0], "saved history")

    def test_committed_wal_is_preserved_without_touching_original(self):
        path = self.shared / "studio.db"
        db = self.database(path)
        self.addCleanup(db.close)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA wal_autocheckpoint=0")
        db.execute("INSERT INTO data VALUES ('committed in WAL')")
        db.commit()
        originals = {p: p.read_bytes() for p in path.parent.glob("studio.db*")}
        self.run_migration()
        with sqlite3.connect(self.studio / "studio.db") as migrated:
            self.assertEqual(migrated.execute("SELECT COUNT(*) FROM data").fetchone()[0], 2)
            self.assertEqual(migrated.execute("PRAGMA quick_check").fetchone()[0], "ok")
        self.assertEqual({p: p.read_bytes() for p in originals}, originals)
        self.assertFalse((self.studio / "studio.db-wal").exists())

    def test_destination_conflict_is_detected_before_copying_any_data(self):
        self.write(self.shared / "auth/password", "old")
        self.write(self.shared / "library/document", "old asset")
        self.write(self.projects / "demo/file", "new project")
        self.write(self.home / "Documents/Unsloth Studio/Projects/demo/file", "old project")
        with self.assertRaises(migration.MigrationError):
            self.run_migration()
        self.assertFalse((self.studio / "auth").exists())
        self.assertEqual((self.projects / "demo/file").read_text(), "new project")
        self.assertFalse((self.studio / migration.MARKER).exists())

    def test_retry_after_partial_publication(self):
        self.database(self.shared / "studio.db").close()
        self.write(self.shared / "auth/password", "old")
        self.write(self.home / "Documents/Unsloth Studio/Projects/demo/file", "project")
        install = migration.install_tree
        calls = 0

        def interrupted(source, target):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated interruption")
            install(source, target)

        with patch.object(migration, "install_tree", side_effect=interrupted):
            with self.assertRaises(OSError):
                self.run_migration()
        self.assertFalse((self.studio / migration.MARKER).exists())
        self.run_migration()
        self.assertEqual((self.projects / "demo/file").read_text(), "project")
        self.assertTrue((self.studio / migration.MARKER).exists())

    def test_completed_migration_does_not_restore_stale_data(self):
        self.write(self.shared / "auth/password", "old")
        self.run_migration()
        (self.studio / "auth/password").write_text("changed password")
        self.run_migration()
        self.assertEqual((self.studio / "auth/password").read_text(), "changed password")

    def test_custom_legacy_subdirectories(self):
        self.home.rename(self.legacy / "custom-home")
        os.environ["UNSLOTH_LEGACY_HOME_PATH"] = "custom-home"
        os.environ["UNSLOTH_LEGACY_WORK_PATH"] = "custom-work"
        self.write(self.legacy / "custom-home/studio-state/auth/password", "custom")
        self.write(self.legacy / "custom-work/.cache/huggingface/token", "custom token")
        self.run_migration()
        self.assertEqual((self.studio / "auth/password").read_text(), "custom")
        self.assertEqual((self.studio / "huggingface/token").read_text(), "custom token")

    def test_bad_database_blocks_startup(self):
        self.write(self.shared / "studio.db", "corrupt")
        with self.assertRaises(migration.MigrationError):
            self.run_migration()
        self.assertFalse((self.studio / migration.MARKER).exists())

    def test_escaping_link_blocks_migration(self):
        (self.release / "auth").symlink_to("/etc")
        with self.assertRaises(migration.MigrationError):
            self.run_migration()

    def test_fresh_deployment_records_current_layout(self):
        self.run_migration()
        self.assertTrue((self.studio / migration.MARKER).exists())

    def test_manual_recovery_marker_skips_legacy_data(self):
        self.write(self.shared / "auth/password", "old password")
        self.write(self.studio / "auth/password", "manually recovered password")
        self.write(self.studio / migration.MARKER, "2\n")
        self.run_migration()
        self.assertEqual((self.studio / "auth/password").read_text(),
                         "manually recovered password")

    def test_killed_process_staging_is_reclaimed(self):
        abandoned = self.studio / ".migration-v1-abandoned"
        self.write(abandoned / ".migration-staging", "")
        self.write(abandoned / "studio/library/file", "partial copy")
        self.run_migration()
        self.assertFalse(abandoned.exists())

    def test_existing_shared_destination_is_not_copied_into_itself(self):
        self.database(self.shared / "studio.db").close()
        self.studio = self.shared
        self.run_migration()
        with sqlite3.connect(self.shared / "studio.db") as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM data").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
