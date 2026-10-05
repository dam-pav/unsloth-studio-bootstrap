#!/usr/bin/env python3
"""Compare SQLite journal modes on disposable fixtures; never open Studio databases."""

import argparse
import concurrent.futures
import contextlib
import sqlite3
import statistics
import tempfile
import threading
import time
from collections import defaultdict
from pathlib import Path


def probe(root: Path, mode: str, iterations: int, workers: int, vfs: str) -> bool:
    print(f"\nroot={root} journal={mode} workers={workers} vfs={vfs}", flush=True)
    samples = defaultdict(list)
    errors = []
    guard = threading.Lock()

    def measured(stage, action):
        started = time.perf_counter()
        try:
            return action()
        finally:
            elapsed = time.perf_counter() - started
            with guard:
                samples[stage].append(elapsed)
                if elapsed >= 1:
                    print(f"  slow: {stage} {elapsed:.3f}s", flush=True)

    with tempfile.TemporaryDirectory(prefix=".unsloth-sqlite-probe-", dir=root) as folder:
        directory = Path(folder)
        database = directory / "fixture.db"

        def connect():
            return sqlite3.connect(database.absolute().as_uri() + f"?vfs={vfs}", uri=True, timeout=5)

        print(f"fixture={database}", flush=True)
        with contextlib.closing(connect()) as conn:
            actual = measured(
                "initial journal", lambda: conn.execute(f"PRAGMA journal_mode={mode}").fetchone()[0]
            )
            if actual.lower() != mode.lower():
                print(f"Requested {mode}, got {actual}; skipping this mode.", flush=True)
                return False
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY, value INTEGER)")
            conn.execute("INSERT INTO probe VALUES (1, 0)")
            conn.commit()

        barrier = threading.Barrier(workers)

        def worker(number):
            barrier.wait()
            for _ in range(iterations):
                conn = None
                try:
                    # Mirror auth get_connection(), including its metadata operations.
                    measured("mkdir", lambda: directory.mkdir(parents=True, exist_ok=True))
                    conn = measured("connect", connect)
                    measured("chmod directory", lambda: directory.chmod(0o700))
                    measured("chmod database", lambda: database.chmod(0o600))
                    conn.execute("PRAGMA busy_timeout=5000")
                    conn.execute("PRAGMA synchronous=FULL")
                    measured(
                        "journal on connect",
                        lambda: conn.execute(f"PRAGMA journal_mode={mode}").fetchone(),
                    )
                    measured("stat", database.stat)
                    measured("schema", lambda: conn.execute("PRAGMA schema_version").fetchone())
                    measured("select", lambda: conn.execute("SELECT value FROM probe").fetchone())
                    if number == 0:
                        measured("update", lambda: conn.execute("UPDATE probe SET value=value+1"))
                        measured("commit", conn.commit)
                except (sqlite3.Error, OSError) as exc:
                    with guard:
                        errors.append(str(exc))
                        print(f"  worker {number}: {exc}", flush=True)
                finally:
                    if conn is not None:
                        measured("close", conn.close)

        started = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(worker, range(workers)))
        print(f"workload elapsed={time.perf_counter() - started:.3f}s errors={len(errors)}")
        for stage, values in samples.items():
            print(
                f"  {stage:20} count={len(values):3} "
                f"median={statistics.median(values):.4f}s max={max(values):.4f}s"
            )
        with contextlib.closing(connect()) as conn:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            writes = conn.execute("SELECT value FROM probe").fetchone()[0]
        print(f"integrity={integrity} committed_writes={writes}/{iterations}", flush=True)
        return not errors and integrity == "ok" and writes == iterations


def positive_integer(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directories", nargs="+", type=Path, help="existing writable directories")
    parser.add_argument("--iterations", type=positive_integer, default=6)
    parser.add_argument("--workers", type=positive_integer, default=4)
    parser.add_argument("--journal", choices=("WAL", "DELETE", "both"), default="both")
    parser.add_argument("--vfs", choices=("unix", "unix-excl"), default="unix")
    args = parser.parse_args()
    for directory in args.directories:
        if not directory.is_dir():
            parser.error(f"not an existing directory: {directory}")
    print(f"SQLite {sqlite3.sqlite_version}; synthetic data only; synchronous=FULL", flush=True)
    print("A successful run measures performance; it does not certify NFS crash safety.", flush=True)
    modes = ("WAL", "DELETE") if args.journal == "both" else (args.journal,)
    success = True
    for directory in args.directories:
        for mode in modes:
            try:
                success = probe(directory, mode, args.iterations, args.workers, args.vfs) and success
            except (sqlite3.Error, OSError) as exc:
                print(f"{directory} {mode}: {exc}", flush=True)
                success = False
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
