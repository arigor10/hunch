"""Label resolver: computes the effective label for a (run, hunch_id) pair.

This is the single function that determines what label to display for
any hunch. See docs/hunch_bank_design.md §Label resolver algorithm
for the full spec and §Scenarios for worked examples.
"""

from __future__ import annotations

from hunch.bank.schema import BankEntry, BankState, LabelRecord, ResolvedLabel

_WEAK_LABELED_BY = frozenset({"operational_live"})


def resolve_label(state: BankState, run: str, hunch_id: str) -> ResolvedLabel:
    """Resolve the effective label for a specific hunch.

    Returns a ResolvedLabel with source indicating how the label was
    determined: "human" (direct label), "inherited" (from another
    linked hunch), "unlabeled", or "not_displayable" (tombstoned run).
    """
    if run in state.tombstoned_runs:
        return ResolvedLabel(label=None, source="not_displayable")

    bank_id = state.hunch_to_bank.get((run, hunch_id))
    if bank_id is None:
        return ResolvedLabel(label=None, source="unlabeled")

    entry = state.entries.get(bank_id)
    if entry is None:
        return ResolvedLabel(label=None, source="unlabeled")

    local = _effective_local_label(entry, run, hunch_id)
    if local is not None and local.label is not None:
        return ResolvedLabel(
            label=local.label,
            source="human",
            category=local.category,
        )

    inherited = _find_inherited_label(state, entry, run, hunch_id)
    if inherited is not None:
        return ResolvedLabel(
            label=inherited.label,
            source="inherited",
            category=inherited.category,
            inherited_from_run=inherited.run,
            inherited_from_hunch_id=inherited.hunch_id,
        )

    return ResolvedLabel(label=None, source="unlabeled")


def _effective_local_label(
    entry: BankEntry,
    run: str,
    hunch_id: str,
) -> LabelRecord | None:
    """Find the effective label for a specific (bank_id, run, hunch_id).

    Tier 1 (deliberate) labels outrank tier 2 (weak/operational_live).
    Within the same tier, last label event by ts wins. Returns None if
    no label events exist for this triple. Returns a LabelRecord with
    label=None if the last event was a retraction.
    """
    candidates = [
        lr for lr in entry.labels
        if lr.run == run and lr.hunch_id == hunch_id
    ]
    if not candidates:
        return None
    tier1 = [c for c in candidates if c.labeled_by not in _WEAK_LABELED_BY]
    if tier1:
        return max(tier1, key=lambda lr: lr.ts)
    return max(candidates, key=lambda lr: lr.ts)


def _find_inherited_label(
    state: BankState,
    entry: BankEntry,
    exclude_run: str,
    exclude_hunch_id: str,
) -> LabelRecord | None:
    """Find the canonical inherited label for a bank entry.

    Looks at all linked hunches (including the source hunch) that
    are NOT the one being resolved. For each, computes the effective
    label. Tier 1 (deliberate) labels are preferred over tier 2
    (weak/operational_live). Within the same tier, the one whose
    first label event has the earliest ts wins (the canonical labeler).
    """
    all_linked = _all_linked_hunches(entry)

    tier1_best: LabelRecord | None = None
    tier1_best_ts: str = ""
    tier2_best: LabelRecord | None = None
    tier2_best_ts: str = ""

    for linked_run, linked_hid in all_linked:
        if linked_run == exclude_run and linked_hid == exclude_hunch_id:
            continue
        # Do NOT skip tombstoned runs here — labels are facts about what
        # a human judged, not about the run's validity. A tombstoned run's
        # label still propagates as inherited.

        effective = _effective_local_label(entry, linked_run, linked_hid)
        if effective is None or effective.label is None:
            continue

        first_ts = _first_label_ts(entry, linked_run, linked_hid)
        is_weak = effective.labeled_by in _WEAK_LABELED_BY

        if not is_weak:
            if tier1_best is None or first_ts < tier1_best_ts:
                tier1_best = effective
                tier1_best_ts = first_ts
        else:
            if tier2_best is None or first_ts < tier2_best_ts:
                tier2_best = effective
                tier2_best_ts = first_ts

    return tier1_best if tier1_best is not None else tier2_best


def _all_linked_hunches(entry: "BankState.entries") -> list[tuple[str, str]]:
    """Return all (run, hunch_id) pairs linked to this bank entry.

    Includes the source hunch (from the entry event) and all linked
    hunches. Uses last-link-wins for hunches that were relinked.
    """
    result: dict[tuple[str, str], None] = {}
    result[(entry.source_run, entry.source_hunch_id)] = None
    for link in entry.links:
        result[(link.run, link.hunch_id)] = None
    return list(result.keys())


def _first_label_ts(
    entry: "BankState.entries",
    run: str,
    hunch_id: str,
) -> str:
    """Return the ts of the first non-null label for (run, hunch_id)."""
    for lr in entry.labels:
        if lr.run == run and lr.hunch_id == hunch_id and lr.label is not None:
            return lr.ts
    return ""
