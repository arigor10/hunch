"""Writer + reader for eval `labels.jsonl`.

Eval labels are distinct from live feedback (`feedback.jsonl`):
- feedback.jsonl: live scientist reaction during a session (good/bad/skip)
- labels.jsonl: offline evaluator annotation for precision measurement (tp/fp/skip)

Append-only. Re-labeling appends a new line; last-write-wins by hunch_id.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hunch.journal.append import append_json_line


@dataclass
class LabelsWriter:
    labels_path: Path

    def __post_init__(self) -> None:
        self.labels_path = Path(self.labels_path)
        self.labels_path.parent.mkdir(parents=True, exist_ok=True)

    def write(
        self,
        hunch_id: str,
        label: str,
        ts: str,
        *,
        category: str = "",
        note: str = "",
        source: str = "evaluator",
        bank_match: str | None = None,
    ) -> None:
        if label not in ("tp", "fp", "skip"):
            raise ValueError(f"label must be tp|fp|skip, got {label!r}")
        self._append({
            "hunch_id": hunch_id,
            "label": label,
            "category": category,
            "source": source,
            "bank_match": bank_match,
            "note": note,
            "ts": ts,
        })

    def _append(self, entry: dict[str, Any]) -> None:
        append_json_line(self.labels_path, entry)


def read_labels(labels_path: str | Path) -> dict[str, dict[str, Any]]:
    """Return {hunch_id: latest_label_record} from labels.jsonl.

    Last-write-wins by hunch_id. Returns {} if file doesn't exist.
    """
    labels_path = Path(labels_path)
    if not labels_path.exists():
        return {}
    records: dict[str, dict[str, Any]] = {}
    with open(labels_path) as f:
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
                records[hid] = d
    return records
