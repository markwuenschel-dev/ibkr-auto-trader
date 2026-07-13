"""Tests for ActiveHandoffLease — the board-level exclusive control lease (ADR-0003 D2, Phase 3).

Pins that exactly one run holds the board: a second run cannot acquire while a live lease is held
(so 032 cannot start while 030 is held), the lease survives a concurrent assessment, a stale lease
(crashed driver) is reclaimable and audited, and a run cannot hold two handoffs at once.
"""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "tools" / "lib"
sys.path.insert(0, str(_LIB))

import collab_common as cc  # noqa: E402
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


class TestHolder:
    def test_holder_reports_none_when_free(self, tmp_path):
        a = hc.ActiveHandoffLease(str(tmp_path), "runA", now=_Clock())
        assert a.holder() is None
        a.acquire("030")
        assert a.holder()["hid"] == "030"
        a.release()
        assert a.holder() is None
