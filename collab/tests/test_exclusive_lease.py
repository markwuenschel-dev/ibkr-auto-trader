"""Tests for ActiveHandoffLease — the board-level exclusive control lease (ADR-0003 D2, Phase 3).

Pins that exactly one run holds the board: a second run cannot acquire while a live lease is held
(so 032 cannot start while 030 is held), the lease survives a concurrent assessment, a stale lease
(crashed driver) is reclaimable and audited, and a run cannot hold two handoffs at once.
"""

from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "tools" / "lib"
sys.path.insert(0, str(_LIB))

import autopilot as ap  # noqa: E402
import collab_common as cc  # noqa: E402
import escalation as esc  # noqa: E402
import handoff_core as hc  # noqa: E402


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class TestExclusivity:
    def test_second_run_blocked_while_first_holds(self, tmp_path):
        a = hc.ActiveHandoffLease(str(tmp_path), "runA", now=_Clock())
        b = hc.ActiveHandoffLease(str(tmp_path), "runB", now=_Clock())
        a.acquire("030")
        with pytest.raises(hc.LeaseHeld) as ei:
            b.acquire("032")  # 032 cannot start while 030 is held
        assert ei.value.holder["hid"] == "030"
        assert ei.value.holder["run_uid"] == "runA"

    def test_release_frees_the_board(self, tmp_path):
        a = hc.ActiveHandoffLease(str(tmp_path), "runA", now=_Clock())
        b = hc.ActiveHandoffLease(str(tmp_path), "runB", now=_Clock())
        a.acquire("030")
        assert a.release() is True
        got = b.acquire("032")  # now free
        assert got["hid"] == "032" and got["run_uid"] == "runB"

    def test_run_cannot_hold_two_handoffs(self, tmp_path):
        a = hc.ActiveHandoffLease(str(tmp_path), "runA", now=_Clock())
        a.acquire("030")
        with pytest.raises(cc.CollabError, match="one board-level"):
            a.acquire("032")  # must release 030 first

    def test_reacquire_same_slice_is_idempotent(self, tmp_path):
        a = hc.ActiveHandoffLease(str(tmp_path), "runA", now=_Clock())
        first = a.acquire("030")
        again = a.acquire("030")
        assert again["acquired_epoch"] == first["acquired_epoch"]  # original acquire time preserved


class TestHeartbeatAndReclaim:
    def test_renew_only_by_holder(self, tmp_path):
        a = hc.ActiveHandoffLease(str(tmp_path), "runA", now=_Clock())
        b = hc.ActiveHandoffLease(str(tmp_path), "runB", now=_Clock())
        a.acquire("030")
        assert a.renew() is True
        assert b.renew() is False  # b does not hold it

    def test_held_across_concurrent_work_stays_with_holder(self, tmp_path):
        clock = _Clock()
        a = hc.ActiveHandoffLease(str(tmp_path), "runA", now=clock)
        a.acquire("030")
        # Simulate a concurrent assessment: many heartbeat renewals from worker threads.
        with ThreadPoolExecutor(max_workers=8) as ex:
            results = list(ex.map(lambda _: a.renew(), range(50)))
        assert all(results)
        assert a.holder()["hid"] == "030"
        assert a.holder()["run_uid"] == "runA"

    def test_stale_lease_is_reclaimable_and_audited(self, tmp_path):
        clock = _Clock(1000.0)
        a = hc.ActiveHandoffLease(str(tmp_path), "runA", ttl=90.0, now=clock)
        a.acquire("030")
        # A live lease still blocks.
        b = hc.ActiveHandoffLease(str(tmp_path), "runB", ttl=90.0, now=clock)
        with pytest.raises(hc.LeaseHeld):
            b.acquire("030")
        # Advance past the TTL: runA looks crashed. runB may reclaim.
        clock.t += 200.0
        got = b.acquire("030")
        assert got["run_uid"] == "runB"
        audit = (tmp_path / "autopilot" / "lease-audit.jsonl").read_text("utf-8")
        recs = [json.loads(x) for x in audit.splitlines() if x.strip()]
        assert any(r["event"] == "reclaim_stale" and r["run_uid"] == "runB" for r in recs)

    def test_fresh_heartbeat_prevents_reclaim(self, tmp_path):
        clock = _Clock(1000.0)
        a = hc.ActiveHandoffLease(str(tmp_path), "runA", ttl=90.0, now=clock)
        b = hc.ActiveHandoffLease(str(tmp_path), "runB", ttl=90.0, now=clock)
        a.acquire("030")
        clock.t += 200.0
        a.renew()  # holder is alive and renewing
        with pytest.raises(hc.LeaseHeld):
            b.acquire("030")


