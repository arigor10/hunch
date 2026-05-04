"""Tests for wiki_validator — hunch mechanism invariant checks."""

import json
from pathlib import Path

import pytest

from hunch.critic.wiki_validator import (
    HunchViolation,
    validate_pending_hunches,
    _collect_wiki_entity_ids,
)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Create a minimal workspace with wiki entities and artifacts."""
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "index.md").write_text("# Index\n")

    (wiki / "evidence").mkdir()
    (wiki / "evidence" / "ev-exp001-phi4-results.md").write_text(
        "---\nid: ev-exp001-phi4-results\ntype: evidence\n---\nContent.\n"
    )
    (wiki / "claims").mkdir()
    (wiki / "claims" / "claim-old-linearized-no-pareto.md").write_text(
        "---\nid: claim-old-linearized-no-pareto\ntype: claim\n---\nContent.\n"
    )
    (wiki / "concepts").mkdir()
    (wiki / "concepts" / "concept-pareto-curves.md").write_text(
        "---\nid: concept-pareto-curves\ntype: concept\n---\nContent.\n"
    )
    (wiki / "hypotheses").mkdir()
    (wiki / "hypotheses" / "hyp-telescopic-regime.md").write_text(
        "---\nid: hyp-telescopic-regime\ntype: hypothesis\n---\nContent.\n"
    )
    (wiki / "questions").mkdir()
    (wiki / "questions" / "q-layer-detection.md").write_text(
        "---\nid: q-layer-detection\ntype: question\n---\nContent.\n"
    )

    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "docs").mkdir()
    (artifacts / "docs" / "experiments").mkdir()
    (artifacts / "docs" / "experiments" / "exp_001_results.md").write_text("data")

    return tmp_path


class TestCollectWikiEntityIds:
    def test_collects_all_types(self, workspace: Path):
        ids = _collect_wiki_entity_ids(workspace / "wiki")
        assert ids == {
            "ev-exp001-phi4-results",
            "claim-old-linearized-no-pareto",
            "concept-pareto-curves",
            "hyp-telescopic-regime",
            "q-layer-detection",
        }

    def test_skips_index(self, workspace: Path):
        ids = _collect_wiki_entity_ids(workspace / "wiki")
        assert "index" not in str(ids)

    def test_empty_wiki(self, tmp_path: Path):
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        assert _collect_wiki_entity_ids(wiki) == set()

    def test_nonexistent_wiki(self, tmp_path: Path):
        assert _collect_wiki_entity_ids(tmp_path / "nope") == set()


class TestWikiIdLeaks:
    def test_catches_ev_id_in_description(self, workspace: Path):
        hunch = {
            "smell": "Something suspicious",
            "description": "See ev-exp001-phi4-results for details.",
            "triggering_refs": {"tick_seqs": [42]},
        }
        valid, invalid = validate_pending_hunches([hunch], workspace)
        assert len(valid) == 0
        assert len(invalid) == 1
        assert "ev-exp001-phi4-results" in invalid[0].violations[0]

    def test_catches_claim_id_in_smell(self, workspace: Path):
        hunch = {
            "smell": "Contradicts claim-old-linearized-no-pareto",
            "description": "Normal text here.",
            "triggering_refs": {"tick_seqs": [10]},
        }
        valid, invalid = validate_pending_hunches([hunch], workspace)
        assert len(valid) == 0
        assert len(invalid) == 1
        assert "smell" in invalid[0].violations[0]

    def test_catches_multiple_ids(self, workspace: Path):
        hunch = {
            "smell": "Ok smell",
            "description": (
                "ev-exp001-phi4-results contradicts "
                "claim-old-linearized-no-pareto"
            ),
            "triggering_refs": {},
        }
        valid, invalid = validate_pending_hunches([hunch], workspace)
        assert len(invalid) == 1
        assert "ev-exp001-phi4-results" in invalid[0].violations[0]
        assert "claim-old-linearized-no-pareto" in invalid[0].violations[0]

    def test_no_false_positive_on_normal_text(self, workspace: Path):
        hunch = {
            "smell": "Baseline not reproducible",
            "description": (
                "The claim that old linearized improves Pareto efficiency "
                "is contradicted by the evidence from turn 221."
            ),
            "triggering_refs": {"tick_seqs": [221]},
        }
        valid, invalid = validate_pending_hunches([hunch], workspace)
        assert len(valid) == 1
        assert len(invalid) == 0

    def test_partial_id_no_match(self, workspace: Path):
        """An ID substring that doesn't match a full entity ID passes."""
        hunch = {
            "smell": "Something about exp001",
            "description": "The exp001 results show...",
            "triggering_refs": {},
        }
        valid, invalid = validate_pending_hunches([hunch], workspace)
        assert len(valid) == 1


