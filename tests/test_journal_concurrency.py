"""Concurrent-append contract test for the shared `append_json_line`
helper.

Contract: concurrent appends from multiple processes using this helper
produce a file where every line is valid JSON — no interleaving, no
lost writes, exactly the expected line count.

On modern Linux local filesystems, this contract happens to hold even
without fcntl.flock (kernel inode locks serialize regular-file
appends). The flock inside the helper is defense-in-depth for NFS,
FUSE, macOS APFS, and Python buffered-I/O split scenarios — see
`append.py` module docstring. This test exercises the helper on the
local filesystem where tests run; it does not attempt to simulate the
pathological filesystems the helper is defending against.
"""

from __future__ import annotations

import json
from multiprocessing import Process
from pathlib import Path

from hunch.journal.append import append_json_line


def _worker(path: str, worker_id: int, count: int, payload_size: int) -> None:
    large = "x" * payload_size
    for i in range(count):
        append_json_line(
            Path(path),
            {"worker": worker_id, "idx": i, "payload": large},
        )


def test_concurrent_appends_do_not_interleave(tmp_path):
    target = tmp_path / "concurrent.jsonl"
    payload_size = 8192  # Exceed typical PIPE_BUF of 4096.
    per_worker = 25
    n_workers = 4

    procs = [
        Process(
            target=_worker,
            args=(str(target), wid, per_worker, payload_size),
        )
        for wid in range(n_workers)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)
    for p in procs:
        assert p.exitcode == 0, f"worker exited {p.exitcode}"

    with open(target) as f:
        lines = [L for L in f if L.strip()]
    assert len(lines) == n_workers * per_worker

    for i, line in enumerate(lines):
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as e:
            raise AssertionError(f"line {i} is corrupt JSON: {e}\n{line[:200]!r}")
        assert "worker" in parsed and "idx" in parsed
