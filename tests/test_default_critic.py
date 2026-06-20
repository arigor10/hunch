"""The `hunch run` default critic = the bundled Sonnet-via-claude-CLI config."""

from __future__ import annotations

import argparse

from hunch.cli import _critic_label, _load_default_config, _resolve_critic_factory
from hunch.critic.stub import StubCritic


def _noop_log(*a, **k):
    return None


def test_bundled_default_config_loads():
    full = _load_default_config()
    assert full.backend.type == "claude_cli"
    assert "sonnet" in full.backend.model
    assert full.engine.low_watermark == 140_000


def test_default_resolves_to_engine_not_stub():
    # No --critic and no --config → bundled config → a real CriticEngine factory.
    factory = _resolve_critic_factory(None, log=_noop_log, config_path=None)
    assert factory is not StubCritic
    assert callable(factory)


def test_explicit_stub_still_resolves_to_stub():
    # An explicit --critic stub must still mean stub (not the new default).
    factory = _resolve_critic_factory("stub", log=_noop_log, config_path=None)
    assert factory is StubCritic


def test_critic_label_describes_the_default():
    ns = argparse.Namespace(config=None, critic=None)
    label = _critic_label(ns)
    assert "claude_cli" in label
    assert "sonnet" in label
    assert "bundled default" in label


def test_critic_label_explicit_name():
    ns = argparse.Namespace(config=None, critic="stub")
    assert _critic_label(ns) == "stub"
