"""Tests for TOML config loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from hunch.backend.config import BackendConfig, EngineConfig, FullConfig, load_config


def _write_toml(tmp_path: Path, content: str, name: str = "test.toml") -> Path:
    p = tmp_path / name
    p.write_text(content)
    return p


class TestLoadConfig:
    def test_minimal_config(self, tmp_path):
        cfg = load_config(_write_toml(tmp_path, """
[backend]
type = "claude_cli"
model = "claude-sonnet-4-5-20250929"
"""))
        assert cfg.backend.type == "claude_cli"
        assert cfg.backend.model == "claude-sonnet-4-5-20250929"
        assert cfg.backend.max_tokens == 8192
        assert cfg.backend.temperature == 0.0
        assert cfg.engine.low_watermark == 140_000
        assert cfg.engine.high_watermark == 180_000

    def test_full_config(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TEST_API_KEY", "sk-test-123")
        cfg = load_config(_write_toml(tmp_path, """
[backend]
type = "openrouter"
model = "deepseek/deepseek-v4-pro"

[backend.auth]
env_var = "TEST_API_KEY"

[backend.params]
max_tokens = 16384
temperature = 0.3
timeout_s = 300.0
max_retries = 3
initial_backoff_s = 2.0

[engine]
low_watermark = 100000
high_watermark = 150000
max_consecutive_failures = 5
prompt_path = "/custom/prompt.md"
"""))
        assert cfg.backend.type == "openrouter"
        assert cfg.backend.model == "deepseek/deepseek-v4-pro"
        assert cfg.backend.api_key == "sk-test-123"
        assert cfg.backend.max_tokens == 16384
        assert cfg.backend.temperature == 0.3
        assert cfg.backend.timeout_s == 300.0
        assert cfg.backend.max_retries == 3
        assert cfg.backend.initial_backoff_s == 2.0
        assert cfg.engine.low_watermark == 100_000
        assert cfg.engine.high_watermark == 150_000
        assert cfg.engine.max_consecutive_failures == 5
        assert cfg.engine.prompt_path == "/custom/prompt.md"

    def test_api_key_missing_from_env(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MISSING_KEY", raising=False)
        cfg = load_config(_write_toml(tmp_path, """
[backend]
type = "openrouter"
model = "test"

[backend.auth]
env_var = "MISSING_KEY"
"""))
        assert cfg.backend.api_key is None

    def test_api_key_from_env_file(self, tmp_path, monkeypatch):
        monkeypatch.delenv("FILE_KEY", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text("OTHER=nope\nFILE_KEY=from-file\n")
        cfg = load_config(_write_toml(tmp_path, f"""
[backend]
type = "openrouter"
model = "test"

[backend.auth]
env_var = "FILE_KEY"
env_file = "{env_file}"
"""))
        assert cfg.backend.api_key == "from-file"

    def test_env_file_quoted_values(self, tmp_path, monkeypatch):
        monkeypatch.delenv("Q_KEY", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text('Q_KEY="my-secret-key"\n')
        cfg = load_config(_write_toml(tmp_path, f"""
[backend]
type = "openrouter"
model = "test"

[backend.auth]
env_var = "Q_KEY"
env_file = "{env_file}"
"""))
        assert cfg.backend.api_key == "my-secret-key"

    def test_env_file_single_quoted(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SQ_KEY", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text("SQ_KEY='single-quoted'\n")
        cfg = load_config(_write_toml(tmp_path, f"""
[backend]
type = "openrouter"
model = "test"

[backend.auth]
env_var = "SQ_KEY"
env_file = "{env_file}"
"""))
        assert cfg.backend.api_key == "single-quoted"

    def test_env_file_export_prefix(self, tmp_path, monkeypatch):
        monkeypatch.delenv("EX_KEY", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text("export EX_KEY=exported-value\n")
        cfg = load_config(_write_toml(tmp_path, f"""
[backend]
type = "openrouter"
model = "test"

[backend.auth]
env_var = "EX_KEY"
env_file = "{env_file}"
"""))
        assert cfg.backend.api_key == "exported-value"

    def test_env_file_inline_comment(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CM_KEY", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text("CM_KEY=the-value # this is a comment\n")
        cfg = load_config(_write_toml(tmp_path, f"""
[backend]
type = "openrouter"
model = "test"

[backend.auth]
env_var = "CM_KEY"
env_file = "{env_file}"
"""))
        assert cfg.backend.api_key == "the-value"

    def test_extra_params_preserved(self, tmp_path):
        cfg = load_config(_write_toml(tmp_path, """
[backend]
type = "openrouter"
model = "test"

[backend.params]
max_tokens = 4096
custom_flag = true
routing = "siliconflow"
"""))
        assert cfg.backend.extra == {"custom_flag": True, "routing": "siliconflow"}

    def test_defaults_without_sections(self, tmp_path):
        cfg = load_config(_write_toml(tmp_path, ""))
        assert cfg.backend.type == "claude_cli"
        assert cfg.backend.model == ""
        assert cfg.engine.low_watermark == 140_000

    def test_frozen_dataclasses(self, tmp_path):
        cfg = load_config(_write_toml(tmp_path, """
[backend]
type = "claude_cli"
model = "test"
"""))
        with pytest.raises(AttributeError):
            cfg.backend.model = "changed"
        with pytest.raises(AttributeError):
            cfg.engine.low_watermark = 999