class TestLeaseRenewer:
    """`renew()` exists but had NO production caller: a healthy driver's lease went stale 90s after
    acquire — during the first agentic call of every real run. The dashboard then showed "no driver
    running" while it worked, and (the real hazard) start_driver's "refuse while a driver is running"
    check passed, so Start would spawn a SECOND driver onto the same board (ADR-0003 D2)."""

    def test_renewer_keeps_a_working_drivers_lease_unreclaimable(self, tmp_path):
        import autopilot as ap

        clock = _Clock(1000.0)
        a = hc.ActiveHandoffLease(str(tmp_path), "runA", ttl=90.0, now=clock)
        b = hc.ActiveHandoffLease(str(tmp_path), "runB", ttl=90.0, now=clock)
        a.acquire("030")
        with ap._LeaseRenewer(a, interval=0.01):
            clock.t += 200.0  # a long agentic call, well past the 90s TTL
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:  # wait for the daemon thread to tick at the new clock
                rec = json.loads((Path(str(tmp_path)) / "autopilot" / "active.lease").read_text("utf-8"))
                if rec["heartbeat_epoch"] >= 1200.0:
                    break
                time.sleep(0.01)
        with pytest.raises(hc.LeaseHeld):
            b.acquire("030")  # a live, renewing holder must still block a second driver

    def test_renewer_is_a_noop_without_a_lease(self):
        import autopilot as ap

        with ap._LeaseRenewer(None, interval=0.01):
            pass  # must not start a thread or raise

    def test_renewer_survives_a_renew_that_raises(self):
        """Best-effort ([C15]): a renew failure must never take the run down mid-call."""
        import autopilot as ap

        calls = []

        class Boom:
            def renew(self):
                calls.append(1)
                raise OSError("disk gone")

        with ap._LeaseRenewer(Boom(), interval=0.01):
            deadline = time.monotonic() + 5.0
            while not calls and time.monotonic() < deadline:
                time.sleep(0.01)
        assert calls, "renewer never ticked"


class TestHolder:
    def test_holder_reports_none_when_free(self, tmp_path):
        a = hc.ActiveHandoffLease(str(tmp_path), "runA", now=_Clock())
        assert a.holder() is None
        a.acquire("030")
        assert a.holder()["hid"] == "030"
        a.release()
        assert a.holder() is None


