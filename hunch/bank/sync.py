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
from hunch.bank.schema import LIVE_RUN_NAME, BankEntry, BankState
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
        unfiltered = check_unfiltered(eval_hunches_path)
        if unfiltered > 0:
            if log:
                log(f"\nSkipping run: {rname} — {unfiltered} unfiltered "
                    f"hunches. Run `hunch filter` first.")
            rr = RunSyncResult(run_name=rname, status="skipped:unfiltered")
            result.runs.append(rr)
            continue

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

    # Live hunches from .hunch/replay/
    if run_name is None or run_name == LIVE_RUN_NAME:
        replay_hunches = bank_dir.parent / "replay" / "hunches.jsonl"
        if replay_hunches.exists():
            if log:
                log(f"\nProcessing live hunches: {replay_hunches}")
            live_result = _sync_live_run(
                replay_hunches_path=replay_hunches,
                bank_path=bank_path,
                judge_fn=judge_fn,
                window_k=window_k,
                max_workers=max_workers,
                log=log,
            )
            result.runs.append(live_result)

    return result


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _discover_runs(
    eval_dir: Path,
    run_name: str | None,
) -> list[tuple[str, Path]]:
    """Find eval dirs matching <eval_dir>/<run_name>/hunches.jsonl.

    Returns runs sorted by hunch count descending so the longest run
    seeds the bank first, producing richer canonical wordings.
    """
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

    runs.sort(key=lambda r: _count_emitted(r[1]), reverse=True)
    return runs


def _count_emitted(path: Path) -> int:
    """Count non-filtered emit events in a hunches.jsonl file."""
    count = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("type") == "emit" and not d.get("filtered"):
                count += 1
    return count


def check_unfiltered(path: Path) -> int:
    """Count emit events that have not been through the filter.

    Returns the number of unfiltered emits (0 means fully filtered or
    no emits at all). Used by sync and annotation tool to refuse
    operating on unfiltered runs.
    """
    count = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("type") == "emit" and not d.get("filter_applied"):
                count += 1
    return count


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
# Live run sync
# ---------------------------------------------------------------------------

_FEEDBACK_LABEL_MAP = {"good": "tp", "bad": "fp"}


def _sync_live_run(
    replay_hunches_path: Path,
    bank_path: Path,
    judge_fn: JudgeFn,
    window_k: int,
    max_workers: int,
    log: Callable[[str], None] | None,
) -> RunSyncResult:
    """Sync live hunches from .hunch/replay/ into the bank.

    No bank copy, no conflict detection — the replay buffer is
    append-only and is the canonical artifact.
    """
    eval_hunches = _load_emitted_hunches(replay_hunches_path)
    if not eval_hunches:
        if log:
            log(f"  No emitted hunches found")
        return RunSyncResult(run_name=LIVE_RUN_NAME, status="skipped_up_to_date")

    state = read_bank(bank_path)
    already_ingested = _already_ingested_ids(state, LIVE_RUN_NAME)
    new_hunches = [h for h in eval_hunches if h["hunch_id"] not in already_ingested]

    if not new_hunches:
        run_result = RunSyncResult(
            run_name=LIVE_RUN_NAME, status="skipped_up_to_date",
        )
    else:
        if log:
            log(f"  {len(new_hunches)} new hunches to process "
                f"({len(already_ingested)} already ingested)")

        status = "resumed" if already_ingested else "ingested"
        run_result = _ingest_hunches(
            rname=LIVE_RUN_NAME,
            new_hunches=new_hunches,
            state=state,
            bank_path=bank_path,
            judge_fn=judge_fn,
            window_k=window_k,
            max_workers=max_workers,
            log=log,
        )
        run_result.status = status

    # Feedback label import
    feedback_path = replay_hunches_path.parent / "feedback.jsonl"
    if feedback_path.exists():
        imported = _sync_feedback_labels(
            feedback_path=feedback_path,
            bank_path=bank_path,
            log=log,
        )
        run_result.labels_migrated = imported

    return run_result


def _sync_feedback_labels(
    feedback_path: Path,
    bank_path: Path,
    log: Callable[[str], None] | None,
) -> int:
    """Import explicit feedback labels into the bank.

    Reads feedback.jsonl, maps good→tp / bad→fp, and writes label
    events for new or changed labels. Idempotent: re-running writes
    nothing if feedback hasn't changed.
    """
    from hunch.journal.feedback import read_labeled_hunch_ids

    feedback_labels = read_labeled_hunch_ids(feedback_path)
    if not feedback_labels:
        return 0

    state = read_bank(bank_path)
    writer = BankWriter(bank_path)
    imported = 0

    for hid, fb_label in feedback_labels.items():
        bank_label = _FEEDBACK_LABEL_MAP.get(fb_label)
        if bank_label is None:
            continue

        bank_id = state.hunch_to_bank.get((LIVE_RUN_NAME, hid))
        if bank_id is None:
            if log:
                log(f"    SKIP feedback for {hid}: not in bank yet")
            continue

        entry = state.entries.get(bank_id)
        if entry is None:
            continue

        existing = _current_feedback_label(entry, hid)
        if existing == bank_label:
            continue

        writer.write_label(
            bank_id=bank_id,
            run=LIVE_RUN_NAME,
            hunch_id=hid,
            label=bank_label,
            ts=_now_ts(),
            labeled_by="operational_live",
        )
        imported += 1
        if log:
            action = "updated" if existing is not None else "imported"
            log(f"    {action} feedback label for {hid}: {fb_label} → {bank_label}")

    if log and imported:
        log(f"  Imported {imported} feedback labels")

    return imported


def _current_feedback_label(entry: BankEntry, hunch_id: str) -> str | None:
    """Find the current operational_live label for a live hunch, if any."""
    best_ts = ""
    best_label = None
    for lr in entry.labels:
        if (lr.run == LIVE_RUN_NAME
                and lr.hunch_id == hunch_id
                and lr.labeled_by == "operational_live"
                and lr.ts > best_ts):
            best_ts = lr.ts
            best_label = lr.label
    return best_label


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

        # Manual within-run dedup: link this hunch to the target's bank entry
        dup_of = label_data.get("duplicate_of")
        if dup_of:
            target_bank_id = state.hunch_to_bank.get((rname, dup_of))
            if target_bank_id and target_bank_id != bank_id:
                writer.write_link(
                    bank_id=target_bank_id,
                    run=rname,
                    hunch_id=hid,
                    ts=_now_ts(),
                    source="manual",
                    replaces_bank_id=bank_id,
                )
                bank_id = target_bank_id
                if log:
                    log(f"    LINK {hid} → {target_bank_id} "
                        f"(duplicate_of {dup_of})")

        writer.write_label(
            bank_id=bank_id,
            run=rname,
            hunch_id=hid,
            label=label_val,
            ts=_now_ts(),
            category=label_data.get("category", ""),
            labeled_by=label_data.get("source", "legacy_migration"),
            note=label_data.get("note", ""),
            tags=label_data.get("tags", []),
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
