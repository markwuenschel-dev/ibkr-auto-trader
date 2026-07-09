"""Tests for watcher.py — handoff watchers (collab-kit slice 4)."""

from __future__ import annotations

import multiprocessing as mp
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

_LIB = Path(__file__).resolve().parent.parent / "tools" / "lib"
sys.path.insert(0, str(_LIB))

import collab_common as cc  # noqa: E402
import handoff_core as hc  # noqa: E402
import registry  # noqa: E402
import watcher  # noqa: E402


def _watch_worker(collab, seat):
    import watcher as w
    w.watch(collab, seat=seat, once=True, catch_up=True)


class TestWatch:
    def test_cold_start_seeds_backlog_then_announces_new(self, capsys):
        with tempfile.TemporaryDirectory() as d:
            hc.create(d, to="reviewer", from_="builder", title="first")  # backlog
            watcher.watch(d, seat="reviewer", once=True)  # cold start -> seed, no announce
            assert "NEW handoff" not in capsys.readouterr().out

            hc.create(d, to="reviewer", from_="builder", title="second")  # id 002, new arrival
            new = watcher.watch(d, seat="reviewer", once=True)
            assert new == ["002"]
            assert "NEW handoff 002" in capsys.readouterr().out

            # restart / re-tick -> not re-announced ([C21])
            assert watcher.watch(d, seat="reviewer", once=True) == []
            assert "NEW handoff" not in capsys.readouterr().out

    def test_to_filtering(self, capsys):
        with tempfile.TemporaryDirectory() as d:
            hc.create(d, to="builder", from_="reviewer", title="for the builder")
            new = watcher.watch(d, seat="reviewer", once=True, catch_up=True)  # reviewer seat
            assert new == []  # not addressed to reviewer
            assert "NEW handoff" not in capsys.readouterr().out

    def test_catch_up_announces_backlog(self, capsys):
        with tempfile.TemporaryDirectory() as d:
            hc.create(d, to="reviewer", from_="builder", title="already here")
            new = watcher.watch(d, seat="reviewer", once=True, catch_up=True)
            assert new == ["001"]
            assert "NEW handoff 001" in capsys.readouterr().out

    def test_persisted_seen_survives_restart(self):
        with tempfile.TemporaryDirectory() as d:
            hc.create(d, to="reviewer", from_="builder", title="x")
            watcher.watch(d, seat="reviewer", once=True, catch_up=True)  # announces 001, persists
            state = Path(d) / "logs" / "watch-reviewer.state"
            assert state.exists() and "001" in state.read_text("utf-8")

    def test_malformed_file_does_not_crash_loop(self, capsys):
        with tempfile.TemporaryDirectory() as d:
            hc.ensure_layout(d)
            (Path(d) / "handoffs" / "pending" / "007-garbage.md").write_text("not a handoff\n", encoding="utf-8")
            r = hc.create(d, to="reviewer", from_="builder", title="real one")  # id bumped past 007
            # the garbage file has empty frontmatter (to=None) -> skipped without crashing;
            # the real handoff is still announced.
            new = watcher.watch(d, seat="reviewer", once=True, catch_up=True)
            assert r["id"] in new  # whatever id it got (008, since 007-garbage bumped the counter)

    def test_parse_exception_warns_once_and_continues(self, capsys):
        with tempfile.TemporaryDirectory() as d:
            hc.create(d, to="reviewer", from_="builder", title="ok")
            real = watcher.contracts.parse_handoff

            def boom(path):
                if "001" in str(path):
                    raise OSError("unreadable")
                return real(path)

            with mock.patch.object(watcher.contracts, "parse_handoff", side_effect=boom):
                new = watcher.watch(d, seat="reviewer", once=True, catch_up=True)  # must not raise
            assert new == []  # the only handoff was unreadable
            assert "could not read" in capsys.readouterr().err

    def test_case_insensitive_to_filter(self, capsys):
        # Lane 2: `to: Reviewer` must still surface for seat `reviewer` (was silently dropped).
        with tempfile.TemporaryDirectory() as d:
            hc.create(d, to="Reviewer", from_="builder", title="mixed case to")
            new = watcher.watch(d, seat="reviewer", once=True, catch_up=True)
            assert new == ["001"]
            assert "NEW handoff 001" in capsys.readouterr().out

    def test_symlink_in_pending_skipped(self, capsys, tmp_path):
        # Lane 2: a symlink in pending/ must not be followed out of the collab (info-disclosure).
        d = tmp_path / "c"
        hc.ensure_layout(str(d))
        outside = tmp_path / "secret.md"
        outside.write_text("---\nto: reviewer\nfrom: attacker\ntitle: LEAKED\n---\n## Summary\nx\n", encoding="utf-8")
        link = d / "handoffs" / "pending" / "001-link.md"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not creatable on this platform/privilege")
        new = watcher.watch(str(d), seat="reviewer", once=True, catch_up=True)
        cap = capsys.readouterr()
        assert new == [] and "LEAKED" not in cap.out  # not read, not announced
        assert "skipping non-regular file" in cap.err

    def test_persist_merge_fences_before_commit(self, monkeypatch, tmp_path):
        # B1: the seen-set write under collab_lock must assert_current() first (slice-1 invariant).
        import collab_common as cc

        calls = []
        orig = cc.LockHandle.assert_current
        monkeypatch.setattr(cc.LockHandle, "assert_current",
                            lambda self: (calls.append(1), orig(self))[1])
        state = tmp_path / "logs" / "watch-reviewer.state"
        watcher._persist_merge(state, {"001"})
        assert calls, "must assert_current() before the seen-set commit"
        assert "001" in state.read_text("utf-8")

    def test_corrupt_state_reseeds_not_reannounces(self, capsys):
        # B2: a corrupt/non-utf-8 state file must quarantine + re-seed the backlog, NOT re-announce.
        with tempfile.TemporaryDirectory() as d:
            hc.create(d, to="reviewer", from_="b", title="first")  # 001
            state = Path(d) / "logs" / "watch-reviewer.state"
            state.parent.mkdir(parents=True, exist_ok=True)
            state.write_bytes(b"\xff\xfe corrupt not utf-8")
            assert watcher.watch(d, seat="reviewer", once=True) == []  # re-seeded, not re-announced
            assert "NEW handoff" not in capsys.readouterr().out
            assert state.exists() and "001" in state.read_text("utf-8")  # repaired + seeded
            hc.create(d, to="reviewer", from_="b", title="second")  # 002, genuinely new
            assert watcher.watch(d, seat="reviewer", once=True) == ["002"]  # only the new one

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="mkfifo unavailable (Windows)")
    def test_fifo_in_pending_skipped(self, capsys, tmp_path):
        # B4: a FIFO (or other non-regular file) must be skipped, never read (could hang).
        d = tmp_path / "c"
        hc.ensure_layout(str(d))
        os.mkfifo(str(d / "handoffs" / "pending" / "001-fifo.md"))
        hc.create(str(d), to="reviewer", from_="b", title="real")  # id bumped past 001
        watcher.watch(str(d), seat="reviewer", once=True, catch_up=True)  # must not hang/crash
        assert "skipping non-regular file" in capsys.readouterr().err

    def test_oversized_file_skipped(self, capsys, tmp_path):
        # B4: a file above the size cap is skipped (huge-file DoS guard).
        d = tmp_path / "c"
        hc.ensure_layout(str(d))
        (d / "handoffs" / "pending" / "001-big.md").write_text("x" * (600 * 1024), encoding="utf-8")
        assert watcher.watch(str(d), seat="reviewer", once=True, catch_up=True) == []
        assert "oversized" in capsys.readouterr().err

    def test_routing_immutable_retarget_not_announced(self):
        # B5 CONTRACT: to: is fixed at creation. Re-addressing after creation is unsupported —
        # the watcher records non-matching handoffs as seen, so a retarget is NOT re-announced.
        with tempfile.TemporaryDirectory() as d:
            r = hc.create(d, to="builder", from_="reviewer", title="for builder")
            assert watcher.watch(d, seat="reviewer", once=True, catch_up=True) == []  # not ours
            path = Path(r["path"])
            path.write_text(path.read_text("utf-8").replace("to: builder", "to: reviewer"), encoding="utf-8")
            assert watcher.watch(d, seat="reviewer", once=True) == []  # by design: not re-announced

    def test_concurrent_watchers_no_lost_update(self):
        # Lane 1 (primary): 4 concurrent same-seat watchers announcing the backlog must not
        # lost-update the persisted seen -> a restart re-announces NOTHING ([C21] under concurrency).
        n = 6
        with tempfile.TemporaryDirectory() as d:
            for i in range(n):
                hc.create(d, to="reviewer", from_="b", title=f"h{i}")
            ctx = mp.get_context("spawn")
            procs = [ctx.Process(target=_watch_worker, args=(d, "reviewer")) for _ in range(4)]
            for p in procs:
                p.start()
            for p in procs:
                p.join(timeout=120)
                assert p.exitcode == 0
            again = watcher.watch(d, seat="reviewer", once=True)  # restart
            assert again == [], f"re-announced after concurrent watchers (lost update): {again}"
            seen = set((Path(d) / "logs" / "watch-reviewer.state").read_text("utf-8").split())
            assert {f"{i:03d}" for i in range(1, n + 1)} <= seen  # all ids persisted, none clobbered

    def test_read_only_never_changes_state(self):
        with tempfile.TemporaryDirectory() as d:
            hc.create(d, to="reviewer", from_="builder", title="untouched")
            before = [(h["id"], h["state"]) for h in hc.list_handoffs(d)]
            watcher.watch(d, seat="reviewer", once=True, catch_up=True)
            after = [(h["id"], h["state"]) for h in hc.list_handoffs(d)]
            assert before == after  # [C20]


