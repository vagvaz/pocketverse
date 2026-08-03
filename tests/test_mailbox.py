"""Tests for pocketverse.mailbox — file-based JSONL mailboxes."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from pocketverse import mailbox
from pocketverse.models import MailboxMessage


@pytest.fixture
def shared(tmp_path: Path) -> Path:
    return tmp_path / "shared"


class TestInboxPath:
    def test_valid(self, shared: Path) -> None:
        p = mailbox.inbox_path(shared, "agent-a.1")
        assert p == shared / "mailbox" / "agent-a.1" / "in.jsonl"

    @pytest.mark.parametrize("bad", ["../etc", "a/b", "a b", "a;b", ""])
    def test_invalid(self, shared: Path, bad: str) -> None:
        with pytest.raises(ValueError):
            mailbox.inbox_path(shared, bad)


class TestAppend:
    def test_creates_layout_and_fills_fields(self, shared: Path) -> None:
        msg = mailbox.append(shared, "alice", "question", {"text": "hi"}, "bob")
        assert msg.id and msg.ts
        assert msg.sender == "bob"
        path = mailbox.inbox_path(shared, "alice")
        lines = path.read_text().splitlines()
        assert len(lines) == 1
        stored = MailboxMessage.model_validate(json.loads(lines[0]))
        assert stored == msg

    def test_atomicity_under_concurrency(self, shared: Path) -> None:
        """8 threads x 25 single-write appends: 200 valid messages, no tears."""
        def worker(n: int) -> None:
            for i in range(25):
                mailbox.append(shared, "all", "progress",
                               {"worker": n, "i": i}, f"w{n}")

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        raw = mailbox.inbox_path(shared, "all").read_bytes()
        assert raw.endswith(b"\n")  # no torn final line
        lines = raw.decode().splitlines()
        assert len(lines) == 200
        seen = set()
        for line in lines:
            m = MailboxMessage.model_validate(json.loads(line))
            seen.add((m.payload["worker"], m.payload["i"]))
        assert len(seen) == 200


class TestRead:
    def test_missing_inbox(self, shared: Path) -> None:
        msgs, off = mailbox.read(shared, "nobody")
        assert msgs == [] and off == 0

    def test_cursor_reads_only_new(self, shared: Path) -> None:
        mailbox.append(shared, "a", "t", {"n": 1}, "s")
        msgs, off = mailbox.read(shared, "a")
        assert [m.payload["n"] for m in msgs] == [1]
        mailbox.append(shared, "a", "t", {"n": 2}, "s")
        msgs2, off2 = mailbox.read(shared, "a", offset=off)
        assert [m.payload["n"] for m in msgs2] == [2]
        msgs3, off3 = mailbox.read(shared, "a", offset=off2)
        assert msgs3 == [] and off3 == off2

    def test_trailing_partial_line(self, shared: Path) -> None:
        m = mailbox.append(shared, "a", "t", {"n": 1}, "s")
        path = mailbox.inbox_path(shared, "a")
        partial = b'{"id":"deadbeef","sender":"s","ts":"'
        with open(path, "ab") as f:
            f.write(partial)
        msgs, off = mailbox.read(shared, "a")
        assert [x.id for x in msgs] == [m.id]
        assert off == path.stat().st_size - len(partial)
        # complete the line; read from cursor gets the rest
        rest = b'2026-01-01","type":"t","payload":{"n":2}}\n'
        with open(path, "ab") as f:
            f.write(rest)
        msgs2, off2 = mailbox.read(shared, "a", offset=off)
        assert [x.payload["n"] for x in msgs2] == [2]
        assert off2 == path.stat().st_size

    def test_malformed_complete_line_skipped(self, shared: Path) -> None:
        mailbox.append(shared, "a", "t", {"n": 1}, "s")
        path = mailbox.inbox_path(shared, "a")
        with open(path, "ab") as f:
            f.write(b"this is not json\n")
        mailbox.append(shared, "a", "t", {"n": 2}, "s")
        msgs, off = mailbox.read(shared, "a")
        assert [m.payload["n"] for m in msgs] == [1, 2]
        assert off == path.stat().st_size


class TestFollow:
    def test_yields_late_message(self, shared: Path) -> None:
        gen = mailbox.follow(shared, "a", interval=0.05)
        time.sleep(0.05)
        mailbox.append(shared, "a", "t", {"n": 1}, "s")
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            msg = next(gen)
            if msg is not None:
                assert msg.payload["n"] == 1
                return
        pytest.fail("follow did not yield the appended message")
