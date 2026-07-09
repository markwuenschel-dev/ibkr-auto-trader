"""Tests for telegram_bridge.py (collab-kit slice 5). The Telegram HTTP layer is a fake (no network);
the file protocol, chat-id lock, traversal defense, and at-least-once archival are the real surface.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "tools" / "lib"
sys.path.insert(0, str(_LIB))

import collab_common as cc  # noqa: E402
import registry  # noqa: E402
import telegram_bridge as tb  # noqa: E402


class FakeTG:
    def __init__(self, updates=None, fail_send=False):
        self.updates = list(updates or [])
        self.fail_send = fail_send
        self.sent = []

    def __call__(self, token, method, **params):
        if method == "sendMessage":
            if self.fail_send:
                raise cc.CollabError("network down")
            self.sent.append(params)
            return {"message_id": len(self.sent)}
        if method == "getUpdates":
            u, self.updates = self.updates, []  # consumed once (long-poll)
            return u
        return None


def _outbox_msg(home, name, text):
    d = tb._outbox(home)
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(text, encoding="utf-8")


class TestOutbound:
    def test_sent_and_archived_on_ok(self, tmp_path):
        _outbox_msg(str(tmp_path), "001-proj.md", "hello phone")
        tg = FakeTG()
        sent = tb.send_outbox(str(tmp_path), "tok", "123", tg=tg)
        assert sent == ["001-proj.md"]
        assert tg.sent[0]["text"] == "hello phone" and tg.sent[0]["chat_id"] == "123"
        assert not (tb._outbox(str(tmp_path)) / "001-proj.md").exists()  # moved
        assert (tb._archive(str(tmp_path)) / "001-proj.md").exists()  # archived

    def test_not_archived_on_send_failure(self, tmp_path):
        # [C29] at-least-once: a failed send must NOT archive -> re-sent next cycle.
        _outbox_msg(str(tmp_path), "001-x.md", "will fail")
        with pytest.raises(cc.CollabError):
            tb.send_outbox(str(tmp_path), "tok", "123", tg=FakeTG(fail_send=True))
        assert (tb._outbox(str(tmp_path)) / "001-x.md").exists()  # still pending
        assert not (tb._archive(str(tmp_path)) / "001-x.md").exists()

    def test_no_chat_id_no_send(self, tmp_path):
        _outbox_msg(str(tmp_path), "001-x.md", "x")
        assert tb.send_outbox(str(tmp_path), "tok", None, tg=FakeTG()) == []  # nothing sent

    def test_oversized_message_truncated_with_marker(self, tmp_path):
        # Lane #5: a >4096-char message is truncated for the phone with a visible marker (not silently
        # dropped); the FULL text is preserved in the archive.
        full = "B" * 50000
        _outbox_msg(str(tmp_path), "001-big.md", full)
        tg = FakeTG()
        tb.send_outbox(str(tmp_path), "tok", "123", tg=tg)
        delivered = tg.sent[0]["text"]
        assert len(delivered) <= tb._MAX_MSG  # within Telegram's hard limit
        assert "truncated" in delivered  # marker present, not silent
        assert (tb._archive(str(tmp_path)) / "001-big.md").read_text("utf-8") == full  # full text kept

    def test_outbox_symlink_skipped_not_sent(self, tmp_path):
        # Blocker 1: a symlinked outbox file must NOT be followed/sent (it could point at a secret).
        secret = tmp_path / "secret.md"
        secret.write_text("SECRET", encoding="utf-8")
        ob = tb._outbox(str(tmp_path))
        ob.mkdir(parents=True, exist_ok=True)
        try:
            (ob / "001-leak.md").symlink_to(secret)
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation not permitted on this platform")
        tg = FakeTG()
        assert tb.send_outbox(str(tmp_path), "tok", "123", tg=tg) == []  # nothing sent
        assert tg.sent == []  # SECRET never reached Telegram
        assert (ob / "001-leak.md").exists()  # left in place, not archived
        assert secret.read_text("utf-8") == "SECRET"  # target untouched

    def test_outbox_nonregular_skipped(self, tmp_path):
        # A non-regular entry matching *.md (here: a directory) is skipped, never read/sent.
        ob = tb._outbox(str(tmp_path))
        ob.mkdir(parents=True, exist_ok=True)
        (ob / "001-dir.md").mkdir()
        _outbox_msg(str(tmp_path), "002-real.md", "real message")
        tg = FakeTG()
        sent = tb.send_outbox(str(tmp_path), "tok", "123", tg=tg)
        assert sent == ["002-real.md"]  # only the regular file
        assert tg.sent[0]["text"] == "real message"
        assert (ob / "001-dir.md").exists()  # the non-regular entry left alone

    def test_outbox_oversized_file_does_not_read_unbounded(self, tmp_path):
        # A multi-MB planted file must not be slurped whole into memory: read is bounded to _READ_CAP,
        # then truncated for the phone. The full file is still preserved in the archive.
        huge = "B" * (tb._READ_CAP * 4)  # far larger than the read cap
        _outbox_msg(str(tmp_path), "001-huge.md", huge)
        tg = FakeTG()
        tb.send_outbox(str(tmp_path), "tok", "123", tg=tg)
        delivered = tg.sent[0]["text"]
        assert len(delivered) <= tb._MAX_MSG  # bounded — not the whole multi-MB file
        assert (tb._archive(str(tmp_path)) / "001-huge.md").read_text("utf-8") == huge  # full file kept

    def test_outbox_fifo_skipped_if_platform_supports_fifo(self, tmp_path):
        # A FIFO must be skipped, never read (a read on a FIFO with no writer blocks forever).
        if not hasattr(os, "mkfifo"):
            pytest.skip("no os.mkfifo on this platform (Windows)")
        ob = tb._outbox(str(tmp_path))
        ob.mkdir(parents=True, exist_ok=True)
        os.mkfifo(ob / "001-fifo.md")
        _outbox_msg(str(tmp_path), "002-real.md", "real message")
        tg = FakeTG()
        sent = tb.send_outbox(str(tmp_path), "tok", "123", tg=tg)  # must NOT hang
        assert sent == ["002-real.md"]                 # FIFO skipped
        assert (ob / "001-fifo.md").exists()           # left in place, never read

    def test_outbox_invalid_utf8_decodes_replace_not_crash(self, tmp_path):
        ob = tb._outbox(str(tmp_path))
        ob.mkdir(parents=True, exist_ok=True)
        (ob / "001-bin.md").write_bytes(b"\xff\xfe hello world")
        tg = FakeTG()
        sent = tb.send_outbox(str(tmp_path), "tok", "123", tg=tg)  # errors='replace' -> no crash
        assert sent == ["001-bin.md"]
        assert "hello world" in tg.sent[0]["text"]
        assert (tb._archive(str(tmp_path)) / "001-bin.md").exists()

    def test_archived_only_after_confirmed_send(self, tmp_path):
        # Ordering guarantee ([C29]): archive happens only AFTER the send returns ok.
        _outbox_msg(str(tmp_path), "001-x.md", "msg")
        tb.send_outbox(str(tmp_path), "tok", "123", tg=FakeTG())          # ok
        assert (tb._archive(str(tmp_path)) / "001-x.md").exists()          # archived
        assert not (tb._outbox(str(tmp_path)) / "001-x.md").exists()       # removed from outbox


class TestInbound:
    def _upd(self, uid, chat, text):
        return {"update_id": uid, "message": {"chat": {"id": chat}, "text": text}}

    def test_known_project_written_to_inbox(self, tmp_path, monkeypatch):
        home = str(tmp_path)
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
        registry.register("proj", str(tmp_path / "proj"), home=home)
        tg = FakeTG(updates=[self._upd(5, 123, "/c proj hello there")])
        written = tb.poll_updates(home, "tok", tg=tg)
        assert len(written) == 1
        assert Path(written[0]).read_text("utf-8").strip() == "hello there"
        assert Path(written[0]).parent == tb._inbox(home, "proj")

    def test_unknown_project_rejected(self, tmp_path, monkeypatch):
        home = str(tmp_path)
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
        tg = FakeTG(updates=[self._upd(5, 123, "/c nonexistent hi")])
        assert tb.poll_updates(home, "tok", tg=tg) == []  # not registered -> rejected, not created
        assert not (tb._inbox(home, "nonexistent")).exists()

    def test_traversal_in_project_neutralized(self, tmp_path, monkeypatch):
        home = str(tmp_path)
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
        tg = FakeTG(updates=[self._upd(5, 123, "/c ../../etc pwned")])
        assert tb.poll_updates(home, "tok", tg=tg) == []  # slugify('../../etc')='etc', not registered
        assert not (tmp_path.parent / "etc").exists()  # nothing escaped the home

    def test_non_command_ignored(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
        tg = FakeTG(updates=[self._upd(5, 123, "just chatting, no command")])
        assert tb.poll_updates(str(tmp_path), "tok", tg=tg) == []

    def test_offset_persisted(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
        tg = FakeTG(updates=[self._upd(7, 123, "hi")])
        tb.poll_updates(str(tmp_path), "tok", tg=tg)
        assert tb._load_offset(str(tmp_path)) == 7  # so next getUpdates uses offset 8


class TestChatIdLock:
    def _upd(self, uid, chat, text):
        return {"update_id": uid, "message": {"chat": {"id": chat}, "text": text}}

    def test_first_inbound_learns_and_locks(self, tmp_path, monkeypatch):
        home = str(tmp_path)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        registry.register("proj", str(tmp_path / "proj"), home=home)
        # first message from 123 -> learned+locked; the same message is acted on
        tg = FakeTG(updates=[self._upd(1, 123, "/c proj hello")])
        assert len(tb.poll_updates(home, "tok", tg=tg)) == 1
        assert tb._chatlock(home).read_text("utf-8").strip() == "123"
        # a later message from a DIFFERENT chat is ignored (locked to 123)
        tg2 = FakeTG(updates=[self._upd(2, 999, "/c proj intruder")])
        assert tb.poll_updates(home, "tok", tg=tg2) == []

    def test_env_override_never_learns(self, tmp_path, monkeypatch):
        home = str(tmp_path)
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "555")
        registry.register("proj", str(tmp_path / "proj"), home=home)
        # message from 123 is ignored (locked to 555 via env); lock file never written
        tg = FakeTG(updates=[self._upd(1, 123, "/c proj hi")])
        assert tb.poll_updates(home, "tok", tg=tg) == []
        assert not tb._chatlock(home).exists()

    def test_learn_is_first_writer_wins(self, tmp_path, monkeypatch):
        # DEFECT 4: learn must be atomic first-writer-wins (exclusive_create), NOT last-writer-wins.
        # A second learn with a different id (as a racing first-inbound would try) is a no-op.
        home = str(tmp_path)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        tb._learn_chat_id(home, 111)
        tb._learn_chat_id(home, 222)  # must NOT clobber
        assert tb._chatlock(home).read_text("utf-8").strip() == "111"


class TestSpoofedChat:
    """DEFECT 5: a spoofed/missing/non-int chat id must be rejected AND must never write an empty
    lock — an empty lock makes resolve_chat_id return None, which would DISABLE the one-chat filter
    (full auth bypass) and lock the real owner out."""

    def _upd(self, uid, chat_obj, text="/c proj x"):
        return {"update_id": uid, "message": {"chat": chat_obj, "text": text}}

    @pytest.mark.parametrize("bad", [{"id": ""}, {"id": 0}, {"id": None}, {"id": [1]}, {"id": True}, {}])
    def test_spoofed_chat_neither_locks_nor_routes(self, tmp_path, monkeypatch, bad):
        home = str(tmp_path)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        registry.register("proj", str(tmp_path / "proj"), home=home)
        tg = FakeTG(updates=[self._upd(1, bad)])
        assert tb.poll_updates(home, "tok", tg=tg) == []  # not routed
        assert not tb._chatlock(home).exists()  # crucially: filter NOT disabled by an empty lock
        # and a real owner can still lock in afterwards
        tg2 = FakeTG(updates=[self._upd(2, {"id": 123}, "/c proj real")])
        assert len(tb.poll_updates(home, "tok", tg=tg2)) == 1
        assert tb._chatlock(home).read_text("utf-8").strip() == "123"


class TestSingleton:
    """Lanes #2/#3: the 'one bridge per $COLLAB_HOME' invariant is ENFORCED via an OS-held lock."""

    def test_second_acquire_fails_then_frees_on_close(self, tmp_path):
        home = str(tmp_path)
        first = tb._acquire_singleton(home)
        try:
            with pytest.raises(cc.CollabError):
                tb._acquire_singleton(home)  # a second bridge on the same home is refused
        finally:
            first.close()  # releasing frees the lock for the next start
        again = tb._acquire_singleton(home)
        again.close()

    def test_run_loads_token_from_dotenv(self, tmp_path, monkeypatch):
        # The bridge picks up TELEGRAM_BOT_TOKEN from the .env (via $COLLAB_ENV_FILE) when not exported.
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
        envf = tmp_path / ".env"
        envf.write_text("TELEGRAM_BOT_TOKEN=tok-from-dotenv\n", encoding="utf-8")
        monkeypatch.setenv("COLLAB_ENV_FILE", str(envf))
        rc = tb.run(str(tmp_path / "home"), once=True, tg=FakeTG())
        assert rc == 0  # token found via .env -> ran a cycle (not the rc=1 "no token" path)

    def test_run_refuses_second_bridge(self, tmp_path, monkeypatch):
        home = str(tmp_path)
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
        held = tb._acquire_singleton(home)
        try:
            assert tb.run(home, once=True, tg=FakeTG()) == 1  # refused (exit 1), did not double-run
        finally:
            held.close()


