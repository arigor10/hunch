"""Textual-based side-panel TUI for Scientist feedback.

Purpose: show current hunches as they appear in the replay buffer,
let the Scientist react quickly with one keystroke (good / bad /
skip). Polls `hunches.jsonl` and `feedback.jsonl` every second so
hunches emitted by a running framework show up without reload.

Layout:

    ┌─ Header ───────────────────────────────────────────────────┐
    │ Hunch — 3 pending · 2 surfaced · 1 labeled                 │
    ├─ Hunch list (one per row, selectable) ─────────────────────┤
    │ > h-0007  pending  calibration drift                       │
    │   h-0008  surfaced ordering inconsistency                  │
    │   h-0009  surfaced figure caption mismatch     [bad]       │
    ├─ Expanded detail for the selected hunch ───────────────────┤
    │ h-0007  (pending)                                          │
    │ calibration drift                                          │
    │ 3× discrepancy between runs A and B...                     │
    ├─ Footer (keybinds) ────────────────────────────────────────┤
    │ g Good  b Bad  s Skip  r Refresh  a All/unlabeled  q Quit  │
    └────────────────────────────────────────────────────────────┘

Import of `textual` is deferred to `run()` so the rest of the package
imports cleanly without the TUI dependency installed — useful for
CI, tests, and environments where the TUI isn't needed.
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hunch.journal.feedback import FeedbackWriter, read_labeled_hunch_ids
from hunch.journal.hunches import HunchRecord, read_current_hunches


# ---------------------------------------------------------------------------
# Data snapshot (pure, testable)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PanelSnapshot:
    """One poll's view of the replay buffer, merged for display."""
    records: list[HunchRecord]
    labels: dict[str, str]
    max_tick_seq: int = 0

    def visible(self, show_labeled: bool) -> list[HunchRecord]:
        if show_labeled:
            return list(self.records)
        return [r for r in self.records if r.hunch_id not in self.labels]

    def counts(self) -> dict[str, int]:
        pending = sum(1 for r in self.records if r.status == "pending")
        surfaced = sum(1 for r in self.records if r.status == "surfaced")
        labeled = len(self.labels)
        return {"pending": pending, "surfaced": surfaced, "labeled": labeled}


