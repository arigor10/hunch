"""Tests for the Sonnet-backed Critic.

The Anthropic API call is stubbed via an injected client so these
tests run offline. We cover prompt rendering, response parsing,
and error swallowing.
"""

from __future__ import annotations

import pytest

from hunch.critic.context import TickContext
from hunch.critic.stateless_sonnet import (
    SonnetCritic,
    SonnetCriticConfig,
    parse_response,
    render_prompt,
)


# ---------------------------------------------------------------------------
# Fake Anthropic client
# ---------------------------------------------------------------------------

class _FakeContentBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeMessage:
    def __init__(self, text: str) -> None:
        self.content = [_FakeContentBlock(text)]


class _FakeMessages:
    def __init__(self, parent):
        self.parent = parent

    def create(self, **kwargs):
        self.parent.last_kwargs = kwargs
        self.parent.calls += 1
        if self.parent.raise_exc is not None:
            raise self.parent.raise_exc
        return _FakeMessage(self.parent.next_response)


class FakeClient:
    def __init__(self, response: str = "[]"):
        self.next_response = response
        self.calls = 0
        self.last_kwargs = None
        self.raise_exc: Exception | None = None
        self.messages = _FakeMessages(self)


# ---------------------------------------------------------------------------
# render_prompt
# ---------------------------------------------------------------------------

def test_render_prompt_substitutes_all_blocks():
    template = (
        "Prior: {prior_hunches_block}\n"
        "Recent: {recent_chunks_block}\n"
        "Art: {artifacts_block}\n"
    )
    ctx = TickContext(
        prior_hunches_block="P",
        recent_chunks_block="R",
        artifacts_block="A",
    )
    out = render_prompt(template, ctx)
    assert out == "Prior: P\nRecent: R\nArt: A\n"


def test_render_prompt_tolerates_braces_in_content():
    template = "X {artifacts_block} Y"
    ctx = TickContext("p", "r", "a {stray} brace")
    out = render_prompt(template, ctx)
    assert "{stray}" in out


# ---------------------------------------------------------------------------
# parse_response
# ---------------------------------------------------------------------------

def test_parse_response_empty_array():
    assert parse_response("[]") == []


def test_parse_response_with_code_fence():
    fenced = "```json\n[]\n```"
    assert parse_response(fenced) == []


def test_parse_response_single_hunch():
    text = """[
      {"smell": "R² disagrees with yesterday's fit",
       "description": "Yesterday c-0031 reported 0.3–0.5, today writeup says 0.94.",
       "triggering_refs": {"chunks": ["c-0031"], "artifacts": ["writeups/exp.md"]}}
    ]"""
    hunches = parse_response(text)
    assert len(hunches) == 1
    h = hunches[0]
    assert h.smell.startswith("R² disagrees")
    assert h.triggering_refs.chunks == ["c-0031"]
    assert h.triggering_refs.artifacts == ["writeups/exp.md"]


def test_parse_response_skips_missing_fields():
    text = '[{"smell": "x"}]'  # no description
    assert parse_response(text) == []


def test_parse_response_skips_blank_fields():
    text = '[{"smell": "", "description": ""}]'
    assert parse_response(text) == []


def test_parse_response_strips_prose_preamble():
    text = "Here's what I found:\n[{\"smell\":\"s\",\"description\":\"d\"}]"
    hunches = parse_response(text)
    assert len(hunches) == 1
    assert hunches[0].smell == "s"


def test_parse_response_not_json_returns_empty():
    assert parse_response("garbage with no brackets at all") == []


def test_parse_response_not_a_list_returns_empty():
    assert parse_response('{"smell": "x", "description": "y"}') == []


def test_parse_response_defaults_missing_refs():
    text = '[{"smell": "s", "description": "d"}]'
    hunches = parse_response(text)
    assert len(hunches) == 1
    assert hunches[0].triggering_refs.chunks == []
    assert hunches[0].triggering_refs.artifacts == []


# ---------------------------------------------------------------------------
# SonnetCritic lifecycle + tick
# ---------------------------------------------------------------------------

def test_init_requires_replay_dir(tmp_path):
    critic = SonnetCritic(client=FakeClient())
    with pytest.raises(RuntimeError, match="replay_dir"):
        critic.init({})


def test_tick_before_init_raises(tmp_path):
    critic = SonnetCritic(client=FakeClient())
    with pytest.raises(RuntimeError, match="before init"):
        critic.tick(tick_id="t-0001", bookmark_prev=0, bookmark_now=0)


def test_init_called_twice_raises(tmp_path):
    critic = SonnetCritic(client=FakeClient())
    critic.init({"replay_dir": str(tmp_path)})
    with pytest.raises(RuntimeError, match="twice"):
        critic.init({"replay_dir": str(tmp_path)})


def test_tick_empty_response_returns_no_hunches(tmp_path):
    client = FakeClient(response="[]")
    critic = SonnetCritic(client=client)
    critic.init({"replay_dir": str(tmp_path)})
    out = critic.tick(tick_id="t-0001", bookmark_prev=0, bookmark_now=0)
    assert out == []
    assert client.calls == 1


def test_tick_returns_parsed_hunches(tmp_path):
    resp = '[{"smell":"s","description":"d","triggering_refs":{"chunks":["c-1"]}}]'
    client = FakeClient(response=resp)
    critic = SonnetCritic(client=client)
    critic.init({"replay_dir": str(tmp_path)})
    out = critic.tick(tick_id="t-0001", bookmark_prev=0, bookmark_now=0)
    assert len(out) == 1
    assert out[0].smell == "s"
    assert out[0].triggering_refs.chunks == ["c-1"]


def test_tick_swallows_api_exception(tmp_path):
    client = FakeClient()
    client.raise_exc = RuntimeError("simulated network failure")
    logs: list[str] = []
    critic = SonnetCritic(client=client, log=logs.append)
    critic.init({"replay_dir": str(tmp_path)})
    out = critic.tick(tick_id="t-0001", bookmark_prev=0, bookmark_now=0)
    assert out == []
    assert any("simulated" in m for m in logs)


def test_tick_uses_configured_model_and_max_tokens(tmp_path):
    client = FakeClient(response="[]")
    cfg = SonnetCriticConfig(model="claude-test", max_tokens=42, temperature=0.3)
    critic = SonnetCritic(config=cfg, client=client)
    critic.init({"replay_dir": str(tmp_path)})
    critic.tick(tick_id="t-0001", bookmark_prev=0, bookmark_now=0)
    assert client.last_kwargs["model"] == "claude-test"
    assert client.last_kwargs["max_tokens"] == 42
    assert client.last_kwargs["temperature"] == 0.3
    msgs = client.last_kwargs["messages"]
    assert len(msgs) == 1
    assert msgs[0]["role"] == "user"
    # Sanity: the prompt should include the stable template text.
    assert "You are the Critic" in msgs[0]["content"]


def test_shutdown_flips_initialized_flag(tmp_path):
    critic = SonnetCritic(client=FakeClient())
    critic.init({"replay_dir": str(tmp_path)})
    critic.shutdown()
    with pytest.raises(RuntimeError, match="before init"):
        critic.tick(tick_id="t-0001", bookmark_prev=0, bookmark_now=0)
