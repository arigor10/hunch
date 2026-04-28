"""Hunch bank: project-level identity + label store for concerns across runs."""

from hunch.bank.schema import (
    BankEntry,
    BankState,
    LinkRecord,
    LabelRecord,
    ResolvedLabel,
)
from hunch.bank.writer import BankWriter
from hunch.bank.reader import read_bank
from hunch.bank.resolver import resolve_label
from hunch.bank.sync import sync, migrate_labels, SyncResult, RunSyncResult

__all__ = [
    "BankEntry",
    "BankState",
    "BankWriter",
    "LabelRecord",
    "LinkRecord",
    "ResolvedLabel",
    "RunSyncResult",
    "SyncResult",
    "migrate_labels",
    "read_bank",
    "resolve_label",
    "sync",
]
