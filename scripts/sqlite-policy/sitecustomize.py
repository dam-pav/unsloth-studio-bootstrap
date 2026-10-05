"""Loaded through PYTHONPATH only when an alternative SQLite policy is selected."""

import os

from unsloth_sqlite_policy import install

install(os.environ.get("UNSLOTH_SQLITE_MODE", "wal"))
