"""Process-wide safety boundary for Rubato's deterministic test suite.

Per-test fixtures still create narrower roots where useful.  This outer root
matters for delayed timers and materialization executor tasks that can finish
after a fixture restores its environment: they must never fall back to the
performer's real Application Support take bank.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path

_TEST_RUNTIME_ROOT = Path(tempfile.mkdtemp(prefix="rubato-pytest-"))
os.environ["AIMUSIC_DATA_ROOT"] = str(_TEST_RUNTIME_ROOT / "data")
os.environ["AIMUSIC_RUNS_ROOT"] = str(_TEST_RUNTIME_ROOT / "runs")
os.environ["AIMUSIC_STATE_ROOT"] = str(_TEST_RUNTIME_ROOT / "state")


@atexit.register
def _remove_test_runtime_root() -> None:
    shutil.rmtree(_TEST_RUNTIME_ROOT, ignore_errors=True)
