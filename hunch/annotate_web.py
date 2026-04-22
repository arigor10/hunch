"""Browser-based annotation UI for offline hunch evaluation.

Local Flask server serving a single-page app with three-column layout:
hunch list (left), detail pane (center), scrollable conversation (right).

Features:
- Labels hunches as tp/fp/skip; persists to ``labels.jsonl`` (append-only,
  last-write-wins). Category and free-text note fields per label.
- Keyboard shortcuts: t/f/s for labels, arrow keys for navigation.
- ``--novel-only``: filter to novel hunches only. Reads
  ``novelty_summary.json`` from ``--run-dir``.
- ``--dedup``: exclude duplicate hunches. Reads
  ``dedup/dedup_summary.json`` from ``--run-dir``.
- Artifact references (listed under "Artifacts:" in the detail pane) are
  clickable — opens a modal with the markdown rendered via marked.js.
  Figures referenced inside artifacts (``figures/*.png``) are also
  clickable and displayed in a separate modal overlay.
- Chunk references (c-XXXX) are clickable — scrolls the conversation
  pane to that tick (centered). The trigger-window link scrolls to the
  start of the window.
- Conversation auto-scrolls to the trigger-window divider on hunch
  selection. In-window events are highlighted green.
- Long messages are collapsed with a show-more toggle.

Usage:
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

#artifact-modal { display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.7); z-index: 100; justify-content: center; align-items: center; }
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
</style>
</head>
<body>

<div id="top-bar">
    <button class="nav-btn" onclick="prevHunch()" title="Left arrow">&#9664; Prev</button>
    <button class="nav-btn" onclick="nextHunch()" title="Right arrow">Next &#9654;</button>
    <span id="position">-/-</span>
    <button class="label-btn btn-tp" onclick="labelCurrent('tp')">TP (t)</button>
    <button class="label-btn btn-fp" onclick="labelCurrent('fp')">FP (f)</button>
    <button class="label-btn btn-skip" onclick="labelCurrent('skip')">Skip (s)</button>
    <span id="current-label"></span>
    <span class="stats" id="stats"></span>
    <span id="shortcuts"><kbd>t</kbd> tp  <kbd>f</kbd> fp  <kbd>s</kbd> skip  <kbd>&larr;</kbd><kbd>&rarr;</kbd> nav</span>
</div>

<div id="main">
    <div id="hunch-list"></div>
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

<div id="artifact-modal">
    <div id="artifact-modal-inner">
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

async function init() {
    const resp = await fetch('/api/hunches');
    const data = await resp.json();
    hunches = data.hunches;
    labels = data.labels;
    renderList();
    if (hunches.length > 0) selectHunch(0);
    updateStats();
}

function renderList() {
    const el = document.getElementById('hunch-list');
    el.innerHTML = hunches.map((h, i) => {
        const lbl = labels[h.hunch_id];
        let badge = '';
        if (lbl) {
            const cls = 'badge-' + lbl.label;
            badge = `<span class="label-badge ${cls}">${lbl.label.toUpperCase()}</span>`;
        }
        return `<div class="hunch-item ${i === currentIdx ? 'active' : ''}" onclick="selectHunch(${i})" title="${esc(h.smell)}">
            <span class="hid">${h.hunch_id}</span>${badge}
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

    const convResp = await fetch(`/api/hunch/${h.hunch_id}/context`);
    const convData = await convResp.json();
    renderConversation(convData.events, h.bookmark_prev, h.bookmark_now);
}

function renderDetail(h) {
    const lbl = labels[h.hunch_id];
    let labelDisplay = '';
    if (lbl) {
        const cls = lbl.label === 'tp' ? 'badge-tp' : lbl.label === 'fp' ? 'badge-fp' : 'badge-skip';
        labelDisplay = `<span class="label-badge ${cls}" style="font-size:13px;padding:3px 10px">${lbl.label.toUpperCase()}</span>`;
        if (lbl.category) labelDisplay += ` <span style="color:#888;font-size:12px">category: ${esc(lbl.category)}</span>`;
    }
    document.getElementById('current-label').innerHTML = labelDisplay;

    const chunkList = h.triggering_refs.chunks || [];
    const chunkLinks = chunkList.length
        ? chunkList.map(c => {
            const num = parseInt(c.split('-')[1], 10);
            return `<a href="#" class="artifact-link" onclick="scrollToTick(${num}, 'center'); return false;">${esc(c)}</a>`;
          }).join(', ')
        : '(none)';
    const artList = h.triggering_refs.artifacts || [];
    const artLinks = artList.length
        ? artList.map(a => `<a href="#" class="artifact-link" onclick="openArtifact('${esc(a)}'); return false;">${esc(a)}</a>`).join(', ')
        : '(none)';

    document.getElementById('detail-pane').innerHTML = `
        <h2>${esc(h.hunch_id)} ${labelDisplay}</h2>
        <div class="meta">critic tick ${h.emitted_by_tick} &middot; <a href="#" class="artifact-link" onclick="scrollToTick(${h.bookmark_prev}, 'start'); return false;">conversation window ${h.bookmark_prev}..${h.bookmark_now}</a></div>
        <div class="smell">${esc(h.smell)}</div>
        <div class="description">${esc(h.description)}</div>
        <div class="refs">Refs: ${chunkLinks}<br>Artifacts: ${artLinks}</div>
        <div id="note-section">
            <label>Category:</label>
            <input type="text" id="category-input" value="${esc((lbl && lbl.category) || '')}"
                   onchange="updateMeta()" placeholder="e.g. confound, measurement, contradiction">
            <label style="margin-top:8px;display:block">Note:</label>
            <input type="text" id="note-input" value="${esc((lbl && lbl.note) || '')}"
                   onchange="updateMeta()" placeholder="free text, stays local">
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
    const cat = document.getElementById('category-input')?.value || '';
    const note = document.getElementById('note-input')?.value || '';
    await fetch(`/api/hunch/${h.hunch_id}/label`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ label, category: cat, note }),
    });
    const resp = await fetch('/api/hunches');
    const data = await resp.json();
    labels = data.labels;
    renderList();
    renderDetail(h);
    updateStats();
}

async function updateMeta() {
    const h = hunches[currentIdx];
    if (!h) return;
    const lbl = labels[h.hunch_id];
    if (!lbl) return;
    const cat = document.getElementById('category-input')?.value || '';
    const note = document.getElementById('note-input')?.value || '';
    await fetch(`/api/hunch/${h.hunch_id}/label`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ label: lbl.label, category: cat, note }),
    });
    const resp = await fetch('/api/hunches');
    labels = (await resp.json()).labels;
}

function updateStats() {
    const total = hunches.length;
    const labeled = Object.keys(labels).length;
    const tp = Object.values(labels).filter(l => l.label === 'tp').length;
    const fp = Object.values(labels).filter(l => l.label === 'fp').length;
    const skip = Object.values(labels).filter(l => l.label === 'skip').length;
    document.getElementById('stats').textContent = `${labeled}/${total} labeled | ${tp} tp ${fp} fp ${skip} skip`;
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

document.addEventListener('keydown', (e) => {
    if (document.getElementById('figure-modal').classList.contains('open')) {
        if (e.key === 'Escape') closeFigure();
        return;
    }
    if (document.getElementById('artifact-modal').classList.contains('open')) {
        if (e.key === 'Escape') closeArtifact();
        return;
    }
    if (e.target.tagName === 'INPUT') return;
    if (e.key === 'ArrowRight') nextHunch();
    else if (e.key === 'ArrowLeft') prevHunch();
    else if (e.key === 't') labelCurrent('tp');
    else if (e.key === 'f') labelCurrent('fp');
    else if (e.key === 's') labelCurrent('skip');
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
    run_dir: Path,
    novel_only: bool = False,
    dedup: bool = False,
) -> Any:
    try:
        from flask import Flask, jsonify, request
    except ImportError:
        raise ImportError("flask is required: pip install flask")

    _validate_replay_dir(replay_dir)
    conversation = _load_conversation(replay_dir / "conversation.jsonl")
    hunches = _load_hunches(run_dir / "hunches.jsonl")

    if novel_only:
        novel_ids = _load_novel_ids(run_dir / "novelty_summary.json")
        if novel_ids is not None:
            hunches = [h for h in hunches if h["hunch_id"] in novel_ids]

    if dedup:
        dup_ids = _load_dedup_ids(run_dir / "dedup" / "dedup_summary.json")
        if dup_ids is not None:
            hunches = [h for h in hunches if h["hunch_id"] not in dup_ids]

    labels_path = run_dir / "labels.jsonl"

    app = Flask(__name__)

    @app.route("/")
    def index():
        return HTML_PAGE

    @app.route("/api/hunches")
    def api_hunches():
        return jsonify({
            "hunches": hunches,
            "labels": _read_labels(labels_path),
        })

    @app.route("/api/hunch/<hunch_id>/context")
    def api_context(hunch_id: str):
        hunch = next((h for h in hunches if h["hunch_id"] == hunch_id), None)
        if hunch is None:
            return jsonify({"error": "not found"}), 404

        bp = hunch["bookmark_prev"]
        bn = hunch["bookmark_now"]

        chunk_nums = []
        for c in (hunch.get("triggering_refs") or {}).get("chunks", []):
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
        return jsonify({"events": events, "bookmark_prev": bp, "bookmark_now": bn})

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

    @app.route("/api/hunch/<hunch_id>/label", methods=["POST"])
    def api_label(hunch_id: str):
        data = request.get_json()
        label = data.get("label")
        if label not in ("tp", "fp", "skip"):
            return jsonify({"error": "invalid label"}), 400

        ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _write_label(labels_path, {
            "hunch_id": hunch_id,
            "label": label,
            "category": data.get("category", ""),
            "source": "evaluator",
            "bank_match": None,
            "note": data.get("note", ""),
            "ts": ts,
        })
        return jsonify({"ok": True})

    return app


def run_server(
    replay_dir: Path,
    run_dir: Path,
    novel_only: bool = False,
    dedup: bool = False,
    port: int = 5555,
) -> int:
    app = create_app(replay_dir, run_dir, novel_only=novel_only, dedup=dedup)
    print(f"Annotation UI: http://localhost:{port}")
    print(f"  replay: {replay_dir}")
    print(f"  run:    {run_dir}")
    print(f"  hunches: {len(app.view_functions)}")
    app.run(host="127.0.0.1", port=port, debug=False)
    return 0


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Web-based hunch annotation UI")
    ap.add_argument("--replay-dir", type=Path, required=True)
    ap.add_argument("--run-dir", type=Path, required=True)
    ap.add_argument("--novel-only", action="store_true")
    ap.add_argument("--dedup", action="store_true")
    ap.add_argument("--port", type=int, default=5555)
    args = ap.parse_args()
    raise SystemExit(run_server(
        args.replay_dir, args.run_dir,
        novel_only=args.novel_only, dedup=args.dedup, port=args.port,
    ))


if __name__ == "__main__":
    main()
