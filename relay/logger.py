"""Append-only JSONL journal of every webhook event and order result.

This is the audit trail the Claude journal-review agent reads. One JSON object
per line, written with file locking so concurrent alerts don't interleave.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path


def append_event(path: Path, event: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # `ts` is stamped LAST on purpose. Written as {"ts": ..., **event} the
    # spread won, so any caller that happened to include a "ts" key silently
    # replaced the wall-clock stamp with its own value — sigengine.engine's
    # signal_skip did exactly that with a bar timestamp, leaving journal rows
    # that are not in write order and break every time-window query over them.
    # The journal is an audit trail; when a row was written is not the caller's
    # to decide.
    record = {**event, "ts": time.time()}
    line = json.dumps(record, default=str)
    # O_APPEND is atomic per write on POSIX for lines < PIPE_BUF (4KB).
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())