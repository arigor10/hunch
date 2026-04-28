"""Sync eval runs into the hunch bank.

Discovers runs under .hunch/eval/, dedup-matches new hunches against
existing bank entries, creates entry/link events, and optionally
migrates legacy labels.jsonl files.

The LLM judge is injected as a callable so tests can mock it.
"""

from __future__ import annotations

import bisect
import json
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from hunch.bank.reader import read_bank
from hunch.bank.schema import BankState
from hunch.bank.writer import BankWriter


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class RunSyncResult:
    run_name: str
    status: str  # "ingested", "resumed", "skipped_up_to_date", "skipped_conflict"
    new_entries: int = 0
    new_links: int = 0
    hunches_processed: int = 0
    labels_migrated: int = 0
    labels_pending: bool = False
    conflict_detail: str = ""


@dataclass
class SyncResult:
    runs: list[RunSyncResult] = field(default_factory=list)

    @property
    def total_entries(self) -> int:
        return sum(r.new_entries for r in self.runs)

    @property
    def total_links(self) -> int:
        return sum(r.new_links for r in self.runs)

    @property
    def total_labels_migrated(self) -> int:
        return sum(r.labels_migrated for r in self.runs)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

JudgeFn = Callable[[str, str, str, str], dict[str, Any]]


def sync(
    bank_dir: Path,
    eval_dir: Path,
    judge_fn: JudgeFn,
    *,
    run_name: str | None = None,
    migrate_labels: bool = False,
    window_k: int = 5,
    max_workers: int = 10,
    log: Callable[[str], None] | None = None,
) -> SyncResult:
    """Sync eval runs into the bank.

    Args:
        bank_dir: Path to .hunch/bank/.
        eval_dir: Path to .hunch/eval/.
        judge_fn: LLM dedup judge. Called as judge_fn(smell_a, desc_a,
            smell_b, desc_b) → {"duplicate": bool, "reasoning": str}.
        run_name: If set, sync only this run. Otherwise sync all.
        migrate_labels: If True, auto-migrate labels.jsonl without prompting.
        window_k: Half-window for dedup comparison (±k hunches).
        max_workers: Parallel workers for LLM calls.
        log: Optional log sink.
    """
    bank_path = bank_dir / "hunch_bank.jsonl"
    runs_dir = bank_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)

    discovered = _discover_runs(eval_dir, run_name)
    if log:
        log(f"Discovered {len(discovered)} run(s) in {eval_dir}")

    result = SyncResult()

    for rname, eval_hunches_path in discovered:
        if log:
            log(f"\nProcessing run: {rname}")

        run_result = _sync_one_run(
            rname=rname,
            eval_hunches_path=eval_hunches_path,
            bank_path=bank_path,
            runs_dir=runs_dir,
            eval_run_dir=eval_hunches_path.parent,
            judge_fn=judge_fn,
            window_k=window_k,
            max_workers=max_workers,
            migrate_labels=migrate_labels,
            log=log,
        )
        result.runs.append(run_result)

    return result


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _discover_runs(
    eval_dir: Path,
    run_name: str | None,
) -> list[tuple[str, Path]]:
    """Find eval dirs matching <eval_dir>/<run_name>/hunches.jsonl."""
    if not eval_dir.is_dir():
        return []

    runs = []
    for child in sorted(eval_dir.iterdir()):
        if not child.is_dir():
            continue
        hunches_path = child / "hunches.jsonl"
        if not hunches_path.exists():
            continue
        if run_name is not None and child.name != run_name:
            continue
        runs.append((child.name, hunches_path))
    return runs


# ---------------------------------------------------------------------------
# Single-run sync
# ---------------------------------------------------------------------------

