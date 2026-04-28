"""Fold the bank event stream into derived state."""

from __future__ import annotations

import json
from pathlib import Path

from hunch.bank.schema import (
    BankEntry,
    BankState,
    LabelRecord,
    LinkRecord,
)


def read_bank(bank_path: str | Path) -> BankState:
    """Read hunch_bank.jsonl and fold events into a BankState.

    Events are processed in file order (which is append order).
    Unknown event types are skipped for forward-compatibility.
    Returns an empty BankState if the file doesn't exist.
    """
    bank_path = Path(bank_path)
    state = BankState()
    if not bank_path.exists():
        return state

    with open(bank_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            _fold_event(state, event)

    return state


def _fold_event(state: BankState, event: dict) -> None:
    etype = event.get("type")

    if etype == "entry":
        _fold_entry(state, event)
    elif etype == "link":
        _fold_link(state, event)
    elif etype == "label":
        _fold_label(state, event)
    elif etype == "tombstone":
        _fold_tombstone(state, event)


def _fold_entry(state: BankState, event: dict) -> None:
    bank_id = event["bank_id"]
    run = event["source_run"]
    hunch_id = event["source_hunch_id"]
    entry = BankEntry(
        bank_id=bank_id,
        canonical_smell=event.get("canonical_smell", ""),
        canonical_description=event.get("canonical_description", ""),
        source_run=run,
        source_hunch_id=hunch_id,
        ts=event.get("ts", ""),
    )
    state.entries[bank_id] = entry
    state.hunch_to_bank[(run, hunch_id)] = bank_id


def _fold_link(state: BankState, event: dict) -> None:
    bank_id = event["bank_id"]
    run = event["run"]
    hunch_id = event["hunch_id"]

    entry = state.entries.get(bank_id)
    if entry is None:
        return

    link = LinkRecord(
        run=run,
        hunch_id=hunch_id,
        judge_score=event.get("judge_score"),
        source=event.get("source", "ingest"),
        replaces_bank_id=event.get("replaces_bank_id"),
        ts=event.get("ts", ""),
    )
    entry.links.append(link)
    state.hunch_to_bank[(run, hunch_id)] = bank_id


def _fold_label(state: BankState, event: dict) -> None:
    bank_id = event.get("bank_id", "")
    entry = state.entries.get(bank_id)
    if entry is None:
        return

    label = LabelRecord(
        run=event.get("run", ""),
        hunch_id=event.get("hunch_id", ""),
        label=event.get("label"),
        category=event.get("category", ""),
        labeled_by=event.get("labeled_by", ""),
        ts=event.get("ts", ""),
    )
    entry.labels.append(label)


def _fold_tombstone(state: BankState, event: dict) -> None:
    run = event.get("run", "")
    if run:
        state.tombstoned_runs.add(run)
