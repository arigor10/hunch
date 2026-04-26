"""TOML config loading for model backends."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore[no-redef]


@dataclass(frozen=True)
class BackendConfig:
    """Parsed backend configuration from a TOML file."""
    type: str
    model: str
    api_key: str | None = None
    max_tokens: int = 8192
    temperature: float = 0.0
    timeout_s: float = 600.0
    max_retries: int = 5
    initial_backoff_s: float = 5.0
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EngineConfig:
    """Engine-level config from the TOML file (optional section)."""
    low_watermark: int = 140_000
    high_watermark: int = 180_000
    max_consecutive_failures: int = 3
    prompt_path: str | None = None


@dataclass(frozen=True)
class FullConfig:
    """Top-level config combining backend + engine settings."""
    backend: BackendConfig
    engine: EngineConfig = field(default_factory=EngineConfig)


def load_config(path: Path) -> FullConfig:
    """Load a TOML config file and return a FullConfig."""
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    be = raw.get("backend", {})
    auth = be.get("auth", {})
    params = be.get("params", {})

    api_key: str | None = None
    env_var = auth.get("env_var")
    if env_var:
        api_key = os.environ.get(env_var)
        if not api_key:
            env_path = auth.get("env_file")
            if env_path:
                api_key = _read_env_file(Path(env_path), env_var)

    backend = BackendConfig(
        type=be.get("type", "claude_cli"),
        model=be.get("model", ""),
        api_key=api_key,
        max_tokens=params.get("max_tokens", 8192),
        temperature=params.get("temperature", 0.0),
        timeout_s=params.get("timeout_s", 600.0),
        max_retries=params.get("max_retries", 5),
        initial_backoff_s=params.get("initial_backoff_s", 5.0),
        extra={k: v for k, v in params.items()
               if k not in {"max_tokens", "temperature", "timeout_s",
                            "max_retries", "initial_backoff_s"}},
    )

    eng_raw = raw.get("engine", {})
    engine = EngineConfig(
        low_watermark=eng_raw.get("low_watermark", 140_000),
        high_watermark=eng_raw.get("high_watermark", 180_000),
        max_consecutive_failures=eng_raw.get("max_consecutive_failures", 3),
        prompt_path=eng_raw.get("prompt_path"),
    )

    return FullConfig(backend=backend, engine=engine)


def _read_env_file(path: Path, key: str) -> str | None:
    """Read a key=value from a .env file."""
    if not path.exists():
        return None
    for line in path.read_text().splitlines():
        line = line.strip()
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1]
    return None
