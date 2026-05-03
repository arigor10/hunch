"""Wiki critic workspace management.

Handles workspace initialization, incremental event copying for causal
isolation, and artifact snapshot management.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any


_PROMPTS_DIR = Path(__file__).parent / "prompts"
_DEFAULT_CLAUDE_MD = _PROMPTS_DIR / "wiki_critic_v1.md"
_DEFAULT_CONTRACT_SPEC = _PROMPTS_DIR / "wiki_contract_spec.md"


def init_workspace(
    workspace: Path,
    claude_md_path: Path | None = None,
    contract_spec_path: Path | None = None,
    seed_docs: list[Path] | None = None,
) -> None:
    """Create and populate the wiki critic workspace. Idempotent for resume."""
    workspace.mkdir(parents=True, exist_ok=True)

    (workspace / "wiki").mkdir(exist_ok=True)
    (workspace / "artifacts").mkdir(exist_ok=True)

    _copy_if_missing(
        src=claude_md_path or _DEFAULT_CLAUDE_MD,
        dst=workspace / "CLAUDE.md",
    )
    _copy_if_missing(
        src=contract_spec_path or _DEFAULT_CONTRACT_SPEC,
        dst=workspace / "wiki_contract_spec.md",
    )

    index_path = workspace / "wiki" / "index.md"
    if not index_path.exists():
        index_path.write_text("")

    conv_path = workspace / "conversation.jsonl"
    if not conv_path.exists():
        conv_path.touch()

    _write_settings_json(workspace)

    if seed_docs:
        docs_dir = workspace / "project_docs"
        docs_dir.mkdir(exist_ok=True)
        for doc in seed_docs:
            if doc.exists():
                _copy_if_missing(src=doc, dst=docs_dir / doc.name)


def copy_events_to_workspace(
    source_conversation: Path,
    workspace_conversation: Path,
    up_to_seq: int,
    last_copied_seq: int,
) -> int:
    """Append events from source to workspace conversation.jsonl.

    Copies events where last_copied_seq < tick_seq <= up_to_seq.
    Returns the new last_copied_seq (= up_to_seq if any events found,
    otherwise last_copied_seq unchanged).
    """
    new_events: list[str] = []
    with open(source_conversation) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            seq = d.get("tick_seq", 0)
            if seq <= last_copied_seq:
                continue
            if seq > up_to_seq:
                break
            new_events.append(line)

    if not new_events:
        return last_copied_seq

    with open(workspace_conversation, "a") as f:
        for line in new_events:
            f.write(line + "\n")

    return up_to_seq


def copy_artifact_snapshots(
    events: list[dict],
    source_artifacts: Path,
    workspace_artifacts: Path,
) -> None:
    """Copy artifact snapshot files referenced by events into the workspace."""
    for event in events:
        snapshot = event.get("snapshot")
        if not snapshot:
            continue
        src = source_artifacts / snapshot
        dst = workspace_artifacts / snapshot
        if src.exists() and not dst.exists():
            shutil.copy2(src, dst)


def _copy_if_missing(src: Path, dst: Path) -> None:
    if not dst.exists():
        shutil.copy2(src, dst)


def _write_settings_json(workspace: Path) -> None:
    """Write .claude/settings.json with workspace-scoped permissions."""
    claude_dir = workspace / ".claude"
    claude_dir.mkdir(exist_ok=True)
    settings_path = claude_dir / "settings.json"
    if settings_path.exists():
        return
    ws = str(workspace)
    settings: dict[str, Any] = {
        "permissions": {
            "deny": ["Bash"],
            "allow": [
                f"Read({ws}/**)",
                f"Edit({ws}/**)",
                f"Write({ws}/**)",
                f"Grep({ws}/**)",
                f"Glob({ws}/**)",
            ],
        }
    }
    settings_path.write_text(json.dumps(settings, indent=2) + "\n")
