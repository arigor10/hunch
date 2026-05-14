"""Nose mining — find moments where the Scientist's nose fired.

Chunks the conversation, sends each chunk to an LLM, deduplicates
findings across overlapping windows, and writes findings.jsonl.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from hunch.backend.claude_cli import ClaudeCliBackend
from hunch.mine.chunker import Chunk, chunk_conversation, read_conversation
from hunch.mine.renderer import render_chunk

log = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).parent / "prompts"
_DEFAULT_PROMPT = _PROMPTS_DIR / "mine_nose.md"


@dataclass
class NoseConfig:
    model: str = "claude-sonnet-4-5-20250929"
    window_size: int = 200
    overlap: int = 50
    prompt_path: Path | None = None
    timeout_s: float = 120.0


@dataclass
class NoseResult:
    total_findings: int = 0
    total_chunks: int = 0
    total_cost_usd: float = 0.0
    errors: int = 0


def run_nose_mining(
    replay_dir: Path,
    output_dir: Path,
    config: NoseConfig | None = None,
    on_log: callable = None,
) -> NoseResult:
    """Run nose mining on a replay directory.

    Reads conversation.jsonl, chunks it, mines each chunk, deduplicates,
    and writes findings.jsonl.
    """
    config = config or NoseConfig()
    _log = on_log or (lambda msg: None)

    conv_path = replay_dir / "conversation.jsonl"
    if not conv_path.exists():
        raise FileNotFoundError(f"No conversation.jsonl in {replay_dir}")

    output_dir.mkdir(parents=True, exist_ok=True)
    findings_path = output_dir / "findings.jsonl"

    _log(f"Reading conversation from {conv_path}")
    events = read_conversation(conv_path)
    _log(f"  {len(events)} events")

    chunks = chunk_conversation(
        events,
        window_size=config.window_size,
        overlap=config.overlap,
    )
    _log(f"  {len(chunks)} chunks (window={config.window_size}, overlap={config.overlap})")

    prompt_template = _load_prompt(config.prompt_path)
    artifacts_dir = replay_dir / "artifacts"

    already_processed = _load_checkpoint(findings_path)
    result = NoseResult(total_chunks=len(chunks))

    all_findings: list[dict] = []
    for chunk_findings_list in already_processed.values():
        all_findings.extend(chunk_findings_list)
    next_id = _next_finding_id(all_findings)
    consecutive_errors = 0
    max_consecutive_errors = 5

    for i, chunk in enumerate(chunks):
        chunk_key = f"{chunk.start_seq}-{chunk.end_seq}"
        if chunk_key in already_processed:
            _log(f"  [{i+1}/{len(chunks)}] chunk {chunk_key} — cached")
            continue

        rendered = render_chunk(chunk.events, artifacts_dir)
        prompt = prompt_template.replace("{chunk_text}", rendered)

        _log(f"  [{i+1}/{len(chunks)}] chunk {chunk_key} ({chunk.n_events} events)...")

        t0 = time.time()
        try:
            raw_findings, cost = _call_mining_llm(prompt, config)
            elapsed = time.time() - t0
            consecutive_errors = 0

            chunk_findings = []
            for f in raw_findings:
                f["_chunk_key"] = chunk_key
                if "id" not in f or not f["id"]:
                    f["id"] = f"NF-{next_id:03d}"
                    next_id += 1
                chunk_findings.append(f)

            all_findings.extend(chunk_findings)
            result.total_cost_usd += cost
            result.total_findings += len(chunk_findings)

            _log(
                f"    {len(chunk_findings)} findings, "
                f"${cost:.3f}, {elapsed:.1f}s"
            )
        except Exception as e:
            elapsed = time.time() - t0
            result.errors += 1
            consecutive_errors += 1
            _log(f"    ERROR: {e} ({elapsed:.1f}s)")
            if consecutive_errors >= max_consecutive_errors:
                raise RuntimeError(
                    f"Aborting: {max_consecutive_errors} consecutive chunk "
                    f"failures (last: {e})"
                )

        _write_findings(findings_path, all_findings)

    deduped = _deduplicate(all_findings)
    _log(f"  Dedup: {len(all_findings)} raw → {len(deduped)} unique")
    result.total_findings = len(deduped)

    _renumber(deduped)
    _write_final_findings(findings_path, deduped)

    _log(
        f"Done: {result.total_findings} findings from {result.total_chunks} chunks, "
        f"${result.total_cost_usd:.3f}"
    )
    if result.errors > 0:
        _log(
            f"  {result.errors} chunk(s) failed. "
            f"Re-run the same command to retry them."
        )
    return result


def _load_prompt(path: Path | None) -> str:
    src = path or _DEFAULT_PROMPT
    if not src.exists():
        raise FileNotFoundError(f"Mining prompt not found: {src}")
    return src.read_text()


def _call_mining_llm(prompt: str, config: NoseConfig) -> tuple[list[dict], float]:
    """Call Claude CLI and parse JSONL output."""
    backend = ClaudeCliBackend(model=config.model, timeout_s=config.timeout_s)
    response = backend.call(prompt)
    findings = _parse_findings(response.text)
    cost = response.cost_usd or 0.0
    return findings, cost


def _parse_findings(text: str) -> list[dict]:
    """Parse LLM output as JSONL (one JSON object per line).

    Also handles JSON arrays as fallback.
    """
    text = text.strip()
    if not text:
        return []

    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.startswith("```")]
        text = "\n".join(lines).strip()

    if text.startswith("["):
        try:
            arr = json.loads(text)
            if isinstance(arr, list):
                return arr
        except json.JSONDecodeError:
            log.warning("LLM output looks like a JSON array but failed to parse; "
                        "falling back to line-by-line: %s", text[:200])

    findings = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                findings.append(obj)
        except json.JSONDecodeError:
            log.warning("Skipping unparseable LLM output line: %s", line[:200])
    return findings


def _deduplicate(findings: list[dict]) -> list[dict]:
    """Remove duplicate findings from overlapping windows.

    Two findings are duplicates if they share the same tick_seq.
    When duplicates exist, keep the one with higher confidence.
    """
    by_seq: dict[int, dict] = {}
    confidence_rank = {"high": 2, "medium": 1}

    for f in findings:
        seq = f.get("tick_seq")
        if seq is None:
            continue
        existing = by_seq.get(seq)
        if existing is None:
            by_seq[seq] = f
        else:
            new_rank = confidence_rank.get(f.get("confidence", ""), 0)
            old_rank = confidence_rank.get(existing.get("confidence", ""), 0)
            if new_rank > old_rank:
                by_seq[seq] = f

    return sorted(by_seq.values(), key=lambda f: f.get("tick_seq", 0))


def _renumber(findings: list[dict]) -> None:
    """Assign sequential NF-001, NF-002, ... IDs after dedup."""
    for i, f in enumerate(findings, 1):
        f["id"] = f"NF-{i:03d}"


def _load_checkpoint(findings_path: Path) -> dict:
    """Load already-processed chunk keys from an existing findings.jsonl."""
    if not findings_path.exists():
        return {}
    seen: dict[str, list[dict]] = {}
    with open(findings_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                key = obj.get("_chunk_key", "")
                if key:
                    seen.setdefault(key, []).append(obj)
            except json.JSONDecodeError:
                continue
    return seen


def _next_finding_id(findings: list[dict]) -> int:
    """Find the next available NF-NNN number."""
    max_n = 0
    for f in findings:
        fid = f.get("id", "")
        if fid.startswith("NF-"):
            try:
                n = int(fid[3:])
                max_n = max(max_n, n)
            except ValueError:
                log.warning("Malformed finding ID (expected NF-NNN): %s", fid)
    return max_n + 1


def _write_findings(path: Path, findings: list[dict]) -> None:
    """Write findings to JSONL with atomic rename.

    Keeps the _chunk_key field for checkpoint/resume. The final dedup
    pass strips internal fields before the last write.
    """
    import shutil
    import tempfile

    if path.exists():
        bak = path.with_suffix(".jsonl.bak")
        shutil.copy2(path, bak)

    fd, tmp = tempfile.mkstemp(
        dir=path.parent, suffix=".tmp", prefix=path.stem,
    )
    try:
        with open(fd, "w") as f:
            for finding in findings:
                f.write(json.dumps(finding, ensure_ascii=False) + "\n")
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _write_final_findings(path: Path, findings: list[dict]) -> None:
    """Write the final deduplicated findings, stripping internal fields."""
    import shutil
    import tempfile

    if path.exists():
        bak = path.with_suffix(".jsonl.bak")
        shutil.copy2(path, bak)

    fd, tmp = tempfile.mkstemp(
        dir=path.parent, suffix=".tmp", prefix=path.stem,
    )
    try:
        with open(fd, "w") as f:
            for finding in findings:
                clean = {k: v for k, v in finding.items() if not k.startswith("_")}
                f.write(json.dumps(clean, ensure_ascii=False) + "\n")
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