def read_max_tick_seq(conversation_path: Path) -> int:
    """Return the highest `tick_seq` in conversation.jsonl, or 0 if the
    file is absent / empty / entirely malformed.

    This is a liveness cue for the TUI: it ticks up as the capture loop
    writes new events, and sits still if the framework isn't running.
    """
    if not conversation_path.exists():
        return 0
    max_seq = 0
    with open(conversation_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            seq = d.get("tick_seq")
            if isinstance(seq, int) and seq > max_seq:
                max_seq = seq
    return max_seq


def read_snapshot(replay_dir: Path) -> PanelSnapshot:
    """Read a consistent view of hunches + feedback from disk."""
    records = read_current_hunches(replay_dir / "hunches.jsonl")
    labels = read_labeled_hunch_ids(replay_dir / "feedback.jsonl")
    max_seq = read_max_tick_seq(replay_dir / "conversation.jsonl")
    return PanelSnapshot(records=records, labels=labels, max_tick_seq=max_seq)


# ---------------------------------------------------------------------------
# TUI app (textual, lazy-imported inside run())
# ---------------------------------------------------------------------------

def run(replay_dir: Path, poll_s: float = 1.0) -> int:
    """Launch the TUI. Blocks until the user quits."""
    try:
        from textual.app import App, ComposeResult
        from textual.binding import Binding
        from textual.containers import Vertical
        from textual.reactive import reactive
        from textual.widgets import (
            DataTable,
            Footer,
            Header,
            Static,
        )
    except ImportError as e:
        import sys
        sys.stderr.write(
            f"hunch panel: textual is not installed ({e}). "
            "Install with: pipx inject hunch textual\n"
        )
        return 1

    class HunchPanel(App):
        CSS = """
        Screen { layout: vertical; }
        #status { height: 1; padding: 0 1; background: $boost; }
        DataTable { height: 1fr; }
        #detail {
            height: auto;
            min-height: 6;
            padding: 1;
            border-top: solid $accent;
            background: $surface;
        }
        """

        BINDINGS = [
            Binding("g", "label_good", "Good"),
            Binding("b", "label_bad", "Bad"),
            Binding("s", "label_skip", "Skip"),
            Binding("a", "toggle_show_all", "Show all"),
            Binding("r", "refresh", "Refresh"),
            Binding("q", "quit", "Quit"),
        ]

        show_labeled: reactive[bool] = reactive(False)
        snapshot: reactive[PanelSnapshot | None] = reactive(None)

        def __init__(self, replay_dir: Path, poll_s: float) -> None:
            super().__init__()
            self.replay_dir = replay_dir
            self.poll_s = poll_s
            self.feedback_writer = FeedbackWriter(
                feedback_path=replay_dir / "feedback.jsonl"
            )

        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            yield Static("", id="status")
            table = DataTable(id="table", cursor_type="row")
            table.add_columns("id", "status", "smell", "label")
            yield table
            yield Static("", id="detail")
            yield Footer()

        def on_mount(self) -> None:
            self._refresh_snapshot()
            self.set_interval(self.poll_s, self._refresh_snapshot)

        # -------- data refresh --------

        def _refresh_snapshot(self) -> None:
            try:
                snap = read_snapshot(self.replay_dir)
            except Exception:
                # Reading must not crash the TUI; just skip this tick.
                return
            self.snapshot = snap  # triggers watch_snapshot

        def watch_snapshot(self, old: PanelSnapshot | None, new: PanelSnapshot | None) -> None:
            if new is None:
                return
            self._rebuild_table(new)

        def watch_show_labeled(self, old: bool, new: bool) -> None:
            if self.snapshot is not None:
                self._rebuild_table(self.snapshot)

        def _rebuild_table(self, snap: PanelSnapshot) -> None:
            table: Any = self.query_one("#table", DataTable)
            status_widget: Static = self.query_one("#status", Static)

            visible = snap.visible(self.show_labeled)
            prev_cursor_key = self._current_cursor_key(table)

            table.clear()
            for r in visible:
                label = snap.labels.get(r.hunch_id, "")
                table.add_row(
                    r.hunch_id,
                    r.status,
                    _truncate(r.smell, 70),
                    label,
                    key=r.hunch_id,
                )

            # Restore selection if possible.
            if prev_cursor_key is not None:
                for i, r in enumerate(visible):
                    if r.hunch_id == prev_cursor_key:
                        table.move_cursor(row=i)
                        break

            c = snap.counts()
            mode = "all" if self.show_labeled else "unlabeled"
            status_widget.update(
                f"Hunch — {c['pending']} pending · {c['surfaced']} surfaced · "
                f"{c['labeled']} labeled  ·  events: {snap.max_tick_seq}  ·  "
                f"showing: {mode} ({len(visible)} of {len(snap.records)})"
            )
            self._refresh_detail()

        def _current_cursor_key(self, table: Any) -> str | None:
            try:
                row = table.cursor_row
                if row is None or row < 0:
                    return None
                coord = table.coordinate_to_cell_key((row, 0))
                if coord is None:
                    return None
                return coord.row_key.value
            except Exception:
                return None

        def on_data_table_row_highlighted(self, _event) -> None:
            self._refresh_detail()

        def _refresh_detail(self) -> None:
            detail: Static = self.query_one("#detail", Static)
            if self.snapshot is None:
                detail.update("")
                return
            key = self._current_cursor_key(self.query_one("#table", DataTable))
            if key is None:
                detail.update("(no hunch selected)")
                return
            records = {r.hunch_id: r for r in self.snapshot.records}
            r = records.get(key)
            if r is None:
                detail.update("")
                return
            label = self.snapshot.labels.get(r.hunch_id, "")
            label_line = f"  —  labeled: {label}" if label else ""
            detail.update(
                f"[b]{r.hunch_id}[/b]  ({r.status}){label_line}\n"
                f"[b]{_markup_escape(r.smell)}[/b]\n\n"
                f"{_markup_escape(r.description)}"
            )

        # -------- actions --------

        def action_toggle_show_all(self) -> None:
            self.show_labeled = not self.show_labeled

        def action_refresh(self) -> None:
            self._refresh_snapshot()

        def action_label_good(self) -> None:
            self._label_current("good")

        def action_label_bad(self) -> None:
            self._label_current("bad")

        def action_label_skip(self) -> None:
            self._label_current("skip")

        def _label_current(self, label: str) -> None:
            table = self.query_one("#table", DataTable)
            key = self._current_cursor_key(table)
            if key is None:
                self.notify("no hunch selected", severity="warning")
                return
            ts = _utc_now_iso()
            try:
                self.feedback_writer.write_explicit(key, label, ts)
            except Exception as e:
                self.notify(f"write failed: {e}", severity="error")
                return
            self.notify(f"{key} → {label}")
            self._refresh_snapshot()

    app = HunchPanel(replay_dir=replay_dir, poll_s=poll_s)
    try:
        # mouse=False: the panel has no click/scroll interactions and
        # leaked mouse-reporting escape sequences on abnormal exit
        # (e.g. alt-key collisions with tmux) turn the parent shell into
        # a gibberish generator. CLI users don't expect mouse anyway.
        app.run(mouse=False)
    finally:
        # Belt-and-suspenders: even with mouse=False, if Textual crashed
        # mid-init or left the terminal in alt-screen / hidden-cursor
        # state, restore it explicitly. Safe to emit unconditionally —
        # these are "disable X" sequences; terminals that weren't in X
        # just ignore them.
        _restore_terminal()
    return 0


def _restore_terminal() -> None:
    """Unconditional terminal cleanup after the TUI exits.

    Disables every mouse-reporting mode Textual (or any other TUI) may
    have enabled, re-shows the cursor, and exits the alternate screen
    buffer. Written to stdout rather than stderr so it reaches the
    terminal the panel was rendering to.
    """
    import sys
    sys.stdout.write(
        "\x1b[?1000l"   # disable X11 mouse reporting
        "\x1b[?1002l"   # disable cell-motion mouse tracking
        "\x1b[?1003l"   # disable all-motion mouse tracking
        "\x1b[?1006l"   # disable SGR-extended mouse mode
        "\x1b[?1015l"   # disable URXVT-extended mouse mode
        "\x1b[?25h"     # show cursor
        "\x1b[?1049l"   # exit alternate screen buffer
    )
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"


def _markup_escape(s: str) -> str:
    """Escape Rich/Textual markup so hunch text with brackets doesn't
    get interpreted as markup (`[emit]`-style ids are common)."""
    return s.replace("[", r"\[")


def _utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
