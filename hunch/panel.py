"""Textual-based side-panel TUI for Scientist feedback.

Purpose: show current hunches as they appear in the replay buffer,
let the Scientist react quickly with one keystroke (good / bad /
skip). Polls `hunches.jsonl` and `feedback.jsonl` every second so
hunches emitted by a running framework show up without reload.

Layout:

    ┌─ Header ───────────────────────────────────────────────────┐
    │ Hunch — 3 pending · 1 approved · 2 delivered               │
    ├─ Hunch list (one per row, selectable) ─────────────────────┤
    │ > h-0007  pending   calibration drift                      │
    │   h-0008  delivered ordering inconsistency                 │
    │   h-0009  dismissed figure caption mismatch                │
    ├─ Expanded detail for the selected hunch ───────────────────┤
    │ h-0007  (pending)                                          │
    │ calibration drift                                          │
    │ 3× discrepancy between runs A and B...                     │
    ├─ Footer (keybinds) ────────────────────────────────────────┤
    │ g Good  b Bad  s Skip  r Refresh  a Show all  q Quit       │
    └────────────────────────────────────────────────────────────┘

Import of `textual` is deferred to `run()` so the rest of the package
imports cleanly without the TUI dependency installed — useful for
CI, tests, and environments where the TUI isn't needed.
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hunch.journal.feedback import (
    FeedbackWriter,
    HunchEdit,
    HunchResponse,
    read_hunch_edits,
    read_hunch_responses,
    read_labeled_hunch_ids,
)
from hunch.journal.hunches import HunchRecord, read_current_hunches


# ---------------------------------------------------------------------------
# Data snapshot (pure, testable)
# ---------------------------------------------------------------------------

def display_status(record: HunchRecord, label: str, acknowledged: bool = False) -> str:
    """Derive user-facing status from raw hunch status + feedback label."""
    if record.status == "surfaced":
        if acknowledged:
            return "acknowledged"
        return "delivered"
    if record.status == "filtered":
        return "filtered"
    if record.status not in ("pending", ""):
        return record.status
    if label == "good":
        return "approved"
    if label == "bad":
        return "dismissed"
    if label == "skip":
        return "skipped"
    return "pending"


@dataclass(frozen=True)
class PanelSnapshot:
    """One poll's view of the replay buffer, merged for display."""
    records: list[HunchRecord]
    labels: dict[str, str]
    edits: dict[str, HunchEdit] = field(default_factory=dict)
    responses: dict[str, HunchResponse] = field(default_factory=dict)
    max_tick_seq: int = 0

    def display_status_for(self, hunch_id: str, record: HunchRecord) -> str:
        return display_status(
            record,
            self.labels.get(hunch_id, ""),
            acknowledged=hunch_id in self.responses,
        )

    def visible(self, show_all: bool) -> list[HunchRecord]:
        if show_all:
            return list(self.records)
        return [
            r for r in self.records
            if self.display_status_for(r.hunch_id, r) in ("pending", "approved")
        ]

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in self.records:
            ds = self.display_status_for(r.hunch_id, r)
            counts[ds] = counts.get(ds, 0) + 1
        return counts


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
    feedback_path = replay_dir / "feedback.jsonl"
    records = read_current_hunches(replay_dir / "hunches.jsonl")
    labels = read_labeled_hunch_ids(feedback_path)
    edits = read_hunch_edits(feedback_path)
    responses = read_hunch_responses(feedback_path)
    max_seq = read_max_tick_seq(replay_dir / "conversation.jsonl")
    return PanelSnapshot(
        records=records, labels=labels, edits=edits,
        responses=responses, max_tick_seq=max_seq,
    )


# ---------------------------------------------------------------------------
# TUI app (textual, lazy-imported inside run())
# ---------------------------------------------------------------------------

