"""Tests for wiki_contract — contract and wiki validation."""

from pathlib import Path

import pytest
import yaml

from hunch.critic.wiki_contract import validate_contract, validate_wiki


# ---------------------------------------------------------------------------
# Meta-validator
# ---------------------------------------------------------------------------

def _write_contract(path: Path, data: dict) -> None:
    path.write_text(yaml.dump(data))


def test_valid_contract(tmp_path):
    contract = tmp_path / "contract.yaml"
    _write_contract(contract, {
        "entity_types": {
            "claim": {
                "required_fields": ["id", "type", "status"],
                "status_values": ["open", "closed"],
            },
            "concept": {
                "required_fields": ["id", "type"],
            },
        },
        "bidirectional_edges": [
            ["supports", "supported-by"],
        ],
    })
    assert validate_contract(contract) == []


def test_missing_entity_types(tmp_path):
    contract = tmp_path / "contract.yaml"
    _write_contract(contract, {"other": "stuff"})
    errors = validate_contract(contract)
    assert any("entity_types" in e for e in errors)


def test_empty_required_fields(tmp_path):
    contract = tmp_path / "contract.yaml"
    _write_contract(contract, {
        "entity_types": {
            "claim": {"required_fields": []},
        },
    })
    errors = validate_contract(contract)
    assert any("required_fields" in e for e in errors)


def test_empty_status_values(tmp_path):
    contract = tmp_path / "contract.yaml"
    _write_contract(contract, {
        "entity_types": {
            "claim": {
                "required_fields": ["id", "type"],
                "status_values": [],
            },
        },
    })
    errors = validate_contract(contract)
    assert any("status_values" in e for e in errors)


def test_bad_bidirectional_edge(tmp_path):
    contract = tmp_path / "contract.yaml"
    _write_contract(contract, {
        "entity_types": {
            "claim": {"required_fields": ["id", "type"]},
        },
        "bidirectional_edges": [["only-one"]],
    })
    errors = validate_contract(contract)
    assert any("2-element" in e for e in errors)


# ---------------------------------------------------------------------------
# Per-tick validator
# ---------------------------------------------------------------------------

def _write_entity(wiki_dir: Path, subdir: str, name: str, frontmatter: dict, body: str = "") -> None:
    d = wiki_dir / subdir
    d.mkdir(parents=True, exist_ok=True)
    fm = yaml.dump(frontmatter, default_flow_style=False)
    (d / name).write_text(f"---\n{fm}---\n\n{body}\n")


@pytest.fixture
def wiki_setup(tmp_path):
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()
    (wiki_dir / "index.md").write_text("# Index\n")

    contract_path = tmp_path / "contract.yaml"
    _write_contract(contract_path, {
        "entity_types": {
            "claim": {
                "required_fields": ["id", "type", "status"],
                "status_values": ["conjectured", "supported", "refuted"],
            },
            "concept": {
                "required_fields": ["id", "type", "created"],
            },
        },
    })
    return wiki_dir, contract_path


def test_valid_wiki(wiki_setup):
    wiki_dir, contract = wiki_setup
    _write_entity(wiki_dir, "claims", "claim-foo.md", {
        "id": "claim-foo",
        "type": "claim",
        "status": "conjectured",
    })
    _write_entity(wiki_dir, "concepts", "concept-bar.md", {
        "id": "concept-bar",
        "type": "concept",
        "created": "2026-01-01",
    })
    assert validate_wiki(wiki_dir, contract) == []


def test_missing_required_field(wiki_setup):
    wiki_dir, contract = wiki_setup
    _write_entity(wiki_dir, "claims", "claim-foo.md", {
        "id": "claim-foo",
        "type": "claim",
        # missing status
    })
    errors = validate_wiki(wiki_dir, contract)
    assert any("status" in e for e in errors)


def test_bad_status_value(wiki_setup):
    wiki_dir, contract = wiki_setup
    _write_entity(wiki_dir, "claims", "claim-foo.md", {
        "id": "claim-foo",
        "type": "claim",
        "status": "bogus",
    })
    errors = validate_wiki(wiki_dir, contract)
    assert any("bogus" in e for e in errors)


def test_unknown_entity_type(wiki_setup):
    wiki_dir, contract = wiki_setup
    _write_entity(wiki_dir, "theorems", "theorem-x.md", {
        "id": "theorem-x",
        "type": "theorem",
    })
    errors = validate_wiki(wiki_dir, contract)
    assert any("unknown type" in e for e in errors)


def test_duplicate_ids(wiki_setup):
    wiki_dir, contract = wiki_setup
    _write_entity(wiki_dir, "claims", "claim-a.md", {
        "id": "claim-dup",
        "type": "claim",
        "status": "conjectured",
    })
    _write_entity(wiki_dir, "claims", "claim-b.md", {
        "id": "claim-dup",
        "type": "claim",
        "status": "supported",
    })
    errors = validate_wiki(wiki_dir, contract)
    assert any("duplicate" in e for e in errors)


def test_bad_id_format(wiki_setup):
    wiki_dir, contract = wiki_setup
    _write_entity(wiki_dir, "claims", "claim-foo.md", {
        "id": "CLAIM_FOO",
        "type": "claim",
        "status": "conjectured",
    })
    errors = validate_wiki(wiki_dir, contract)
    assert any("format" in e.lower() for e in errors)


def test_index_md_skipped(wiki_setup):
    wiki_dir, contract = wiki_setup
    (wiki_dir / "index.md").write_text("# No frontmatter here\nJust narrative.\n")
    assert validate_wiki(wiki_dir, contract) == []
