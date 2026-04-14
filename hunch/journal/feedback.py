"""Writer for `.hunch/replay/feedback.jsonl`.

Feedback is append-only, one line per Scientist reaction to a hunch.
Two channels:

- `explicit`  — the Scientist pressed Alt-g / Alt-b / Alt-s in the side
                panel. `label` is one of `"good"`, `"bad"`, `"skip"`.
- `implicit`  — the Scientist's reply prompt happened to mention the
                hunch (or, more loosely, happened after injection). The
                `scientist_reply` field carries the raw text so a later
                reader can weight this differently from explicit labels.

v0 keeps the schema small; richer metadata (e.g. reply latency,
categorical reaction tags) is a post-v0 concern. See framework_v0.md
Appendix A for the schema this module implements.
"""

from __future__ import annotations

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

    def _append(self, entry: dict[str, Any]) -> None:
        append_json_line(self.feedback_path, entry)
