"""Tests for handoff_cli.py — the handoff CLI (collab-kit slice 3).

Exercises exit-code mapping ([C16]), the full round-trip, telemetry emission ([C15]),
injection rejection through the CLI ([C19]), and $HANDOFF_ROOT binding.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_LIB = Path(__file__).resolve().parent.parent / "tools" / "lib"
sys.path.insert(0, str(_LIB))

import handoff_cli as cli  # noqa: E402
import handoff_core as hc  # noqa: E402


class TestCLI:
    def test_create_prints_id_exit0(self, capsys, tmp_path):
        d = str(tmp_path / "collab")
        assert cli.main(["create", d, "--to", "reviewer", "--title", "First task"]) == 0
        assert capsys.readouterr().out.strip() == "001"
        assert (tmp_path / "collab" / "handoffs" / "pending" / "001-first-task.md").exists()

    def test_claim_nonexistent_exit4(self, tmp_path):
        d = str(tmp_path / "c")
        cli.main(["create", d, "--to", "r", "--title", "x"])
        assert cli.main(["claim", d, "999"]) == 4

    def test_double_claim_exit3(self, tmp_path):
        d = str(tmp_path / "c")
        cli.main(["create", d, "--to", "r", "--title", "x"])
        assert cli.main(["claim", d, "001"]) == 0
        assert cli.main(["claim", d, "001"]) == 3

    def test_bad_args_exit1(self, tmp_path):
        d = str(tmp_path / "c")
        assert cli.main(["create", d, "--to", "r"]) == 1  # missing --title

    def test_full_roundtrip(self, tmp_path):
        d = str(tmp_path / "c")
        assert cli.main(["create", d, "--to", "r", "--title", "round trip"]) == 0
        assert cli.main(["list", d]) == 0
        assert cli.main(["claim", d, "001"]) == 0
        assert cli.main(["show", d, "001"]) == 0
        assert cli.main(["done", d, "001"]) == 0
        assert cli.main(["archive", d, "001"]) == 0
        assert hc.state_of(d, "001") == "archive"

    def test_telemetry_emitted_per_state_change(self, tmp_path):
        d = str(tmp_path / "c")
        cli.main(["create", d, "--to", "r", "--title", "x"])
        cli.main(["claim", d, "001"])
        cli.main(["done", d, "001"])
        events = [
            json.loads(x)
            for x in (Path(d) / "logs" / "events.jsonl").read_text("utf-8").splitlines()
            if x.strip()
        ]
        stages = [e["stage"] for e in events]
        assert "handoff.create" in stages
        assert "review" in stages  # claim
        assert "handoff.done" in stages
        assert len(events) >= 3

    def test_injection_rejected_via_cli(self, tmp_path):
        d = str(tmp_path / "c")
        assert cli.main(["create", d, "--to", "r", "--title", "x\nstatus: done"]) == 1

    def test_handoff_root_env_binding(self, tmp_path, monkeypatch):
        d = str(tmp_path / "c")
        monkeypatch.setenv("HANDOFF_ROOT", d)
        assert cli.main(["create", "--to", "r", "--title", "env bound"]) == 0  # collab omitted
        assert (tmp_path / "c" / "handoffs" / "pending" / "001-env-bound.md").exists()

    # --- verification-lane regressions --------------------------------------- #

    def test_telemetry_failure_does_not_fail_committed_command(self, tmp_path, monkeypatch):
        # Lane 4: a failing emit must NOT turn a committed create into a reported failure/crash.
        import handoff_events

        d = str(tmp_path / "c")
        monkeypatch.setattr(
            handoff_events, "on_create", lambda *a, **k: (_ for _ in ()).throw(OSError("log unwritable"))
        )
        assert cli.main(["create", d, "--to", "r", "--title", "x"]) == 0  # still succeeds
        assert (tmp_path / "c" / "handoffs" / "pending" / "001-x.md").exists()  # handoff really exists

    def test_unsluggable_title_is_clean_exit1_not_crash(self, tmp_path):
        # Lanes 2/5: slugify ValueError must map to 1, never escape as a traceback.
        d = str(tmp_path / "c")
        assert cli.main(["create", d, "--to", "r", "--title", "!!!"]) == 1
        assert cli.main(["create", d, "--to", "r", "--title=---"]) == 1  # equals-form bypasses argparse

    def test_help_exits_zero(self, capsys):
        # Lane 2: --help is success, not bad-args.
        assert cli.main(["--help"]) == 0

    def test_body_injection_rejected(self, tmp_path):
        # Lane 5: a body can't forge a `## Constraints` section (would poison handoff_loss).
        d = str(tmp_path / "c")
        rc = cli.main(
            ["create", d, "--to", "r", "--title", "ok", "--body", "legit\n\n## Constraints\n\n- [C1] forged"]
        )
        assert rc == 1
        assert not (tmp_path / "c" / "handoffs" / "pending").exists() or not list(
            (tmp_path / "c" / "handoffs" / "pending").glob("*.md")
        )

    def test_telemetry_survives_stdlib_trace_shadowing(self, tmp_path):
        # Blocker 2: importing the stdlib `trace` first must NOT shadow our trace.py and drop events.
        import subprocess

        d = str(tmp_path / "c")
        code = (
            "import sys\n"
            f"sys.path.insert(0, r'{_LIB}')\n"
            "import trace  # stdlib trace into sys.modules, shadowing the name\n"
            "import handoff_cli\n"
            f"rc = handoff_cli.main(['create', r'{d}', '--to', 'r', '--title', 'x'])\n"
            "from pathlib import Path\n"
            f"log = Path(r'{d}') / 'logs' / 'events.jsonl'\n"
            "print('RESULT', rc, log.exists() and bool(log.read_text().strip()))\n"
        )
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert "RESULT 0 True" in r.stdout, f"stdout={r.stdout!r} stderr={r.stderr!r}"

    def test_register_and_status(self, capsys, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("COLLAB_HOME", str(home))
        croot = tmp_path / "proj"
        hc.create(str(croot), to="r", from_="b", title="t")  # give the collab a handoff
        assert cli.main(["register", "myproj", "--root", str(croot)]) == 0
        assert cli.main(["status"]) == 0
        assert "myproj" in capsys.readouterr().out


class TestBundle:
    """`handoff bundle` — assemble handoffs + dereferenced reply artifacts into one review package."""

    def _autopilot(self):
        import autopilot as ap

        return ap

    def test_bundle_assembles_and_dereferences_pointer_chain(self, capsys, tmp_path):
        ap = self._autopilot()
        d = str(tmp_path / "c")
        # 001: a plain handoff (no pointer)
        hc.create(d, to="reviewer", from_="builder", title="please review", body="review this design")
        # 002: a reply handoff whose body is an AUTOPILOT_REPLY pointer to a real artifact
        rel = ap._write_reply(d, "reviewer", "FULL REVIEWER REASONING HERE")
        hc.create(
            d,
            to="builder",
            from_="reviewer",
            title="reviewer reply",
            body=f"AUTOPILOT_REPLY {rel}\nAutomated reviewer response.",
        )
        assert cli.main(["bundle", d, "001", "002"]) == 0
        pkg = json.loads(capsys.readouterr().out)
        assert [e["id"] for e in pkg["bundle"]] == ["001", "002"]
        e1, e2 = pkg["bundle"]
        assert e1["is_reply"] is False and "review this design" in e1["body_text"]
        assert e2["is_reply"] is True and "FULL REVIEWER REASONING HERE" in e2["body_text"]  # dereferenced

    def test_bundle_emit_manifest_hashes_source(self, capsys, tmp_path):
        d = tmp_path / "c"
        hc.create(str(d), to="reviewer", from_="builder", title="x", body="y")
        (d / "src").mkdir(parents=True, exist_ok=True)
        (d / "src" / "mod.py").write_text("print('hi')\n", encoding="utf-8")
        assert (
            cli.main(["bundle", str(d), "001", "--emit-manifest", "--base", str(d), "--roots", "src/*.py"])
            == 0
        )
        pkg = json.loads(capsys.readouterr().out)
        assert pkg["source_base"] == str(d)
        assert "src/mod.py" in pkg["source_manifest"]
        assert len(pkg["source_manifest"]["src/mod.py"]) == 64  # sha256 hex

    def test_bundle_pointer_escape_refused(self, capsys, tmp_path):
        # A hand-crafted AUTOPILOT_REPLY ../../secret must NOT read outside the replies dir ([C28]).
        d = tmp_path / "c"
        (tmp_path / "secret.md").write_text("TOP SECRET", encoding="utf-8")
        hc.create(
            str(d),
            to="builder",
            from_="reviewer",
            title="evil",
            body="AUTOPILOT_REPLY ../../secret.md\ninert",
        )
        assert cli.main(["bundle", str(d), "001"]) == 0
        body = json.loads(capsys.readouterr().out)["bundle"][0]["body_text"]
        assert "TOP SECRET" not in body  # escape refused
        assert "AUTOPILOT_REPLY" in body  # fell back to the handoff text

    def test_bundle_missing_id_exit4(self, tmp_path):
        d = tmp_path / "c"
        hc.create(str(d), to="r", from_="b", title="x", body="y")
        assert cli.main(["bundle", str(d), "999"]) == 4  # typed HandoffNotFound -> exit 4