def _sync_one_run(
    rname: str,
    eval_hunches_path: Path,
    bank_path: Path,
    runs_dir: Path,
    eval_run_dir: Path,
    judge_fn: JudgeFn,
    window_k: int,
    max_workers: int,
    migrate_labels: bool,
    log: Callable[[str], None] | None,
) -> RunSyncResult:
    bank_copy_dir = runs_dir / rname
    bank_copy_path = bank_copy_dir / "hunches.jsonl"

    # Check if already ingested
    if bank_copy_path.exists():
        conflict = _check_conflict(eval_hunches_path, bank_copy_path)
        if conflict:
            if log:
                log(f"  CONFLICT: {conflict}")
            return RunSyncResult(
                run_name=rname,
                status="skipped_conflict",
                conflict_detail=conflict,
            )
    else:
        bank_copy_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(eval_hunches_path, bank_copy_path)

    eval_hunches = _load_emitted_hunches(eval_hunches_path)
    if not eval_hunches:
        if log:
            log(f"  No emitted hunches found")
        return RunSyncResult(run_name=rname, status="skipped_up_to_date")

    state = read_bank(bank_path)
    already_ingested = _already_ingested_ids(state, rname)
    new_hunches = [h for h in eval_hunches if h["hunch_id"] not in already_ingested]

    if not new_hunches:
        run_result = RunSyncResult(run_name=rname, status="skipped_up_to_date")
    else:
        if log:
            log(f"  {len(new_hunches)} new hunches to process "
                f"({len(already_ingested)} already ingested)")

        status = "resumed" if already_ingested else "ingested"
        run_result = _ingest_hunches(
            rname=rname,
            new_hunches=new_hunches,
            state=state,
            bank_path=bank_path,
            judge_fn=judge_fn,
            window_k=window_k,
            max_workers=max_workers,
            log=log,
        )
        run_result.status = status

    # Legacy labels.jsonl migration
    # Trigger if labels.jsonl exists — even if .bak also exists (interrupted
    # previous migration: copy succeeded but rename didn't).
    labels_path = eval_run_dir / "labels.jsonl"
    labels_bak = eval_run_dir / "labels.jsonl.bak"
    if labels_path.exists():
        if migrate_labels:
            migrated = _migrate_labels(
                labels_path=labels_path,
                rname=rname,
                bank_path=bank_path,
                log=log,
            )
            run_result.labels_migrated = migrated
        else:
            run_result.labels_pending = True
            if log:
                log(f"  Found labels.jsonl ({_count_labels(labels_path)} labels) "
                    f"— needs migration (use --yes or migrate_labels=True)")

    return run_result


# ---------------------------------------------------------------------------
# Conflict detection
# ---------------------------------------------------------------------------

def _check_conflict(eval_path: Path, bank_copy_path: Path) -> str:
    """Compare eval and bank copies by (hunch_id, smell) tuples.

    Returns empty string if identical, or a description of the conflict.
    """
    eval_tuples = _hunch_identity_tuples(eval_path)
    bank_tuples = _hunch_identity_tuples(bank_copy_path)

    if eval_tuples == bank_tuples:
        return ""

    only_in_eval = eval_tuples - bank_tuples
    only_in_bank = bank_tuples - eval_tuples

    parts = []
    if only_in_eval:
        parts.append(f"{len(only_in_eval)} hunches added/changed in eval dir")
    if only_in_bank:
        parts.append(f"{len(only_in_bank)} hunches missing/changed vs bank copy")
    detail = "; ".join(parts)

    return (
        f"hunches.jsonl has changed since ingestion ({detail}). "
        f"To replace: `hunch bank drop --run {eval_path.parent.name}`, then re-sync. "
        f"To keep both: rename the eval dir, then re-sync."
    )


def _hunch_identity_tuples(path: Path) -> set[tuple[str, str]]:
    """Extract (hunch_id, smell) tuples from emitted hunches."""
    tuples = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("type") != "emit":
                continue
            if d.get("filtered"):
                continue
            tuples.add((d.get("hunch_id", ""), d.get("smell", "")))
    return tuples


# ---------------------------------------------------------------------------
# Hunch loading
# ---------------------------------------------------------------------------

def _load_emitted_hunches(path: Path) -> list[dict]:
    """Load non-filtered emit events, sorted by bookmark_now."""
    hunches = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("type") != "emit":
                continue
            if d.get("filtered"):
                continue
            hunches.append(d)
    hunches.sort(key=lambda h: h.get("bookmark_now", 0))
    return hunches


def _already_ingested_ids(state: BankState, run_name: str) -> set[str]:
    """Find hunch IDs from this run that are already in the bank."""
    ids = set()
    for (run, hid), _bank_id in state.hunch_to_bank.items():
        if run == run_name:
            ids.add(hid)
    return ids


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------

