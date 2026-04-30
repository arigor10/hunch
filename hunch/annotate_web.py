"""Browser-based annotation UI for offline hunch evaluation.

Local Flask server serving a single-page app with three-column layout:
hunch list (left), detail pane (center), scrollable conversation (right).

Two modes:

**Project mode** (``--project-dir``): reads from the label bank, shows
hunches from all eval runs with a run selector, groups by bank entry.
Labels are written to the bank and propagate across linked hunches.

**Legacy mode** (``--run-dir``): single-run annotation using per-run
``labels.jsonl``. Still auto-detects and uses the bank for label storage
if one exists and the run has been synced.

Features:
- Labels hunches as tp/fp/skip with category and free-text note fields.
- Keyboard shortcuts: t/f/s for labels, arrow keys for navigation.
- ``--novel-only``: filter to novel hunches only (legacy mode).
- ``--dedup``: exclude duplicate hunches (legacy mode).
- Artifact references are clickable (markdown modal via marked.js).
- Chunk references scroll the conversation pane.
- Conversation auto-scrolls to the trigger-window divider on hunch
  selection. In-window events are highlighted green.

Usage:
    # Project mode (recommended)
    hunch annotate-web --project-dir /path/to/project

    # Legacy single-run mode
    hunch annotate-web \\
        --replay-dir /path/to/.hunch/replay \\
        --run-dir data/critic_run_01 \\
        [--novel-only] [--dedup] [--port 5555]
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate_replay_dir(replay_dir: Path) -> None:
    if not replay_dir.is_dir():
        raise SystemExit(
            f"Error: replay directory does not exist: {replay_dir}"
        )
    expected = ["conversation.jsonl", "artifacts.jsonl"]
    missing = [f for f in expected if not (replay_dir / f).exists()]
    if missing:
        raise SystemExit(
            f"Error: replay directory {replay_dir} is missing: "
            + ", ".join(missing)
            + "\n  (Did you mean to pass the replay/ subdirectory?)"
        )


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _load_conversation(path: Path) -> list[dict]:
    events: list[dict] = []
    if not path.exists():
        return events
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            seq = d.get("tick_seq")
            if not isinstance(seq, int):
                continue
            events.append({
                "tick_seq": seq,
                "type": d.get("type", ""),
                "text": d.get("text", ""),
                "timestamp": d.get("timestamp", ""),
            })
    return events


def _load_hunches(path: Path) -> list[dict]:
    hunches: list[dict] = []
    if not path.exists():
        return hunches
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("type") != "emit":
                continue
            hunches.append({
                "hunch_id": d.get("hunch_id", ""),
                "smell": d.get("smell", ""),
                "description": d.get("description", ""),
                "bookmark_prev": d.get("bookmark_prev", -1),
                "bookmark_now": d.get("bookmark_now", -1),
                "emitted_by_tick": d.get("emitted_by_tick", -1),
                "triggering_refs": d.get("triggering_refs") or {},
            })
    return hunches


def _load_novel_ids(path: Path) -> set[str] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return set(data.get("novel_ids", []))
    except (json.JSONDecodeError, KeyError):
        return None


def _read_labels(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    records: dict[str, dict] = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            hid = d.get("hunch_id")
            if hid:
                records[hid] = d
    return records


def _write_label(path: Path, record: dict) -> None:
    from hunch.journal.append import append_json_line
    append_json_line(path, record)


# ---------------------------------------------------------------------------
# Bank-aware helpers
# ---------------------------------------------------------------------------

def _infer_project_dir(run_dir: Path) -> Path | None:
    """Try to infer project dir from a run dir path.

    Expected layout: ``<project>/.hunch/eval/<run_name>/``
    """
    if run_dir.parent.name == "eval" and run_dir.parent.parent.name == ".hunch":
        return run_dir.parent.parent.parent
    return None


def _discover_runs(eval_dir: Path) -> list[dict]:
    """Return metadata for each eval run directory."""
    runs: list[dict] = []
    if not eval_dir.is_dir():
        return runs
    for d in sorted(eval_dir.iterdir()):
        if not d.is_dir():
            continue
        hunches_path = d / "hunches.jsonl"
        if not hunches_path.exists():
            continue
        count = 0
        with open(hunches_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entry = json.loads(line)
                        if entry.get("type") == "emit":
                            count += 1
                    except json.JSONDecodeError:
                        pass
        runs.append({"name": d.name, "hunch_count": count, "selected": True})
    return runs


def _load_bank_items(
    state: Any,
    eval_dir: Path,
    selected_runs: list[str],
) -> list[dict]:
    """Load hunches from selected runs, grouped by bank entry."""
    items_by_id: dict[str, dict] = {}

    for run_name in selected_runs:
        hunches_path = eval_dir / run_name / "hunches.jsonl"
        if not hunches_path.exists():
            continue
        hunches = _load_hunches(hunches_path)

        for h in hunches:
            bank_id = state.hunch_to_bank.get((run_name, h["hunch_id"]))
            if bank_id is None:
                item_id = f"{run_name}:{h['hunch_id']}"
                items_by_id[item_id] = {
                    "id": item_id,
                    "bank_id": None,
                    "hunch_id": h["hunch_id"],
                    "canonical_smell": h["smell"],
                    "canonical_description": h["description"],
                    "bookmark_prev": h["bookmark_prev"],
                    "bookmark_now": h["bookmark_now"],
                    "emitted_by_tick": h["emitted_by_tick"],
                    "triggering_refs": h["triggering_refs"],
                    "runs": [run_name],
                    "source_run": run_name,
                    "source_hunch_id": h["hunch_id"],
                    "unsynced": True,
                }
                continue

            entry = state.entries.get(bank_id)
            if bank_id not in items_by_id:
                items_by_id[bank_id] = {
                    "id": bank_id,
                    "bank_id": bank_id,
                    "hunch_id": h["hunch_id"],
                    "canonical_smell": entry.canonical_smell if entry else h["smell"],
                    "canonical_description": (
                        entry.canonical_description if entry else h["description"]
                    ),
                    "bookmark_prev": h["bookmark_prev"],
                    "bookmark_now": h["bookmark_now"],
                    "emitted_by_tick": h["emitted_by_tick"],
                    "triggering_refs": h["triggering_refs"],
                    "runs": [run_name],
                    "source_run": entry.source_run if entry else run_name,
                    "source_hunch_id": (
                        entry.source_hunch_id if entry else h["hunch_id"]
                    ),
                    "unsynced": False,
                }
            else:
                existing = items_by_id[bank_id]
                if run_name not in existing["runs"]:
                    existing["runs"].append(run_name)

    return sorted(items_by_id.values(), key=lambda x: x["bookmark_now"])


def _resolve_bank_labels(state: Any, items: list[dict]) -> dict[str, dict]:
    """Resolve labels from bank for each item."""
    from hunch.bank import resolve_label

    labels: dict[str, dict] = {}
    for item in items:
        bid = item.get("bank_id")
        if bid is None:
            continue
        resolved = resolve_label(
            state, item["source_run"], item["source_hunch_id"],
        )
        if resolved.label is None:
            continue

        note = ""
        tags: list[str] = []
        entry = state.entries.get(bid)
        if entry is not None:
            run = item["source_run"]
            hid = item["source_hunch_id"]
            if resolved.source == "inherited":
                run = resolved.inherited_from_run
                hid = resolved.inherited_from_hunch_id
            matching = [
                lr for lr in entry.labels
                if lr.run == run and lr.hunch_id == hid
                and lr.label is not None
            ]
            if matching:
                latest = max(matching, key=lambda lr: lr.ts)
                note = latest.note
                tags = latest.tags

        duplicate_of = None
        display_tags = []
        for t in tags:
            if t.startswith("dup_of:"):
                duplicate_of = t[len("dup_of:"):]
            else:
                display_tags.append(t)

        labels[item["id"]] = {
            "label": resolved.label,
            "source": resolved.source,
            "category": resolved.category,
            "inherited_from_run": resolved.inherited_from_run,
            "inherited_from_hunch_id": resolved.inherited_from_hunch_id,
            "note": note,
            "tags": display_tags,
            "duplicate_of": duplicate_of,
        }
    return labels


# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------

HTML_PAGE = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Hunch Annotator</title>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, monospace; background: #1a1a2e; color: #e0e0e0; height: 100vh; display: flex; flex-direction: column; }

#top-bar { background: #16213e; padding: 8px 16px; display: flex; align-items: center; gap: 16px; border-bottom: 1px solid #333; flex-shrink: 0; }
#top-bar .stats { color: #888; font-size: 13px; }
#top-bar .nav-btn { background: #0f3460; color: #e0e0e0; border: 1px solid #555; padding: 4px 12px; cursor: pointer; border-radius: 3px; font-size: 13px; }
#top-bar .nav-btn:hover { background: #1a4a7a; }
#top-bar .label-btn { padding: 5px 16px; border: none; border-radius: 3px; cursor: pointer; font-weight: bold; font-size: 13px; }
.btn-tp { background: #2d6a4f; color: #fff; }
.btn-tp:hover { background: #40916c; }
.btn-fp { background: #9d0208; color: #fff; }
.btn-fp:hover { background: #d00000; }
.btn-skip { background: #555; color: #fff; }
.btn-skip:hover { background: #777; }
.btn-dup { background: #4a3800; color: #ffd54f; }
.btn-dup:hover { background: #6d5200; }
.badge-dup { background: #4a3800; color: #ffd54f; }
#current-label { font-size: 13px; margin-left: 8px; }

#main { display: flex; flex: 1; overflow: hidden; }

#hunch-list { width: 220px; background: #16213e; border-right: 1px solid #333; overflow-y: auto; flex-shrink: 0; }
.hunch-item { padding: 6px 10px; cursor: pointer; border-bottom: 1px solid #222; font-size: 12px; display: flex; justify-content: space-between; align-items: center; }
.hunch-item:hover { background: #1a3a5c; }
.hunch-item.active { background: #0f3460; border-left: 3px solid #7eb8da; }
.hunch-item .hid { font-weight: bold; color: #aaa; }
.hunch-item .label-badge { font-size: 10px; padding: 1px 5px; border-radius: 3px; font-weight: bold; }
.badge-tp { background: #2d6a4f; color: #fff; }
.badge-fp { background: #9d0208; color: #fff; }
.badge-skip { background: #555; color: #fff; }

#detail-pane { flex: 1; padding: 16px; overflow-y: auto; border-right: 1px solid #333; min-width: 300px; }
#detail-pane h2 { color: #7eb8da; margin-bottom: 8px; font-size: 16px; }
#detail-pane .smell { font-size: 15px; font-weight: bold; margin-bottom: 12px; color: #fff; }
#detail-pane .description { line-height: 1.6; margin-bottom: 16px; white-space: pre-wrap; }
#detail-pane .meta { color: #888; font-size: 12px; margin-bottom: 8px; }
#detail-pane .refs { color: #888; font-size: 12px; margin-bottom: 16px; }

#note-section { margin-top: 12px; }
#note-section input, #note-section select { background: #16213e; color: #e0e0e0; border: 1px solid #555; padding: 4px 8px; border-radius: 3px; font-size: 12px; }
#note-section input { width: 100%; margin-top: 4px; }
#note-section label { font-size: 12px; color: #888; }

.tag-btn { padding: 3px 10px; border: 1px solid #555; border-radius: 3px; cursor: pointer; font-size: 11px; background: #16213e; color: #888; margin-right: 4px; }
.tag-btn.active { background: #0f3460; color: #7eb8da; border-color: #7eb8da; }
.tag-btn:hover { border-color: #7eb8da; }

#dup-picker { background: #2a2a1a; border: 1px solid #ffd54f; border-radius: 4px; padding: 10px; margin-top: 10px; display: none; }
#dup-picker.open { display: block; }
#dup-picker select { background: #16213e; color: #e0e0e0; border: 1px solid #555; padding: 4px 8px; border-radius: 3px; font-size: 12px; width: 100%; margin: 6px 0; }
#dup-picker .dup-actions { margin-top: 6px; display: flex; gap: 6px; }
#dup-picker .dup-actions button { padding: 4px 12px; border: none; border-radius: 3px; cursor: pointer; font-size: 12px; }
#dup-picker .dup-confirm { background: #4a3800; color: #ffd54f; }
#dup-picker .dup-cancel { background: #333; color: #e0e0e0; }

#conv-pane { flex: 2; overflow-y: auto; padding: 0; background: #111; }
.conv-event { padding: 8px 16px; border-bottom: 1px solid #1a1a2e; }
.conv-event.in-window { background: #1a2a1a; border-left: 3px solid #4caf50; }
.conv-event .role { font-weight: bold; font-size: 12px; margin-bottom: 4px; }
.conv-event .role.researcher { color: #64b5f6; }
.conv-event .role.scientist { color: #81c784; }
.conv-event .role.other { color: #888; }
.conv-event .tick { color: #666; font-size: 11px; }
.conv-event .text { white-space: pre-wrap; font-size: 13px; line-height: 1.5; max-height: 600px; overflow-y: hidden; position: relative; }
.conv-event .text.collapsed { max-height: 200px; }
.conv-event .text.collapsed::after { content: ''; position: absolute; bottom: 0; left: 0; right: 0; height: 40px; background: linear-gradient(transparent, #111); pointer-events: none; }
.conv-event.in-window .text.collapsed::after { background: linear-gradient(transparent, #1a2a1a); }
.conv-event .expand-btn { color: #64b5f6; cursor: pointer; font-size: 11px; margin-top: 4px; }
.window-divider { background: #2a2a1a; padding: 6px 16px; color: #7eb8da; font-weight: bold; font-size: 12px; border-top: 2px solid #7eb8da; border-bottom: 2px solid #7eb8da; }

kbd { background: #333; padding: 1px 5px; border-radius: 3px; font-size: 11px; border: 1px solid #555; }
#shortcuts { color: #666; font-size: 11px; margin-left: auto; }
.artifact-link { color: #64b5f6; text-decoration: underline; cursor: pointer; font-size: 12px; }
.artifact-link:hover { color: #90caf9; }

#figure-modal { display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.7); z-index: 200; justify-content: center; align-items: center; }
#figure-modal.open { display: flex; }
#figure-modal-inner { background: #1a1a2e; border: 1px solid #555; border-radius: 6px; max-width: 90vw; max-height: 90vh; display: flex; flex-direction: column; }
#figure-modal-body { padding: 16px; overflow: auto; flex: 1; display: flex; justify-content: center; }

#artifact-modal { display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.92); z-index: 100; justify-content: center; align-items: center; }
#artifact-modal.open { display: flex; }
#artifact-modal-inner { background: #1a1a2e; border: 1px solid #555; border-radius: 6px; width: 70vw; max-height: 85vh; display: flex; flex-direction: column; }
#artifact-modal-header { padding: 10px 16px; border-bottom: 1px solid #333; display: flex; justify-content: space-between; align-items: center; }
#artifact-modal-header h3 { color: #7eb8da; font-size: 14px; margin: 0; }
#artifact-modal-close { background: #333; border: 1px solid #555; color: #e0e0e0; padding: 4px 12px; cursor: pointer; border-radius: 3px; font-size: 13px; }
#artifact-modal-close:hover { background: #555; }
#artifact-modal-body { padding: 16px; overflow-y: auto; flex: 1; line-height: 1.6; font-size: 14px; }
#artifact-modal-body h1, #artifact-modal-body h2, #artifact-modal-body h3 { color: #7eb8da; margin: 16px 0 8px; }
#artifact-modal-body p { margin: 8px 0; }
#artifact-modal-body code { background: #16213e; padding: 2px 6px; border-radius: 3px; }
#artifact-modal-body pre { background: #16213e; padding: 12px; border-radius: 4px; overflow-x: auto; }
#artifact-modal-body ul, #artifact-modal-body ol { margin: 8px 0 8px 24px; }
#artifact-modal-body table { border-collapse: collapse; margin: 8px 0; }
#artifact-modal-body th, #artifact-modal-body td { border: 1px solid #444; padding: 4px 8px; }
#artifact-modal-body th { background: #16213e; }

#run-selector { background: #0d1b2a; padding: 6px 10px; border-bottom: 1px solid #333; }
#run-selector .run-title { font-size: 11px; color: #888; margin-bottom: 4px; font-weight: bold; }
#run-selector label { display: block; font-size: 12px; padding: 2px 0; cursor: pointer; color: #ccc; }
#run-selector label:hover { color: #fff; }
#run-selector input[type="checkbox"] { margin-right: 6px; }
.run-count { color: #666; font-size: 10px; }

.hunch-item .run-dots { display: flex; gap: 2px; }
.run-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
.label-source { font-size: 9px; color: #888; font-style: italic; }
.badge-inherited { background: #1a3a5c; color: #7eb8da; }
.bank-id { font-size: 10px; color: #666; }

#unsynced-banner { background: #4a3800; color: #ffd54f; padding: 8px 16px; font-size: 12px; display: none; border-bottom: 1px solid #ffd54f; }
#unsynced-banner.visible { display: block; }

#hunch-modal { display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.7); z-index: 150; justify-content: center; align-items: center; }
#hunch-modal.open { display: flex; }
</style>
</head>
<body>

<div id="top-bar">
    <button class="nav-btn" onclick="prevHunch()" title="Left arrow">&#9664; Prev</button>
    <button class="nav-btn" onclick="nextHunch()" title="Right arrow">Next &#9654;</button>
    <span id="position">-/-</span>
    <button class="label-btn btn-tp" onclick="labelCurrent('tp')">TP (t)</button>
    <button class="label-btn btn-fp" onclick="labelCurrent('fp')">FP (f)</button>
    <button class="label-btn btn-dup" onclick="startDup()">Dup (d)</button>
    <button class="label-btn btn-skip" onclick="clearLabel()" id="btn-clear" style="display:none;background:#333;border:1px solid #666">Clear (x)</button>
    <span id="current-label"></span>
    <span class="stats" id="stats"></span>
    <span id="run-dir" style="color:#7eb8da;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:400px" title=""></span>
    <span id="shortcuts"><kbd>&larr;</kbd><kbd>&rarr;</kbd> nav</span>
</div>

<div id="unsynced-banner"></div>
<div id="main">
    <div style="display:flex;flex-direction:column;width:220px;flex-shrink:0">
        <div id="run-selector"></div>
        <div id="hunch-list" style="flex:1;overflow-y:auto"></div>
    </div>
    <div id="detail-pane"></div>
    <div id="conv-pane"></div>
</div>

<div id="figure-modal" onclick="closeFigure()">
    <div id="figure-modal-inner" onclick="event.stopPropagation()">
        <div id="artifact-modal-header">
            <h3 id="figure-modal-title"></h3>
            <button id="artifact-modal-close" onclick="closeFigure()">Close (Esc)</button>
        </div>
        <div id="figure-modal-body">
            <img id="figure-modal-img" src="" style="max-width:100%;max-height:80vh">
        </div>
    </div>
</div>

<div id="hunch-modal" onclick="closeHunchModal()">
    <div onclick="event.stopPropagation()" style="background:#1a1a2e;border:1px solid #555;border-radius:6px;width:55vw;max-height:85vh;display:flex;flex-direction:column">
        <div style="padding:10px 16px;border-bottom:1px solid #333;display:flex;justify-content:space-between;align-items:center">
            <h3 id="hunch-modal-title" style="color:#7eb8da;font-size:14px;margin:0"></h3>
            <button onclick="closeHunchModal()" style="background:#333;border:1px solid #555;color:#e0e0e0;padding:4px 12px;cursor:pointer;border-radius:3px;font-size:13px">Close (Esc)</button>
        </div>
        <div id="hunch-modal-body" style="padding:16px;overflow-y:auto;flex:1;line-height:1.6;font-size:14px"></div>
    </div>
</div>

<div id="artifact-modal" onclick="closeArtifact()">
    <div id="artifact-modal-inner" onclick="event.stopPropagation()">
        <div id="artifact-modal-header">
            <h3 id="artifact-modal-title"></h3>
            <button id="artifact-modal-close" onclick="closeArtifact()">Close (Esc)</button>
        </div>
        <div id="artifact-modal-body"></div>
    </div>
</div>

<script>
let hunches = [];
let labels = {};
let currentIdx = 0;
let bankMode = false;
let availableRuns = [];
const KNOWN_TAGS = ['not_novel', 'borderline', 'interesting', 'nit'];

async function init() {
    const cfgResp = await fetch('/api/config');
    const cfg = await cfgResp.json();
    bankMode = cfg.bank_mode || false;
    availableRuns = cfg.runs || [];

    const rdEl = document.getElementById('run-dir');
    rdEl.textContent = cfg.project_dir || cfg.run_dir || '';
    rdEl.title = rdEl.textContent;

    if (bankMode && availableRuns.length > 0) {
        renderRunSelector();
    }

    await refreshHunches();
}

async function refreshHunches() {
    let url = '/api/hunches';
    if (bankMode) {
        const selected = availableRuns.filter(r => r.selected).map(r => r.name);
        url += '?runs=' + encodeURIComponent(selected.join(','));
    }
    const resp = await fetch(url);
    const data = await resp.json();
    hunches = data.hunches;
    labels = data.labels;

    if (data.unsynced_runs && data.unsynced_runs.length > 0) {
        const banner = document.getElementById('unsynced-banner');
        banner.textContent = 'Unsynced runs: ' + data.unsynced_runs.join(', ') +
            ' — run `hunch bank sync` to enable cross-run label propagation';
        banner.classList.add('visible');
    }

    renderList();
    if (hunches.length > 0) selectHunch(Math.min(currentIdx, hunches.length - 1));
    updateStats();
}

function renderRunSelector() {
    const el = document.getElementById('run-selector');
    const checks = availableRuns.map((r, i) =>
        `<label><input type="checkbox" ${r.selected ? 'checked' : ''} onchange="toggleRun(${i})"> ${esc(r.name)} <span class="run-count">(${r.hunch_count})</span></label>`
    ).join('');
    el.innerHTML = `<div class="run-title">Runs</div>${checks}`;
}

async function toggleRun(idx) {
    availableRuns[idx].selected = !availableRuns[idx].selected;
    currentIdx = 0;
    await refreshHunches();
}

function itemId(h) { return bankMode ? h.id : h.hunch_id; }
function itemSmell(h) { return bankMode ? h.canonical_smell : h.smell; }
function itemDesc(h) { return bankMode ? h.canonical_description : h.description; }

function renderList() {
    const el = document.getElementById('hunch-list');
    el.innerHTML = hunches.map((h, i) => {
        const id = itemId(h);
        const lbl = labels[id];
        let badge = '';
        if (lbl) {
            if (lbl.duplicate_of) {
                badge = `<span class="label-badge badge-dup">DUP</span>`;
            } else {
                const cls = 'badge-' + lbl.label;
                badge = `<span class="label-badge ${cls}">${lbl.label.toUpperCase()}</span>`;
                if (lbl.source === 'inherited') badge += `<span class="label-source"> inh</span>`;
            }
            const tags = lbl.tags || [];
            if (tags.length) badge += `<span style="color:#7eb8da;font-size:9px;margin-left:2px">${tags.map(t=>t.replace(/_/g,' ')).join(', ')}</span>`;
        }
        const displayId = bankMode ? (h.bank_id || h.hunch_id) : h.hunch_id;
        const runDots = (bankMode && h.runs && h.runs.length > 1)
            ? `<span class="run-count" style="margin-left:4px">${h.runs.length}r</span>` : '';
        return `<div class="hunch-item ${i === currentIdx ? 'active' : ''}" onclick="selectHunch(${i})" title="${esc(itemSmell(h))}">
            <span class="hid">${displayId}${runDots}</span><span>${badge}</span>
        </div>`;
    }).join('');
}

function esc(s) { return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

async function selectHunch(idx) {
    currentIdx = idx;
    const h = hunches[idx];
    renderList();
    renderDetail(h);
    document.getElementById('position').textContent = `${idx+1}/${hunches.length}`;

    const ctxId = bankMode ? encodeURIComponent(h.id) : h.hunch_id;
    const convResp = await fetch(`/api/hunch/${ctxId}/context`);
    const convData = await convResp.json();
    renderConversation(convData.events, h.bookmark_prev, h.bookmark_now);
}

function renderDetail(h) {
    const id = itemId(h);
    const smell = itemSmell(h);
    const desc = itemDesc(h);
    const lbl = labels[id];
    let labelDisplay = '';
    if (lbl) {
        const cls = lbl.label === 'tp' ? 'badge-tp' : lbl.label === 'fp' ? 'badge-fp' : 'badge-skip';
        labelDisplay = `<span class="label-badge ${cls}" style="font-size:13px;padding:3px 10px">${lbl.label.toUpperCase()}</span>`;
        if (lbl.source === 'inherited') {
            let inhLink = 'inherited';
            if (lbl.inherited_from_run && lbl.inherited_from_hunch_id) {
                const srcItem = hunches.find(h => h.source_run === lbl.inherited_from_run && h.source_hunch_id === lbl.inherited_from_hunch_id);
                if (srcItem) {
                    const srcId = itemId(srcItem);
                    inhLink = `<a href="#" class="artifact-link" style="color:#7eb8da" onclick="goToHunch('${esc(srcId)}'); return false;">inherited from ${esc(srcId)}</a>`;
                } else {
                    inhLink = `<a href="#" class="artifact-link" style="color:#7eb8da" onclick="openHunchModal('${esc(lbl.inherited_from_run)}', '${esc(lbl.inherited_from_hunch_id)}'); return false;">inherited from ${esc(lbl.inherited_from_run)}:${esc(lbl.inherited_from_hunch_id)}</a>`;
                }
            }
            labelDisplay += ` <span class="label-badge badge-inherited" style="font-size:10px;padding:1px 6px">${inhLink}</span>`;
        }
        if (lbl.category) labelDisplay += ` <span style="color:#888;font-size:12px">category: ${esc(lbl.category)}</span>`;
    }
    document.getElementById('current-label').innerHTML = labelDisplay;
    const clearBtn = document.getElementById('btn-clear');
    clearBtn.style.display = (bankMode && lbl && lbl.source === 'human') ? 'inline-block' : 'none';

    const chunkList = (h.triggering_refs || {}).chunks || [];
    const chunkLinks = chunkList.length
        ? chunkList.map(c => {
            const num = parseInt(c.split('-')[1], 10);
            return `<a href="#" class="artifact-link" onclick="scrollToTick(${num}, 'center'); return false;">${esc(c)}</a>`;
          }).join(', ')
        : '(none)';
    const artList = (h.triggering_refs || {}).artifacts || [];
    const artLinks = artList.length
        ? artList.map(a => `<a href="#" class="artifact-link" onclick="openArtifact('${esc(a)}'); return false;">${esc(a)}</a>`).join(', ')
        : '(none)';

    const displayId = bankMode ? (h.bank_id || h.hunch_id || h.id) : h.hunch_id;
    const bankInfo = (bankMode && h.bank_id) ? `<span class="bank-id">${esc(h.bank_id)}</span> &middot; ` : '';
    const runsInfo = (bankMode && h.runs && h.runs.length > 0)
        ? `<div class="meta" style="margin-top:2px">Runs: ${h.runs.map(r => esc(r)).join(', ')}${h.unsynced ? ' <span style="color:#ffd54f">(unsynced)</span>' : ''}</div>` : '';

    document.getElementById('detail-pane').innerHTML = `
        <h2>${esc(displayId)} ${labelDisplay}</h2>
        <div class="meta">${bankInfo}critic tick ${h.emitted_by_tick} &middot; <a href="#" class="artifact-link" onclick="scrollToTick(${h.bookmark_prev}, 'start'); return false;">conversation window ${h.bookmark_prev}..${h.bookmark_now}</a></div>
        ${runsInfo}
        <div class="smell">${esc(smell)}</div>
        <div class="description">${esc(desc)}</div>
        <div class="refs">Refs: ${chunkLinks}<br>Artifacts: ${artLinks}</div>
        <div id="note-section">
            <label>Tags:</label>
            <div id="tag-toggles" style="margin:4px 0 8px">${renderTagButtons(lbl)}</div>
            <label>Category:</label>
            <input type="text" id="category-input" value="${esc((lbl && lbl.category) || '')}"
                   onchange="updateMeta()" placeholder="e.g. confound, measurement, contradiction">
            <label style="margin-top:8px;display:block">Note:</label>
            <input type="text" id="note-input" value="${esc((lbl && lbl.note) || '')}"
                   onchange="updateMeta()" placeholder="free text, stays local">
            ${lbl && lbl.duplicate_of ? `<div style="margin-top:8px;color:#ffd54f;font-size:12px">Duplicate of <a href="#" class="artifact-link" style="color:#ffd54f" onclick="goToHunch('${esc(lbl.duplicate_of)}'); return false;">${esc(lbl.duplicate_of)}</a></div>` : ''}
            <div id="dup-picker">
                <label style="color:#ffd54f;font-size:12px;font-weight:bold">Mark as duplicate of:</label>
                <select id="dup-target"></select>
                <div class="dup-actions">
                    <button class="dup-confirm" onclick="confirmDup()">Confirm</button>
                    <button class="dup-cancel" onclick="cancelDup()">Cancel (Esc)</button>
                </div>
            </div>
        </div>
    `;
}

function renderConversation(events, bp, bn) {
    const pane = document.getElementById('conv-pane');
    const roleMap = { user_text: 'Scientist', assistant_text: 'Researcher' };
    const roleClass = { user_text: 'scientist', assistant_text: 'researcher' };

    let html = '';
    let dividerPlaced = false;
    let scrollTargetId = '';

    for (const ev of events) {
        if (!dividerPlaced && ev.tick_seq >= bp) {
            const divId = 'trigger-divider';
            scrollTargetId = divId;
            html += `<div class="window-divider" id="${divId}">&#9654; trigger window (${bp}..${bn})</div>`;
            dividerPlaced = true;
        }

        const inWindow = ev.tick_seq >= bp && ev.tick_seq <= bn;
        const role = roleMap[ev.type] || ev.type;
        const rclass = roleClass[ev.type] || 'other';
        const textLen = ev.text.length;
        const collapsed = textLen > 800 ? 'collapsed' : '';
        const evId = 'ev-' + ev.tick_seq;

        html += `<div class="conv-event ${inWindow ? 'in-window' : ''}" id="${evId}">
            <div class="role ${rclass}">[${esc(role)}] <span class="tick">tick ${ev.tick_seq}</span></div>
            <div class="text ${collapsed}" id="text-${ev.tick_seq}">${esc(ev.text)}</div>
            ${textLen > 800 ? `<div class="expand-btn" onclick="toggleExpand(${ev.tick_seq})">show more (${textLen} chars)</div>` : ''}
        </div>`;
    }

    pane.innerHTML = html;

    if (scrollTargetId) {
        const target = document.getElementById(scrollTargetId);
        if (target) {
            setTimeout(() => target.scrollIntoView({ block: 'start', behavior: 'auto' }), 50);
        }
    }
}

function toggleExpand(tickSeq) {
    const el = document.getElementById('text-' + tickSeq);
    el.classList.toggle('collapsed');
    const btn = el.nextElementSibling;
    if (btn) btn.textContent = el.classList.contains('collapsed') ? 'show more' : 'show less';
}

async function labelCurrent(label) {
    const h = hunches[currentIdx];
    if (!h) return;
    const id = itemId(h);
    const cat = document.getElementById('category-input')?.value || '';
    const note = document.getElementById('note-input')?.value || '';
    const tags = getCurrentTags();
    await fetch(`/api/hunch/${encodeURIComponent(id)}/label`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ label, category: cat, note, tags }),
    });
    await refreshHunches();
}

async function clearLabel() {
    const h = hunches[currentIdx];
    if (!h) return;
    const id = itemId(h);
    await fetch(`/api/hunch/${encodeURIComponent(id)}/label`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ label: '__clear__' }),
    });
    await refreshHunches();
}

async function updateMeta() {
    const h = hunches[currentIdx];
    if (!h) return;
    const id = itemId(h);
    const lbl = labels[id];
    if (!lbl) return;
    const cat = document.getElementById('category-input')?.value || '';
    const note = document.getElementById('note-input')?.value || '';
    const tags = getCurrentTags();
    await fetch(`/api/hunch/${encodeURIComponent(id)}/label`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ label: lbl.label, category: cat, note, tags }),
    });
    await refreshHunches();
}

function renderTagButtons(lbl) {
    const activeTags = (lbl && lbl.tags) || [];
    return KNOWN_TAGS.map(tag => {
        const active = activeTags.includes(tag);
        const display = tag.replace(/_/g, ' ');
        return `<button class="tag-btn ${active ? 'active' : ''}" onclick="toggleTag('${tag}')">${display}</button>`;
    }).join('');
}

function getCurrentTags() {
    const h = hunches[currentIdx];
    if (!h) return [];
    const lbl = labels[itemId(h)];
    return (lbl && lbl.tags) || [];
}

async function toggleTag(tag) {
    const h = hunches[currentIdx];
    if (!h) return;
    const id = itemId(h);
    const lbl = labels[id];
    if (!lbl) return;
    const tags = [...getCurrentTags()];
    const idx = tags.indexOf(tag);
    if (idx >= 0) tags.splice(idx, 1);
    else tags.push(tag);
    await fetch(`/api/hunch/${encodeURIComponent(id)}/label`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ label: lbl.label, category: lbl.category || '', note: lbl.note || '', tags }),
    });
    await refreshHunches();
    const updatedLbl = labels[id];
    document.getElementById('tag-toggles').innerHTML = renderTagButtons(updatedLbl);
}

function goToHunch(hunchId) {
    const idx = hunches.findIndex(h => itemId(h) === hunchId);
    if (idx >= 0) selectHunch(idx);
}

function startDup() {
    const h = hunches[currentIdx];
    if (!h) return;
    const hId = itemId(h);
    const picker = document.getElementById('dup-picker');
    const select = document.getElementById('dup-target');
    const options = hunches
        .filter(o => itemId(o) !== hId)
        .map(o => {
            const oId = itemId(o);
            const oLbl = labels[oId];
            const lblTag = oLbl ? ` [${oLbl.label.toUpperCase()}]` : '';
            return `<option value="${oId}">${oId}${lblTag} — ${esc(itemSmell(o).substring(0, 80))}</option>`;
        });
    select.innerHTML = options.join('');
    picker.classList.add('open');
    select.focus();
}

function cancelDup() {
    document.getElementById('dup-picker').classList.remove('open');
}

async function confirmDup() {
    const h = hunches[currentIdx];
    if (!h) return;
    const hId = itemId(h);
    const targetId = document.getElementById('dup-target').value;
    if (!targetId) return;
    const targetLabel = labels[targetId];
    const inheritedLabel = (targetLabel && targetLabel.label) || 'skip';
    const cat = document.getElementById('category-input')?.value || '';
    const note = document.getElementById('note-input')?.value || '';
    const tags = getCurrentTags();
    await fetch(`/api/hunch/${encodeURIComponent(hId)}/label`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ label: inheritedLabel, category: cat, note, tags, duplicate_of: targetId }),
    });
    document.getElementById('dup-picker').classList.remove('open');
    await refreshHunches();
}

function updateStats() {
    const total = hunches.length;
    const ids = new Set(hunches.map(h => itemId(h)));
    const relevantLabels = Object.entries(labels).filter(([k]) => ids.has(k));
    const labeled = relevantLabels.length;
    const tp = relevantLabels.filter(([,l]) => l.label === 'tp').length;
    const fp = relevantLabels.filter(([,l]) => l.label === 'fp').length;
    const inherited = relevantLabels.filter(([,l]) => l.source === 'inherited').length;
    const inhNote = inherited > 0 ? ` (${inherited} inherited)` : '';
    document.getElementById('stats').textContent = `${labeled}/${total} labeled${inhNote} | ${tp} tp ${fp} fp`;
}

function nextHunch() { if (currentIdx < hunches.length - 1) selectHunch(currentIdx + 1); }
function prevHunch() { if (currentIdx > 0) selectHunch(currentIdx - 1); }

const renderer = new marked.Renderer();
renderer.link = function(token) {
    const href = token.href || '';
    const text = token.text || href;
    if (href.startsWith('mailto:')) return text;
    return `<a href="${href}" target="_blank">${text}</a>`;
};
renderer.image = function(token) {
    const src = token.href || '';
    const alt = token.text || src;
    const figUrl = '/api/figure?name=' + encodeURIComponent(src);
    return `<img src="${figUrl}" alt="${esc(alt)}" style="max-width:100%;cursor:pointer;border:1px solid #333;border-radius:4px;margin:8px 0" onclick="openFigure('${figUrl}', '${esc(alt)}')" title="Click to enlarge">`;
};
marked.setOptions({ renderer });

function scrollToTick(tickSeq, block) {
    const pane = document.getElementById('conv-pane');
    let target = document.getElementById('ev-' + tickSeq);
    if (!target) {
        const evEls = pane.querySelectorAll('.conv-event');
        let best = null;
        let bestDist = Infinity;
        for (const el of evEls) {
            const elTick = parseInt(el.id.replace('ev-', ''), 10);
            const dist = Math.abs(elTick - tickSeq);
            if (dist < bestDist) { bestDist = dist; best = el; }
        }
        target = best;
    }
    if (target) {
        target.scrollIntoView({ block: block || 'center', behavior: 'smooth' });
        target.style.outline = '2px solid #7eb8da';
        setTimeout(() => { target.style.outline = ''; }, 2000);
    }
}

async function openArtifact(name) {
    const resp = await fetch('/api/artifact?name=' + encodeURIComponent(name));
    const data = await resp.json();
    if (data.error) {
        alert('Artifact not found: ' + name);
        return;
    }
    document.getElementById('artifact-modal-title').textContent = name;
    let html = marked.parse(data.content, {mangle: false, headerIds: false});
    html = html.replace(/(figures\/[^\s<"'`]+\.(?:png|jpg|jpeg|svg|gif))/gi, function(match) {
        const url = '/api/figure?name=' + encodeURIComponent(match);
        return `<a href="#" class="artifact-link" onclick="openFigure('${url}', '${esc(match)}'); return false;">${match}</a>`;
    });
    document.getElementById('artifact-modal-body').innerHTML = html;
    document.getElementById('artifact-modal').classList.add('open');
}

function closeArtifact() {
    document.getElementById('artifact-modal').classList.remove('open');
}

function openFigure(url, title) {
    document.getElementById('figure-modal-title').textContent = title;
    document.getElementById('figure-modal-img').src = url;
    document.getElementById('figure-modal').classList.add('open');
}

function closeFigure() {
    document.getElementById('figure-modal').classList.remove('open');
}

async function openHunchModal(run, hunchId) {
    const resp = await fetch(`/api/hunch-detail/${encodeURIComponent(run)}/${encodeURIComponent(hunchId)}`);
    const data = await resp.json();
    if (data.error) { alert(data.error); return; }
    const h = data;
    document.getElementById('hunch-modal-title').textContent = `${run}:${hunchId}`;

    const chunkList = (h.triggering_refs || {}).chunks || [];
    const chunkLinks = chunkList.length
        ? chunkList.map(c => {
            const num = parseInt(c.split('-')[1], 10);
            return `<a href="#" class="artifact-link" onclick="closeHunchModal(); scrollToTick(${num}, 'center'); return false;">${esc(c)}</a>`;
          }).join(', ')
        : '(none)';
    const artList = (h.triggering_refs || {}).artifacts || [];
    const artLinks = artList.length
        ? artList.map(a => `<a href="#" class="artifact-link" onclick="closeHunchModal(); openArtifact('${esc(a)}'); return false;">${esc(a)}</a>`).join(', ')
        : '(none)';

    document.getElementById('hunch-modal-body').innerHTML = `
        <div style="margin-bottom:8px;color:#888;font-size:12px">Run: ${esc(run)} &middot; Hunch: ${esc(hunchId)} &middot; tick ${h.emitted_by_tick} &middot; window ${h.bookmark_prev}..${h.bookmark_now}</div>
        <div style="font-size:15px;font-weight:bold;margin-bottom:12px;color:#fff">${esc(h.smell)}</div>
        <div style="white-space:pre-wrap;margin-bottom:16px;line-height:1.6">${esc(h.description)}</div>
        <div style="color:#888;font-size:12px">Refs: ${chunkLinks}<br>Artifacts: ${artLinks}</div>
    `;
    document.getElementById('hunch-modal').classList.add('open');
}

function closeHunchModal() {
    document.getElementById('hunch-modal').classList.remove('open');
}

document.addEventListener('keydown', (e) => {
    if (document.getElementById('hunch-modal').classList.contains('open')) {
        if (e.key === 'Escape') closeHunchModal();
        return;
    }
    if (document.getElementById('figure-modal').classList.contains('open')) {
        if (e.key === 'Escape') closeFigure();
        return;
    }
    if (document.getElementById('artifact-modal').classList.contains('open')) {
        if (e.key === 'Escape') closeArtifact();
        return;
    }
    if (document.getElementById('dup-picker').classList.contains('open')) {
        if (e.key === 'Escape') cancelDup();
        else if (e.key === 'Enter') confirmDup();
        return;
    }
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
    if (e.key === 'ArrowRight') nextHunch();
    else if (e.key === 'ArrowLeft') prevHunch();
});

init();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

def _load_dedup_ids(path: Path) -> set[str] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return set(data.get("duplicate_ids", []))
    except (json.JSONDecodeError, KeyError):
        return None


def create_app(
    replay_dir: Path,
    run_dir: Path | None = None,
    project_dir: Path | None = None,
    novel_only: bool = False,
    dedup: bool = False,
) -> Any:
    try:
        from flask import Flask, jsonify, request
    except ImportError:
        raise ImportError("flask is required: pip install flask")

    _validate_replay_dir(replay_dir)
    conversation = _load_conversation(replay_dir / "conversation.jsonl")

    # Determine bank mode vs legacy mode
    bank_mode = False
    bank_state = None
    bank_path = None
    eval_dir = None
    available_runs: list[dict] = []

    if project_dir is not None:
        eval_dir = project_dir / ".hunch" / "eval"
        bank_dir = project_dir / ".hunch" / "bank"
        bank_path = bank_dir / "hunch_bank.jsonl"
        if bank_path.exists():
            from hunch.bank import read_bank
            bank_state = read_bank(bank_path)
            bank_mode = True
        available_runs = _discover_runs(eval_dir)
    elif run_dir is not None:
        inferred = _infer_project_dir(run_dir)
        if inferred is not None:
            eval_dir = inferred / ".hunch" / "eval"
            bank_dir = inferred / ".hunch" / "bank"
            bank_path = bank_dir / "hunch_bank.jsonl"
            if bank_path.exists():
                from hunch.bank import read_bank
                bank_state = read_bank(bank_path)
                bank_mode = True
                available_runs = _discover_runs(eval_dir)
                run_name = run_dir.name
                for r in available_runs:
                    r["selected"] = (r["name"] == run_name)

    # Legacy mode: load hunches from single run dir
    legacy_hunches: list[dict] = []
    labels_path: Path | None = None
    if not bank_mode and run_dir is not None:
        legacy_hunches = _load_hunches(run_dir / "hunches.jsonl")
        if novel_only:
            novel_ids = _load_novel_ids(run_dir / "novelty_summary.json")
            if novel_ids is not None:
                legacy_hunches = [
                    h for h in legacy_hunches if h["hunch_id"] in novel_ids
                ]
        if dedup:
            dup_ids = _load_dedup_ids(run_dir / "dedup" / "dedup_summary.json")
            if dup_ids is not None:
                legacy_hunches = [
                    h for h in legacy_hunches if h["hunch_id"] not in dup_ids
                ]
        labels_path = run_dir / "labels.jsonl"

    app = Flask(__name__)

    @app.route("/")
    def index():
        return HTML_PAGE

    @app.route("/api/config")
    def api_config():
        return jsonify({
            "run_dir": str(run_dir.resolve()) if run_dir else "",
            "project_dir": str(project_dir.resolve()) if project_dir else "",
            "replay_dir": str(replay_dir.resolve()),
            "bank_mode": bank_mode,
            "runs": available_runs,
        })

    @app.route("/api/hunches")
    def api_hunches():
        if bank_mode:
            runs_param = request.args.get("runs", "")
            if runs_param:
                selected = runs_param.split(",")
            else:
                selected = [r["name"] for r in available_runs if r["selected"]]
            items = _load_bank_items(bank_state, eval_dir, selected)

            unsynced = [
                it["source_run"] for it in items
                if it.get("unsynced")
            ]
            unsynced_runs = sorted(set(unsynced))

            labels_dict = _resolve_bank_labels(bank_state, items)
            return jsonify({
                "hunches": items,
                "labels": labels_dict,
                "bank_mode": True,
                "unsynced_runs": unsynced_runs,
            })
        return jsonify({
            "hunches": legacy_hunches,
            "labels": _read_labels(labels_path) if labels_path else {},
            "bank_mode": False,
            "unsynced_runs": [],
        })

    @app.route("/api/hunch/<path:item_id>/context")
    def api_context(item_id: str):
        if bank_mode:
            items = _load_bank_items(
                bank_state, eval_dir,
                [r["name"] for r in available_runs if r["selected"]],
            )
            item = next((it for it in items if it["id"] == item_id), None)
        else:
            item = next(
                (h for h in legacy_hunches if h["hunch_id"] == item_id),
                None,
            )
        if item is None:
            return jsonify({"error": "not found"}), 404

        bp = item["bookmark_prev"]
        bn = item["bookmark_now"]

        chunk_nums = []
        for c in (item.get("triggering_refs") or {}).get("chunks", []):
            try:
                chunk_nums.append(int(c.split("-")[1]))
            except (IndexError, ValueError):
                pass

        lo = max(1, bp - 200)
        if chunk_nums:
            lo = min(lo, min(chunk_nums) - 10)
            lo = max(1, lo)
        hi = bn + 50

        events = [
            e for e in conversation
            if lo <= e["tick_seq"] <= hi
            and e["type"] in ("user_text", "assistant_text")
        ]
        return jsonify({
            "events": events,
            "bookmark_prev": bp,
            "bookmark_now": bn,
        })

    @app.route("/api/artifact")
    def api_artifact():
        name = request.args.get("name", "")
        if not name:
            return jsonify({"error": "missing name"}), 400
        stem = name.replace("/", "_")
        arts_dir = replay_dir / "artifacts"
        if not arts_dir.is_dir():
            return jsonify({"error": "no artifacts dir"}), 404
        matches = sorted(
            p for p in arts_dir.iterdir()
            if p.name.startswith(stem + "__")
        )
        if not matches:
            return jsonify({"error": f"artifact {name!r} not found"}), 404
        content = matches[-1].read_text(errors="replace")
        return jsonify({"name": name, "content": content})

    @app.route("/api/figure")
    def api_figure():
        from flask import send_file
        name = request.args.get("name", "")
        if not name or ".." in name:
            return jsonify({"error": "invalid name"}), 400
        fig_path = replay_dir.parent.parent / name
        if not fig_path.is_file():
            return jsonify({"error": f"figure {name!r} not found"}), 404
        return send_file(fig_path, mimetype="image/png")

    @app.route("/api/hunch-detail/<run>/<hunch_id>")
    def api_hunch_detail(run: str, hunch_id: str):
        if eval_dir is None:
            return jsonify({"error": "no eval dir"}), 404
        hunches_path = eval_dir / run / "hunches.jsonl"
        if not hunches_path.exists():
            return jsonify({"error": f"run {run!r} not found"}), 404
        hunches = _load_hunches(hunches_path)
        h = next((h for h in hunches if h["hunch_id"] == hunch_id), None)
        if h is None:
            return jsonify({"error": f"hunch {hunch_id!r} not found in {run}"}), 404
        return jsonify(h)

    @app.route("/api/hunch/<path:item_id>/label", methods=["POST"])
    def api_label(item_id: str):
        nonlocal bank_state
        data = request.get_json()
        label = data.get("label")
        is_clear = label == "__clear__"
        if not is_clear and label not in ("tp", "fp"):
            return jsonify({"error": "invalid label"}), 400

        tags = data.get("tags", [])
        if not isinstance(tags, list):
            tags = []
        duplicate_of = data.get("duplicate_of") or None
        ts = _dt.datetime.now(_dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ",
        )

        if bank_mode and bank_path is not None:
            from hunch.bank import BankWriter

            items = _load_bank_items(
                bank_state, eval_dir,
                [r["name"] for r in available_runs],
            )
            item = next(
                (it for it in items if it["id"] == item_id), None,
            )
            if item is None:
                return jsonify({"error": "item not found"}), 404

            bid = item.get("bank_id")
            if bid is None:
                return jsonify({
                    "error": "hunch not synced to bank",
                }), 400

            write_tags = list(tags)
            if duplicate_of:
                write_tags = [
                    t for t in write_tags if not t.startswith("dup_of:")
                ]
                write_tags.append(f"dup_of:{duplicate_of}")

            writer = BankWriter(bank_path)
            writer.write_label(
                bank_id=bid,
                run=item["source_run"],
                hunch_id=item["source_hunch_id"],
                label=None if is_clear else label,
                ts=ts,
                category=data.get("category", ""),
                labeled_by="scientist_retro",
                note=data.get("note", ""),
                tags=write_tags,
            )

            # Re-read bank state so subsequent reads are fresh
            from hunch.bank import read_bank
            bank_state = read_bank(bank_path)

        elif labels_path is not None:
            record = {
                "hunch_id": item_id,
                "label": label,
                "category": data.get("category", ""),
                "source": "evaluator",
                "bank_match": None,
                "note": data.get("note", ""),
                "tags": tags,
                "ts": ts,
            }
            if duplicate_of:
                record["duplicate_of"] = duplicate_of
            _write_label(labels_path, record)

        return jsonify({"ok": True})

    return app


def run_server(
    replay_dir: Path | None = None,
    run_dir: Path | None = None,
    project_dir: Path | None = None,
    novel_only: bool = False,
    dedup: bool = False,
    port: int = 5555,
) -> int:
    if project_dir is not None:
        if replay_dir is None:
            replay_dir = project_dir / ".hunch" / "replay"
    if replay_dir is None:
        raise SystemExit(
            "Error: --replay-dir is required (or use --project-dir)"
        )

    app = create_app(
        replay_dir,
        run_dir=run_dir,
        project_dir=project_dir,
        novel_only=novel_only,
        dedup=dedup,
    )
    print(f"Annotation UI: http://localhost:{port}")
    print(f"  replay: {replay_dir}")
    if project_dir:
        print(f"  project: {project_dir}")
        print(f"  mode: bank (multi-run)")
    elif run_dir:
        print(f"  run:    {run_dir}")
    app.run(host="127.0.0.1", port=port, debug=False)
    return 0


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Web-based hunch annotation UI")
    ap.add_argument("--project-dir", type=Path, default=None,
                    help="project root (discovers runs and bank automatically)")
    ap.add_argument("--replay-dir", type=Path, default=None)
    ap.add_argument("--run-dir", type=Path, default=None)
    ap.add_argument("--novel-only", action="store_true")
    ap.add_argument("--dedup", action="store_true")
    ap.add_argument("--port", type=int, default=5555)
    args = ap.parse_args()
    if args.project_dir is None and args.run_dir is None:
        ap.error("one of --project-dir or --run-dir is required")
    raise SystemExit(run_server(
        replay_dir=args.replay_dir,
        run_dir=args.run_dir,
        project_dir=args.project_dir,
        novel_only=args.novel_only,
        dedup=args.dedup,
        port=args.port,
    ))


if __name__ == "__main__":
    main()
