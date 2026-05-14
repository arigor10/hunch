"""Evidence mining — locate the earliest raisable point for each finding.

For each finding in findings.jsonl, sets up a workspace with
conversation.jsonl sliced up to the signal turn and artifact snapshots,
then invokes a Claude agent to search for the evidence trail.  Writes
hunches.jsonl ready for ``hunch bank sync``.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from hunch.critic.wiki_workspace import (
    copy_artifact_snapshots,
    copy_events_to_workspace,
)

log = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).parent / "prompts"
_DEFAULT_PROMPT = _PROMPTS_DIR / "mine_evidence.md"

EVIDENCE_SCHEMA = json.dumps({
    "type": "object",
    "properties": {
        "evidence_tick_seqs": {
            "type": "array",
            "items": {"type": "integer"},
            "description": "Key tick_seqs where evidence accumulated",
        },
        "earliest_raisable": {
            "type": "integer",
            "description": "Tick_seq after which a critic could first have raised this",
        },
        "artifacts": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Doc paths containing relevant evidence",
        },
        "evidence_summary": {
            "type": "string",
            "description": "2-3 sentences describing the evidence chain",
        },
        "smell": {
            "type": "string",
            "description": "One-line hunch title (short, specific)",
        },
        "description": {
            "type": "string",
            "description": "3-5 sentence hunch description from a critic's perspective",
        },
    },
    "required": [
        "evidence_tick_seqs", "earliest_raisable", "artifacts",
        "evidence_summary", "smell", "description",
    ],
})


@dataclass
class EvidenceConfig:
    model: str = "claude-sonnet-4-5-20250929"
    prompt_path: Path | None = None
    timeout_s: float = 600.0


@dataclass
class EvidenceResult:
    total_processed: int = 0
    total_errors: int = 0
    total_cost_usd: float = 0.0


def run_evidence_mining(
    replay_dir: Path,
    findings_path: Path,
    output_dir: Path,
    config: EvidenceConfig | None = None,
    on_log: callable = None,
) -> EvidenceResult:
    """Run evidence mining on a set of findings.

    Reads findings.jsonl, creates a workspace per finding, invokes an
    agent, and writes hunches.jsonl.  Supports checkpoint/resume.
    """
    config = config or EvidenceConfig()
    _log = on_log or (lambda msg: None)

    conv_path = replay_dir / "conversation.jsonl"
    if not conv_path.exists():
        raise FileNotFoundError(f"No conversation.jsonl in {replay_dir}")
    if not findings_path.exists():
        raise FileNotFoundError(f"Findings file not found: {findings_path}")

    output_dir.mkdir(parents=True, exist_ok=True)
    hunches_path = output_dir / "hunches.jsonl"
    prompt_template = _load_prompt(config.prompt_path)

    findings = _load_findings(findings_path)
    _log(f"Loaded {len(findings)} findings from {findings_path}")

    already_done = _load_processed_ids(hunches_path)
    _log(f"  {len(already_done)} already processed")

    result = EvidenceResult()
    base_dir = Path(tempfile.mkdtemp(prefix="mine_evidence_"))
    consecutive_errors = 0
    max_consecutive_errors = 5

    try:
        for i, finding in enumerate(findings):
            fid = finding["id"]

            if fid in already_done:
                _log(f"  [{i+1}/{len(findings)}] {fid} — cached")
                result.total_processed += 1
                continue

            signal_seq = finding["tick_seq"]
            cutoff_seq = signal_seq - 1
            workspace = base_dir / fid

            _log(
                f"  [{i+1}/{len(findings)}] {fid} "
                f"(signal_seq={signal_seq})..."
            )

            t0 = time.time()
            try:
                _setup_workspace(workspace, replay_dir, cutoff_seq)
                prompt = _build_prompt(prompt_template, finding)
                response = _run_agent(prompt, workspace, config)
                elapsed = time.time() - t0
                cost = response.get("_cost_usd", 0)
                result.total_cost_usd += cost
                consecutive_errors = 0

                hunch_event = _build_hunch_event(finding, response)
                _append_hunch(hunches_path, hunch_event)

                result.total_processed += 1
                _log(
                    f"    OK (earliest={response.get('earliest_raisable')}, "
                    f"n_evidence={len(response.get('evidence_tick_seqs', []))}, "
                    f"${cost:.3f}, {elapsed:.1f}s)"
                )
            except Exception as e:
                elapsed = time.time() - t0
                result.total_errors += 1
                consecutive_errors += 1
                error_event = {
                    "type": "mine_error",
                    "source_finding_id": fid,
                    "error": str(e),
                }
                _append_hunch(hunches_path, error_event)
                _log(f"    ERROR: {e} ({elapsed:.1f}s)")
                if consecutive_errors >= max_consecutive_errors:
                    raise RuntimeError(
                        f"Aborting: {max_consecutive_errors} consecutive "
                        f"failures (last: {e})"
                    )
            finally:
                if workspace.exists():
                    shutil.rmtree(workspace, ignore_errors=True)
    finally:
        shutil.rmtree(base_dir, ignore_errors=True)

    _log(
        f"Done: {result.total_processed} processed, "
        f"{result.total_errors} errors, "
        f"${result.total_cost_usd:.3f}"
    )
    return result


def _load_prompt(path: Path | None) -> str:
    src = path or _DEFAULT_PROMPT
    if not src.exists():
        raise FileNotFoundError(f"Evidence prompt not found: {src}")
    return src.read_text()


def _load_findings(path: Path) -> list[dict]:
    """Load findings from JSONL."""
    findings = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            findings.append(json.loads(line))
    return findings


def _load_processed_ids(hunches_path: Path) -> set[str]:
    """Load already-processed finding IDs from hunches.jsonl."""
    if not hunches_path.exists():
        return set()
    ids = set()
    with open(hunches_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                fid = obj.get("source_finding_id")
                if fid and obj.get("type") != "mine_error":
                    ids.add(fid)
            except json.JSONDecodeError:
                continue
    return ids


def _setup_workspace(
    workspace: Path,
    replay_dir: Path,
    cutoff_seq: int,
) -> None:
    """Create workspace with conversation sliced to cutoff + artifact snapshots."""
    workspace.mkdir(parents=True, exist_ok=True)

    conv_dst = workspace / "conversation.jsonl"
    conv_dst.touch()
    copy_events_to_workspace(
        source_conversation=replay_dir / "conversation.jsonl",
        workspace_conversation=conv_dst,
        up_to_seq=cutoff_seq,
        last_copied_seq=0,
    )

    events = _read_events_up_to(replay_dir / "conversation.jsonl", cutoff_seq)
    artifact_events = [e for e in events if e.get("snapshot")]
    if artifact_events:
        docs_dir = workspace / "project_docs"
        docs_dir.mkdir(exist_ok=True)
        copy_artifact_snapshots(
            events=artifact_events,
            source_artifacts=replay_dir / "artifacts",
            workspace_artifacts=docs_dir,
        )

    _write_settings_json(workspace)


def _read_events_up_to(conv_path: Path, cutoff_seq: int) -> list[dict]:
    events = []
    with open(conv_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            e = json.loads(line)
            if e.get("tick_seq", 0) > cutoff_seq:
                break
            events.append(e)
    return events


def _build_prompt(template: str, finding: dict) -> str:
    return template.format(
        signal_seq=finding["tick_seq"],
        signal_text=finding.get("signal_text", ""),
        anomaly=finding.get("anomaly", ""),
    )


def _write_settings_json(workspace: Path) -> None:
    """Write .claude/settings.json scoping permissions to read-only file tools."""
    claude_dir = workspace / ".claude"
    claude_dir.mkdir(exist_ok=True)
    settings_path = claude_dir / "settings.json"
    if settings_path.exists():
        return
    ws = str(workspace)
    settings = {
        "permissions": {
            "deny": ["Bash", "Edit", "Write"],
            "allow": [
                f"Read({ws}/**)",
                f"Grep({ws}/**)",
                f"Glob({ws}/**)",
            ],
        }
    }
    settings_path.write_text(json.dumps(settings, indent=2) + "\n")


def _run_agent(prompt: str, workspace: Path, config: EvidenceConfig) -> dict:
    """Invoke Claude CLI as an agent with file tools."""
    tools = "Read,Glob,Grep"
    cmd = [
        "claude",
        "--model", config.model,
        "--print",
        "--output-format", "json",
        "--json-schema", EVIDENCE_SCHEMA,
        "--permission-mode", "dontAsk",
        "--allowedTools", tools,
        "--tools", tools,
        "-p", prompt,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=config.timeout_s,
            stdin=subprocess.DEVNULL,
            cwd=str(workspace),
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Agent timed out after {config.timeout_s}s")

    if result.returncode != 0:
        raise RuntimeError(
            f"Agent failed (rc={result.returncode}): "
            f"{(result.stderr or '').strip()[-500:]}"
        )

    response = json.loads(result.stdout)
    parsed: dict = {}
    if isinstance(response, dict):
        parsed["_cost_usd"] = response.get("total_cost_usd", 0)
        parsed["_num_turns"] = response.get("num_turns", 0)
        if "structured_output" in response and response["structured_output"]:
            parsed.update(response["structured_output"])
            return parsed
        if "result" in response and isinstance(response["result"], str):
            content = response["result"].strip()
            if content:
                parsed.update(json.loads(content))
                return parsed
    raise RuntimeError(
        f"Could not parse agent response: {json.dumps(response)[:500]}"
    )


def _build_hunch_event(finding: dict, response: dict) -> dict:
    """Convert an evidence mining response into a hunch event for bank sync."""
    earliest = response["earliest_raisable"]
    return {
        "type": "emit",
        "source": "mined",
        "source_finding_id": finding["id"],
        "hunch_id": finding["id"].lower().replace("nf-", "h-"),
        "emitted_by_tick": -1,
        "bookmark_now": earliest,
        "bookmark_prev": earliest - 1,
        "smell": response["smell"],
        "description": response["description"],
        "triggering_refs": {
            "tick_seqs": response["evidence_tick_seqs"],
            "artifacts": response.get("artifacts", []),
        },
        "evidence_summary": response["evidence_summary"],
    }


def _append_hunch(path: Path, event: dict) -> None:
    """Append a single hunch event to hunches.jsonl, with fsync."""
    line = json.dumps(event, ensure_ascii=False) + "\n"
    with open(path, "a") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())