class TestOrphanReclaim:
    """`claimed/` conflates three states. Only ORPHANED (dead driver, no escalation) is reclaimable.

    An orphan is invisible to `_next_root` (which scans `pending/` only), so before this the board stayed
    wedged forever and the next Start idled to a false "done".
    """

    def _claimed(self, collab, title="x"):
        hid = hc.create(collab, to="builder", from_="reviewer", title=title, body="y")["id"]
        hc.claim(collab, hid)
        return hid

    def test_orphan_is_returned_to_pending(self, tmp_path):
        collab = str(tmp_path / "c")
        hid = self._claimed(collab)
        assert ap._reclaim_orphans(collab) == [hid]
        assert hc._reconcile(collab, hid)[0] == "pending"
        assert [h["id"] for h in hc.list_handoffs(collab, "pending")] == [hid]

    def test_reclaimed_orphan_becomes_selectable_again(self, tmp_path):
        # The actual point: _next_root scans pending/ only, so an orphan is invisible to it until reclaimed.
        collab = str(tmp_path / "c")
        seats = {"builder": {"backend": "cli", "cmd": ["m"], "system": "s"}}
        hid = self._claimed(collab)
        assert ap._next_root(collab, seats, set()) is None, "an orphan must be invisible while claimed"
        ap._reclaim_orphans(collab)
        assert ap._next_root(collab, seats, set()) == hid

    def test_a_parked_handoff_is_never_reclaimed(self, tmp_path):
        # An open escalation means a human was asked to decide. Auto-driving it re-opens a budget epoch
        # with no authorization and re-drives a known-blocked slice on every Start.
        collab = str(tmp_path / "c")
        hid = self._claimed(collab)
        esc.write(collab, hid, [], attempts=1, reason="infrastructure_blocked")
        assert ap._reclaim_orphans(collab) == []
        assert hc._reconcile(collab, hid)[0] == "claimed"

    def test_clearing_the_escalation_unparks_it(self, tmp_path):
        # The operator path: clearing the escalation is the human saying "I looked". The slice is then a
        # plain orphan, so the next start reclaims it and it runs -- no operator request needed.
        collab = str(tmp_path / "c")
        hid = self._claimed(collab)
        esc.write(collab, hid, [], attempts=1, reason="infrastructure_blocked")
        assert ap._reclaim_orphans(collab) == []
        esc.clear(collab, hid)
        assert ap._reclaim_orphans(collab) == [hid]
        assert hc._reconcile(collab, hid)[0] == "pending"

    def test_a_live_drivers_work_is_never_stolen(self, tmp_path):
        # A live lease means IN PROGRESS, not orphaned. Reclaiming it would hand the same slice to a
        # second driver -- the exact double-drive ADR-0003 D2 exists to prevent.
        collab = str(tmp_path / "c")
        hid = self._claimed(collab)
        lease = hc.ActiveHandoffLease(collab, "live-run", pid=4242)
        lease.acquire(hid)
        assert ap._reclaim_orphans(collab) == []
        assert hc._reconcile(collab, hid)[0] == "claimed"
        lease.release()
        assert ap._reclaim_orphans(collab) == [hid]  # holder gone -> now a genuine orphan

    def test_a_stale_lease_does_not_protect_an_orphan(self, tmp_path):
        # A dead driver's lease outlives it by up to the TTL. Past the TTL it proves nothing.
        collab = str(tmp_path / "c")
        hid = self._claimed(collab)
        clock = _Clock()
        hc.ActiveHandoffLease(collab, "dead-run", pid=1, now=clock).acquire(hid)
        clock.t += hc._LEASE_TTL_S + 1
        assert ap._reclaim_orphans(collab) == [hid]

    def test_reclaim_is_idempotent_and_leaves_other_states_alone(self, tmp_path):
        collab = str(tmp_path / "c")
        hid = self._claimed(collab)
        done_hid = hc.create(collab, to="builder", from_="reviewer", title="d", body="y")["id"]
        hc.claim(collab, done_hid)
        hc.done(collab, done_hid)
        assert ap._reclaim_orphans(collab) == [hid]
        assert ap._reclaim_orphans(collab) == []  # nothing left to reclaim
        assert hc._reconcile(collab, done_hid)[0] == "done"


class TestTransitionIntoAMissingStateDir:
    """A state dir vanishes once its last handoff moves out (and git cannot track an empty dir), so a
    transition routinely targets a directory that does not exist. `os.link` then fails on the missing
    PARENT, which `_transition` used to report as "lost the race" -- a hard failure wearing the costume
    of a benign concurrency outcome. This is exactly how the real board's reclaim silently no-op'd.
    """

    def test_reclaim_works_when_pending_dir_is_gone(self, tmp_path):
        collab = str(tmp_path / "c")
        hid = hc.create(collab, to="builder", from_="reviewer", title="x", body="y")["id"]
        hc.claim(collab, hid)
        pending = Path(collab) / "handoffs" / "pending"
        if pending.exists():
            pending.rmdir()  # the real board's state: last handoff moved out, dir went with it
        assert not pending.exists()
        hc.reclaim(collab, hid)
        assert hc._reconcile(collab, hid)[0] == "pending"

    def test_done_works_when_done_dir_is_gone(self, tmp_path):
        collab = str(tmp_path / "c")
        hid = hc.create(collab, to="builder", from_="reviewer", title="x", body="y")["id"]
        hc.claim(collab, hid)
        done = Path(collab) / "handoffs" / "done"
        if done.exists():
            done.rmdir()
        hc.done(collab, hid)
        assert hc._reconcile(collab, hid)[0] == "done"

    def test_a_genuine_lost_race_still_reports_as_one(self, tmp_path):
        # The mkdir must not mask a REAL race: src gone => still HandoffConflict.
        collab = str(tmp_path / "c")
        hid = hc.create(collab, to="builder", from_="reviewer", title="x", body="y")["id"]
        hc.claim(collab, hid)
        hc.reclaim(collab, hid)
        with pytest.raises(hc.HandoffConflict):
            hc.reclaim(collab, hid)  # no longer claimed -> cannot reclaim again
