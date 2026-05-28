"""Writer for `.hunch/replay/feedback.jsonl`.

Feedback is append-only, one line per Scientist reaction to a hunch.
Channels:

- `explicit`  — the Scientist pressed Alt-g / Alt-b / Alt-s in the side
                panel. `label` is one of `"good"`, `"bad"`, `"skip"`.
- `implicit`  — the Scientist's reply prompt happened to mention the
                hunch (or, more loosely, happened after injection). The
                `scientist_reply` field carries the raw text so a later
                reader can weight this differently from explicit labels.
- `edit`      — the Scientist edited a hunch's smell/description before
                approval. Fields: original + edited smell/description.
- `response`  — the Researcher acknowledged a hunch (a line matching
                ``Re h-XXXX:`` in its output). Transitions the hunch
                to ``acknowledged`` status.
- `reminder`  — the UPS hook reminded the Researcher about a surfaced
                but unacknowledged hunch. Carries the tick_seq at
                which the reminder was issued for frequency control.

v0 keeps the schema small; richer metadata (e.g. reply latency,
categorical reaction tags) is a post-v0 concern. See framework_v0.md
Appendix A for the schema this module implements.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hunch.journal.append import append_json_line


@dataclass
class FeedbackWriter:
    """Append-only writer for `feedback.jsonl`."""
    feedback_path: Path

    def __post_init__(self) -> None:
        self.feedback_path = Path(self.feedback_path)
        self.feedback_path.parent.mkdir(parents=True, exist_ok=True)

    def write_explicit(
        self,
        hunch_id: str,
        label: str,
        ts: str,
    ) -> None:
        """Record an explicit Scientist label (good / bad / skip)."""
        if label not in ("good", "bad", "skip"):
            # Explicit labels are closed-set; typos here would silently
            # skew later analyses. Better to fail loudly.
            raise ValueError(f"explicit label must be good|bad|skip, got {label!r}")
        self._append(
            {
                "ts": ts,
                "hunch_id": hunch_id,
                "channel": "explicit",
                "label": label,
                "scientist_reply": None,
            }
        )

    def write_implicit(
        self,
        hunch_id: str,
        scientist_reply: str,
        ts: str,
    ) -> None:
        """Record an implicit reaction — the text of the Scientist's
        prompt that followed the hunch's injection."""
        self._append(
            {
                "ts": ts,
                "hunch_id": hunch_id,
                "channel": "implicit",
                "label": "implicit",
                "scientist_reply": scientist_reply,
            }
        )

    def write_edit(
        self,
        hunch_id: str,
        original_smell: str,
        original_description: str,
        edited_smell: str,
        edited_description: str,
        ts: str,
    ) -> None:
        """Record that the Scientist edited a hunch before approval."""
        self._append(
            {
                "ts": ts,
                "hunch_id": hunch_id,
                "channel": "edit",
                "original_smell": original_smell,
                "original_description": original_description,
                "edited_smell": edited_smell,
                "edited_description": edited_description,
            }
        )

    def write_response(
        self,
        hunch_id: str,
        response_text: str,
        ts: str,
    ) -> None:
        """Record that the Researcher acknowledged a hunch.

        Written when the parser detects a ``Re h-XXXX:`` line in the
        Researcher's output. Transitions the hunch to ``acknowledged``.
        """
        self._append(
            {
                "ts": ts,
                "hunch_id": hunch_id,
                "channel": "response",
                "response_text": response_text,
            }
        )

    def write_reminder(
        self,
        hunch_id: str,
        ts: str,
        tick_seq: int,
    ) -> None:
        """Record that the UPS hook reminded the Researcher about a hunch.

        ``tick_seq`` is the current conversation tick at reminder time,
        used for frequency control (remind again after N more turns).
        """
        self._append(
            {
                "ts": ts,
                "hunch_id": hunch_id,
                "channel": "reminder",
                "tick_seq": tick_seq,
            }
        )

    def _append(self, entry: dict[str, Any]) -> None:
        append_json_line(self.feedback_path, entry)


@dataclass(frozen=True)
class HunchEdit:
    """A Scientist's edit of a hunch's smell and description."""
    edited_smell: str
    edited_description: str


def read_hunch_edits(feedback_path: str | Path) -> dict[str, HunchEdit]:
    """Return `{hunch_id: latest_edit}` from feedback.jsonl.

    Only `channel == "edit"` events contribute. Last-write-wins per hunch_id.
    Returns `{}` if the file doesn't exist.
    """
    feedback_path = Path(feedback_path)
    if not feedback_path.exists():
        return {}
    edits: dict[str, HunchEdit] = {}
    with open(feedback_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("channel") != "edit":
                continue
            hid = d.get("hunch_id")
            smell = d.get("edited_smell")
            desc = d.get("edited_description")
            if hid and smell is not None:
                edits[hid] = HunchEdit(
                    edited_smell=smell,
                    edited_description=desc or "",
                )
    return edits


@dataclass(frozen=True)
class HunchResponse:
    """A Researcher's acknowledgment of a hunch."""
    response_text: str


def read_hunch_responses(feedback_path: str | Path) -> dict[str, HunchResponse]:
    """Return ``{hunch_id: latest_response}`` from feedback.jsonl.

    Only ``channel == "response"`` events contribute. Last-write-wins
    per hunch_id. Returns ``{}`` if the file doesn't exist.
    """
    feedback_path = Path(feedback_path)
    if not feedback_path.exists():
        return {}
    responses: dict[str, HunchResponse] = {}
    with open(feedback_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("channel") != "response":
                continue
            hid = d.get("hunch_id")
            text = d.get("response_text")
            if hid and text is not None:
                responses[hid] = HunchResponse(response_text=text)
    return responses


def read_hunch_reminders(feedback_path: str | Path) -> dict[str, int]:
    """Return ``{hunch_id: tick_seq_of_last_reminder}`` from feedback.jsonl.

    Only ``channel == "reminder"`` events contribute. Last-write-wins.
    Returns ``{}`` if the file doesn't exist.
    """
    feedback_path = Path(feedback_path)
    if not feedback_path.exists():
        return {}
    reminders: dict[str, int] = {}
    with open(feedback_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("channel") != "reminder":
                continue
            hid = d.get("hunch_id")
            seq = d.get("tick_seq")
            if hid and isinstance(seq, int):
                reminders[hid] = seq
    return reminders


def read_labeled_hunch_ids(feedback_path: str | Path) -> dict[str, str]:
    """Return `{hunch_id: latest_explicit_label}` from feedback.jsonl.

    Only `channel == "explicit"` events contribute. If a hunch has been
    labeled multiple times (e.g., skip then good), the latest wins —
    file order is append order, which is chronological in v0.

    Returns `{}` if the file doesn't exist. Unknown / malformed lines
    are skipped silently; the reader is forgiving by design.
    """
    feedback_path = Path(feedback_path)
    if not feedback_path.exists():
        return {}
    labels: dict[str, str] = {}
    with open(feedback_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("channel") != "explicit":
                continue
            hid = d.get("hunch_id")
            lbl = d.get("label")
            if hid and lbl:
                labels[hid] = lbl
    return labels