def run(replay_dir: Path, poll_s: float = 1.0, web_port: int = 5556) -> int:
    """Launch the TUI. Blocks until the user quits."""
    try:
        from textual.app import App, ComposeResult
        from textual.binding import Binding
        from textual.containers import Vertical
        from textual.reactive import reactive
        from textual.screen import ModalScreen
        from textual.widgets import (
            DataTable,
            Footer,
            Header,
            Static,
            TextArea,
        )
    except ImportError as e:
        import sys
        sys.stderr.write(
            f"hunch panel: textual is not installed ({e}). "
            "Install with: pipx inject hunch textual\n"
        )
        return 1

    class EditScreen(ModalScreen[tuple[str, str] | None]):
        """Modal editor with separate fields for smell and description.

        Returns (smell, description) on Ctrl+S, or None on Escape.
        """

        CSS = """
        EditScreen {
            align: center middle;
        }
        #edit-container {
            width: 90%;
            height: 80%;
            background: $surface;
            border: solid $accent;
            padding: 1;
        }
        #edit-hint {
            height: 1;
            padding: 0 1;
            background: $boost;
        }
        .field-label {
            height: 1;
            padding: 0 1;
            color: $text-muted;
        }
        #smell-area {
            height: auto;
            min-height: 3;
            max-height: 8;
        }
        #desc-label {
            margin-top: 1;
        }
        #desc-area {
            height: 1fr;
        }
        """

        BINDINGS = [
            Binding("escape", "cancel", "Cancel", priority=True),
            Binding("ctrl+s", "save", "Save", priority=True),
        ]

        def __init__(self, hunch_id: str, smell: str, description: str) -> None:
            super().__init__()
            self.hunch_id = hunch_id
            self._smell = smell
            self._description = description

        def compose(self) -> ComposeResult:
            with Vertical(id="edit-container"):
                yield Static(
                    f"Editing [b]{self.hunch_id}[/b]  —  "
                    "Ctrl+S to save, Escape to cancel",
                    id="edit-hint",
                )
                yield Static("Smell (one-liner):", classes="field-label")
                yield TextArea(self._smell, id="smell-area")
                yield Static("Description:", classes="field-label", id="desc-label")
                yield TextArea(self._description, id="desc-area")

        def on_mount(self) -> None:
            self.query_one("#smell-area", TextArea).focus()

        def action_save(self) -> None:
            smell = self.query_one("#smell-area", TextArea).text.strip()
            desc = self.query_one("#desc-area", TextArea).text.strip()
            self.dismiss((smell, desc))

        def action_cancel(self) -> None:
            self.dismiss(None)

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
            Binding("e", "edit_hunch", "Edit"),
            Binding("o", "open_in_browser", "Open"),
            Binding("a", "toggle_show_all", "Show all"),
            Binding("r", "refresh", "Refresh"),
            Binding("q", "quit", "Quit"),
        ]

        show_all: reactive[bool] = reactive(False)
        snapshot: reactive[PanelSnapshot | None] = reactive(None)

        def __init__(self, replay_dir: Path, poll_s: float, web_port: int = 5556) -> None:
            super().__init__()
            self.replay_dir = replay_dir
            self.poll_s = poll_s
            self.web_port = web_port
            self.feedback_writer = FeedbackWriter(
                feedback_path=replay_dir / "feedback.jsonl"
            )

        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            yield Static("", id="status")
            table = DataTable(id="table", cursor_type="row")
            table.add_columns("id", "status", "smell")
            yield table
            yield Static("", id="detail")
            yield Footer()

        def on_mount(self) -> None:
            self._refresh_snapshot()
            self.set_interval(self.poll_s, self._refresh_snapshot)
            self._start_web_server()

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

        def watch_show_all(self, old: bool, new: bool) -> None:
            if self.snapshot is not None:
                self._rebuild_table(self.snapshot)

        def _rebuild_table(self, snap: PanelSnapshot) -> None:
            table: Any = self.query_one("#table", DataTable)
            status_widget: Static = self.query_one("#status", Static)

            visible = snap.visible(self.show_all)
            prev_cursor_key = self._current_cursor_key(table)

            table.clear()
            for r in visible:
                ds = snap.display_status_for(r.hunch_id, r)
                edit = snap.edits.get(r.hunch_id)
                smell = edit.edited_smell if edit else r.smell
                table.add_row(
                    r.hunch_id,
                    ds,
                    _truncate(smell, 70),
                    key=r.hunch_id,
                )

            # Restore selection if possible.
            if prev_cursor_key is not None:
                for i, r in enumerate(visible):
                    if r.hunch_id == prev_cursor_key:
                        table.move_cursor(row=i)
                        break

            c = snap.counts()
            mode = "all" if self.show_all else "active"
            parts = []
            for key in ("pending", "approved", "delivered", "acknowledged", "dismissed", "skipped", "filtered"):
                n = c.get(key, 0)
                if n:
                    parts.append(f"{n} {key}")
            status_widget.update(
                f"Hunch — {' · '.join(parts)}  ·  events: {snap.max_tick_seq}  ·  "
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
            ds = self.snapshot.display_status_for(r.hunch_id, r)
            edit = self.snapshot.edits.get(r.hunch_id)
            resp = self.snapshot.responses.get(r.hunch_id)
            smell = edit.edited_smell if edit else r.smell
            description = edit.edited_description if edit else r.description
            edited_tag = "  [i](edited)[/i]" if edit else ""
            parts = [
                f"[b]{r.hunch_id}[/b]  ({ds}){edited_tag}",
                f"[b]{_markup_escape(smell)}[/b]",
                "",
                _markup_escape(description),
            ]
            if resp:
                parts.append("")
                parts.append(f"[dim]Researcher:[/dim] {_markup_escape(resp.response_text)}")
            detail.update("\n".join(parts))

        # -------- actions --------

        def action_toggle_show_all(self) -> None:
            self.show_all = not self.show_all

        def action_refresh(self) -> None:
            self._refresh_snapshot()

        def action_label_good(self) -> None:
            self._label_current("good")

        def action_label_bad(self) -> None:
            self._label_current("bad")

        def action_label_skip(self) -> None:
            self._label_current("skip")

        def action_edit_hunch(self) -> None:
            table = self.query_one("#table", DataTable)
            key = self._current_cursor_key(table)
            if key is None or self.snapshot is None:
                self.notify("no hunch selected", severity="warning")
                return
            records = {r.hunch_id: r for r in self.snapshot.records}
            r = records.get(key)
            if r is None:
                return

            ds = self.snapshot.display_status_for(r.hunch_id, r)
            if ds != "pending":
                self.notify(
                    f"can only edit pending hunches ({r.hunch_id} is {ds})",
                    severity="warning",
                )
                return

            def _on_edit_result(result: tuple[str, str] | None) -> None:
                if result is None:
                    return
                edited_smell, edited_desc = result
                if not edited_smell:
                    self.notify("smell cannot be empty", severity="warning")
                    return
                ts = _utc_now_iso()
                try:
                    self.feedback_writer.write_edit(
                        hunch_id=r.hunch_id,
                        original_smell=r.smell,
                        original_description=r.description,
                        edited_smell=edited_smell,
                        edited_description=edited_desc,
                        ts=ts,
                    )
                except Exception as e:
                    self.notify(f"edit write failed: {e}", severity="error")
                    return
                self.notify(f"{r.hunch_id} edited")
                self._refresh_snapshot()

            self.push_screen(
                EditScreen(r.hunch_id, r.smell, r.description),
                callback=_on_edit_result,
            )

        def action_open_in_browser(self) -> None:
            import webbrowser
            table = self.query_one("#table", DataTable)
            key = self._current_cursor_key(table)
            url = f"http://localhost:{self.web_port}/"
            if key:
                url += f"#{key}"
            webbrowser.open(url)
            self.notify(f"opened {url}")

        def _start_web_server(self) -> None:
            import threading
            try:
                from hunch.annotate_web import create_app
            except ImportError:
                self.notify("flask not installed — web viewer disabled", severity="warning")
                return
            try:
                flask_app = create_app(self.replay_dir, live=True)
            except Exception as e:
                self.notify(f"web server failed: {e}", severity="warning")
                return
            import logging
            log = logging.getLogger("werkzeug")
            log.setLevel(logging.ERROR)
            t = threading.Thread(
                target=flask_app.run,
                kwargs={"host": "127.0.0.1", "port": self.web_port},
                daemon=True,
            )
            t.start()
            self.notify(f"context viewer: http://localhost:{self.web_port}")

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
            if label == "good":
                self._relay_if_parked()
            self._refresh_snapshot()

        def _relay_if_parked(self) -> None:
            """If Claude is parked in a tmux research pane, type the approved
            hunch(es) in now (instant idle delivery). Otherwise a no-op — the
            Stop/UPS hooks carry it. Lazy import so the panel stays light, and
            never raises into the TUI."""
            try:
                from hunch.relay import FAILED, RELAYED, relay_pending

                outcome = relay_pending(self.replay_dir)
                if outcome == RELAYED:
                    self.notify("delivered to Claude")
                elif outcome == FAILED:
                    self.notify(
                        "relay failed — will deliver on your next message",
                        severity="warning",
                    )
                # not_in_tmux / no_research_pane / not_parked / nothing: silent —
                # a hook will deliver it.
            except Exception as e:
                self.notify(f"relay error: {e}", severity="warning")

    app = HunchPanel(replay_dir=replay_dir, poll_s=poll_s, web_port=web_port)
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
