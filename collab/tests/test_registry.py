"""Tests for registry.py — collabs.json registry (collab-kit slice 3, §A2 / [C18])."""

from __future__ import annotations

import json
import multiprocessing as mp
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

_LIB = Path(__file__).resolve().parent.parent / "tools" / "lib"
sys.path.insert(0, str(_LIB))

import handoff_core as hc  # noqa: E402
import registry  # noqa: E402


def _register_worker(home, name, root):
    import registry as r

    r.register(name, root, home=home)


class TestRegistry:
    def test_register_and_load(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as croot:
            registry.register("Ibkr Auto Trader", croot, reviewer="grok", guardrails=["money"], home=home)
            data = registry.load(home)
            assert "ibkr-auto-trader" in data["collabs"]
            e = data["collabs"]["ibkr-auto-trader"]
            assert e["reviewer"] == "grok" and e["guardrails"] == ["money"]
            assert Path(e["root"]) == Path(croot).resolve()

    def test_registry_is_valid_json_after_write(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as croot:
            registry.register("x", croot, home=home)
            json.loads((Path(home) / "collabs.json").read_text("utf-8"))  # raises if corrupt

    def test_resolve(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as croot:
            registry.register("proj", croot, home=home)
            assert registry.resolve("proj", home=home) == Path(croot).resolve()
            assert registry.resolve("nope", home=home) is None

    def test_status_counts_by_state(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as croot:
            hc.create(croot, to="r", from_="b", title="one")
            hid = hc.create(croot, to="r", from_="b", title="two")["id"]
            hc.claim(croot, hid)
            registry.register("proj", croot, home=home)
            st = registry.status(home)
            assert len(st) == 1
            assert st[0]["counts"] == {"pending": 1, "claimed": 1}
            assert st[0]["oldest_pending_age_s"] is not None

    def test_corrupt_registry_refuses_not_clobbers(self):
        # Lane 1: a corrupt collabs.json must NOT be silently reset by the next register (data loss).
        import collab_common as cc

        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as croot:
            registry.register("alpha", croot, home=home)
            reg = Path(home) / "collabs.json"
            reg.write_text("{ this is not valid json", encoding="utf-8")  # corrupt it
            before = reg.read_text(encoding="utf-8")
            with pytest.raises(cc.CollabError):
                registry.register("beta", croot, home=home)
            assert reg.read_text(encoding="utf-8") == before  # refused to overwrite the corrupt file

    def test_load_retries_transient_permission_error(self):
        # Lane 1: a Windows os.replace-transient PermissionError is retried, not fatal.
        with tempfile.TemporaryDirectory() as home:
            good = '{"version": 1, "collabs": {}}'
            with (
                mock.patch("registry.time.sleep"),
                mock.patch.object(
                    Path,
                    "read_text",
                    side_effect=[PermissionError("locked"), PermissionError("locked"), good],
                ),
            ):
                data = registry.load(home)
            assert data["collabs"] == {}

    def test_concurrent_register_no_corruption(self):
        # [C18] N processes register distinct names concurrently -> all present, JSON never corrupt.
        n = 8
        with tempfile.TemporaryDirectory() as home:
            croots = [tempfile.mkdtemp() for _ in range(n)]
            ctx = mp.get_context("spawn")
            procs = [
                ctx.Process(target=_register_worker, args=(home, f"proj-{i}", croots[i])) for i in range(n)
            ]
            for p in procs:
                p.start()
            for p in procs:
                p.join(timeout=120)
                assert p.exitcode == 0
            data = json.loads((Path(home) / "collabs.json").read_text("utf-8"))  # valid JSON
            assert set(data["collabs"]) == {f"proj-{i}" for i in range(n)}  # no lost writes
