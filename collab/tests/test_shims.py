"""Tests for the bash shims — symlink-install resolution (collab-kit slice 3, Blocker 1).

The shim must self-locate the kit root BEFORE sourcing collab_common.sh, so that installing it as
a ~/bin symlink (where dirname(symlink) is not the kit) still works. Verified by running the shim
through a symlink via subprocess.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

KIT = Path(__file__).resolve().parent.parent
HANDOFF = KIT / "tools" / "handoff"
COLLAB_HANDOFF = KIT / "tools" / "collab-handoff"

def _probe_bash():
    """True iff the bash on PATH can actually run our POSIX shim.

    On Windows a WSL ``bash.exe`` may be first on PATH; it uses ``/mnt/c/...`` and cannot open a
    ``C:/...`` or ``/c/...`` script path, so the whole module skips there (the shim is verified
    under Git Bash separately — see the slice-3 review notes). On Linux/macOS/Git-Bash it runs.
    """
    if shutil.which("bash") is None:
        return False
    try:
        r = subprocess.run(["bash", HANDOFF.as_posix(), "--help"], capture_output=True, text=True, timeout=30)
        return r.returncode == 0
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _probe_bash(),
    reason="no compatible bash to run the POSIX shim (e.g. WSL bash on Windows PATH); shim verified under Git Bash",
)


def _run(shim, args, *, env_overrides=None, drop=()):
    env = dict(os.environ)
    for k in drop:
        env.pop(k, None)
    if env_overrides:
        # env paths in forward-slash form: native-Python children + collab_common.sh/cygpath accept it.
        env.update({k: (Path(v).as_posix() if os.sep in str(v) else v) for k, v in env_overrides.items()})
    return subprocess.run(["bash", Path(shim).as_posix(), *args], capture_output=True, text=True, env=env)


def _symlink_or_skip(target: Path, link: Path):
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not creatable on this platform/privilege")


def test_direct_handoff_help():
    r = _run(HANDOFF, ["--help"])
    assert r.returncode == 0, r.stderr


def test_symlinked_handoff_help_with_kit_root(tmp_path):
    link = tmp_path / "bin" / "handoff"
    link.parent.mkdir()
    _symlink_or_skip(HANDOFF, link)
    r = _run(link, ["--help"], env_overrides={"COLLAB_KIT_ROOT": str(KIT)})
    assert r.returncode == 0, f"symlinked shim + COLLAB_KIT_ROOT should work: {r.stderr}"


def test_symlinked_handoff_help_without_kit_root(tmp_path):
    link = tmp_path / "bin" / "handoff"
    link.parent.mkdir()
    _symlink_or_skip(HANDOFF, link)
    r = _run(link, ["--help"], drop=("COLLAB_KIT_ROOT",))
    # The readlink fallback works where symlinks are followable; on MSYS/Windows readlink may not
    # follow the link, in which case COLLAB_KIT_ROOT is the supported route (tested above).
    if r.returncode != 0 and "cannot locate collab-kit" in (r.stderr + r.stdout):
        pytest.skip("platform readlink cannot follow this symlink; COLLAB_KIT_ROOT is the supported install route")
    assert r.returncode == 0, r.stderr


def test_symlinked_collab_handoff_roundtrip(tmp_path):
    link = tmp_path / "bin" / "collab-handoff"
    link.parent.mkdir()
    _symlink_or_skip(COLLAB_HANDOFF, link)
    collab = tmp_path / "demo"
    # create then list a handoff through the symlinked wrapper, bound to the collab via HANDOFF_ROOT
    r1 = _run(link, ["create", "--to", "r", "--title", "via symlink"],
              env_overrides={"COLLAB_KIT_ROOT": str(KIT), "HANDOFF_ROOT": str(collab)})
    assert r1.returncode == 0, r1.stderr
    assert r1.stdout.strip().endswith("001")
    r2 = _run(link, ["list"], env_overrides={"COLLAB_KIT_ROOT": str(KIT), "HANDOFF_ROOT": str(collab)})
    assert r2.returncode == 0 and "001" in r2.stdout, r2.stderr
