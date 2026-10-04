#!/usr/bin/env python3
"""Copy v1 user data before Studio starts; never modify the legacy source."""

import argparse
from contextlib import closing
import fcntl
import filecmp
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile


STATE_PATHS = (
    "auth", "studio.db", "rag", "runs", "exports", "outputs", "assets/datasets",
    "share/studio_install_id", "accounts", "library", "chat-originals", "images",
    "videos", "audio", "transcripts", "security", "mcp-oauth-tokens",
)
MARKER = ".storage-layout-v2"


class MigrationError(Exception):
    """Migration cannot safely proceed without operator intervention."""


def legacy_path(root: Path, relative: str) -> Path:
    path = Path(os.path.abspath(root / relative))
    if not path.is_relative_to(root):
        raise MigrationError(f"Legacy subdirectory escapes DATA_DIR: {relative}")
    return path


def resolve_source(path: Path, root: Path, home: Path, work: Path) -> Path:
    """Resolve old container-absolute links within the read-only host mount."""
    for _ in range(40):
        if not path.is_relative_to(root):
            raise MigrationError(f"Legacy link escapes DATA_DIR: {path}")
        cursor = root
        parts = path.relative_to(root).parts
        for index, part in enumerate(parts):
            cursor /= part
            if not cursor.is_symlink():
                continue
            target = Path(os.readlink(cursor))
            if target.is_absolute():
                for old, mapped in ((Path("/home/unsloth"), home),
                                    (Path("/workspace/work"), work)):
                    if target.is_relative_to(old):
                        target = mapped / target.relative_to(old)
                        break
            else:
                target = cursor.parent / target
            path = Path(os.path.abspath(target.joinpath(*parts[index + 1:])))
            break
        else:
            return path
    raise MigrationError(f"Legacy symlink loop: {path}")


def copy_source(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, target, symlinks=True)
    elif source.is_file():
        shutil.copy2(source, target)
    else:
        raise MigrationError(f"Unsupported legacy data: {source}")


def snapshot_databases(stage: Path) -> None:
    """Read copied WALs on writable staging storage, then create clean snapshots."""
    for database in stage.rglob("*.db"):
        if database.is_symlink():
            raise MigrationError(f"Database symlinks require manual migration: {database}")
        if database.stat().st_size == 0:
            continue
        snapshot = database.with_name(database.name + ".snapshot")
        try:
            with closing(sqlite3.connect(database)) as source, closing(sqlite3.connect(snapshot)) as dest:
                if source.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise MigrationError(f"SQLite integrity check failed: {database}")
                source.backup(dest)
                dest.execute("PRAGMA journal_mode=DELETE")
            shutil.copystat(database, snapshot)
            os.replace(snapshot, database)
            for suffix in ("-wal", "-shm", "-journal"):
                database.with_name(database.name + suffix).unlink(missing_ok=True)
        except sqlite3.Error as error:
            raise MigrationError(f"Cannot migrate database {database}: {error}") from error


def entries(tree: Path):
    for path in sorted(tree.iterdir()):
        yield path
        if path.is_dir() and not path.is_symlink():
            yield from entries(path)


def same_file(source: Path, target: Path) -> bool:
    if source.is_symlink():
        return target.is_symlink() and os.readlink(source) == os.readlink(target)
    if target.is_symlink():
        return False
    if source.is_dir():
        return target.is_dir()
    return target.is_file() and filecmp.cmp(source, target, shallow=False)


def check_destinations(trees: list[tuple[Path, Path]]) -> None:
    # Check every destination before changing either mount. Identical files from
    # an interrupted copy can be reused, but unrelated installations never merge.
    for source, destination in trees:
        for item in entries(source):
            target = destination / item.relative_to(source)
            if (target.exists() or target.is_symlink()) and not same_file(item, target):
                raise MigrationError(f"Destination conflicts with legacy data: {target}")


