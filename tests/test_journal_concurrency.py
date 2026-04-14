"""Concurrent-append contract test for the shared `append_json_line`
helper.

Contract: concurrent appends from multiple processes using this
helper produce a file where

  1. every line is valid JSON,
  2. the total line count equals the expected count (no lost writes),
  3. every (worker, idx) pair appears exactly once (no merged lines,
     no silently-duplicated writes).

Claim (3) is the discriminating check — a count-only assertion would
miss a scenario where two writes collided into one merged line
*and* an extra blank line or a duplicate appeared elsewhere, keeping
the count right by accident.

On modern Linux local filesystems this contract happens to hold even
without `fcntl.flock` (kernel inode locks serialize regular-file
appends). The flock inside the helper is belt-and-suspenders for the
scenarios listed in `append.py`'s docstring, not a fix for a bug we
can reproduce on the test filesystem. This test verifies the helper
doesn't *break* the contract on its home filesystem; it does not
claim to exercise the failure modes the helper defends against.
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
    payload_size = 8192
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

    seen: set[tuple[int, int]] = set()
    for i, line in enumerate(lines):
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as e:
            raise AssertionError(f"line {i} is corrupt JSON: {e}\n{line[:200]!r}")
        key = (parsed["worker"], parsed["idx"])
        assert key not in seen, f"duplicate write for {key} at line {i}"
        seen.add(key)

    expected = {(wid, i) for wid in range(n_workers) for i in range(per_worker)}
    assert seen == expected, f"missing pairs: {expected - seen}"
