"""Tests for the OpenRouter backend."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

from hunch.backend.openrouter import OpenRouterBackend


@dataclass
class FakeUsage:
    prompt_tokens: int = 800


@dataclass
class FakeChoice:
    message: Any = None

    def __post_init__(self):
        if self.message is None:
            self.message = MagicMock(content="model output")


@dataclass
class FakeCompletion:
    choices: list = None
    usage: FakeUsage = None

    def __post_init__(self):
        if self.choices is None:
            self.choices = [FakeChoice()]
        if self.usage is None:
            self.usage = FakeUsage()


class FakeChatCompletions:
    def __init__(self, completions=None, errors=None):
        self.completions = list(completions or [FakeCompletion()])
        self.errors = list(errors or [])
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        if self.completions:
            return self.completions.pop(0)
        return FakeCompletion()


class FakeChat:
    def __init__(self, completions_handler):
        self.completions = completions_handler


class FakeOpenAIClient:
    def __init__(self, completions=None, errors=None):
        handler = FakeChatCompletions(completions, errors)
        self.chat = FakeChat(handler)


class TestOpenRouterBackend:
    def _make(self, client=None, **kwargs):
        defaults = dict(
            model="test/model",
            api_key="test-key",
            max_retries=3,
            initial_backoff_s=0.001,
        )
        defaults.update(kwargs)
        backend = object.__new__(OpenRouterBackend)
        for k, v in defaults.items():
            object.__setattr__(backend, k, v)
        object.__setattr__(backend, "log", None)
        object.__setattr__(backend, "_client", client or FakeOpenAIClient())
        return backend

    def test_basic_call(self):
        client = FakeOpenAIClient()
        backend = self._make(client=client)
        resp = backend.call("hello")
        assert resp.text == "model output"
        assert resp.input_tokens == 800
        assert client.chat.completions.calls == 1

    def test_none_content_returns_empty(self):
        choice = FakeChoice()
        choice.message.content = None
        completion = FakeCompletion(choices=[choice])
        client = FakeOpenAIClient(completions=[completion])
        backend = self._make(client=client)
        resp = backend.call("p")
        assert resp.text == ""

    def test_no_usage_returns_none_tokens(self):
        completion = FakeCompletion()
        completion.usage = None
        client = FakeOpenAIClient(completions=[completion])
        backend = self._make(client=client)
        resp = backend.call("p")
        assert resp.input_tokens is None

    def test_retry_on_transient_error(self):
        handler = FakeChatCompletions(
            errors=[ConnectionError("flake")],
            completions=[FakeCompletion()],
        )
        client = FakeOpenAIClient()
        client.chat.completions = handler
        backend = self._make(client=client)
        resp = backend.call("p")
        assert resp.text == "model output"
        assert handler.calls == 2

    def test_exhausted_retries_raises(self):
        handler = FakeChatCompletions(
            errors=[ConnectionError("fail")] * 3,
        )
        client = FakeOpenAIClient()
        client.chat.completions = handler
        backend = self._make(client=client, max_retries=3)
        with pytest.raises(RuntimeError, match="failed after 3 attempts"):
            backend.call("p")

    def test_missing_api_key_raises(self, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
            OpenRouterBackend(api_key=None)

    def test_api_key_from_env(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "env-key")
        backend = object.__new__(OpenRouterBackend)
        object.__setattr__(backend, "api_key", None)
        object.__setattr__(backend, "model", "test/m")
        object.__setattr__(backend, "max_tokens", 100)
        object.__setattr__(backend, "temperature", 0.0)
        object.__setattr__(backend, "timeout_s", 10.0)
        object.__setattr__(backend, "max_retries", 1)
        object.__setattr__(backend, "initial_backoff_s", 0.001)
        object.__setattr__(backend, "log", None)
        object.__setattr__(backend, "_client", None)
        OpenRouterBackend.__post_init__(backend)
        assert backend.api_key == "env-key"
        assert backend._client is not None
