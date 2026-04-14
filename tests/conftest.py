"""Shared pytest fixtures for hunch tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Helpers for building synthetic Claude Code .jsonl transcript records
# ---------------------------------------------------------------------------

def _user_text_record(text: str, ts: str) -> dict[str, Any]:
    return {
        "type": "user",
        "timestamp": ts,
        "message": {"role": "user", "content": text},
    }


def _assistant_text_record(text: str, ts: str) -> dict[str, Any]:
    return {
        "type": "assistant",
        "timestamp": ts,
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
    }


def _assistant_tool_use_record(
    tool_id: str, name: str, tool_input: dict[str, Any], ts: str
) -> dict[str, Any]:
    return {
        "type": "assistant",
        "timestamp": ts,
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_id,
                    "name": name,
                    "input": tool_input,
                }
            ],
        },
    }


def _user_tool_result_record(
    tool_id: str, content: str, ts: str, is_error: bool = False
) -> dict[str, Any]:
    return {
        "type": "user",
        "timestamp": ts,
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": content,
                    "is_error": is_error,
                }
            ],
        },
    }


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _append_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    with open(path, "a") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


# ---------------------------------------------------------------------------
# Exposed fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def transcript_factory(tmp_path):
    """Build Claude Code-shaped .jsonl transcripts on the fly.

    Returns a callable: `transcript_factory(records)` writes the given
    records to a fresh tmp file and returns its Path. Record-builder
    helpers are exposed as attributes on the factory function for
    convenience.
    """
    counter = {"n": 0}

    def _build(records: list[dict[str, Any]], name: str = "transcript.jsonl") -> Path:
        counter["n"] += 1
        p = tmp_path / f"{counter['n']:02d}_{name}"
        _write_jsonl(p, records)
        return p

    _build.user_text = _user_text_record
    _build.assistant_text = _assistant_text_record
    _build.assistant_tool_use = _assistant_tool_use_record
    _build.user_tool_result = _user_tool_result_record
    _build.append = _append_jsonl
    return _build


@pytest.fixture
def project_root(tmp_path) -> str:
    """A synthetic project root path (string with trailing slash, as the parser expects)."""
    root = tmp_path / "yoc_proj"
    root.mkdir()
    return str(root) + "/"
