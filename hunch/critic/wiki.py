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

from hunch.critic.protocol import Critic, Hunch, TriggeringRefs
from hunch.critic.wiki_contract import validate_contract, validate_wiki
from hunch.critic.wiki_renderer import read_events_in_range, render_current_block
from hunch.critic.wiki_validator import validate_pending_hunches
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

class WikiCritic(Critic):
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
        self._setup_attempts: int = 0
        self._violations: list[str] = []
        self._hunch_violations: list[str] = []
        self._consecutive_violation_ticks: int = 0
        self._malformed_hunches: int = 0

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
            raise RuntimeError(
                f"WikiCritic: {tick_id} claude invocation failed "
                f"(consecutive={self._consecutive_failures}): {e}"
            ) from e

        self._accumulate_stats(response)

        if self._contract_exists():
            self._violations = validate_wiki(
                self._workspace / "wiki",
                self._workspace / "wiki_contract.yaml",
            )
            if self._violations:
                self._consecutive_violation_ticks += 1
                self._emit(
                    f"[wiki] {tick_id} validation: {len(self._violations)} violation(s)"
                )
                if (
                    len(self._violations) > self.config.max_contract_violations
                    and self._consecutive_violation_ticks >= 3
                ):
                    raise RuntimeError(
                        f"WikiCritic: {len(self._violations)} violations "
                        f"persisted for {self._consecutive_violation_ticks} "
                        f"consecutive ticks (threshold: "
                        f"{self.config.max_contract_violations})"
                    )
            else:
                self._consecutive_violation_ticks = 0

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
            "malformed_hunches": self._malformed_hunches,
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
        self._setup_attempts += 1
        if self._setup_attempts > 2:
            raise RuntimeError(
                f"WikiCritic: contract generation failed on "
                f"{self._setup_attempts - 1} attempts, aborting"
            )
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

        if not self._contract_exists():
            raise RuntimeError(
                "Contract generation invocation succeeded but "
                "wiki_contract.yaml was not created. Check agent "
                "permissions and workspace."
            )

        errors = validate_contract(self._workspace / "wiki_contract.yaml")
        if errors:
            raise RuntimeError(
                f"Generated contract is invalid: {errors}"
            )

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
            if wiki_index.stat().st_size == 0:
                raise RuntimeError(
                    "Seed pass invocation succeeded but wiki/index.md "
                    "is still empty. The agent failed to populate the wiki."
                )

    # ---------------------------------------------------------------
    # Claude invocation
    # ---------------------------------------------------------------

    def _invoke_claude(self, prompt: str) -> dict[str, Any]:
        tools = "Read,Edit,Write,Grep,Glob,WebSearch,WebFetch"
        cmd = [
            "claude",
            "--print",
            "--model", self.config.model,
            "--output-format", "json",
            "--permission-mode", "dontAsk",
            "--allowedTools", tools,
            "--tools", tools,
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
            self._consecutive_failures += 1
            self._total_failures += 1
            raise RuntimeError(
                f"claude -p returned non-JSON stdout "
                f"({len(result.stdout)} chars, {elapsed:.0f}s)"
            )

        return envelope

    # ---------------------------------------------------------------
    # Pending hunches
    # ---------------------------------------------------------------

    def _read_pending_hunches(self) -> list[Hunch]:
        path = self._workspace / "pending_hunches.jsonl"
        if not path.exists() or path.stat().st_size == 0:
            self._hunch_violations = []
            return []

        raw_hunches: list[dict[str, Any]] = []
        total_lines = 0
        malformed = 0
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            total_lines += 1
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                log.warning("malformed hunch JSON: %s", line[:80])
                continue

            smell = (d.get("smell") or "").strip()
            description = (d.get("description") or "").strip()
            if not smell or not description:
                malformed += 1
                log.warning("hunch missing smell/description: %s", line[:80])
                continue

            raw_hunches.append(d)

        if malformed:
            self._malformed_hunches += malformed
            self._emit(
                f"[wiki] WARNING: {malformed}/{total_lines} hunch lines "
                f"malformed or incomplete"
            )
        if total_lines > 0 and malformed == total_lines:
            raise RuntimeError(
                f"All {total_lines} hunch lines were malformed — "
                f"agent may be writing wrong format"
            )

        valid_raw, invalid = validate_pending_hunches(
            raw_hunches, self._workspace,
        )

        if invalid:
            self._hunch_violations = []
            with open(path, "w") as f:
                for hv in invalid:
                    f.write(json.dumps(hv.raw) + "\n")
                    for v in hv.violations:
                        self._hunch_violations.append(
                            f"[{(hv.raw.get('smell') or '')[:50]}] {v}"
                        )
            self._emit(
                f"[wiki] {len(invalid)} hunch(es) failed validation, "
                f"kept in pending for self-correction"
            )
        else:
            self._hunch_violations = []
            path.write_text("")

        return [self._raw_to_hunch(d) for d in valid_raw]

    @staticmethod
    def _raw_to_hunch(d: dict[str, Any]) -> Hunch:
        refs = d.get("triggering_refs") or {}
        tick_seqs: list[int] = []
        for s in refs.get("tick_seqs") or []:
            if isinstance(s, (int, float)):
                tick_seqs.append(int(s))
        artifacts = [str(a) for a in (refs.get("artifacts") or [])]
        return Hunch(
            smell=(d.get("smell") or "").strip(),
            description=(d.get("description") or "").strip(),
            triggering_refs=TriggeringRefs(
                tick_seqs=tick_seqs,
                artifacts=artifacts,
            ),
        )

    # ---------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------

    def _contract_exists(self) -> bool:
        if self._workspace is None:
            return False
        return (self._workspace / "wiki_contract.yaml").exists()

    def _build_tick_prompt(self) -> str:
        parts: list[str] = []

        if self._violations:
            n = len(self._violations)
            if n > self.config.max_contract_violations:
                self._emit(
                    f"[wiki] WARNING: {n} violations exceeds threshold "
                    f"({self.config.max_contract_violations}), "
                    f"still reporting all to agent"
                )
            parts.append(
                "IMPORTANT: The following wiki violations were found after "
                "your last tick. Fix them before proceeding with normal work:\n"
            )
            for v in self._violations:
                parts.append(f"  - {v}")
            parts.append("")

        if self._hunch_violations:
            parts.append(
                "IMPORTANT: The following hunches from your last tick "
                "failed validation and were NOT promoted. They are still "
                "in pending_hunches.jsonl. Fix the violations, then "
                "rewrite the corrected hunches to pending_hunches.jsonl:\n"
            )
            for v in self._hunch_violations:
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
        parse_errors = 0
        for line_num, line in enumerate(ws_conv.read_text().splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                last_seq = max(last_seq, d.get("tick_seq", 0))
            except json.JSONDecodeError:
                parse_errors += 1
                log.warning(
                    "conversation.jsonl line %d: malformed JSON: %s",
                    line_num, line[:80],
                )
        if parse_errors > 10:
            raise RuntimeError(
                f"conversation.jsonl has {parse_errors} malformed lines — "
                f"file may be corrupted"
            )
        return last_seq

    def _accumulate_stats(self, response: dict[str, Any]) -> None:
        usage = response.get("usage")
        cost = response.get("cost_usd")
        if usage is None and cost is None:
            log.warning(
                "Claude response missing both 'usage' and 'cost_usd' — "
                "stats will be incomplete. Response keys: %s",
                list(response.keys()),
            )
        if cost is not None:
            self._total_cost_usd += float(cost)
        usage = usage or {}
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
