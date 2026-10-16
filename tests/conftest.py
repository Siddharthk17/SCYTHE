"""Ensure subprocess-based tests resolve the repo's own `ctx` binary.

Some environments (e.g. mise shims) shadow `ctx` on PATH, causing
`subprocess.run(["ctx", ...])` to fail with a shim error instead of
invoking the locally built entry point. Prepending the active venv's
`bin/` (which contains the `ctx` console script from `pip install -e .`)
makes those tests hermetic without editing any existing test.
"""
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

_CANDIDATE_BINS = [
    _REPO_ROOT / ".venv" / "bin",
    Path(sys.prefix) / "bin",
    Path(os.path.expanduser("~/.local/bin")),
]

_current = os.environ.get("PATH", "")
_parts = _current.split(os.pathsep) if _current else []
for bin_dir in _CANDIDATE_BINS:
    if bin_dir.is_dir():
        s = str(bin_dir)
        if s not in _parts:
            _parts.insert(0, s)
os.environ["PATH"] = os.pathsep.join(_parts)
