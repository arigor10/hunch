"""Wiki contract validation.

Validates wiki_contract.yaml (meta-validator) and wiki entity files
against the contract (per-tick validator). Generic — no hardcoded
entity types or field names.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml


# ---------------------------------------------------------------------------
# YAML frontmatter parsing
# ---------------------------------------------------------------------------

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)


def _parse_frontmatter(path: Path) -> dict[str, Any] | None:
    """Extract YAML frontmatter from a markdown file. Returns None on failure."""
    text = path.read_text(errors="replace")
    m = _FM_RE.match(text)
    if not m:
        return None
    try:
        return yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return None


# ---------------------------------------------------------------------------
# Meta-validator: check the contract itself
# ---------------------------------------------------------------------------

def validate_contract(contract_path: Path) -> list[str]:
    """Check that wiki_contract.yaml is well-formed. Returns list of errors."""
    errors: list[str] = []
    try:
        data = yaml.safe_load(contract_path.read_text())
    except (yaml.YAMLError, OSError) as e:
        return [f"cannot parse contract: {e}"]

    if not isinstance(data, dict):
        return ["contract is not a YAML mapping"]

    entity_types = data.get("entity_types")
    if not isinstance(entity_types, dict) or not entity_types:
        errors.append("missing or empty 'entity_types'")
        return errors

    for etype, spec in entity_types.items():
        if not isinstance(spec, dict):
            errors.append(f"{etype}: spec is not a mapping")
            continue
        rf = spec.get("required_fields")
        if not isinstance(rf, list) or not rf:
            errors.append(f"{etype}: missing or empty 'required_fields'")
        sv = spec.get("status_values")
        if sv is not None and (not isinstance(sv, list) or not sv):
            errors.append(f"{etype}: 'status_values' declared but empty")

    edges = data.get("bidirectional_edges")
    if edges is not None:
        if not isinstance(edges, list):
            errors.append("'bidirectional_edges' is not a list")
        else:
            for i, pair in enumerate(edges):
                if not isinstance(pair, list) or len(pair) != 2:
                    errors.append(f"bidirectional_edges[{i}]: expected 2-element list, got {pair!r}")

    return errors


# ---------------------------------------------------------------------------
# Per-tick validator: check wiki files against contract
# ---------------------------------------------------------------------------

def validate_wiki(wiki_dir: Path, contract_path: Path) -> list[str]:
    """Validate all wiki entity files against the contract.

    Checks: entity type known, required fields present, status values
    valid, IDs unique and well-formed. Skips index.md.
    """
    errors: list[str] = []

    try:
        contract = yaml.safe_load(contract_path.read_text())
    except (yaml.YAMLError, OSError) as e:
        return [f"cannot load contract: {e}"]

    entity_types = contract.get("entity_types", {})
    if not entity_types:
        return ["contract has no entity_types"]

    seen_ids: dict[str, Path] = {}

    for md_file in sorted(wiki_dir.rglob("*.md")):
        if md_file.name == "index.md":
            continue

        fm = _parse_frontmatter(md_file)
        if fm is None:
            errors.append(f"{md_file.name}: no valid YAML frontmatter")
            continue

        rel = md_file.relative_to(wiki_dir)

        etype = fm.get("type")
        if etype not in entity_types:
            errors.append(f"{rel}: unknown type '{etype}'")
            continue

        spec = entity_types[etype]

        for field_name in spec.get("required_fields", []):
            val = fm.get(field_name)
            if val is None or (isinstance(val, str) and not val.strip()):
                errors.append(f"{rel}: missing required field '{field_name}'")

        status_values = spec.get("status_values")
        if status_values and "status" in fm:
            if fm["status"] not in status_values:
                errors.append(
                    f"{rel}: status '{fm['status']}' not in {status_values}"
                )

        entity_id = fm.get("id")
        if entity_id:
            if entity_id in seen_ids:
                errors.append(
                    f"{rel}: duplicate id '{entity_id}' (also in {seen_ids[entity_id]})"
                )
            else:
                seen_ids[entity_id] = rel

            if not re.match(r"^[a-z]+-[a-z0-9-]+$", entity_id):
                errors.append(f"{rel}: id '{entity_id}' doesn't match <type>-<slug> format")

    return errors
