"""Launch the unmodified Studio CLI after checking the selected policy loaded."""

import os
import runpy
import sys

from unsloth_sqlite_policy import require_active

require_active(os.environ.get("UNSLOTH_SQLITE_MODE", "wal"))
script = sys.argv[1]
sys.argv = sys.argv[1:]
runpy.run_path(script, run_name="__main__")