class TestArtifactRefs:
    def test_valid_artifact_path(self, workspace: Path):
        hunch = {
            "smell": "Data issue",
            "description": "Normal description.",
            "triggering_refs": {
                "artifacts": ["docs/experiments/exp_001_results.md"],
                "tick_seqs": [10],
            },
        }
        valid, invalid = validate_pending_hunches([hunch], workspace)
        assert len(valid) == 1
        assert len(invalid) == 0

    def test_catches_wiki_path_in_artifacts(self, workspace: Path):
        hunch = {
            "smell": "Data issue",
            "description": "Normal description.",
            "triggering_refs": {
                "artifacts": ["wiki/evidence/ev-exp001-phi4-results.md"],
                "tick_seqs": [10],
            },
        }
        valid, invalid = validate_pending_hunches([hunch], workspace)
        assert len(invalid) == 1
        assert "Wiki-internal path" in invalid[0].violations[0]

    def test_catches_nonexistent_artifact(self, workspace: Path):
        hunch = {
            "smell": "Data issue",
            "description": "Normal description.",
            "triggering_refs": {
                "artifacts": ["results/nonexistent.json"],
                "tick_seqs": [10],
            },
        }
        valid, invalid = validate_pending_hunches([hunch], workspace)
        assert len(invalid) == 1
        assert "not found" in invalid[0].violations[0]

    def test_multiple_artifact_violations(self, workspace: Path):
        hunch = {
            "smell": "Data issue",
            "description": "Normal description.",
            "triggering_refs": {
                "artifacts": [
                    "wiki/claims/something.md",
                    "results/ghost.json",
                ],
            },
        }
        valid, invalid = validate_pending_hunches([hunch], workspace)
        assert len(invalid) == 1
        assert len(invalid[0].violations) == 2


class TestMixedBatch:
    def test_splits_valid_and_invalid(self, workspace: Path):
        good = {
            "smell": "Clean hunch",
            "description": "Turn 42 shows a discrepancy.",
            "triggering_refs": {"tick_seqs": [42]},
        }
        bad = {
            "smell": "See ev-exp001-phi4-results",
            "description": "Wiki leak.",
            "triggering_refs": {},
        }
        valid, invalid = validate_pending_hunches([good, bad], workspace)
        assert len(valid) == 1
        assert valid[0]["smell"] == "Clean hunch"
        assert len(invalid) == 1
        assert invalid[0].raw["smell"] == "See ev-exp001-phi4-results"

    def test_multiple_violations_on_single_hunch(self, workspace: Path):
        hunch = {
            "smell": "ev-exp001-phi4-results issue",
            "description": "See claim-old-linearized-no-pareto for context.",
            "triggering_refs": {
                "artifacts": ["wiki/evidence/something.md"],
            },
        }
        valid, invalid = validate_pending_hunches([hunch], workspace)
        assert len(invalid) == 1
        assert len(invalid[0].violations) >= 3

    def test_empty_list(self, workspace: Path):
        valid, invalid = validate_pending_hunches([], workspace)
        assert valid == []
        assert invalid == []

    def test_violation_message_encourages_wiki_fix(self, workspace: Path):
        hunch = {
            "smell": "Ok",
            "description": "Check ev-exp001-phi4-results data.",
            "triggering_refs": {},
        }
        _, invalid = validate_pending_hunches([hunch], workspace)
        assert len(invalid) == 1
        assert "wiki entities" in invalid[0].violations[0].lower() or \
               "wiki" in invalid[0].violations[0].lower()
