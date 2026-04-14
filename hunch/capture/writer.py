"""Replay buffer writer.

Consumes parser events and appends them to the replay buffer. Artifact
contents are snapshotted into `<replay>/artifacts/` and the event logs
reference those snapshots by relative path.

Invariants (per framework_v0.md §Design invariants):
  - All JSONL files are append-only.
  - Entries carry a monotonic `tick_seq` so consumers can bookmark.
  - Artifacts referenced by relative path from `artifacts/`, never
    absolute.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hunch.parse import Event, ParserState, poll_new_events


# ---------------------------------------------------------------------------
# Path normalization
# ---------------------------------------------------------------------------

def _normalize_artifact_path(abs_path: str, project_roots: list[str]) -> str:
    """Convert an absolute artifact path to a relative-to-project-root path.

    If the path lies under one of the project roots, strip that root.
    If multiple roots match (shouldn't happen in practice), prefer the
    longest match. If no root matches, fall back to the filename alone.
    """
    matched_root = ""
    for root in project_roots:
        if abs_path.startswith(root) and len(root) > len(matched_root):
            matched_root = root
    if matched_root:
        return abs_path[len(matched_root):]
    # Fallback: just the filename
    return Path(abs_path).name


# ---------------------------------------------------------------------------
# Snapshot filenames
# ---------------------------------------------------------------------------

_UNSAFE_CHARS = re.compile(r"[^a-zA-Z0-9._-]+")


def _snapshot_filename(relative_path: str, timestamp: str, content_hash: str) -> str:
    """Construct a stable, unique filename for an artifact snapshot.

    Format: <flattened-relpath>__<iso-timestamp>__<hash8>.md

    The flattening replaces path separators with single underscores so the
    original path is recoverable by eye. The hash suffix makes
    simultaneous-write collisions a non-issue.
    """
    flat = relative_path.replace("/", "_").replace("\\", "_")
    flat = _UNSAFE_CHARS.sub("_", flat)
    # Strip timezone/microsecond noise for readability; keep second precision.
    ts = re.sub(r"[^0-9T]", "", timestamp)[:15]  # YYYYMMDDTHHMMSS
    return f"{flat}__{ts}__{content_hash[:8]}"


def _hash_content(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()


# ---------------------------------------------------------------------------
# Applying edits (to maintain an in-memory current-content map)
# ---------------------------------------------------------------------------

def _apply_edit(current: str, old_string: str, new_string: str) -> tuple[str, bool]:
    """Apply a single-replacement edit. Returns (new_content, success).

    Mirrors Claude Code's Edit tool: replace the first occurrence of
    old_string with new_string. If old_string isn't present, the edit
    fails; we keep the current content unchanged and the caller should
    log the failure.
    """
    if old_string not in current:
        return current, False
    return current.replace(old_string, new_string, 1), True


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------

@dataclass
class ReplayBufferWriter:
    """Writes Hunch's replay buffer.

    Owns: `.hunch/replay/` directory layout, append-only JSONL files,
    artifact snapshot dir, and the in-memory current-content map used
    to apply edits.

    Does NOT own: polling schedule, parser state (those live in the
    caller / the live framework's Capture loop).
    """
    replay_dir: Path
    tick_seq: int = 0
    current_artifact_content: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.replay_dir = Path(self.replay_dir)
        self.artifacts_dir = self.replay_dir / "artifacts"
        self.conversation_path = self.replay_dir / "conversation.jsonl"
        self.artifacts_log_path = self.replay_dir / "artifacts.jsonl"
        # Ensure directory exists — safe to call repeatedly.
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)

    # -----------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------

    def append_events(self, events: list[Event], project_roots: list[str]) -> None:
        """Append a batch of parser events to the replay buffer.

        For each event:
          - artifact_write/edit: snapshot content, log to both
            conversation.jsonl and artifacts.jsonl.
          - everything else: log to conversation.jsonl only.

        Events are written in order; tick_seq is assigned monotonically.
        """
        for event in events:
            self.tick_seq += 1
            etype = event["type"]

            if etype == "artifact_write":
                self._handle_artifact_write(event, project_roots)
            elif etype == "artifact_edit":
                self._handle_artifact_edit(event, project_roots)
            else:
                # Pass-through events (text, figure, tool_error)
                self._append_conversation({
                    "tick_seq": self.tick_seq,
                    **event,
                })

    # -----------------------------------------------------------------
    # Artifact handling
    # -----------------------------------------------------------------

    def _handle_artifact_write(self, event: Event, project_roots: list[str]) -> None:
        abs_path = event["path"]
        content = event["content"]
        rel_path = _normalize_artifact_path(abs_path, project_roots)
        content_hash = _hash_content(content)

        snapshot_name = _snapshot_filename(rel_path, event["timestamp"], content_hash)
        snapshot_path = self.artifacts_dir / snapshot_name
        snapshot_path.write_text(content)

        self.current_artifact_content[rel_path] = content

        # Log in conversation.jsonl (lightweight reference, no inline content)
        self._append_conversation({
            "tick_seq": self.tick_seq,
            "type": "artifact_write",
            "timestamp": event["timestamp"],
            "path": rel_path,
            "snapshot": snapshot_name,
            "content_hash": content_hash,
        })

        # Log in artifacts.jsonl (artifact-event-only stream)
        self._append_artifact_event({
            "tick_seq": self.tick_seq,
            "ts": event["timestamp"],
            "event": "write",
            "path": rel_path,
            "snapshot": snapshot_name,
            "content_hash": content_hash,
        })

    def _handle_artifact_edit(self, event: Event, project_roots: list[str]) -> None:
        abs_path = event["path"]
        old_string = event["old_string"]
        new_string = event["new_string"]
        rel_path = _normalize_artifact_path(abs_path, project_roots)

        prev_content = self.current_artifact_content.get(rel_path)

        if prev_content is None:
            # Edit-before-write: the file existed on disk before the
            # transcript started, so we don't have its base content.
            # Log the event but skip snapshotting.
            self._append_conversation({
                "tick_seq": self.tick_seq,
                "type": "artifact_edit",
                "timestamp": event["timestamp"],
                "path": rel_path,
                "skipped_reason": "edit_before_known_base",
                "old_string": old_string,
                "new_string": new_string,
            })
            self._append_artifact_event({
                "tick_seq": self.tick_seq,
                "ts": event["timestamp"],
                "event": "edit_skipped",
                "path": rel_path,
                "reason": "edit_before_known_base",
            })
            return

        new_content, ok = _apply_edit(prev_content, old_string, new_string)
        if not ok:
            # Failed edit: old_string wasn't found. Log but keep prev content.
            self._append_conversation({
                "tick_seq": self.tick_seq,
                "type": "artifact_edit",
                "timestamp": event["timestamp"],
                "path": rel_path,
                "skipped_reason": "old_string_not_found",
                "old_string": old_string,
                "new_string": new_string,
            })
            self._append_artifact_event({
                "tick_seq": self.tick_seq,
                "ts": event["timestamp"],
                "event": "edit_failed",
                "path": rel_path,
                "reason": "old_string_not_found",
            })
            return

        content_hash = _hash_content(new_content)
        snapshot_name = _snapshot_filename(rel_path, event["timestamp"], content_hash)
        snapshot_path = self.artifacts_dir / snapshot_name
        snapshot_path.write_text(new_content)
        self.current_artifact_content[rel_path] = new_content

        self._append_conversation({
            "tick_seq": self.tick_seq,
            "type": "artifact_edit",
            "timestamp": event["timestamp"],
            "path": rel_path,
            "snapshot": snapshot_name,
            "content_hash": content_hash,
            "diff": {"old_string": old_string, "new_string": new_string},
        })
        self._append_artifact_event({
            "tick_seq": self.tick_seq,
            "ts": event["timestamp"],
            "event": "edit",
            "path": rel_path,
            "snapshot": snapshot_name,
            "content_hash": content_hash,
        })

    # -----------------------------------------------------------------
    # File I/O (thin wrappers for append-only semantics)
    # -----------------------------------------------------------------

    def _append_conversation(self, entry: dict[str, Any]) -> None:
        with open(self.conversation_path, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _append_artifact_event(self, entry: dict[str, Any]) -> None:
        with open(self.artifacts_log_path, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Convenience: one-shot poll (transcript → replay buffer)
# ---------------------------------------------------------------------------

def poll_once(
    transcript_path: str | Path,
    writer: ReplayBufferWriter,
    parser_state: ParserState,
) -> ParserState:
    """Read new transcript lines, parse, write to replay buffer.

    Returns updated ParserState for the next call. Safe to call in a loop
    with any cadence.
    """
    events, new_state = poll_new_events(transcript_path, parser_state)
    if events:
        writer.append_events(events, new_state.project_roots)
    return new_state