class TestWatchAll:
    def test_watch_all_across_collabs(self, capsys, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        a, b = tmp_path / "a", tmp_path / "b"
        hc.create(str(a), to="reviewer", from_="builder", title="from a")
        hc.create(str(b), to="reviewer", from_="builder", title="from b")
        registry.register("proj-a", str(a), home=str(home))
        registry.register("proj-b", str(b), home=str(home))
        out = watcher.watch_all(seat="reviewer", once=True, home=str(home), catch_up=True)
        assert out == {"proj-a": ["001"], "proj-b": ["001"]}

    def test_watch_all_corrupt_registry_clean_exit(self, capsys, tmp_path, monkeypatch):
        # Lane 3: a corrupt collabs.json must yield a clean exit-1 message from main(), not a traceback.
        home = tmp_path / "home"
        home.mkdir()
        (home / "collabs.json").write_text("{ corrupt json", encoding="utf-8")
        monkeypatch.setenv("COLLAB_HOME", str(home))
        rc = watcher.main(["--all", "--seat", "reviewer", "--once"])
        assert rc == 1
        assert "corrupt" in capsys.readouterr().err

    def test_watch_all_skips_broken_collab(self, capsys, tmp_path):
        # A broken collab (root missing) must not sink the sweep, and must NOT be materialized.
        home = tmp_path / "home"
        home.mkdir()
        good = tmp_path / "good"
        hc.create(str(good), to="reviewer", from_="b", title="ok")
        missing = tmp_path / "does-not-exist"
        registry.register("good", str(good), home=str(home))
        registry.register("broken", str(missing), home=str(home))
        out = watcher.watch_all(seat="reviewer", once=True, home=str(home), catch_up=True)
        assert out == {"good": ["001"]}  # good processed, broken skipped
        assert not missing.exists()  # lane-4 fix: nonexistent root not conjured into being
        assert "skip broken" in capsys.readouterr().err


class TestInboxDrain:
    # §A6 wire: the bridge (slice 5) writes inbox/live/<project>/from-user-*.md; the watcher drains it.
    def test_drain_surfaces_and_consumes_exactly_once(self, capsys, tmp_path):
        home = str(tmp_path)
        d = Path(home) / "inbox" / "live" / "proj"
        d.mkdir(parents=True)
        (d / "from-user-1.md").write_text("please review 003\n", encoding="utf-8")
        drained = watcher.drain_inbox(home, "proj", seat="reviewer")
        assert len(drained) == 1
        assert "MESSAGE from you (proj): please review 003" in capsys.readouterr().out
        assert not (d / "from-user-1.md").exists()  # consumed (moved to archive)
        assert (d / "archive" / "from-user-1.md").exists()
        assert watcher.drain_inbox(home, "proj", seat="reviewer") == []  # consumed once (no re-surface absent a crash)

    def test_watch_drains_inbox_for_its_collab(self, capsys, tmp_path):
        home = str(tmp_path)
        collab = str(tmp_path / "proj")
        hc.create(collab, to="reviewer", from_="b", title="x")
        d = Path(home) / "inbox" / "live" / "proj"
        d.mkdir(parents=True)
        (d / "from-user-9.md").write_text("hello from phone\n", encoding="utf-8")
        watcher.watch(collab, seat="reviewer", once=True, project="proj", home=home)
        assert "MESSAGE from you (proj): hello from phone" in capsys.readouterr().out

    def test_watch_all_drains_inbox(self, capsys, tmp_path):
        home = str(tmp_path / "home")
        Path(home).mkdir()
        a = tmp_path / "a"
        hc.create(str(a), to="reviewer", from_="b", title="x")
        registry.register("a", str(a), home=home)
        d = Path(home) / "inbox" / "live" / "a"
        d.mkdir(parents=True)
        (d / "from-user-5.md").write_text("cross-collab msg\n", encoding="utf-8")
        watcher.watch_all(seat="reviewer", once=True, home=home, catch_up=True)
        assert "MESSAGE from you (a): cross-collab msg" in capsys.readouterr().out

    def test_inbox_single_consumer_lock_skips_when_held(self, tmp_path):
        # Blocker 2 (Option A): the drain is single-consumer. While a peer holds the per-project drain
        # lock, a second drain must NOT read/surface/consume the file — it skips this tick and leaves it.
        home = str(tmp_path)
        d = Path(home) / "inbox" / "live" / "proj"
        d.mkdir(parents=True)
        (d / "from-user-1.md").write_text("under contention\n", encoding="utf-8")
        with cc.collab_lock(watcher._inbox_lockdir(home, "proj"), ttl=10.0, acquire_timeout=30.0):
            assert watcher.drain_inbox(home, "proj", seat="reviewer") == []  # lock held -> skip
        assert (d / "from-user-1.md").exists()  # not consumed by the blocked drainer
        # once the lock frees, the message is still there to be drained (no drop)
        assert len(watcher.drain_inbox(home, "proj", seat="reviewer")) == 1

    def test_inbox_oversized_file_skipped_or_bounded(self, capsys, tmp_path):
        # A local process can drop a huge from-user file; the drain must not read it unbounded.
        home = str(tmp_path)
        d = Path(home) / "inbox" / "live" / "proj"
        d.mkdir(parents=True)
        big = d / "from-user-1.md"
        big.write_text("B" * (watcher._MAX_INBOX_BYTES + 1024), encoding="utf-8")
        assert watcher.drain_inbox(home, "proj", seat="reviewer") == []  # skipped, not consumed
        assert big.exists()  # left in place (never read into memory)
        assert "oversized inbox file" in capsys.readouterr().err

    def test_inbox_symlink_skipped(self, tmp_path):
        # A symlinked queue file must never be followed/consumed (info-disclosure defense).
        home = str(tmp_path)
        d = Path(home) / "inbox" / "live" / "proj"
        d.mkdir(parents=True)
        secret = tmp_path / "secret.md"
        secret.write_text("TOP SECRET\n", encoding="utf-8")
        try:
            (d / "from-user-1.md").symlink_to(secret)
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation not permitted on this platform")
        assert watcher.drain_inbox(home, "proj", seat="reviewer") == []  # non-regular -> skipped
        assert secret.exists()  # target untouched, never consumed
