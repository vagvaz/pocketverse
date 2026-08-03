"""File-based mailboxes on the shared dir — the agent comms plane.

Layout:  <shared_root>/mailbox/<recipient>/in.jsonl
One MailboxMessage JSON object per line. This is the ONLY coordination
plane between agents, the supervisor, and the human — keep it simple,
atomic, and inspectable with cat/tail/jq.

Writes are single os.write() calls under O_APPEND (atomic on local
filesystems for reasonable sizes — no locks). Reads are non-destructive
byte-offset cursor reads: the cursor (persisted by the caller) is the
consumption mechanism, giving at-least-once delivery without drains.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from .models import MailboxMessage

_log = logging.getLogger(__name__)

RECIPIENT_RE = r"^[A-Za-z0-9._-]+$"


def inbox_path(shared_root: Path, recipient: str) -> Path:
    """Path of recipient's inbox; validates the recipient name."""
    if not re.fullmatch(RECIPIENT_RE, recipient):
        raise ValueError(f"invalid recipient name: {recipient!r}")
    return Path(shared_root) / "mailbox" / recipient / "in.jsonl"


def append(
    shared_root: Path,
    recipient: str,
    msg_type: str,
    payload: dict,
    sender: str,
) -> MailboxMessage:
    """Atomically append a message; returns it with id/ts filled."""
    path = inbox_path(shared_root, recipient)
    path.parent.mkdir(parents=True, exist_ok=True)
    msg = MailboxMessage(
        id=uuid.uuid4().hex,
        sender=sender,
        ts=datetime.now(timezone.utc).isoformat(),
        type=msg_type,
        payload=payload,
    )
    line = json.dumps(msg.model_dump(mode="json"), separators=(",", ":")) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)
    return msg


def read(
    shared_root: Path,
    recipient: str,
    *,
    offset: int = 0,
) -> tuple[list[MailboxMessage], int]:
    """Read complete messages from byte offset; return (messages, new_offset).

    Stops before a trailing partial line (writer mid-append): the returned
    offset points at its start. Malformed complete lines are skipped with
    a logged warning. Missing inbox: ([], offset).
    """
    path = inbox_path(shared_root, recipient)
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read()
    except FileNotFoundError:
        return [], offset

    messages: list[MailboxMessage] = []
    pos = offset
    while True:
        nl = data.find(b"\n", pos - offset)
        if nl == -1:
            break  # no more complete lines
        line = data[pos - offset : nl]
        line_start = pos
        pos = offset + nl + 1
        line = line.strip()
        if not line:
            continue
        try:
            messages.append(MailboxMessage.model_validate(json.loads(line)))
        except Exception as exc:
            _log.warning("skipping malformed mailbox line at %d: %s", line_start, exc)
    return messages, pos


def follow(
    shared_root: Path,
    recipient: str,
    *,
    interval: float = 1.0,
    offset: int = 0,
) -> Iterator[MailboxMessage]:
    """Yield messages as they arrive (poll-based, non-destructive)."""
    cursor = offset
    while True:
        messages, cursor = read(shared_root, recipient, offset=cursor)
        yield from messages
        if not messages:
            time.sleep(interval)
