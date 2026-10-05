"""Opt-in SQLite policies for Studio state; upstream WAL mode installs no hook."""

import functools
import os
import sqlite3
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

MODES = ("wal", "rollback-journal", "wal-exclusive")
_active_mode = "wal"
_connector = None


def canonical_mode(mode):
    mode = mode.lower()
    if mode not in MODES:
        raise ValueError(f"UNSLOTH_SQLITE_MODE must be one of {', '.join(MODES)}")
    return mode


def _parameter(args, kwargs, name, position, default):
    return args[position] if len(args) > position else kwargs.get(name, default)


def _set_parameter(args, kwargs, name, position, value):
    if len(args) > position:
        args[position] = value
    else:
        kwargs[name] = value


def _database_path(database, uri):
    filename = os.fsdecode(database)
    if not filename or filename == ":memory:":
        return None
    if uri and filename.startswith("file:"):
        parts = urlsplit(filename)
        if parts.netloc not in ("", "localhost"):
            return None
        options = [tuple(unquote(value) for value in item.partition("=")[::2])
                   for item in parts.query.split("&")]
        if ("mode", "memory") in options:
            return None
        filename = unquote(parts.path)
        if not filename or filename == ":memory:":
            return None
    return Path(filename).resolve()


def _exclusive_uri(database, uri):
    filename = os.fsdecode(database)
    if not uri or not filename.startswith("file:"):
        filename = Path(filename).absolute().as_uri()
    parts = urlsplit(filename)
    # SQLite decodes %HH but not form-style '+'. Preserve other options byte-for-byte.
    options = [item for item in parts.query.split("&")
               if unquote(item.partition("=")[0]) != "vfs" and item]
    options.append("vfs=unix-excl")
    return urlunsplit(parts._replace(query="&".join(options)))


class _DeletePolicy:
    def __init__(self, *args, **kwargs):
        self._unsloth_user_authorizer = None
        super().__init__(*args, **kwargs)
        try:
            mode = super().execute("PRAGMA journal_mode=DELETE").fetchone()[0]
            if mode.lower() != "delete":
                raise sqlite3.OperationalError(f"Cannot select DELETE journaling: got {mode}")
            super().execute("PRAGMA synchronous=FULL")
            super().set_authorizer(self._unsloth_authorize)
        except BaseException:
            self.close()
            raise

    def _unsloth_authorize(self, action, first, second, database, trigger):
        callback = self._unsloth_user_authorizer
        if callback is not None:
            result = callback(action, first, second, database, trigger)
            if result != sqlite3.SQLITE_OK:
                return result
        if action == sqlite3.SQLITE_PRAGMA and second is not None:
            name = (first or "").lower()
            value = second.lower()
            if name == "journal_mode" and value != "delete":
                return sqlite3.SQLITE_IGNORE
            if name == "synchronous" and value not in ("full", "2", "extra", "3"):
                return sqlite3.SQLITE_IGNORE
        return sqlite3.SQLITE_OK

    def set_authorizer(self, callback):
        # Preserve application authorization, including reset, without clearing the policy.
        self._unsloth_user_authorizer = callback
        super().set_authorizer(self._unsloth_authorize)


@functools.lru_cache(maxsize=None)
def _delete_factory(factory):
    if not isinstance(factory, type) or not issubclass(factory, sqlite3.Connection):
        raise TypeError("DELETE policy requires a sqlite3.Connection class factory")
    return type("StudioDeleteConnection", (_DeletePolicy, factory), {})


def install(mode, roots=(Path("/workspace/studio"),)):
    """Install once at Python startup, before Studio imports database modules."""
    global _active_mode, _connector
    mode = canonical_mode(mode)
    if mode == "wal":
        if _connector is not None:
            raise RuntimeError("SQLite mode changes require restarting Studio")
        return
    if _connector is not None:
        if mode != _active_mode:
            raise RuntimeError("SQLite mode changes require restarting Studio")
        return
    if mode == "wal-exclusive":
        # Fail startup if this Python build lacks the requested VFS.
        probe = sqlite3.connect("file::memory:?vfs=unix-excl", uri=True)
        probe.close()
    selected_roots = tuple(Path(root).resolve() for root in roots)
    original_connect = sqlite3.connect

    @functools.wraps(original_connect)
    def connect(database, *args, **kwargs):
        uri = _parameter(args, kwargs, "uri", 6, False)
        path = _database_path(database, uri)
        if path is None or not any(path == root or root in path.parents for root in selected_roots):
            return original_connect(database, *args, **kwargs)
        parameters = list(args)
        if mode == "wal-exclusive":
            database = _exclusive_uri(database, uri)
            _set_parameter(parameters, kwargs, "uri", 6, True)
        else:
            factory = _parameter(parameters, kwargs, "factory", 4, sqlite3.Connection)
            _set_parameter(parameters, kwargs, "factory", 4, _delete_factory(factory))
        return original_connect(database, *parameters, **kwargs)

    sqlite3.connect = connect
    sqlite3.dbapi2.connect = connect
    _active_mode = mode
    _connector = connect


def require_active(mode):
    """Python tolerates sitecustomize errors; the opt-in launcher must not."""
    mode = canonical_mode(mode)
    if mode != _active_mode or (mode != "wal" and sqlite3.connect is not _connector):
        raise RuntimeError("Requested SQLite policy did not load; refusing to start Studio")
