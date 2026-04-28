"""Data types for the hunch bank.

The bank is an append-only JSONL event stream (hunch_bank.jsonl).
These dataclasses represent the *derived state* produced by folding
the event stream — not the events themselves (those are plain dicts).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class LinkRecord:
    """A hunch from a run that is linked to a bank entry."""
    run: str
    hunch_id: str
    judge_score: float | None = None
    source: str = "ingest"
    replaces_bank_id: str | None = None
    ts: str = ""


@dataclass
class LabelRecord:
    """A human label on a specific linked hunch."""
    run: str
    hunch_id: str
    label: str | None  # "tp", "fp", or None (retraction)
    category: str = ""
    labeled_by: str = ""
    ts: str = ""


@dataclass
class BankEntry:
    """Derived state for one unique concern in the bank."""
    bank_id: str
    canonical_smell: str
    canonical_description: str
    source_run: str
    source_hunch_id: str
    ts: str
    links: list[LinkRecord] = field(default_factory=list)
    labels: list[LabelRecord] = field(default_factory=list)


@dataclass
class ResolvedLabel:
    """Result of the label resolver for a specific (run, hunch_id)."""
    label: str | None
    source: str  # "human", "inherited", "unlabeled", "not_displayable"
    category: str = ""
    inherited_from_run: str = ""
    inherited_from_hunch_id: str = ""


@dataclass
class BankState:
    """Full derived state from folding the bank event stream."""
    entries: dict[str, BankEntry] = field(default_factory=dict)
    tombstoned_runs: set[str] = field(default_factory=set)
    hunch_to_bank: dict[tuple[str, str], str] = field(default_factory=dict)
