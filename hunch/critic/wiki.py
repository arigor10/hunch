"""Wiki Critic — agentic critic that maintains a persistent wiki.

Each tick invokes ``claude -p`` as an agentic session with file tools.
The agent reads a rendered conversation block, updates the wiki, and
writes hunches to ``pending_hunches.jsonl``.

Implements the same ``Critic`` protocol as ``SonnetCritic`` / ``CriticEngine``,
so it plugs into the existing replay driver and CLI unchanged.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from hunch.critic.protocol import Hunch, TriggeringRefs
from hunch.critic.wiki_contract import validate_contract, validate_wiki
from hunch.critic.wiki_renderer import read_events_in_range, render_current_block
from hunch.critic.wiki_workspace import (
    copy_artifact_snapshots,
    copy_events_to_workspace,
    init_workspace,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class WikiCriticConfig:
    model: str = "claude-sonnet-4-5-20250929"
    claude_md_path: Path | None = None
    contract_spec_path: Path | None = None
    seed_docs: list[Path] = field(default_factory=list)
    timeout_s: float = 600.0
    max_contract_violations: int = 5
    dry_run: bool = False


# ---------------------------------------------------------------------------
# WikiCritic
# ---------------------------------------------------------------------------

class WikiCritic:
    def __init__(
        self,
        config: WikiCriticConfig | None = None,
        log: Callable[[str], None] | None = None,
    ):
        self.config = config or WikiCriticConfig()
        self._log = log

        self._workspace: Path | None = None
        self._replay_dir: Path | None = None
        self._last_copied_seq: int = 0
        self._tick_count: int = 0
        self._consecutive_failures: int = 0
        self._violations: list[str] = []

        self._total_calls: int = 0
        self._total_input_tokens: int = 0
        self._total_output_tokens: int = 0
        self._total_cost_usd: float = 0.0
        self._total_failures: int = 0

    # ---------------------------------------------------------------
    # Critic protocol
    # ---------------------------------------------------------------

    def init(self, config: dict[str, Any]) -> None:
        self._replay_dir = Path(config["replay_dir"])
        workspace_str = config.get("output_dir") or config.get("workspace_dir")
        if not workspace_str:
            raise ValueError(
                "WikiCritic requires 'output_dir' in critic config "
                "(the workspace where the wiki lives)"
            )
        self._workspace = Path(workspace_str)

        init_workspace(
            workspace=self._workspace,
            claude_md_path=self.config.claude_md_path,
            contract_spec_path=self.config.contract_spec_path,
            seed_docs=self.config.seed_docs or None,
        )
        self._last_copied_seq = self._detect_last_copied_seq()
        self._emit(f"[wiki] workspace initialized at {self._workspace}")

    def tick(
        self,
        tick_id: str,
        bookmark_prev: int,
        bookmark_now: int,
    ) -> list[Hunch]:
        assert self._workspace is not None
        assert self._replay_dir is not None
        self._tick_count += 1

        source_conv = self._replay_dir / "conversation.jsonl"
        ws_conv = self._workspace / "conversation.jsonl"

        events = read_events_in_range(source_conv, bookmark_prev, bookmark_now)
        self._last_copied_seq = copy_events_to_workspace(
            source_conv, ws_conv, bookmark_now, self._last_copied_seq,
        )
        copy_artifact_snapshots(
            events,
            self._replay_dir / "artifacts",
            self._workspace / "artifacts",
        )

        block_text = render_current_block(
            events, tick_id, bookmark_prev, bookmark_now,
            self._replay_dir / "artifacts",
        )
        (self._workspace / "current_block.md").write_text(block_text)

        if not self._contract_exists():
            self._run_first_tick_setup()

        prompt = self._build_tick_prompt()
        if self.config.dry_run:
            self._emit(f"[wiki] {tick_id} dry-run — skipping claude invocation")
            return []

        try:
            response = self._invoke_claude(prompt)
            self._consecutive_failures = 0
        except Exception as e:
            self._total_failures += 1
            self._consecutive_failures += 1
            self._emit(f"[wiki] {tick_id} claude invocation failed: {e}")
            if self._consecutive_failures >= 3:
                raise RuntimeError(
                    f"WikiCritic: {self._consecutive_failures} consecutive failures"
                ) from e
            return []

        self._accumulate_stats(response)

        if self._contract_exists():
            self._violations = validate_wiki(
                self._workspace / "wiki",
                self._workspace / "wiki_contract.yaml",
            )
            if self._violations:
                self._emit(
                    f"[wiki] {tick_id} validation: {len(self._violations)} violation(s)"
                )

        hunches = self._read_pending_hunches()
        self._emit(
            f"[wiki] {tick_id} done — hunches={len(hunches)} "
            f"violations={len(self._violations)}"
        )
        return hunches

    def shutdown(self) -> None:
        if self._total_calls > 0:
            self._emit(
                f"[wiki] shutdown: {self._total_calls} calls, "
                f"${self._total_cost_usd:.4f}, "
                f"{self._total_input_tokens:,} input tokens"
            )

    def stats(self) -> dict[str, Any]:
        return {
            "calls": self._total_calls,
            "failures": self._total_failures,
            "input_tokens": self._total_input_tokens,
            "output_tokens": self._total_output_tokens,
            "cached_tokens": 0,
            "cache_hit_pct": 0,
            "total_cost_usd": self._total_cost_usd,
        }

    # ---------------------------------------------------------------
    # First-tick setup (contract generation + seed pass)
    # ---------------------------------------------------------------

    def _run_first_tick_setup(self) -> None:
        self._emit("[wiki] first tick — generating contract + seeding wiki")

        contract_prompt = (
            "This is your first invocation. Read CLAUDE.md and "
            "wiki_contract_spec.md, then generate wiki_contract.yaml "
            "from the entity definitions in CLAUDE.md. Write it to "
            "wiki_contract.yaml in the current directory."
        )
        if not self.config.dry_run:
            response = self._invoke_claude(contract_prompt)
            self._accumulate_stats(response)

        if self._contract_exists():
            errors = validate_contract(self._workspace / "wiki_contract.yaml")
            if errors:
                self._emit(f"[wiki] contract validation errors: {errors}")

        seed_docs_dir = self._workspace / "project_docs"
        wiki_index = self._workspace / "wiki" / "index.md"
        has_seed_docs = seed_docs_dir.exists() and any(seed_docs_dir.iterdir())
        wiki_empty = not wiki_index.exists() or wiki_index.stat().st_size == 0

        if has_seed_docs and wiki_empty:
            seed_prompt = (
                "This is the seed pass. Read all files in project_docs/ "
                "and seed the wiki per CLAUDE.md instructions. Extract "
                "initial Concepts, Questions, and Hypotheses. Do NOT "
                "raise any hunches during seeding."
            )
            if not self.config.dry_run:
                response = self._invoke_claude(seed_prompt)
                self._accumulate_stats(response)

    # ---------------------------------------------------------------
    # Claude invocation
    # ---------------------------------------------------------------

    def _invoke_claude(self, prompt: str) -> dict[str, Any]:
        cmd = [
            "claude",
            "--print",
            "--model", self.config.model,
            "--output-format", "json",
            "--dangerously-skip-permissions",
            "--tools", "Read,Edit,Write,Grep,Glob",
        ]
        t0 = time.monotonic()
        result = subprocess.run(
            cmd,
            input=prompt,
            cwd=str(self._workspace),
            capture_output=True,
            text=True,
            timeout=int(self.config.timeout_s),
        )
        elapsed = time.monotonic() - t0

        if result.returncode != 0:
            stderr_tail = (result.stderr or "").strip()[-500:]
            raise RuntimeError(
                f"claude -p failed (rc={result.returncode}, {elapsed:.0f}s): "
                f"{stderr_tail}"
            )

        self._total_calls += 1

        try:
            envelope = json.loads(result.stdout)
        except json.JSONDecodeError:
            self._emit(f"[wiki] warning: non-JSON stdout ({len(result.stdout)} chars)")
            return {"result": result.stdout}

        return envelope

    # ---------------------------------------------------------------
    # Pending hunches
    # ---------------------------------------------------------------

    def _read_pending_hunches(self) -> list[Hunch]:
        path = self._workspace / "pending_hunches.jsonl"
        if not path.exists() or path.stat().st_size == 0:
            return []

        hunches: list[Hunch] = []
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                self._emit(f"[wiki] warning: malformed hunch line: {line[:80]}")
                continue

            smell = (d.get("smell") or "").strip()
            description = (d.get("description") or "").strip()
            if not smell or not description:
                self._emit(f"[wiki] warning: hunch missing smell/description")
                continue

            refs = d.get("triggering_refs") or {}
            tick_seqs: list[int] = []
            for s in refs.get("tick_seqs") or []:
                if isinstance(s, (int, float)):
                    tick_seqs.append(int(s))
            artifacts = [str(a) for a in (refs.get("artifacts") or [])]

            hunches.append(Hunch(
                smell=smell,
                description=description,
                triggering_refs=TriggeringRefs(
                    tick_seqs=tick_seqs,
                    artifacts=artifacts,
                ),
            ))

        path.write_text("")
        return hunches

    # ---------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------

    def _contract_exists(self) -> bool:
        if self._workspace is None:
            return False
        return (self._workspace / "wiki_contract.yaml").exists()

    def _build_tick_prompt(self) -> str:
        parts: list[str] = []

        if self._violations and len(self._violations) <= self.config.max_contract_violations:
            parts.append(
                "IMPORTANT: The following wiki violations were found after "
                "your last tick. Fix them before proceeding with normal work:\n"
            )
            for v in self._violations:
                parts.append(f"  - {v}")
            parts.append("")

        parts.append(
            "A new conversation block has arrived at current_block.md. "
            "Process it per CLAUDE.md."
        )
        return "\n".join(parts)

    def _detect_last_copied_seq(self) -> int:
        ws_conv = self._workspace / "conversation.jsonl"
        if not ws_conv.exists() or ws_conv.stat().st_size == 0:
            return 0
        last_seq = 0
        for line in ws_conv.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                last_seq = max(last_seq, d.get("tick_seq", 0))
            except json.JSONDecodeError:
                continue
        return last_seq

    def _accumulate_stats(self, response: dict[str, Any]) -> None:
        usage = response.get("usage") or {}
        cost = response.get("cost_usd")
        if cost is not None:
            self._total_cost_usd += float(cost)
        cached = (
            usage.get("cache_read_input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0)
        )
        inp = usage.get("input_tokens", 0) + cached
        out = usage.get("output_tokens", 0)
        self._total_input_tokens += inp
        self._total_output_tokens += out

    def _emit(self, msg: str) -> None:
        if self._log is not None:
            self._log(msg)
