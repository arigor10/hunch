"""Centralized append-one-JSON-line helper for the replay buffer.

All append writes to replay-buffer JSONL files (conversation,
artifacts, hunches, feedback) go through this helper so there is
exactly one place to enforce append-only semantics under concurrent
writers — framework, UserPromptSubmit hook, side panel, future
agentic Critic.

Concurrency posture: on mainstream Linux local filesystems (ext4,
xfs, btrfs) the kernel already serializes regular-file appends via
inode locks, so a Python `f.write(line)` that translates to one
`write(2)` call cannot interleave with another process's write. We
verified this empirically — 8 writers × 100 × 16KB lines without any
locking produced zero corrupt JSON.

We still take an exclusive advisory `fcntl.flock(LOCK_EX)` around
the write for two reasons:

1. Concentration. Funnelling all writers through one locking point
   means a single place to strengthen guarantees if we ever run on a
   filesystem where the ext4 guarantee doesn't hold, or move to a
   format that needs more than `O_APPEND` atomicity.
2. Short-write retry. Python's `BufferedWriter` will retry a
   partial `write(2)`; the lock keeps those retries contiguous so a
   second writer can't slip a line into the middle.

Caveats, honestly:

- `flock` is advisory. Readers that open the file without
  `flock(LOCK_SH)` can still see torn lines on filesystems without
  atomic writes. v0 readers (side panel, ad-hoc `tail`) do not take
  shared locks; on ext4 this is fine because kernel inode-lock
  serialization already gives them whole lines.
- We do not target NFS. `flock`-over-NFS behavior is mount-option
  dependent (`local_lock=flock` makes it process-local); if Hunch
  ever runs on NFS, `fcntl.lockf(F_SETLK)` would be the portable
  choice.
- This helper does not protect against partial writes from
  `ENOSPC`, `SIGKILL`, or power loss. The append-only invariant is
  about concurrency, not crash safety.

Advisory: writers that bypass this helper defeat serialization for
everyone. Keep replay-buffer JSONL writes funneling through here.

`fcntl.flock` is Linux + macOS; Hunch targets Unix per
framework_v0.md.
"""

from __future__ import annotations

import fcntl
import json
import re
from pathlib import Path
from typing import Any


def scan_max_numeric_id(
    path: Path,
    field: str,
    pattern: re.Pattern[str],
) -> int:
    """Scan a JSONL file for the largest numeric ID matching ``pattern``.

    Args:
        path: JSONL file to scan.
        field: JSON key containing the ID string (e.g. ``"hunch_id"``, ``"bank_id"``).
        pattern: Compiled regex with one capture group for the numeric part.

    Returns:
        The largest integer found, or 0 if the file is empty / missing.
    """
    if not path.exists():
        return 0
    max_n = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            m = pattern.match(d.get(field, ""))
            if m:
                n = int(m.group(1))
                if n > max_n:
                    max_n = n
    return max_n


def append_json_line(path: Path, entry: dict[str, Any]) -> None:
    """Serialize `entry` as one JSON line and append it to `path`
    under an exclusive advisory file lock. See module docstring for
    context.

    The lock is released implicitly when the file is closed; no
    explicit `LOCK_UN` is needed.
    """
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        f.write(line)
        f.flush()
