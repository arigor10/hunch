"""Centralized append-one-JSON-line helper for the replay buffer.

All append writes to replay-buffer JSONL files (conversation, artifacts,
hunches, feedback) go through this helper so the append-only invariant
is enforced under concurrent writers — framework, UserPromptSubmit
hook, side panel, future agentic Critic.

The concern: POSIX does not guarantee that a single `write(2)` of an
arbitrary size is atomic on a regular file across all filesystems.
Modern Linux local filesystems (ext4, xfs, btrfs) do serialize
regular-file appends via inode locks, so a Python `f.write(line)` that
translates to one `write(2)` call won't interleave with another
process's write. But:

- NFS does not guarantee this.
- Python's buffered I/O can, under conditions (partial writes, very
  large payloads), split one Python-level write into multiple
  `write(2)` syscalls; each is atomic but they can interleave across
  writers.
- macOS APFS and FUSE filesystems have weaker guarantees than local
  Linux ext4.

We take an exclusive advisory `fcntl.flock(LOCK_EX)` around the
serialize-and-write block so writers serialize across the entire
append regardless of filesystem or buffering behavior. Readers are
not blocked; they still see whole lines only.

Advisory: writers that bypass this helper defeat the serialization
for everyone. Keep replay-buffer JSONL writes funneling through here.

fcntl.flock is Linux + macOS; Hunch targets Unix per framework_v0.md.
"""

from __future__ import annotations

import fcntl
import json
from pathlib import Path
from typing import Any


def append_json_line(path: Path, entry: dict[str, Any]) -> None:
    """Serialize `entry` as one JSON line and append it to `path` under
    an exclusive advisory file lock. See module docstring for details.
    """
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(line)
            f.flush()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
