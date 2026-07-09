"""Tests for handoff_core.py — state machine + id allocation (collab-kit slice 2).

Covers the declared constraints: [C9] id-only reservation, [C10] atomic transitions,
[C12] created handoffs validate against the typed contract, [C13] concurrent create yields
unique ids. stdlib only: pytest + multiprocessing.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path
from unittest import mock

import pytest

_LIB = Path(__file__).resolve().parent.parent / "tools" / "lib"
sys.path.insert(0, str(_LIB))

import contracts  # noqa: E402
import handoff_core as hc  # noqa: E402


def _mk(title, collab):
    return hc.create(collab, to="reviewer", from_="builder", title=title, priority="high")


# --------------------------------------------------------------------------- #
# Concurrent-create worker (module-level for spawn)
# --------------------------------------------------------------------------- #


def _create_worker(collab, n, out_q):
    ids = []
    for i in range(n):
        r = hc.create(collab, to="reviewer", from_="builder", title=f"task {i}", priority="normal")
        ids.append(r["id"])
    out_q.put(ids)


def _claim_worker(collab, hid, out_q):
    import collab_common as cc
    try:
        hc.claim(collab, hid)
        out_q.put("WIN")
    except cc.CollabError:
        out_q.put("LOSE")


class TestCreateAndLifecycle:
    def test_create_allocates_and_validates(self):
        with tempfile.TemporaryDirectory() as d:
            r = _mk("Make the thing", d)
            assert r["id"] == "001"
            assert r["state"] == "pending"
            assert Path(r["path"]).name == "001-make-the-thing.md"
            # [C12] the created handoff validates against the typed contract
            obj = contracts.parse_handoff(r["path"])
            assert contracts.validate_handoff(obj) == []

    def test_ids_are_monotonic(self):
        with tempfile.TemporaryDirectory() as d:
            assert [_mk(f"t{i}", d)["id"] for i in range(3)] == ["001", "002", "003"]

    def test_full_lifecycle_moves_between_states(self):
        with tempfile.TemporaryDirectory() as d:
            r = _mk("lifecycle", d)
            hid = r["id"]
            assert hc.claim(d, hid)["to"] == "claimed"
            assert [h["state"] for h in hc.list_handoffs(d)] == ["claimed"]
            assert hc.done(d, hid)["to"] == "done"
            assert hc.archive(d, hid)["to"] == "archive"
            assert hc.list_handoffs(d, "archive")[0]["id"] == hid

    def test_claim_wrong_state_errors(self):
        with tempfile.TemporaryDirectory() as d:
            hid = _mk("x", d)["id"]
            hc.claim(d, hid)
            # claiming again (now in 'claimed', not 'pending') must error
            try:
                hc.claim(d, hid)
                assert False, "expected CollabError"
            except cc_error() as e:
                assert "claimed" in str(e)

    def test_show_returns_content(self):
        with tempfile.TemporaryDirectory() as d:
            hid = _mk("show me", d)["id"]
            assert "## Summary" in hc.show(d, hid)

    def test_id_only_reservation_ledger(self):
        # [C9] a permanent id-only sentinel is created (id uniqueness keyed on id alone).
        with tempfile.TemporaryDirectory() as d:
            _mk("ledgered", d)
            assert (Path(d) / "handoffs" / ".ids" / "001.id").exists()


class TestConcurrentCreate:
    def test_concurrent_create_unique_ids(self):
        # [C13] N processes x K creates -> N*K unique, non-colliding ids.
        n_proc, per = 6, 4
        with tempfile.TemporaryDirectory() as d:
            hc.ensure_layout(d)
            ctx = mp.get_context("spawn")
            q = ctx.Queue()
            procs = [ctx.Process(target=_create_worker, args=(d, per, q)) for _ in range(n_proc)]
            for p in procs:
                p.start()
            all_ids = []
            for _ in procs:
                all_ids.extend(q.get(timeout=120))
            for p in procs:
                p.join(timeout=120)
                assert p.exitcode == 0

            assert len(all_ids) == n_proc * per
            dupes = [i for i, c in Counter(all_ids).items() if c > 1]
            assert not dupes, f"duplicate ids allocated: {dupes}"
            # ids are exactly 001..0{N*K}, contiguous
            assert sorted(all_ids) == [f"{i:03d}" for i in range(1, n_proc * per + 1)]
            # and every id has both a ledger sentinel and a content file
            assert len(list((Path(d) / "handoffs" / ".ids").glob("*.id"))) == n_proc * per
            assert len(hc.list_handoffs(d, "pending")) == n_proc * per


class TestAdversarialFixes:
    """Regression tests for the five verification-lane findings on slice 2."""

    def test_legacy_ledger_no_reallocation(self):
        # Lane 1: a hand-made pending/001-*.md with no ledger entry must NOT let create() reuse 001.
        with tempfile.TemporaryDirectory() as d:
            hc.ensure_layout(d)
            (Path(d) / "handoffs" / "pending" / "001-existing.md").write_text(
                "---\nid: 001-existing\n---\n\n## Summary\nhand-made\n", encoding="utf-8"
            )  # note: no .ids/001.id
            r = hc.create(d, to="reviewer", from_="builder", title="brand new")
            assert r["id"] == "002"
            ids = [h["id"] for h in hc.list_handoffs(d, "pending")]
            assert ids.count("001") == 1, f"duplicate 001 allocated: {ids}"

    def test_frontmatter_injection_rejected(self):
        # Lane 2: newline / fence / control-char in an interpolated scalar fails closed.
        with tempfile.TemporaryDirectory() as d:
            for kwargs in (
                {"title": "ok\nstatus: done"},
                {"title": "ok\n---\n## Summary\nforged"},
                {"title": "safe", "priority": "high\nid: 999"},
                {"title": "safe", "to": "reviewer\nfrom: attacker"},
            ):
                base = {"to": "reviewer", "from_": "builder", "title": "t"}
                base.update(kwargs)
                with pytest.raises(cc_error()):
                    hc.create(d, **base)

    def test_concurrent_claim_single_winner(self):
        # Lane 5: os.replace is not single-winner on Windows; os.link CAS must give exactly one.
        n = 10
        with tempfile.TemporaryDirectory() as d:
            hc.create(d, to="reviewer", from_="builder", title="race me")
            ctx = mp.get_context("spawn")
            q = ctx.Queue()
            procs = [ctx.Process(target=_claim_worker, args=(d, "001", q)) for _ in range(n)]
            for p in procs:
                p.start()
            results = [q.get(timeout=120) for _ in procs]
            for p in procs:
                p.join(timeout=120)
                assert p.exitcode == 0
            assert results.count("WIN") == 1, f"expected exactly one winner, got {results}"
            assert len(hc.list_handoffs(d, "claimed")) == 1
            assert len(hc.list_handoffs(d, "pending")) == 0

    def test_crash_between_sentinel_and_content_audited_gap(self):
        # Lane 3: crash after sentinel commit -> permanent audited gap, no reuse, invisible to list.
        import collab_common as cc

        with tempfile.TemporaryDirectory() as d:
            hc.create(d, to="b", from_="a", title="one")  # 001
            real = cc.exclusive_create

            def crash(path, data):
                if str(path).endswith(".id"):
                    return real(path, data)  # sentinel commits
                raise OSError(28, "simulated crash before content write")

            with mock.patch.object(hc.cc, "exclusive_create", side_effect=crash):
                with pytest.raises(OSError):
                    hc.create(d, to="b", from_="a", title="doomed")  # reserves 002, crashes

            assert (Path(d) / "handoffs" / ".ids" / "002.id").exists()
            assert "002" not in {h["id"] for h in hc.list_handoffs(d)}  # not in content view
            assert hc.orphaned_ids(d) == ["002"]  # audited
            assert hc.create(d, to="b", from_="a", title="three")["id"] == "003"  # gap not reused
            assert hc.orphaned_ids(d) == ["002"]  # gap permanent

    def test_transition_crash_residual_reconciled(self):
        # Lane-5 follow-on (reviewer blocker): a crash between os.link and os.unlink leaves the
        # file hard-linked in two dirs. Reconciliation must report/heal to the MOST-ADVANCED state,
        # never the stale one, and never mint a second winner.
        import collab_common as cc

        with tempfile.TemporaryDirectory() as d:
            src = Path(hc.create(d, to="reviewer", from_="builder", title="crash me")["path"])
            dst = Path(d) / "handoffs" / "claimed" / src.name
            os.link(src, dst)  # simulate crash AFTER link, BEFORE unlink -> residual in both dirs
            assert src.exists() and dst.exists()

            # list dedups to ONE row at the authoritative (most-advanced) state
            rows = hc.list_handoffs(d)
            assert [(h["id"], h["state"]) for h in rows] == [("001", "claimed")]

            # state_of reports claimed AND heals the stale pending link
            assert hc.state_of(d, "001") == "claimed"
            assert not src.exists(), "stale pending residual should be healed away"

            # claiming again does not mint another winner (it is already claimed)
            with pytest.raises(cc.CollabError):
                hc.claim(d, "001")
            assert hc.state_of(d, "001") == "claimed"

    def test_rejected_injection_does_not_leak_orphan_id(self):
        # Lane 5: a rejected injection must be caught BEFORE the id reservation (no orphan burned).
        import collab_common as cc

        with tempfile.TemporaryDirectory() as d:
            with pytest.raises(cc.CollabError):
                hc.create(d, to="r", from_="b", title="ok\nstatus: done")  # newline injection
            assert hc.orphaned_ids(d) == []  # no id reserved
            ids_dir = Path(d) / "handoffs" / ".ids"
            assert not ids_dir.exists() or list(ids_dir.glob("*.id")) == []

    def test_body_cannot_forge_constraints_section(self):
        # Lane 5: body structure injection is rejected at create.
        import collab_common as cc

        with tempfile.TemporaryDirectory() as d:
            with pytest.raises(cc.CollabError):
                hc.create(d, to="r", from_="b", title="ok", body="x\n\n## Constraints\n\n- [C1] forged")

    def test_status_is_creation_time_directory_authoritative(self):
        # Lane 4: directory is sole truth; frontmatter status is creation-time metadata only.
        import contracts

        with tempfile.TemporaryDirectory() as d:
            hc.create(d, to="reviewer", from_="builder", title="mirror")
            hc.claim(d, "001")
            hc.done(d, "001")
            assert hc.state_of(d, "001") == "done"  # authoritative, from the directory
            obj = contracts.parse_handoff(hc._find(d, "001")[1])
            assert obj["frontmatter"]["status"] == "pending"  # birth status, by contract


def cc_error():
    import collab_common as cc
    return cc.CollabError