def install_tree(source: Path, destination: Path) -> None:
    for item in entries(source):
        target = destination / item.relative_to(source)
        if target.exists() or target.is_symlink():
            continue
        if item.is_dir() and not item.is_symlink():
            target.mkdir()
            shutil.copystat(item, target)
            continue
        # Atomic per-file publication allows a retry after an interrupted copy.
        descriptor, temporary = tempfile.mkstemp(prefix=".migration-", dir=target.parent)
        os.close(descriptor)
        try:
            if item.is_symlink():
                os.unlink(temporary)
                os.symlink(os.readlink(item), temporary)
            else:
                shutil.copy2(item, temporary)
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)


def migrate(root: Path, studio: Path, projects: Path) -> None:
    studio.mkdir(parents=True, exist_ok=True)
    with (studio / ".storage-migration.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        migrate_locked(root, studio, projects)


def migrate_locked(root: Path, studio: Path, projects: Path) -> None:
    marker = studio / MARKER
    if marker.exists():
        return
    home = legacy_path(root, os.environ.get("UNSLOTH_LEGACY_HOME_PATH", "home"))
    work = legacy_path(root, os.environ.get("UNSLOTH_LEGACY_WORK_PATH", "work"))
    home = resolve_source(home, root, home, work)
    release = resolve_source(home / "current", root, home, work)
    studio.mkdir(parents=True, exist_ok=True)
    projects.mkdir(parents=True, exist_ok=True)
    # Reclaim staging data left by a killed process, while holding the lock.
    for interrupted in studio.glob(".migration-v1-*"):
        if (not interrupted.is_symlink() and interrupted.is_dir()
                and (interrupted / ".migration-staging").is_file()):
            shutil.rmtree(interrupted)
    # Stage on durable storage. A failed attempt is restaged from untouched v1
    # sources on the next start; already published identical files are accepted.
    with tempfile.TemporaryDirectory(prefix=".migration-v1-", dir=studio) as temporary:
        stage = Path(temporary)
        (stage / ".migration-staging").touch()
        staged_studio, staged_projects = stage / "studio", stage / "projects"
        staged_studio.mkdir()
        staged_projects.mkdir()
        found = False
        for relative in STATE_PATHS:
            source = resolve_source(home / "studio-state" / relative, root, home, work)
            if not source.exists():
                source = resolve_source(release / relative, root, home, work)
            if not source.exists():
                continue
            target = studio / relative
            if target.exists() and os.path.samefile(source, target):
                continue  # Already configured to use the old shared state directly.
            found = True
            copy_source(source, staged_studio / relative)
            if source.is_file() and source.suffix == ".db":
                for suffix in ("-wal", "-shm", "-journal"):
                    sidecar = source.with_name(source.name + suffix)
                    if sidecar.exists():
                        copy_source(sidecar, staged_studio / (relative + suffix))
        for old, new in (("Projects", ""), ("Accounts", "Accounts")):
            source = resolve_source(home / "Documents" / "Unsloth Studio" / old,
                                    root, home, work)
            if source.exists():
                found = True
                if new:
                    copy_source(source, staged_projects / new)
                else:
                    for child in source.iterdir():
                        copy_source(child, staged_projects / child.name)
        for name in ("token", "stored_tokens"):
            source = resolve_source(work / ".cache" / "huggingface" / name, root, home, work)
            if source.is_file():
                found = True
                copy_source(source, staged_studio / "huggingface" / name)
        snapshot_databases(staged_studio)
        trees = [(staged_studio, studio), (staged_projects, projects)]
        check_destinations(trees)
        for source, destination in trees:
            install_tree(source, destination)
        marker.write_text("2\n", encoding="utf-8")
        if found:
            print("Migrated storage layout v1 to v2; legacy files were left untouched.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-data", type=Path, required=True)
    parser.add_argument("--studio", type=Path, required=True)
    parser.add_argument("--projects", type=Path, required=True)
    args = parser.parse_args()
    try:
        migrate(args.legacy_data.absolute(), args.studio.absolute(), args.projects.absolute())
    except (MigrationError, OSError) as error:
        print(f"ERROR: storage migration failed: {error}. Studio was not started. "
              "Resolve the reported paths and restart, or follow the manual recovery "
              "instructions in docs/storage-layout-v2-migration.md.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
