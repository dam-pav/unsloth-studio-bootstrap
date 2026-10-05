"""Policy isolation, generated URI cases, transactions, and process boundaries."""

import concurrent.futures
import importlib.util
import itertools
import os
import sqlite3
import subprocess  # nosec B404 - Process isolation is the behavior under test.
import sys
import tempfile
import unittest
from pathlib import Path

POLICY_DIRECTORY = Path(__file__).resolve().parents[1] / "scripts" / "sqlite-policy"


class SQLitePolicyTests(unittest.TestCase):
    def setUp(self):
        self.original_connect = sqlite3.connect
        self.original_dbapi_connect = sqlite3.dbapi2.connect
        spec = importlib.util.spec_from_file_location("policy_under_test", POLICY_DIRECTORY / "unsloth_sqlite_policy.py")
        self.policy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.policy)
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name) / "state"
        self.root.mkdir()

    def tearDown(self):
        sqlite3.connect = self.original_connect
        sqlite3.dbapi2.connect = self.original_dbapi_connect
        self.folder.cleanup()

    def select(self, mode):
        self.policy.install(mode, (self.root,))
        self.policy.require_active(mode)

    def test_default_preserves_connect_identity(self):
        self.select("wal")
        self.assertIs(sqlite3.connect, self.original_connect)
        self.assertIs(sqlite3.dbapi2.connect, self.original_dbapi_connect)
        with sqlite3.connect(self.root / "normal.db") as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
        conn.close()

    def test_journal_name_and_case_select_sqlite_rollback_journal(self):
        self.select("ROLLBACK-JOURNAL")
        connector = sqlite3.connect
        self.policy.install("rollback-journal", (self.root,))
        self.assertIs(sqlite3.connect, connector)
        self.policy.require_active("rollback-journal")
        conn = sqlite3.connect(self.root / "named.db")
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        self.assertEqual(conn.execute("PRAGMA synchronous").fetchone()[0], 2)
        conn.close()
        for obsolete in ("delete", "journal"):
            with self.assertRaises(ValueError):
                self.policy.install(obsolete, (self.root,))

    def test_unrelated_and_memory_databases_keep_normal_behavior(self):
        self.select("rollback-journal")
        for database, options in (
            (Path(self.folder.name) / "outside.db", {}),
            (":memory:", {}),
            ("file:memory?mode=memory&cache=shared", {"uri": True}),
            (str(Path(self.folder.name) / "state-sibling.db"), {}),
        ):
            conn = sqlite3.connect(database, **options)
            self.assertIs(type(conn), sqlite3.Connection)
            conn.execute("PRAGMA synchronous=OFF")
            self.assertEqual(conn.execute("PRAGMA synchronous").fetchone()[0], 0)
            conn.close()

    def test_generated_filenames_are_same_database_through_uris(self):
        self.select("rollback-journal")
        # Roundtrip property: URI encoding must not select a different file or escape scope.
        for characters in itertools.product(("x", " ", "#", "?", "%", "é"), repeat=2):
            database = self.root / ("".join(characters) + ".db")
            conn = sqlite3.connect(database)
            conn.execute("CREATE TABLE value (text TEXT)")
            conn.execute("INSERT INTO value VALUES (?)", (database.name,))
            conn.commit()
            conn.close()
            conn = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
            self.assertEqual(conn.execute("SELECT text FROM value").fetchone()[0], database.name)
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "delete")
            conn.close()
        self.assertEqual(len(list(self.root.iterdir())), 36)

    def test_symlinked_release_path_is_in_scope_and_escape_is_not(self):
        self.select("rollback-journal")
        release = Path(self.folder.name) / "release"
        release.symlink_to(self.root, target_is_directory=True)
        outside = Path(self.folder.name) / "outside.db"
        escape = self.root / "escape.db"
        escape.symlink_to(outside)
        with sqlite3.connect(release / "auth.db") as conn:
            self.assertNotEqual(type(conn), sqlite3.Connection)
        conn.close()
        conn = sqlite3.connect(escape)
        self.assertIs(type(conn), sqlite3.Connection)
        conn.close()

    def test_delete_policy_uses_sqlite_authorization_for_all_statement_paths(self):
        self.select("rollback-journal")
        conn = sqlite3.connect(self.root / "app.db")
        for name, value in itertools.product(("journal_mode", '"journal_mode"', "main.journal_mode"), ("WAL", "'wal'", "MEMORY", "OFF")):
            for spacing in (" ", "\n\t"):
                sql = f"pragma{spacing}{name}{spacing}={spacing}{value};"
                # Fixed cases deliberately vary SQL syntax; PRAGMAs cannot bind parameters.
                conn.execute(sql)  # nosec B608; nosemgrep
                conn.cursor().execute(sql)  # nosec B608; nosemgrep
                conn.executescript(sql)
                self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        for value in ("OFF", "NORMAL", "0", "1"):
            # Only the fixed synchronization values above are interpolated.
            conn.executescript(f"PRAGMA synchronous={value};")  # nosec B608; nosemgrep
            self.assertEqual(conn.execute("PRAGMA synchronous").fetchone()[0], 2)
        self.assertEqual(conn.execute("SELECT 'PRAGMA journal_mode=WAL'").fetchone()[0], "PRAGMA journal_mode=WAL")
        conn.close()

    def test_application_authorizer_is_preserved_and_reset_keeps_policy(self):
        self.select("rollback-journal")
        conn = sqlite3.connect(self.root / "app.db")
        conn.execute("CREATE TABLE data (id INTEGER)")
        conn.set_authorizer(lambda action, *_: sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_INSERT else sqlite3.SQLITE_OK)
        with self.assertRaises(sqlite3.DatabaseError):
            conn.execute("INSERT INTO data VALUES (1)")
        conn.set_authorizer(None)
        conn.execute("INSERT INTO data VALUES (1)")
        conn.commit()
        conn.execute("PRAGMA journal_mode=WAL")
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        conn.close()

    def test_connection_options_and_class_factory_are_preserved(self):
        self.select("rollback-journal")
        class Factory(sqlite3.Connection):
            marker = "custom"
        conn = sqlite3.connect(self.root / "options.db", 0.7, 0, None, False, Factory, 32, False)
        self.assertIsInstance(conn, Factory)
        self.assertEqual(conn.marker, "custom")
        self.assertEqual(conn.isolation_level, None)
        self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 700)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            self.assertEqual(pool.submit(lambda: conn.execute("SELECT 1").fetchone()[0]).result(), 1)
        conn.close()

    def test_concurrent_connections_commit_without_losing_transactions(self):
        self.assert_concurrent_connections("rollback-journal")

    def test_exclusive_supports_concurrent_connections_in_one_process(self):
        self.assert_concurrent_connections("wal-exclusive")

    def assert_concurrent_connections(self, mode):
        self.select(mode)
        database = self.root / "threads.db"
        conn = sqlite3.connect(database)
        conn.execute("CREATE TABLE writes (worker INTEGER, value INTEGER, PRIMARY KEY(worker,value))")
        conn.close()
        def worker(number):
            conn = sqlite3.connect(database, timeout=30)
            try:
                for value in range(15):
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute("INSERT INTO writes VALUES (?,?)", (number, value))
                    conn.commit()
                    conn.execute("SELECT count(*) FROM writes").fetchone()
            finally:
                conn.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(worker, range(4)))
        conn = sqlite3.connect(database)
        self.assertEqual(conn.execute("SELECT count(*) FROM writes").fetchone()[0], 60)
        self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        conn.close()

    def test_exclusive_uri_preserves_filename_and_read_only_option(self):
        self.select("wal-exclusive")
        database = self.root / "quoted #?%.db"
        conn = sqlite3.connect(database)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE data (id INTEGER)")
        conn.commit()
        self.assertFalse(Path(str(database) + "-shm").exists())
        conn.close()
        conn = sqlite3.connect(database.as_uri() + "?mode=ro&vfs=unix", uri=True)
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("INSERT INTO data VALUES (1)")
        conn.close()

    def test_exclusive_uri_preserves_raw_sqlite_option_encoding(self):
        for value in ("x+y", "x%20y", "x%2By", "", "%23%3F", "é"):
            uri = (self.root / "file #?.db").as_uri() + f"?mode=ro&opaque={value}&v%66s=unix"
            selected = self.policy._exclusive_uri(uri, True)
            self.assertIn(f"mode=ro&opaque={value}&vfs=unix-excl", selected)
            self.assertEqual(self.policy._database_path(selected, True), self.root / "file #?.db")

    def test_exclusive_blocks_another_process_until_connections_close(self):
        self.select("wal-exclusive")
        database = self.root / "process.db"
        conn = sqlite3.connect(database)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE data (id INTEGER)")
        conn.commit()
        code = "import sqlite3,sys; c=sqlite3.connect(sys.argv[1],timeout=0.1); c.execute('SELECT count(*) FROM data').fetchone(); c.close()"
        # Controlled executable, fixture paths and code; separate argv entries, never a shell.
        result = subprocess.run([sys.executable, "-c", code, str(database)], shell=False, capture_output=True, text=True, timeout=10)  # nosec B603; nosemgrep
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("locked", result.stderr)
        conn.close()
        # Controlled executable, fixture paths and code; separate argv entries, never a shell.
        result = subprocess.run([sys.executable, "-c", code, str(database)], shell=False, capture_output=True, text=True, timeout=10)  # nosec B603; nosemgrep
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_delete_allows_concurrent_writes_from_separate_processes(self):
        self.select("rollback-journal")
        database = self.root / "process.db"
        conn = sqlite3.connect(database)
        conn.execute("CREATE TABLE data (worker INTEGER, value INTEGER, PRIMARY KEY(worker,value))")
        conn.commit()
        code = """import sys,sqlite3
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from unsloth_sqlite_policy import install
install('rollback-journal',(Path(sys.argv[2]),))
conn=sqlite3.connect(sys.argv[3],timeout=5)
for value in range(15):
    conn.execute('INSERT INTO data VALUES (?,?)',(int(sys.argv[4]),value))
    conn.commit()
    conn.execute('SELECT count(*) FROM data').fetchone()
conn.close()
"""
        # Controlled executable, fixture paths and code; separate argv entries, never a shell.
        processes = [subprocess.Popen([sys.executable, "-c", code, str(POLICY_DIRECTORY), str(self.root), str(database), str(worker)], shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for worker in range(3)]  # nosec B603; nosemgrep
        try:
            for process in processes:
                _, error = process.communicate(timeout=20)
                self.assertEqual(process.returncode, 0, error)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
        self.assertEqual(conn.execute("SELECT count(*) FROM data").fetchone()[0], 45)
        conn.close()

    def test_invalid_and_unloaded_modes_fail(self):
        with self.assertRaises(ValueError):
            self.select("typo")
        with self.assertRaises(RuntimeError):
            self.policy.require_active("rollback-journal")
        self.select("rollback-journal")
        with self.assertRaises(RuntimeError):
            self.select("wal")


class SQLiteLauncherTests(unittest.TestCase):
    def test_alternative_launcher_loads_policy_and_preserves_arguments(self):
        with tempfile.TemporaryDirectory() as folder:
            cli = Path(folder) / "cli.py"
            cli.write_text("import sys\nfrom unsloth_sqlite_policy import require_active\nrequire_active('rollback-journal')\nassert sys.argv[1:] == ['studio', '-p', '8000']\nprint('policy active')\n")
            env = dict(os.environ, UNSLOTH_SQLITE_MODE="rollback-journal", PYTHONPATH=str(POLICY_DIRECTORY))
            # Controlled executable, fixture paths and code; separate argv entries, never a shell.
            result = subprocess.run([sys.executable, str(POLICY_DIRECTORY / "launch.py"), str(cli), "studio", "-p", "8000"], env=env, shell=False, capture_output=True, text=True, timeout=15)  # nosec B603; nosemgrep
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("policy active", result.stdout)

    def test_missing_startup_hook_refuses_to_run_cli(self):
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / "launched"
            cli = Path(folder) / "cli.py"
            cli.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
            env = dict(os.environ, UNSLOTH_SQLITE_MODE="rollback-journal")
            env.pop("PYTHONPATH", None)
            # Controlled executable, fixture paths and code; separate argv entries, never a shell.
            result = subprocess.run([sys.executable, "-S", str(POLICY_DIRECTORY / "launch.py"), str(cli)], env=env, shell=False, capture_output=True, text=True, timeout=15)  # nosec B603; nosemgrep
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("policy did not load", result.stderr)
            self.assertFalse(marker.exists())

    def test_invalid_mode_fails_before_bootstrap_side_effects(self):
        for mode in ("typo", "delete", "journal"):
            with self.subTest(mode=mode):
                # Controlled executable, fixture paths and code; separate argv entries, never a shell.
                result = subprocess.run(["bash", str(POLICY_DIRECTORY.parent / "bootstrap.sh")], env=dict(os.environ, UNSLOTH_SQLITE_MODE=mode), shell=False, capture_output=True, text=True, timeout=15)  # nosec B603; nosemgrep
                self.assertEqual(result.returncode, 2)
                self.assertIn("UNSLOTH_SQLITE_MODE must be", result.stderr)
                self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
