"""Tests for collab_common (handoff 001 slice 1). stdlib only: unittest + multiprocessing.

Run:  python -m unittest discover -s tests   (from the collab-kit repo root)
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

# Make the core importable whether run from repo root or elsewhere.
_LIB = Path(__file__).resolve().parent.parent / "tools" / "lib"
sys.path.insert(0, str(_LIB))

import collab_common as cc  # noqa: E402

# --------------------------------------------------------------------------- #
# Top-level workers (must be module-level for the 'spawn' start method on Windows)
# --------------------------------------------------------------------------- #


def _contend_worker(lockdir: str, counter_file: str, increments: int) -> None:
    for _ in range(increments):
        with cc.collab_lock(lockdir, ttl=30.0, acquire_timeout=60.0):
            v = int(Path(counter_file).read_text())
            time.sleep(0.001)  # widen the interleave window; lost updates => bug
            Path(counter_file).write_text(str(v + 1))


def _stale_regime_worker(lockdir: str, counter_file: str, increments: int) -> None:
    """Exercises the STALE regime: each worker stalls past ttl exactly once (triggering a
    break by a waiter), fences with assert_current before mutating, and retries on LockBroken.
    A lost update here would mean release/break destroyed a valid lock -> the fix regressed.
    """
    stalled = False
    done = 0
    while done < increments:
        try:
            with cc.collab_lock(lockdir, ttl=0.3, acquire_timeout=120.0) as h:
                v = int(Path(counter_file).read_text())
                if not stalled:
                    stalled = True
                    time.sleep(0.45)  # exceed ttl once -> a waiter breaks us
                else:
                    time.sleep(0.02)  # < ttl -> progress guaranteed
                h.assert_current()  # FENCE: raises LockBroken if we lost the lock
                Path(counter_file).write_text(str(v + 1))
                done += 1
        except cc.LockBroken:
            continue  # broken mid-section; no mutation happened, retry


class LockMutualExclusionTests(unittest.TestCase):
    N_WORKERS = 8
    INCREMENTS = 5

    def _run_contention(self, lockdir: Path, counter: Path) -> int:
        counter.write_text("0")
        ctx = mp.get_context("spawn")
        procs = [
            ctx.Process(target=_contend_worker, args=(str(lockdir), str(counter), self.INCREMENTS))
            for _ in range(self.N_WORKERS)
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=120)
            self.assertEqual(p.exitcode, 0, "a contending worker crashed")
        return int(counter.read_text())

    def test_mutual_exclusion_no_lost_updates(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            final = self._run_contention(base / "lock.d", base / "counter")
            self.assertEqual(final, self.N_WORKERS * self.INCREMENTS)

    def test_stale_break_preserves_mutual_exclusion(self):
        # Pre-plant a STALE lock (old mtime, foreign token). Contention must still be exact.
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            lockdir = base / "lock.d"
            lockdir.mkdir()
            (lockdir / "meta.json").write_text(json.dumps({"owner_token": "dead-owner"}))
            old = time.time() - 10_000
            os.utime(lockdir, (old, old))
            final = self._run_contention(lockdir, base / "counter")
            self.assertEqual(final, self.N_WORKERS * self.INCREMENTS)

    def test_stale_regime_with_fencing_no_lost_updates(self):
        # Repeated live breaking (sections exceed ttl); fencing + retry must keep it exact.
        # A lost update would mean rename-capture release/break deleted a valid lock.
        n, inc = 4, 2
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            counter = base / "counter"
            counter.write_text("0")
            ctx = mp.get_context("spawn")
            procs = [
                ctx.Process(target=_stale_regime_worker, args=(str(base / "lock.d"), str(counter), inc))
                for _ in range(n)
            ]
            for p in procs:
                p.start()
            for p in procs:
                p.join(timeout=180)
                self.assertEqual(p.exitcode, 0, "a stale-regime worker crashed/hung")
            self.assertEqual(int(counter.read_text()), n * inc)


class FencedReleaseTests(unittest.TestCase):
    def test_release_removes_only_own(self):
        with tempfile.TemporaryDirectory() as d:
            lockdir = Path(d) / "lock.d"
            lockdir.mkdir()
            (lockdir / "meta.json").write_text(json.dumps({"owner_token": "T"}))
            self.assertTrue(cc._fenced_release(lockdir, "T"))
            self.assertFalse(lockdir.exists())

    def test_release_refuses_foreign_fast_path(self):
        with tempfile.TemporaryDirectory() as d:
            lockdir = Path(d) / "lock.d"
            lockdir.mkdir()
            (lockdir / "meta.json").write_text(json.dumps({"owner_token": "OTHER"}))
            self.assertFalse(cc._fenced_release(lockdir, "T"))
            self.assertTrue(lockdir.exists())  # removed nothing of another's

    def test_release_restores_when_captured_dir_is_foreign(self):
        # Deterministically drive the TOCTOU window: fast-path reads OUR token, but the
        # captured dir turns out foreign -> must restore it and report not-released.
        with tempfile.TemporaryDirectory() as d:
            lockdir = Path(d) / "lock.d"
            lockdir.mkdir()
            (lockdir / "meta.json").write_text(json.dumps({"owner_token": "whatever"}))
            with mock.patch(
                "collab_common._read_json_or_none",
                side_effect=[{"owner_token": "T"}, {"owner_token": "foreign"}],
            ):
                self.assertFalse(cc._fenced_release(lockdir, "T"))
            self.assertTrue(lockdir.exists())  # restored, not deleted


class LivenessTests(unittest.TestCase):
    def test_acquire_breaks_metaless_dir_after_ttl(self):
        # A process that mkdir-won then died before writing meta.json: a waiter must block
        # ~ttl (can't tell it from a live acquirer) then break and acquire. Bounded, not forever.
        with tempfile.TemporaryDirectory() as d:
            lockdir = Path(d) / "lock.d"
            lockdir.mkdir()  # fresh mtime, NO meta.json
            t0 = time.monotonic()
            with cc.collab_lock(lockdir, ttl=0.2, acquire_timeout=5.0) as h:
                waited = time.monotonic() - t0
                self.assertGreaterEqual(waited, 0.2)  # waited ~ttl before breaking
                self.assertTrue(h.is_current())


class FencingTests(unittest.TestCase):
    def test_assert_current_raises_after_takeover(self):
        with tempfile.TemporaryDirectory() as d:
            lockdir = Path(d) / "lock.d"
            with self.assertRaises(cc.LockBroken), cc.collab_lock(lockdir, ttl=100.0) as h:
                h.assert_current()  # ours: fine
                # Simulate a newer owner stamping a different token.
                cc.atomic_write(h.meta_path, json.dumps({"owner_token": "someone-else"}))
                h.assert_current()  # must raise now

    def test_release_does_not_delete_newer_owners_lock(self):
        with tempfile.TemporaryDirectory() as d:
            lockdir = Path(d) / "lock.d"
            with self.assertRaises(cc.LockBroken), cc.collab_lock(lockdir, ttl=100.0) as h:
                cc.atomic_write(h.meta_path, json.dumps({"owner_token": "newer-owner"}))
            # Fenced release removed nothing: the "newer owner's" lock dir survives.
            self.assertTrue(lockdir.exists())
            self.assertEqual(json.loads((lockdir / "meta.json").read_text())["owner_token"], "newer-owner")

    def test_acquire_timeout(self):
        with tempfile.TemporaryDirectory() as d:
            lockdir = Path(d) / "lock.d"
            lockdir.mkdir()  # fresh foreign lock, never released
            (lockdir / "meta.json").write_text(json.dumps({"owner_token": "held"}))
            with self.assertRaises(cc.LockTimeout), cc.collab_lock(lockdir, ttl=1000.0, acquire_timeout=0.3):
                pass

    def test_recompetes_when_broken_in_mkdir_meta_window(self):
        # Gap 3: a breaker rips lockdir away between mkdir and the meta commit. atomic_write
        # then raises FileNotFoundError -> the lock must RE-COMPETE, not crash.
        calls = {"n": 0}
        real = cc.atomic_write

        def flaky(path, data):
            calls["n"] += 1
            if calls["n"] == 1:
                cc._robust_rmtree(Path(path).parent)  # mimic the breaker removing lockdir
                raise FileNotFoundError("lockdir ripped away in mkdir->meta window")
            return real(path, data)

        with tempfile.TemporaryDirectory() as d:
            lockdir = Path(d) / "lock.d"
            with (
                mock.patch("collab_common.atomic_write", side_effect=flaky),
                cc.collab_lock(lockdir, ttl=30.0, acquire_timeout=5.0) as h,
            ):
                self.assertTrue(h.is_current())  # re-competed and truly acquired
            self.assertGreaterEqual(calls["n"], 2)  # first attempt failed, second succeeded


class AtomicWriteTests(unittest.TestCase):
    def test_no_partial_and_no_tmp_leftover_on_failure(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "f.json"
            p.write_text("OLD")
            with (
                mock.patch("collab_common.os.replace", side_effect=OSError("boom")),
                self.assertRaises(OSError),
            ):
                cc.atomic_write(p, "NEW" * 10_000)
            self.assertEqual(p.read_text(), "OLD")  # original intact
            leftovers = [x for x in Path(d).iterdir() if ".tmp." in x.name]
            self.assertEqual(leftovers, [], "temp file leaked on failure")

    def test_safe_write_retries_then_raises_collaberror(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "f"
            with (
                mock.patch("collab_common.atomic_write", side_effect=PermissionError("locked")),
                self.assertRaises(cc.CollabError),
            ):
                cc.safe_write(p, "x", retries=3, backoff=0.0)

    def test_safe_write_succeeds_after_transient(self):
        calls = {"n": 0}
        real = cc.atomic_write

        def flaky(path, data):
            calls["n"] += 1
            if calls["n"] < 3:
                raise PermissionError("briefly open")
            return real(path, data)

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "f"
            with mock.patch("collab_common.atomic_write", side_effect=flaky):
                cc.safe_write(p, "done", retries=5, backoff=0.0)
            self.assertEqual(p.read_text(), "done")


class ExclusiveCreateCommitTests(unittest.TestCase):
    """Commit-primitive invariant: destination is absent or COMPLETE, never empty/partial."""

    @staticmethod
    def _tmp_siblings(d: Path):
        return [x for x in d.iterdir() if ".tmp." in x.name]

    def test_1_complete_publish(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            p = d / "id-001"
            cc.exclusive_create(p, "hello-payload")
            self.assertEqual(p.read_text(), "hello-payload")
            self.assertEqual(self._tmp_siblings(d), [])  # temp removed

    def test_2_existing_destination_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            p = d / "id-001"
            cc.exclusive_create(p, "first")
            with self.assertRaises(FileExistsError):
                cc.exclusive_create(p, "second")
            self.assertEqual(p.read_text(), "first")  # unchanged
            self.assertEqual(self._tmp_siblings(d), [])  # temp removed

    def test_3_partial_short_writes_loop_to_completion(self):
        real_write = os.write

        def short(fd, b):
            return real_write(fd, bytes(b[:1]))  # force 1-byte writes

        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            p = d / "id-001"
            with mock.patch("collab_common.os.write", side_effect=short):
                cc.exclusive_create(p, "abcdef")
            self.assertEqual(p.read_text(), "abcdef")  # all bytes written despite short writes
            self.assertEqual(self._tmp_siblings(d), [])

    def test_4_write_failure_before_publish(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            p = d / "id-001"
            with (
                mock.patch("collab_common.os.write", side_effect=OSError("disk full")),
                self.assertRaises(OSError),
            ):
                cc.exclusive_create(p, "data")
            self.assertFalse(p.exists())  # final never appeared
            self.assertEqual(self._tmp_siblings(d), [])  # temp cleaned

    def test_5_fsync_failure_before_publish(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            p = d / "id-001"
            with (
                mock.patch("collab_common.os.fsync", side_effect=OSError("no fsync")),
                self.assertRaises(OSError),
            ):
                cc.exclusive_create(p, "data")
            self.assertFalse(p.exists())
            self.assertEqual(self._tmp_siblings(d), [])

    def test_6_link_failure_surfaced_as_collaberror(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            p = d / "id-001"
            with (
                mock.patch("collab_common.os.link", side_effect=OSError("no hardlink support")),
                self.assertRaises(cc.CollabError),
            ):
                cc.exclusive_create(p, "data")
            self.assertFalse(p.exists())  # final never appeared
            self.assertEqual(self._tmp_siblings(d), [])  # temp cleaned

    def test_7_crash_after_link_before_unlink_leaves_complete_final(self):
        # Documented residual: a crash between the link and the temp unlink leaves a COMPLETE
        # final file plus a temp leak. Acceptable — cleanup debt, never corruption.
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            p = d / "id-001"
            with mock.patch("collab_common._best_effort_unlink") as noop_unlink:
                cc.exclusive_create(p, "committed")
            self.assertEqual(p.read_text(), "committed")  # final is complete
            self.assertTrue(self._tmp_siblings(d))  # temp leaked (acceptable)
            noop_unlink.assert_called()

    def test_9_write_all_raises_on_no_progress(self):
        # Defensive: os.write returning 0 on a non-empty buffer must not spin forever.
        with tempfile.TemporaryDirectory() as d:
            fd = os.open(str(Path(d) / "f"), os.O_CREAT | os.O_WRONLY)
            try:
                with mock.patch("collab_common.os.write", return_value=0), self.assertRaises(cc.CollabError):
                    cc._write_all(fd, b"nonempty")
            finally:
                os.close(fd)

    def test_8_newline_payload_is_byte_exact(self):
        # Regression: Windows text-mode fd would translate \n -> \r\n and corrupt records.
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            p = d / "id-001-slug.md"
            body = 'to: reviewer\nfrom: builder\n{"k": 1}\n'
            cc.exclusive_create(p, body)
            self.assertEqual(p.read_bytes(), body.encode("utf-8"))  # byte-exact
            self.assertNotIn(b"\r\n", p.read_bytes())


class SlugifyTests(unittest.TestCase):
    def test_table(self):
        cases = {
            "ibkr-auto-trader": "ibkr-auto-trader",
            "Hello World": "hello-world",
            "../../etc/passwd": "etc-passwd",
            "..\\..\\windows": "windows",
            "café": "cafe",
            "  spaced  ": "spaced",
            "UPPER_snake.Case": "upper-snake-case",
            "a///b": "a-b",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(cc.slugify(raw), expected)

    def test_rejects_empty(self):
        for raw in ("", "   ", "..", "///", "!!!"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                cc.slugify(raw)

    def test_mangles_reserved_windows_names(self):
        for raw in ("CON", "nul", "COM1", "LPT9", "AuX"):
            with self.subTest(raw=raw):
                slug = cc.slugify(raw)
                self.assertNotIn(slug, cc._WIN_RESERVED)
                # 'reserved--' prefix is collision-free (a double-dash can't occur in a
                # normal slug, where non-alnum runs collapse to a single '-').
                self.assertTrue(slug.startswith("reserved--"))
                self.assertRegex(slug, r"^[a-z0-9][a-z0-9-]*$")

    def test_output_always_valid(self):
        for raw in ("a b c", "---x---", "Ünïcödé", "1", "z9-9z"):
            with self.subTest(raw=raw):
                self.assertRegex(cc.slugify(raw), r"^[a-z0-9][a-z0-9-]*$")


class PathResolutionTests(unittest.TestCase):
    def test_resolve_kit_root_walks_up_to_signature(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "tools" / "lib").mkdir(parents=True)
            (root / "install.sh").write_text("#!/bin/sh\n")
            # Starting from the deep lib dir, we should walk up to `root`.
            self.assertEqual(cc.resolve_kit_root(start=root / "tools" / "lib"), root.resolve())

    def test_resolve_kit_root_honors_marker(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "tools").mkdir()
            (root / ".collab-kit").write_text("")
            self.assertEqual(cc.resolve_kit_root(start=root / "tools"), root.resolve())

    def test_resolve_kit_root_raises_when_absent(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("COLLAB_KIT_ROOT", None)
            with self.assertRaises(cc.CollabError):
                cc.resolve_kit_root(start=Path(d))

    def test_resolve_kit_root_env_override_wins(self):
        # install.sh embeds COLLAB_KIT_ROOT; it must win over runtime signature-walk
        # (the robust path on Windows where symlink resolution is unreliable).
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "kit"
            root.mkdir()
            with mock.patch.dict(os.environ, {"COLLAB_KIT_ROOT": str(root)}):
                # start= points somewhere with NO signature; env override must still win.
                self.assertEqual(cc.resolve_kit_root(start=Path(d)), root.resolve())

    def test_collab_home_env_override_is_canonicalized(self):
        with tempfile.TemporaryDirectory() as d:
            sub = Path(d) / "home"
            sub.mkdir()
            with mock.patch.dict(os.environ, {"COLLAB_HOME": str(sub)}):
                self.assertEqual(cc.resolve_collab_home(), sub.resolve())

    @unittest.skipUnless(sys.platform == "win32", "MSYS /c/.. path form is Windows-only")
    def test_collab_home_msys_form_normalized(self):
        # A Git-Bash-provided COLLAB_HOME like /c/Users/.. must resolve to the SAME dir as
        # its native C:\Users\.. spelling (DEFECT A: else it becomes bogus C:\c\Users\..).
        with tempfile.TemporaryDirectory() as d:
            p = Path(d).resolve()
            msys = f"/{p.drive[0].lower()}{p.as_posix()[2:]}"  # 'C:/Users/..' -> '/c/Users/..'
            with mock.patch.dict(os.environ, {"COLLAB_HOME": msys}):
                self.assertEqual(cc.resolve_collab_home(), p)


class TestLoadDotenv(unittest.TestCase):
    def test_parses_export_and_quotes(self):
        with tempfile.TemporaryDirectory() as d:
            envf = Path(d) / ".env"
            envf.write_text('# comment\nexport A_KEY="v1"\nB_KEY=v2\nnot-a-pair-line\n', encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("A_KEY", None)
                os.environ.pop("B_KEY", None)
                cc.load_dotenv(envf)
                self.assertEqual(os.environ.get("A_KEY"), "v1")  # `export ` + surrounding quotes stripped
                self.assertEqual(os.environ.get("B_KEY"), "v2")

    def test_real_env_wins(self):
        with tempfile.TemporaryDirectory() as d:
            envf = Path(d) / ".env"
            envf.write_text("C_KEY=file-value\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"C_KEY": "shell-value"}, clear=False):
                cc.load_dotenv(envf)
                self.assertEqual(os.environ.get("C_KEY"), "shell-value")  # setdefault: shell wins

    def test_missing_file_is_noop(self):
        with tempfile.TemporaryDirectory() as d:
            cc.load_dotenv(Path(d) / "nope.env")  # must not raise

    def test_collab_env_file_override(self):
        with tempfile.TemporaryDirectory() as d:
            envf = Path(d) / "custom.env"
            envf.write_text("D_KEY=from-override\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"COLLAB_ENV_FILE": str(envf)}, clear=False):
                os.environ.pop("D_KEY", None)
                cc.load_dotenv()  # no explicit path -> honors $COLLAB_ENV_FILE
                self.assertEqual(os.environ.get("D_KEY"), "from-override")


if __name__ == "__main__":
    unittest.main()
