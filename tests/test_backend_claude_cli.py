"""Tests for the Claude CLI backend."""

from __future__ import annotations

import json
import subprocess

import pytest

from hunch.backend.claude_cli import ClaudeCliBackend


class FakeCompletedProcess:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = ""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def _make_envelope(text: str, input_tokens: int = 500,
                   cache_read: int = 100, cache_create: int = 50) -> str:
    return json.dumps({
        "result": text,
        "usage": {
            "input_tokens": input_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_create,
        },
    })


class TestClaudeCliBackend:
    def test_basic_call(self, monkeypatch):
        envelope = _make_envelope("hello world", 500, 100, 50)
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["input"] = kwargs.get("input")
            return FakeCompletedProcess(stdout=envelope)

        monkeypatch.setattr(subprocess, "run", fake_run)
        backend = ClaudeCliBackend(model="claude-test", timeout_s=30.0)
        resp = backend.call("test prompt")

        assert resp.text == "hello world"
        assert resp.input_tokens == 650  # 500 + 100 + 50
        assert captured["input"] == "test prompt"
        assert "--model" in captured["cmd"]
        assert "claude-test" in captured["cmd"]

    def test_token_summing(self, monkeypatch):
        envelope = _make_envelope("ok", 1000, 200, 300)

        monkeypatch.setattr(subprocess, "run",
                            lambda cmd, **kw: FakeCompletedProcess(stdout=envelope))

        resp = ClaudeCliBackend().call("p")
        assert resp.input_tokens == 1500

    def test_no_usage(self, monkeypatch):
        envelope = json.dumps({"result": "text only"})

        monkeypatch.setattr(subprocess, "run",
                            lambda cmd, **kw: FakeCompletedProcess(stdout=envelope))

        resp = ClaudeCliBackend().call("p")
        assert resp.text == "text only"
        assert resp.input_tokens is None

    def test_non_json_stdout(self, monkeypatch):
        monkeypatch.setattr(subprocess, "run",
                            lambda cmd, **kw: FakeCompletedProcess(stdout="raw text"))

        resp = ClaudeCliBackend().call("p")
        assert resp.text == "raw text"
        assert resp.input_tokens is None

    def test_nonzero_exit(self, monkeypatch):
        monkeypatch.setattr(subprocess, "run",
                            lambda cmd, **kw: FakeCompletedProcess(
                                stdout="", returncode=1, stderr="boom"))

        with pytest.raises(RuntimeError, match="claude CLI exited 1"):
            ClaudeCliBackend().call("p")

    def test_passes_timeout(self, monkeypatch):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            return FakeCompletedProcess(stdout=_make_envelope("ok"))

        monkeypatch.setattr(subprocess, "run", fake_run)
        ClaudeCliBackend(timeout_s=42.0).call("p")
        assert captured["timeout"] == 42.0
