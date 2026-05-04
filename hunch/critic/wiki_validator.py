"""Hunch validator — mechanism invariants for wiki critic output.

Validates pending hunches against invariants that are universal across
all wiki critic deployments (not project-specific).  Invalid hunches
stay in pending_hunches.jsonl for agent self-correction on the next tick.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class HunchViolation:
    """A pending hunch that failed validation."""

    raw: dict[str, Any]
    violations: list[str] = field(default_factory=list)


def validate_pending_hunches(
    hunches: list[dict[str, Any]],
    workspace: Path,
) -> tuple[list[dict[str, Any]], list[HunchViolation]]:
    """Validate pending hunches against mechanism invariants.

    Returns (valid_hunches, invalid_hunches_with_violations).
    """
    wiki_ids = _collect_wiki_entity_ids(workspace / "wiki")
    artifacts_dir = workspace / "artifacts"

    valid: list[dict[str, Any]] = []
    invalid: list[HunchViolation] = []

    for h in hunches:
        violations: list[str] = []
        _check_wiki_id_leaks(h, wiki_ids, violations)
        _check_artifact_refs(h, artifacts_dir, violations)

        if violations:
            invalid.append(HunchViolation(raw=h, violations=violations))
        else:
            valid.append(h)

    return valid, invalid


def _check_wiki_id_leaks(
    hunch: dict[str, Any],
    wiki_ids: set[str],
    violations: list[str],
) -> None:
    """Flag wiki entity IDs that leaked into hunch text.

    The Scientist never sees the wiki — references like 'ev-exp001-phi4-results'
    or 'claim-old-linearized-no-pareto-improvement' are meaningless to them.
    Hunches must use conversation-visible references (turn numbers, artifact paths).
    """
    for field_name in ("smell", "description"):
        text = hunch.get(field_name, "")
        found = [eid for eid in wiki_ids if eid in text]
        if found:
            violations.append(
                f"Wiki entity ID(s) in {field_name}: {', '.join(sorted(found))}. "
                "The Scientist cannot see the wiki. Replace with "
                "conversation-visible references (turn numbers, artifact "
                "paths). Also review the corresponding wiki entities to "
                "ensure their source-turns and source-artifact fields are "
                "correct — the same confusion that leaks IDs into hunches "
                "often indicates weak provenance in the wiki itself."
            )


def _check_artifact_refs(
    hunch: dict[str, Any],
    artifacts_dir: Path,
    violations: list[str],
) -> None:
    """Validate triggering_refs.artifacts paths."""
    refs = hunch.get("triggering_refs") or {}
    artifact_paths = refs.get("artifacts") or []

    for path_str in artifact_paths:
        if not isinstance(path_str, str) or not path_str.strip():
            continue
        path_str = path_str.strip()

        if path_str.startswith("wiki/"):
            violations.append(
                f"Wiki-internal path in triggering_refs.artifacts: "
                f"'{path_str}'. Use project artifact paths, not wiki "
                "files. Also check the wiki entity that sourced this "
                "reference — its source-artifact field may need the "
                "same correction."
            )
        elif not (artifacts_dir / path_str).exists():
            violations.append(
                f"Artifact path not found in workspace: '{path_str}'. "
                "Only reference artifacts visible in the artifacts/ "
                "directory. Also check any wiki entities that cite this "
                "path in their source-artifact field and correct them."
            )


def _collect_wiki_entity_ids(wiki_dir: Path) -> set[str]:
    """Enumerate all entity IDs from wiki frontmatter."""
    ids: set[str] = set()
    if not wiki_dir.is_dir():
        return ids

    for md_file in wiki_dir.rglob("*.md"):
        if md_file.name == "index.md":
            continue
        try:
            text = md_file.read_text()
        except OSError:
            continue
        if not text.startswith("---"):
            continue
        end = text.find("---", 3)
        if end < 0:
            continue
        for line in text[3:end].splitlines():
            line = line.strip()
            if line.startswith("id:"):
                entity_id = line[3:].strip()
                if entity_id:
                    ids.add(entity_id)

    return ids
