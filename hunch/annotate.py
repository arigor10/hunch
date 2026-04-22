"""Two-pane annotation TUI for offline hunch evaluation.

Renders each hunch alongside its conversation context so the evaluator
can judge whether the critic's catch is a true positive.

Layout:

    +-- Hunch 3/84 [UNLABELED] --------+-- Conversation Context -----------+
    |                                    | [Researcher] (42): Let's look... |
    | Smell: 4-bit + SDPA diagnosed...  | [Scientist] (43): The results... |
    |                                    |                                   |
    | Description: In c-0612, the...    | --- trigger window (44..47) ---   |
    |                                    | [Scientist] (44): Running with... |
    | Refs: c-0612, c-0845             |                                   |
    |                                    |                                   |
    +------------------------------------+-----------------------------------+
    | [t]p  [f]p  [s]kip  [c]ategory  [n]ote  [</>] nav  [a]ll  [q]uit     |
    +-----------------------------------------------------------------------+

Usage:
    python -m hunch.annotate \\
        --replay-dir /path/to/.hunch/replay \\
        --run-dir data/critic_run_01
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Data loading (pure, no TUI dependency)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ConversationEvent:
    tick_seq: int
    type: str
    text: str
    timestamp: str = ""


@dataclass(frozen=True)
class HunchEntry:
    hunch_id: str
    smell: str
    description: str
    bookmark_prev: int
    bookmark_now: int
    emitted_by_tick: int
    triggering_refs: dict[str, list[str]] = field(default_factory=dict)


def load_conversation(path: Path) -> list[ConversationEvent]:
    events: list[ConversationEvent] = []
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
            events.append(ConversationEvent(
                tick_seq=seq,
                type=d.get("type", ""),
                text=d.get("text", ""),
                timestamp=d.get("timestamp", ""),
            ))
    return events


def load_hunches(path: Path) -> list[HunchEntry]:
    hunches: list[HunchEntry] = []
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
            hunches.append(HunchEntry(
                hunch_id=d.get("hunch_id", ""),
                smell=d.get("smell", ""),
                description=d.get("description", ""),
                bookmark_prev=d.get("bookmark_prev", -1),
                bookmark_now=d.get("bookmark_now", -1),
                emitted_by_tick=d.get("emitted_by_tick", -1),
                triggering_refs=d.get("triggering_refs") or {},
            ))
    return hunches


def render_dialogue(
    events: list[ConversationEvent],
    bookmark_prev: int,
    bookmark_now: int,
    context_before: int = 200,
    context_after: int = 50,
) -> str:
    """Render conversation events around the hunch's trigger window.

    Shows events in [bookmark_prev - context_before, bookmark_now + context_after].
    Inserts a divider at the trigger window boundary.
    """
    lo = max(1, bookmark_prev - context_before)
    hi = bookmark_now + context_after

    relevant = [e for e in events if lo <= e.tick_seq <= hi]
    if not relevant:
        return "(no conversation events in range)"

    role_map = {
        "user_text": "Researcher",
        "assistant_text": "Scientist",
    }

    lines: list[str] = []
    divider_placed = False

    for ev in relevant:
        if not divider_placed and ev.tick_seq >= bookmark_prev:
            lines.append(f"{'─' * 40}")
            lines.append(f"  trigger window ({bookmark_prev}..{bookmark_now})")
            lines.append(f"{'─' * 40}")
            divider_placed = True

        role = role_map.get(ev.type, ev.type)
        text = ev.text
        if len(text) > 2000:
            text = text[:2000] + "... [truncated]"
        lines.append(f"[{role}] (tick {ev.tick_seq}):")
        lines.append(text)
        lines.append("")

    if not divider_placed:
        lines.insert(0, f"(trigger window {bookmark_prev}..{bookmark_now} outside visible range)")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# TUI app (textual, lazy-imported inside run())
# ---------------------------------------------------------------------------

def load_novel_ids(path: Path) -> set[str] | None:
    """Load novel hunch IDs from a novelty_summary.json. Returns None if absent."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return set(data.get("novel_ids", []))
    except (json.JSONDecodeError, KeyError):
        return None