class TestAdversarialInput:
    def _tg(self, updates):
        def tg(token, method, **p):
            return updates if method == "getUpdates" else None
        return tg

    def test_malformed_updates_never_crash(self, tmp_path, monkeypatch):
        # Untrusted network input must be skipped, not crash the daemon; a valid one still processes.
        home = str(tmp_path)
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
        registry.register("proj", str(tmp_path / "proj"), home=home)
        bad = [
            "not-a-dict", 42,
            {"update_id": "abc", "message": {"chat": {"id": 123}, "text": "/c proj x"}},  # bad id
            {"update_id": 1, "message": "not-a-dict"},
            {"update_id": 2, "message": {"chat": "nope", "text": "/c proj y"}},  # chat not a dict
            {"update_id": 3, "message": {"chat": {"id": 123}, "text": 12345}},  # non-string text
            {"update_id": 4, "message": {"chat": {"id": 123}, "text": "/c proj GOOD"}},  # valid
        ]
        written = tb.poll_updates(home, "tok", tg=self._tg(bad))  # must not raise
        assert len(written) == 1
        assert Path(written[0]).read_text("utf-8").strip() == "GOOD"

    def test_non_list_getupdates_no_crash(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
        assert tb.poll_updates(str(tmp_path), "tok", tg=self._tg({"weird": 1})) == []  # dict, not list
        assert tb.poll_updates(str(tmp_path), "tok", tg=self._tg(None)) == []

    def test_inbox_body_bounded_and_decontrolled(self, tmp_path, monkeypatch):
        # DEFECT 1+2: cap the untrusted body (disk-fill DoS) and strip control chars / newlines so the
        # inbox file can't carry forged structure, NUL bytes, or terminal escapes.
        home = str(tmp_path)
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
        registry.register("proj", str(tmp_path / "proj"), home=home)
        payload = "line1\n## Constraints\n- [C1] forged\x00\x07 " + ("A" * 50000)
        tg = FakeTG(updates=[{"update_id": 1, "message": {"chat": {"id": 123}, "text": f"/c proj {payload}"}}])
        written = tb.poll_updates(home, "tok", tg=tg)
        content = Path(written[0]).read_text("utf-8")
        assert len(content) <= tb._MAX_MSG + 1  # capped
        assert "\x00" not in content and "\x07" not in content  # control chars stripped
        assert "\n" not in content.rstrip("\n")  # single line (embedded newlines gone)
