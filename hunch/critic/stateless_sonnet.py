"""Stateless-Sonnet Critic (critic v0).

In-process Python wrapping one Anthropic API call per tick. See
`docs/critic_v0.md` — this is the minimal implementation that speaks
the Critic protocol and emits plausible hunches end-to-end.

Each tick:
  1. Read the replay buffer via `hunch.critic.context.build_tick_context`.
  2. Splice the rendered blocks into the prompt template
     (`hunch/critic/prompts/nose_v0.md`).
  3. Call Sonnet.
  4. Parse the JSON response into `Hunch` objects (0 or 1 in v0).
  5. Return them to the framework, which writes emit events.

Errors on the API call or response parsing are logged and swallowed:
the framework keeps ticking, and the Critic simply emits nothing this
turn. Dropping a tick is strictly better than crashing the loop.

The API client is injected via the `client` parameter so tests can
substitute a fake without patching the Anthropic SDK directly. In
production, `SonnetCritic()` constructs a real client from the
environment on `init`.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from hunch.critic.context import ContextConfig, TickContext, build_tick_context
from hunch.critic.protocol import Hunch, TriggeringRefs


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SonnetCriticConfig:
    model: str = "claude-sonnet-4-5-20250929"
    max_tokens: int = 1024
    context: ContextConfig = field(default_factory=ContextConfig)
    prompt_path: Path | None = None  # None → packaged default
    temperature: float = 0.0


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------

def _load_prompt_template(prompt_path: Path | None) -> str:
    if prompt_path is not None:
        return Path(prompt_path).read_text()
    # Default: the file next to this module.
    here = Path(__file__).resolve().parent
    return (here / "prompts" / "nose_v0.md").read_text()


def render_prompt(template: str, ctx: TickContext) -> str:
    """Splice the tick context into the prompt template.

    Placeholders are `{name}` markers in the template. We use plain
    string replacement rather than str.format so that any stray `{}`
    in artifact content doesn't blow up.
    """
    return (
        template
        .replace("{prior_hunches_block}", ctx.prior_hunches_block)
        .replace("{recent_chunks_block}", ctx.recent_chunks_block)
        .replace("{artifacts_block}", ctx.artifacts_block)
    )


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^```(?:json)?\s*\n(.*?)\n```\s*$", re.DOTALL)


def _strip_fences(text: str) -> str:
    text = text.strip()
    m = _FENCE_RE.match(text)
    if m:
        return m.group(1).strip()
    return text


def parse_response(text: str) -> list[Hunch]:
    """Parse the model response into a list of Hunches.

    The prompt asks for a raw JSON array. We tolerate accidental code
    fences and prose-preamble on a best-effort basis — if parsing fails
    at any level we return `[]` and let the caller log.
    """
    stripped = _strip_fences(text)
    # If there's leading prose before the `[`, try to grab the array.
    bracket = stripped.find("[")
    if bracket == -1:
        return []
    candidate = stripped[bracket:]
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []

    hunches: list[Hunch] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        smell = item.get("smell")
        description = item.get("description")
        if not isinstance(smell, str) or not isinstance(description, str):
            continue
        if not smell.strip() or not description.strip():
            continue
        refs_raw = item.get("triggering_refs") or {}
        if not isinstance(refs_raw, dict):
            refs_raw = {}
        chunks = refs_raw.get("chunks") or []
        artifacts = refs_raw.get("artifacts") or []
        if not isinstance(chunks, list):
            chunks = []
        if not isinstance(artifacts, list):
            artifacts = []
        hunches.append(
            Hunch(
                smell=smell.strip(),
                description=description.strip(),
                triggering_refs=TriggeringRefs(
                    chunks=[str(c) for c in chunks],
                    artifacts=[str(a) for a in artifacts],
                ),
            )
        )
    return hunches


# ---------------------------------------------------------------------------
# The Critic
# ---------------------------------------------------------------------------

# A CallableClient is anything with `messages.create(...)` that returns an
# object with `.content[0].text`. We type it loosely to avoid coupling to
# the anthropic SDK at import time (so the module imports even on machines
# without the SDK — tests inject a fake).
CallableClient = Any


@dataclass
class SonnetCritic:
    """Stateless Sonnet-backed Critic.

    Instances are constructed by the framework via `critic_factory`
    (see `hunch/run.py`). Holds prompt template, config, replay-dir
    handle (set in `init`), and an Anthropic client (constructed in
    `init` or injected for tests).
    """
    config: SonnetCriticConfig = field(default_factory=SonnetCriticConfig)
    client: CallableClient | None = None
    log: Callable[[str], None] | None = None

    _template: str = field(default="", init=False, repr=False)
    _replay_dir: Path | None = field(default=None, init=False, repr=False)
    _initialized: bool = field(default=False, init=False, repr=False)

    # ------------------------------------------------------------------
    # Critic protocol
    # ------------------------------------------------------------------

    def init(self, config: dict[str, Any]) -> None:
        if self._initialized:
            raise RuntimeError("SonnetCritic.init called twice")
        replay_dir = config.get("replay_dir")
        if not replay_dir:
            raise RuntimeError("SonnetCritic.init: 'replay_dir' missing from config")
        self._replay_dir = Path(replay_dir)
        self._template = _load_prompt_template(self.config.prompt_path)
        if self.client is None:
            self.client = _build_default_client()
        self._initialized = True

    def tick(
        self,
        tick_id: str,
        bookmark_prev: int,
        bookmark_now: int,
    ) -> list[Hunch]:
        if not self._initialized:
            raise RuntimeError("SonnetCritic.tick called before init")
        if self._replay_dir is None:
            raise RuntimeError("SonnetCritic has no replay_dir")

        ctx = build_tick_context(self._replay_dir, self.config.context)
        prompt = render_prompt(self._template, ctx)

        try:
            text = self._call_model(prompt)
        except Exception as e:  # pylint: disable=broad-except
            self._log(f"[critic] model call failed: {e}")
            return []

        hunches = parse_response(text)
        if not hunches:
            self._log("[critic] (no hunches this tick)")
        return hunches

    def shutdown(self) -> None:
        # Stateless — nothing to flush.
        self._initialized = False

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _call_model(self, prompt: str) -> str:
        if self.client is None:
            raise RuntimeError("SonnetCritic has no client")
        response = self.client.messages.create(
            model=self.config.model,
            max_tokens=self.config.max_tokens,
            temperature=self.config.temperature,
            messages=[{"role": "user", "content": prompt}],
        )
        # Anthropic SDK returns Message with .content = list[ContentBlock]
        content = getattr(response, "content", None) or []
        if not content:
            return ""
        first = content[0]
        text = getattr(first, "text", None)
        if isinstance(text, str):
            return text
        # Fallback: if someone injected a dict-style fake.
        if isinstance(first, dict):
            return str(first.get("text", ""))
        return ""

    def _log(self, msg: str) -> None:
        if self.log is not None:
            self.log(msg)


# ---------------------------------------------------------------------------
# Default client construction
# ---------------------------------------------------------------------------

def _build_default_client() -> CallableClient:
    """Build an Anthropic client from the environment.

    Deferred so machines without the SDK can still import this module
    (tests use an injected fake client).
    """
    try:
        import anthropic  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "anthropic SDK is required for SonnetCritic; "
            "install with `pipx inject hunch anthropic` "
            "or pass a client= at construction time"
        ) from e
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.stderr.write(
            "SonnetCritic: ANTHROPIC_API_KEY is not set — API calls will fail\n"
        )
    return anthropic.Anthropic()