def _ingest_hunches(
    rname: str,
    new_hunches: list[dict],
    state: BankState,
    bank_path: Path,
    judge_fn: JudgeFn,
    window_k: int,
    max_workers: int,
    log: Callable[[str], None] | None,
) -> RunSyncResult:
    """Dedup new hunches against bank, write entry/link events."""
    new_hunches_sorted = sorted(new_hunches, key=lambda h: h.get("bookmark_now", 0))
    new_bookmarks = [h.get("bookmark_now", 0) for h in new_hunches_sorted]

    # Include all entries for dedup matching — even dormant ones (all links
    # tombstoned). Per design: "dormant entries still exist for dedup matching;
    # if a future run rediscovers the same concern, it links to the dormant
    # entry, reviving it."
    active_entries = list(state.entries.values())

    if log:
        log(f"  Comparing against {len(active_entries)} bank entries "
            f"(window ±{window_k})")

    # Build (bank_entry, new_hunch) pairs via windowed matching
    pairs: list[tuple[str, str, str, str, str, dict]] = []
    for entry in active_entries:
        bm = entry.bookmark_now
        if bm < 0:
            continue
        pos = bisect.bisect_left(new_bookmarks, bm)
        start = max(0, pos - window_k)
        end = min(len(new_hunches_sorted), pos + window_k)
        for idx in range(start, end):
            nh = new_hunches_sorted[idx]
            pairs.append((
                entry.canonical_smell,
                entry.canonical_description,
                nh.get("smell", ""),
                nh.get("description", ""),
                entry.bank_id,
                nh,
            ))

    if log:
        log(f"  {len(pairs)} comparison pairs to judge")

    # Run judge in parallel
    # matches: new_hunch_id → (bank_id, score)
    matches: dict[str, tuple[str, float]] = {}

    if pairs:
        done = 0
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            future_to_pair = {
                pool.submit(
                    judge_fn, p[0], p[1], p[2], p[3]
                ): (p[4], p[5])
                for p in pairs
            }
            for future in as_completed(future_to_pair):
                bank_id, nh = future_to_pair[future]
                done += 1
                result = future.result()
                if result.get("duplicate"):
                    hid = nh["hunch_id"]
                    score = result.get("score", result.get("judge_score", 0.0))
                    if isinstance(score, str):
                        try:
                            score = float(score)
                        except ValueError:
                            score = 0.0
                    existing = matches.get(hid)
                    if existing is None or score > existing[1]:
                        matches[hid] = (bank_id, score)
                    if log:
                        log(f"    MATCH: {hid} ↔ {bank_id} "
                            f"({result.get('reasoning', '')})")
                if done % 50 == 0 and log:
                    log(f"    [{done}/{len(pairs)}] {len(matches)} matches so far")

    # Write events
    writer = BankWriter(bank_path)
    new_entries = 0
    new_links = 0

    for nh in new_hunches:
        hid = nh["hunch_id"]
        bm = nh.get("bookmark_now", -1)
        ts = _now_ts()

        if hid in matches:
            bank_id, score = matches[hid]
            writer.write_link(
                bank_id=bank_id,
                run=rname,
                hunch_id=hid,
                ts=ts,
                bookmark_now=bm,
                judge_score=score,
                source="ingest",
            )
            new_links += 1
        else:
            bank_id = writer.allocate_id()
            writer.write_entry(
                bank_id=bank_id,
                canonical_smell=nh.get("smell", ""),
                canonical_description=nh.get("description", ""),
                source_run=rname,
                source_hunch_id=hid,
                ts=ts,
                bookmark_now=bm,
            )
            new_entries += 1

    if log:
        log(f"  Done: {new_entries} new entries, {new_links} links")

    return RunSyncResult(
        run_name=rname,
        status="",
        new_entries=new_entries,
        new_links=new_links,
        hunches_processed=len(new_hunches),
    )


# ---------------------------------------------------------------------------
# Legacy labels migration
# ---------------------------------------------------------------------------

def migrate_labels(
    labels_path: Path,
    rname: str,
    bank_path: Path,
    *,
    log: Callable[[str], None] | None = None,
) -> int:
    """Migrate a legacy labels.jsonl into the bank. Public entry point.

    Backs up labels.jsonl to labels.jsonl.bak before processing.
    Returns the number of labels migrated.
    """
    return _migrate_labels(labels_path, rname, bank_path, log=log)


def _migrate_labels(
    labels_path: Path,
    rname: str,
    bank_path: Path,
    log: Callable[[str], None] | None = None,
) -> int:
    """Internal: migrate labels and create backup."""
    labels_bak = labels_path.parent / "labels.jsonl.bak"
    shutil.copy2(labels_path, labels_bak)

    state = read_bank(bank_path)
    writer = BankWriter(bank_path)
    migrated = 0

    labels = _read_legacy_labels(labels_path)
    for hid, label_data in labels.items():
        bank_id = state.hunch_to_bank.get((rname, hid))
        if bank_id is None:
            if log:
                log(f"    SKIP label for {hid}: not found in bank for run {rname}")
            continue

        label_val = label_data.get("label")
        if label_val not in ("tp", "fp"):
            if log:
                log(f"    SKIP label for {hid}: unsupported label '{label_val}'")
            continue

        writer.write_label(
            bank_id=bank_id,
            run=rname,
            hunch_id=hid,
            label=label_val,
            ts=_now_ts(),
            category=label_data.get("category", ""),
            labeled_by=label_data.get("source", "legacy_migration"),
        )
        migrated += 1

    labels_path.rename(labels_bak)

    if log:
        log(f"    Migrated {migrated} labels, backed up to {labels_bak.name}")

    return migrated


def _read_legacy_labels(path: Path) -> dict[str, dict]:
    """Read labels.jsonl, last-write-wins by hunch_id."""
    labels: dict[str, dict] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            hid = d.get("hunch_id")
            if hid:
                labels[hid] = d
    return labels


def _count_labels(path: Path) -> int:
    """Count label entries in a labels.jsonl file."""
    return len(_read_legacy_labels(path))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ts_counter = 0


def _now_ts() -> str:
    """Generate a monotonically increasing timestamp.

    Uses microsecond precision. If multiple calls land in the same
    microsecond (common in tight loops), appends a monotonic suffix
    to guarantee strict ordering.
    """
    global _ts_counter
    _ts_counter += 1
    base = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    return f"{base}-{_ts_counter:06d}"
