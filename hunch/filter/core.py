"""Core filter logic: dedup + novelty checks.

The filter is stateful within a session — it tracks prior hunches
so dedup comparisons stay O(K) per new hunch rather than O(N^2) over
the full session.

LLM calls use the same dual-mode pattern as the Critic (SDK when an
Anthropic client is provided, CLI fallback otherwise).
"""

from __future__ import annotations

import json
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from hunch.critic.protocol import Hunch
from hunch.journal.hunches import HunchRecord


# ---------------------------------------------------------------------------
# Prompt loading
# ---------------------------------------------------------------------------

_PROMPTS_DIR = Path(__file__).parent / "prompts"


def _load_prompt(name: str) -> str:
    return (_PROMPTS_DIR / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FilterResult:
    """Outcome of filtering a single hunch."""
    hunch: Hunch
    passed: bool
    reason: str = ""
    filter_type: str = ""
    duplicate_of: str | None = None


# ---------------------------------------------------------------------------
# Dialogue rendering (for novelty check)
# ---------------------------------------------------------------------------

_DIALOGUE_TYPES = frozenset({"user_text", "assistant_text"})
_MAX_CONTEXT_CHARS = 80_000


def _render_dialogue(
    conversation_path: Path,
    bookmark_prev: int,
    bookmark_now: int,
) -> str:
    """Render dialogue events up to bookmark_now from conversation.jsonl.

    Places a divider between bookmark_prev and bookmark_now so the judge
    can see what was in the triggering window vs prior context.
    """
    if not conversation_path.exists():
        return ""
    lines: list[str] = []
    divider_inserted = False
    with open(conversation_path) as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                d = json.loads(raw)
            except json.JSONDecodeError:
                continue
            seq = d.get("tick_seq")
            if not isinstance(seq, int) or seq > bookmark_now:
                continue
            etype = d.get("type", "")
            if etype not in _DIALOGUE_TYPES:
                continue
            text = d.get("text", "").strip()
            if not text:
                continue
            if not divider_inserted and seq > bookmark_prev:
                lines.append(
                    f"--- begin triggering window "
                    f"(tick_seq {bookmark_prev + 1}..{bookmark_now}) ---"
                )
                divider_inserted = True
            role = "Researcher" if etype == "assistant_text" else "Scientist"
            lines.append(f"[{role}] (tick {seq}): {text}")

    rendered = "\n\n".join(lines)
    if len(rendered) > _MAX_CONTEXT_CHARS:
        rendered = rendered[-_MAX_CONTEXT_CHARS:]
    return rendered


# ---------------------------------------------------------------------------
# LLM call (dual-mode: SDK or CLI)
# ---------------------------------------------------------------------------

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

DEFAULT_DEDUP_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_NOVELTY_MODEL = "claude-sonnet-4-5-20250929"


def _parse_json_response(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = _JSON_RE.search(text)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
    return None


def _call_llm(
    prompt: str,
    model: str,
    client: Any | None,
    timeout_s: float = 120.0,
) -> str:
    if client is not None:
        return _call_via_sdk(prompt, model, client)
    return _call_via_cli(prompt, model, timeout_s)


def _call_via_sdk(prompt: str, model: str, client: Any) -> str:
    response = client.messages.create(
        model=model,
        max_tokens=256,
        temperature=0.0,
        messages=[{"role": "user", "content": prompt}],
    )
    content = getattr(response, "content", None) or []
    if content:
        first = content[0]
        t = getattr(first, "text", None)
        if isinstance(t, str):
            return t
        if isinstance(first, dict):
            return str(first.get("text", ""))
    raise RuntimeError(f"Anthropic SDK returned empty content: {response}")


def _call_via_cli(prompt: str, model: str, timeout_s: float) -> str:
    result = subprocess.run(
        ["claude", "--print", "--model", model, "--output-format", "json"],
        input=prompt,
        cwd="/tmp",
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"claude --print exited {result.returncode}: "
            f"{(result.stderr or result.stdout)[:200]}"
        )
    try:
        envelope = json.loads(result.stdout)
        return envelope.get("result", "")
    except json.JSONDecodeError:
        return result.stdout


# ---------------------------------------------------------------------------
# HunchFilter
# ---------------------------------------------------------------------------

@dataclass
class HunchFilter:
    """Filters hunches for duplicates and already-raised concerns.

    Stateful: tracks prior hunches within a session for dedup.
    Call ``filter_batch`` after each tick with the critic's output.

    Args:
        replay_dir: path to the replay buffer (for reading conversation.jsonl).
        client: optional Anthropic SDK client. None = CLI fallback.
        dedup_model: model for dedup checks. Defaults to Haiku.
        novelty_model: model for novelty checks. Defaults to Sonnet.
        dedup_backend: optional Backend instance for dedup calls.
            When set, uses this instead of client/CLI.
        novelty_backend: optional Backend instance for novelty calls.
        dedup_window: max prior hunches to compare against (avoids quadratic).
        enabled: master switch. When False, all hunches pass through.
        log: optional log sink.
    """
    replay_dir: Path
    client: Any | None = None
    dedup_model: str = DEFAULT_DEDUP_MODEL
    novelty_model: str = DEFAULT_NOVELTY_MODEL
    dedup_backend: Any | None = None
    novelty_backend: Any | None = None
    dedup_window: int = 10
    max_retries: int = 3
    enabled: bool = True
    log: Callable[[str], None] | None = None

    _prior_hunches: list[_PriorHunch] = field(
        default_factory=list, init=False, repr=False,
    )

    def __post_init__(self) -> None:
        if not self.enabled:
            return
        needs_default_llm = (
            self.dedup_backend is None or self.novelty_backend is None
        )
        if needs_default_llm and self.client is None:
            import shutil
            if not shutil.which("claude"):
                raise RuntimeError(
                    "HunchFilter is enabled but has no LLM backend: "
                    "no Anthropic client (ANTHROPIC_API_KEY not set), "
                    "no Backend instances, and 'claude' CLI not found on PATH."
                )

    def init_from_existing(self, existing: list[HunchRecord]) -> None:
        """Seed the dedup window from hunches that were already on disk
        (e.g. from a prior session or earlier ticks in this run)."""
        for rec in existing:
            self._prior_hunches.append(
                _PriorHunch(
                    hunch_id=rec.hunch_id,
                    smell=rec.smell,
                    description=rec.description,
                )
            )

    def filter_batch(
        self,
        hunches: list[Hunch],
        bookmark_prev: int,
        bookmark_now: int,
        hunch_ids: list[str] | None = None,
    ) -> list[FilterResult]:
        """Filter a batch of hunches (from one tick).

        Returns a FilterResult per hunch. Hunches that pass are also
        added to the internal dedup window for future comparisons.

        ``hunch_ids``, when provided, are the pre-allocated ids for each
        hunch. They are stored in the dedup window so that ``duplicate_of``
        pointers on future FilterResults are meaningful.
        """
        if not self.enabled:
            return [FilterResult(hunch=h, passed=True) for h in hunches]

        ids = hunch_ids or [""] * len(hunches)
        results: list[FilterResult] = []
        for hunch, hid in zip(hunches, ids):
            result = self._check_one(hunch, bookmark_prev, bookmark_now)
            results.append(result)
            if result.passed:
                self._prior_hunches.append(
                    _PriorHunch(
                        hunch_id=hid,
                        smell=hunch.smell,
                        description=hunch.description,
                    )
                )
        return results

    def _check_one(
        self, hunch: Hunch, bookmark_prev: int, bookmark_now: int,
    ) -> FilterResult:
        dup = self._check_dedup(hunch)
        if dup is not None:
            if self.log:
                self.log(f"  [filter] dedup: {hunch.smell[:60]}")
            return dup

        nov = self._check_novelty(hunch, bookmark_prev, bookmark_now)
        if nov is not None:
            if self.log:
                self.log(f"  [filter] already raised: {hunch.smell[:60]}")
            return nov

        return FilterResult(hunch=hunch, passed=True)

    # -- internal LLM dispatch -----------------------------------------------

    def _call_dedup(self, prompt: str) -> str:
        if self.dedup_backend is not None:
            return self.dedup_backend.call(prompt).text
        return _call_llm(prompt, self.dedup_model, self.client)

    def _call_novelty(self, prompt: str) -> str:
        if self.novelty_backend is not None:
            return self.novelty_backend.call(prompt).text
        return _call_llm(prompt, self.novelty_model, self.client)

    def _call_and_parse(
        self, call_fn: Callable[[], str], label: str,
    ) -> dict[str, Any]:
        """Call an LLM and parse the JSON response, retrying on failure."""
        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                raw = call_fn()
                parsed = _parse_json_response(raw)
                if parsed is None:
                    raise ValueError(
                        f"{label}: unparseable LLM response: {raw[:200]}"
                    )
                return parsed
            except Exception as exc:
                last_exc = exc
                if self.log:
                    self.log(
                        f"  [filter] {label} attempt {attempt}/"
                        f"{self.max_retries} failed: {exc}"
                    )
        raise last_exc  # type: ignore[misc]

    # -- dedup ---------------------------------------------------------------

    def _check_dedup(self, hunch: Hunch) -> FilterResult | None:
        window = self._prior_hunches[-self.dedup_window:]
        if not window:
            return None

        template = _load_prompt("judge_dedup.md")

        def _check_one_prior(prior: _PriorHunch) -> str | None:
            prompt = template.format(
                smell_a=prior.smell,
                description_a=prior.description,
                smell_b=hunch.smell,
                description_b=hunch.description,
            )
            parsed = self._call_and_parse(
                lambda: self._call_dedup(prompt), "dedup",
            )
            if parsed.get("duplicate") is True:
                return parsed.get("reasoning", "duplicate of prior hunch")
            return None

        with ThreadPoolExecutor(max_workers=min(len(window), 5)) as pool:
            futures = {
                pool.submit(_check_one_prior, p): p
                for p in reversed(window)
            }
            for future in as_completed(futures):
                reason = future.result()
                if reason is not None:
                    prior = futures[future]
                    for f in futures:
                        f.cancel()
                    return FilterResult(
                        hunch=hunch,
                        passed=False,
                        reason=reason,
                        filter_type="dedup",
                        duplicate_of=prior.hunch_id or None,
                    )
        return None

    # -- novelty -------------------------------------------------------------

    def _check_novelty(
        self, hunch: Hunch, bookmark_prev: int, bookmark_now: int,
    ) -> FilterResult | None:
        conv_path = self.replay_dir / "conversation.jsonl"
        dialogue = _render_dialogue(conv_path, bookmark_prev, bookmark_now)
        if not dialogue:
            return None

        template = _load_prompt("judge_novelty.md")
        prompt = template.format(
            hunch_smell=hunch.smell,
            hunch_description=hunch.description,
            dialogue_context=dialogue,
        )
        parsed = self._call_and_parse(
            lambda: self._call_novelty(prompt), "novelty",
        )
        if parsed.get("already_raised") is True:
            who = parsed.get("who", "unknown")
            reason = parsed.get("reasoning", f"already raised by {who}")
            return FilterResult(
                hunch=hunch,
                passed=False,
                reason=reason,
                filter_type="novelty",
            )
        return None


# ---------------------------------------------------------------------------
# Internal types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _PriorHunch:
    hunch_id: str
    smell: str
    description: str