def run(
    replay_dir: Path,
    run_dir: Path,
    show_all: bool = False,
    novel_only: bool = False,
) -> int:
    """Launch the annotation TUI. Blocks until the user quits."""
    try:
        from textual.app import App, ComposeResult
        from textual.binding import Binding
        from textual.containers import Horizontal, Vertical
        from textual.widgets import Footer, Header, Static
        from textual.widgets import Input as TextInput
    except ImportError as e:
        import sys
        sys.stderr.write(
            f"hunch annotate: textual is not installed ({e}). "
            "Install with: pip install textual\n"
        )
        return 1

    from hunch.journal.labels import LabelsWriter, read_labels

    events = load_conversation(replay_dir / "conversation.jsonl")
    hunches = load_hunches(run_dir / "hunches.jsonl")

    if novel_only:
        novel_ids = load_novel_ids(run_dir / "novelty_summary.json")
        if novel_ids is None:
            print("No novelty_summary.json found in run dir.")
            return 1
        hunches = [h for h in hunches if h.hunch_id in novel_ids]

    if not hunches:
        print("No hunches to show.")
        return 0

    labels_path = run_dir / "labels.jsonl"

    class AnnotateApp(App):
        CSS = """
        Screen { layout: vertical; }
        #status { height: 1; padding: 0 1; background: $boost; }
        #main { height: 1fr; }
        #left-pane {
            width: 1fr;
            padding: 1;
            border-right: solid $accent;
            overflow-y: auto;
        }
        #right-pane {
            width: 2fr;
            padding: 1;
            overflow-y: auto;
        }
        #input-bar {
            height: 3;
            padding: 0 1;
            border-top: solid $accent;
            display: none;
        }
        #input-bar.visible { display: block; }
        """

        BINDINGS = [
            Binding("t", "label_tp", "TP"),
            Binding("f", "label_fp", "FP"),
            Binding("s", "label_skip", "Skip"),
            Binding("c", "enter_category", "Category"),
            Binding("n", "enter_note", "Note"),
            Binding("right", "next_hunch", "Next", show=True),
            Binding("left", "prev_hunch", "Prev", show=True),
            Binding("a", "toggle_show_all", "All/unlabeled"),
            Binding("q", "quit", "Quit"),
        ]

        def __init__(self) -> None:
            super().__init__()
            self._hunches = hunches
            self._events = events
            self._labels = read_labels(labels_path)
            self._writer = LabelsWriter(labels_path)
            self._show_all = show_all
            self._visible_ids: list[str] = []
            self._cursor = 0
            self._input_mode: str = ""
            self._update_visible()

        def _update_visible(self) -> None:
            if self._show_all:
                self._visible_ids = [h.hunch_id for h in self._hunches]
            else:
                self._visible_ids = [
                    h.hunch_id for h in self._hunches
                    if h.hunch_id not in self._labels
                ]
            if self._cursor >= len(self._visible_ids):
                self._cursor = max(0, len(self._visible_ids) - 1)

        def _current_hunch(self) -> HunchEntry | None:
            if not self._visible_ids:
                return None
            hid = self._visible_ids[self._cursor]
            for h in self._hunches:
                if h.hunch_id == hid:
                    return h
            return None

        def compose(self) -> ComposeResult:
            yield Header(show_clock=False)
            yield Static("", id="status")
            with Horizontal(id="main"):
                yield Static("", id="left-pane")
                yield Static("", id="right-pane")
            yield TextInput(id="input-bar", placeholder="")
            yield Footer()

        def on_mount(self) -> None:
            self._refresh_display()

        def _refresh_display(self) -> None:
            status: Static = self.query_one("#status", Static)
            left: Static = self.query_one("#left-pane", Static)
            right: Static = self.query_one("#right-pane", Static)

            total = len(self._hunches)
            labeled = len(self._labels)
            tp = sum(1 for r in self._labels.values() if r.get("label") == "tp")
            fp = sum(1 for r in self._labels.values() if r.get("label") == "fp")
            skip = sum(1 for r in self._labels.values() if r.get("label") == "skip")
            mode = "all" if self._show_all else "unlabeled"
            n_visible = len(self._visible_ids)

            status.update(
                f"  {labeled}/{total} labeled  |  "
                f"{tp} tp  {fp} fp  {skip} skip  |  "
                f"showing: {mode} ({n_visible})  |  "
                f"[{self._cursor + 1}/{n_visible}]"
            )

            hunch = self._current_hunch()
            if hunch is None:
                left.update("No hunches to show.\nPress [a] to toggle show-all, [q] to quit.")
                right.update("")
                return

            label_rec = self._labels.get(hunch.hunch_id, {})
            label_str = label_rec.get("label", "")
            cat = label_rec.get("category", "")
            note = label_rec.get("note", "")

            label_display = ""
            if label_str:
                label_display = f"  [{label_str.upper()}]"
                if cat:
                    label_display += f"  category: {cat}"
                if note:
                    label_display += f"\n  note: {note}"

            refs = hunch.triggering_refs
            chunk_refs = ", ".join(refs.get("chunks", []))
            artifact_refs = ", ".join(refs.get("artifacts", []))

            left_text = (
                f"[bold]{_esc(hunch.hunch_id)}[/bold]{_esc(label_display)}\n"
                f"tick {hunch.emitted_by_tick}  "
                f"window {hunch.bookmark_prev}..{hunch.bookmark_now}\n\n"
                f"[bold]Smell:[/bold] {_esc(hunch.smell)}\n\n"
                f"[bold]Description:[/bold]\n{_esc(hunch.description)}\n\n"
                f"[bold]Refs:[/bold] {_esc(chunk_refs or '(none)')}\n"
                f"[bold]Artifacts:[/bold] {_esc(artifact_refs or '(none)')}"
            )
            left.update(left_text)

            dialogue = render_dialogue(
                self._events,
                hunch.bookmark_prev,
                hunch.bookmark_now,
            )
            right.update(_esc(dialogue))

        # -------- navigation --------

        def action_next_hunch(self) -> None:
            if self._visible_ids and self._cursor < len(self._visible_ids) - 1:
                self._cursor += 1
                self._refresh_display()

        def action_prev_hunch(self) -> None:
            if self._cursor > 0:
                self._cursor -= 1
                self._refresh_display()

        def action_toggle_show_all(self) -> None:
            self._show_all = not self._show_all
            current_hid = self._visible_ids[self._cursor] if self._visible_ids else None
            self._update_visible()
            if current_hid and current_hid in self._visible_ids:
                self._cursor = self._visible_ids.index(current_hid)
            self._refresh_display()

        # -------- labeling --------

        def _write_label(self, label: str) -> None:
            hunch = self._current_hunch()
            if hunch is None:
                self.notify("no hunch selected", severity="warning")
                return
            ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            existing = self._labels.get(hunch.hunch_id, {})
            self._writer.write(
                hunch.hunch_id, label, ts,
                category=existing.get("category", ""),
                note=existing.get("note", ""),
            )
            self._labels = read_labels(labels_path)
            self.notify(f"{hunch.hunch_id} -> {label}")
            if not self._show_all:
                self._update_visible()
            self._refresh_display()

        def action_label_tp(self) -> None:
            self._write_label("tp")

        def action_label_fp(self) -> None:
            self._write_label("fp")

        def action_label_skip(self) -> None:
            self._write_label("skip")

        # -------- category / note input --------

        def action_enter_category(self) -> None:
            hunch = self._current_hunch()
            if hunch is None:
                return
            self._input_mode = "category"
            inp: TextInput = self.query_one("#input-bar", TextInput)
            existing = self._labels.get(hunch.hunch_id, {})
            inp.value = existing.get("category", "")
            inp.placeholder = "Category (e.g. confound, measurement, contradiction):"
            inp.add_class("visible")
            inp.focus()

        def action_enter_note(self) -> None:
            hunch = self._current_hunch()
            if hunch is None:
                return
            self._input_mode = "note"
            inp: TextInput = self.query_one("#input-bar", TextInput)
            existing = self._labels.get(hunch.hunch_id, {})
            inp.value = existing.get("note", "")
            inp.placeholder = "Note (free text, stays local):"
            inp.add_class("visible")
            inp.focus()

        def on_input_submitted(self, event) -> None:
            inp: TextInput = self.query_one("#input-bar", TextInput)
            hunch = self._current_hunch()
            if hunch is None:
                inp.remove_class("visible")
                return

            value = inp.value.strip()
            existing = self._labels.get(hunch.hunch_id, {})
            label = existing.get("label", "skip")
            category = existing.get("category", "")
            note = existing.get("note", "")

            if self._input_mode == "category":
                category = value
            elif self._input_mode == "note":
                note = value

            ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            self._writer.write(
                hunch.hunch_id, label, ts,
                category=category, note=note,
            )
            self._labels = read_labels(labels_path)
            inp.remove_class("visible")
            self._input_mode = ""
            self.notify(f"{hunch.hunch_id}: {self._input_mode or 'updated'}")
            self._refresh_display()

        def on_key(self, event) -> None:
            if event.key == "escape":
                inp: TextInput = self.query_one("#input-bar", TextInput)
                if inp.has_class("visible"):
                    inp.remove_class("visible")
                    self._input_mode = ""
                    event.prevent_default()

    app = AnnotateApp()
    try:
        app.run(mouse=False)
    finally:
        _restore_terminal()
    return 0


def _esc(s: str) -> str:
    return s.replace("[", r"\[")


def _restore_terminal() -> None:
    import sys
    sys.stdout.write(
        "\x1b[?1000l"
        "\x1b[?1002l"
        "\x1b[?1003l"
        "\x1b[?1006l"
        "\x1b[?1015l"
        "\x1b[?25h"
        "\x1b[?1049l"
    )
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Annotate hunches with TP/FP labels")
    ap.add_argument(
        "--replay-dir", type=Path, required=True,
        help="Path to .hunch/replay/ directory (conversation.jsonl)",
    )
    ap.add_argument(
        "--run-dir", type=Path, required=True,
        help="Path to critic run directory (hunches.jsonl, labels.jsonl)",
    )
    ap.add_argument(
        "--show-all", action="store_true",
        help="Show already-labeled hunches too (default: unlabeled only)",
    )
    ap.add_argument(
        "--novel-only", action="store_true",
        help="Only show novel hunches (requires novelty_summary.json in run-dir)",
    )
    args = ap.parse_args()
    raise SystemExit(run(
        args.replay_dir, args.run_dir,
        show_all=args.show_all, novel_only=args.novel_only,
    ))


if __name__ == "__main__":
    main()
