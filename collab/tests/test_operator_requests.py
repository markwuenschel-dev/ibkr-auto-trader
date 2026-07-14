"""Tests for operator_requests — the durable retry/adopt queue the dashboard writes and the driver consumes.

Pins the file protocol (one open request per hid, latest supersedes), path-safety (a hostile id can never
escape the requests dir), tolerant reads (a torn/unknown file is skipped, never a crash), and the
consume-then-gone semantics.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "tools" / "lib"
sys.path.insert(0, str(_LIB))

import operator_requests as opreq  # noqa: E402


class TestWriteReadConsume:
    def test_write_get_roundtrip(self, tmp_path):
        collab = str(tmp_path / "c")
        rec = opreq.write(collab, "030", opreq.RETRY, by="dashboard-web", note="give it another go")
        assert rec["hid"] == "030" and rec["action"] == "retry" and rec["requested_by"] == "dashboard-web"
        got = opreq.get(collab, "030")
        assert got["action"] == "retry" and got["note"] == "give it another go"

    def test_pending_sorted_by_numeric_id(self, tmp_path):
        collab = str(tmp_path / "c")
        opreq.write(collab, "9", opreq.RETRY)
        opreq.write(collab, "030", opreq.ADOPT)
        opreq.write(collab, "007", opreq.RETRY)
        # sorted by NUMERIC id (7 < 9 < 30), zero-padded slugs preserved verbatim
        assert [r["hid"] for r in opreq.pending(collab)] == ["007", "9", "030"]

    def test_latest_supersedes(self, tmp_path):
        collab = str(tmp_path / "c")
        opreq.write(collab, "030", opreq.RETRY)
        opreq.write(collab, "030", opreq.ADOPT)  # supersedes
        assert opreq.get(collab, "030")["action"] == "adopt"
        assert len(opreq.pending(collab)) == 1

    def test_consume_removes_it(self, tmp_path):
        collab = str(tmp_path / "c")
        opreq.write(collab, "030", opreq.RETRY)
        assert opreq.consume(collab, "030") is True
        assert opreq.get(collab, "030") is None
        assert opreq.consume(collab, "030") is False  # idempotent — already gone


class TestRobustness:
    def test_bad_hid_rejected(self, tmp_path):
        collab = str(tmp_path / "c")
        for bad in ("../x", "a/b", "", "abc", "1234567890"):  # non-digit / too long / path fragment
            with pytest.raises(opreq.BadRequest):
                opreq.write(collab, bad, opreq.RETRY)

    def test_unknown_action_rejected(self, tmp_path):
        with pytest.raises(opreq.BadRequest):
            opreq.write(str(tmp_path / "c"), "030", "delete")

    def test_torn_or_unknown_file_skipped(self, tmp_path):
        collab = str(tmp_path / "c")
        d = Path(collab) / "autopilot" / "requests"
        d.mkdir(parents=True)
        (d / "030.json").write_text("{ not json", encoding="utf-8")  # torn
        (d / "031.json").write_text(
            json.dumps({"hid": "031", "action": "nope"}), encoding="utf-8"
        )  # bad action
        (d / "not-an-id.json").write_text(json.dumps({"action": "retry"}), encoding="utf-8")  # bad name
        assert opreq.get(collab, "030") is None
        assert opreq.pending(collab) == []  # all three skipped, no crash

    def test_missing_dir_is_empty(self, tmp_path):
        assert opreq.pending(str(tmp_path / "nope")) == []
        assert opreq.get(str(tmp_path / "nope"), "030") is None
