"""Tests for the `panel` data layer (`PanelSnapshot` + `read_snapshot`).

The Textual app itself is exercised only through a smoke import; the
reactive UI is hard to test headlessly and doesn't carry logic that
isn't also covered by the data-layer tests here.
"""

from __future__ import annotations

from hunch.critic.protocol import Hunch, TriggeringRefs
from hunch.journal.feedback import FeedbackWriter
from hunch.journal.hunches import HunchesWriter
from hunch.capture.writer import ReplayBufferWriter
from hunch.panel import PanelSnapshot, display_status, read_max_tick_seq, read_snapshot


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _emit(writer: HunchesWriter, smell: str, description: str = "") -> str:
    hid = writer.allocate_id()
    writer.write_emit(
        hunch=Hunch(
            smell=smell,
            description=description,
            triggering_refs=TriggeringRefs(),
        ),
        hunch_id=hid,
        ts="2026-04-14T12:00:00Z",
        emitted_by_tick=1,
        bookmark_prev=0,
        bookmark_now=1,
    )
    return hid


# ---------------------------------------------------------------------------
# display_status
# ---------------------------------------------------------------------------

def test_display_status_pending_no_label(tmp_path):
    w = HunchesWriter(hunches_path=tmp_path / "hunches.jsonl")
    _emit(w, "smell A")
    snap = read_snapshot(tmp_path)
    assert snap.display_status_for("h-0001", snap.records[0]) == "pending"


def test_display_status_approved(tmp_path):
    w = HunchesWriter(hunches_path=tmp_path / "hunches.jsonl")
    _emit(w, "smell A")
    fb = FeedbackWriter(feedback_path=tmp_path / "feedback.jsonl")
    fb.write_explicit("h-0001", "good", "2026-04-14T12:05:00Z")
    snap = read_snapshot(tmp_path)
    assert snap.display_status_for("h-0001", snap.records[0]) == "approved"


def test_display_status_delivered(tmp_path):
    w = HunchesWriter(hunches_path=tmp_path / "hunches.jsonl")
    _emit(w, "smell A")
    w.write_status_change(
        hunch_id="h-0001", new_status="surfaced",
        ts="2026-04-14T12:10:00Z", by="hook:async_delivery",
    )
    snap = read_snapshot(tmp_path)
    assert snap.display_status_for("h-0001", snap.records[0]) == "delivered"


def test_display_status_dismissed(tmp_path):
    w = HunchesWriter(hunches_path=tmp_path / "hunches.jsonl")
    _emit(w, "smell A")
    fb = FeedbackWriter(feedback_path=tmp_path / "feedback.jsonl")
    fb.write_explicit("h-0001", "bad", "2026-04-14T12:05:00Z")
    snap = read_snapshot(tmp_path)
    assert snap.display_status_for("h-0001", snap.records[0]) == "dismissed"


def test_display_status_skipped(tmp_path):
    w = HunchesWriter(hunches_path=tmp_path / "hunches.jsonl")
    _emit(w, "smell A")
    fb = FeedbackWriter(feedback_path=tmp_path / "feedback.jsonl")
    fb.write_explicit("h-0001", "skip", "2026-04-14T12:05:00Z")
    snap = read_snapshot(tmp_path)
    assert snap.display_status_for("h-0001", snap.records[0]) == "skipped"


def test_display_status_delivered_overrides_label(tmp_path):
    """Once surfaced, display status is 'delivered' regardless of label."""
    w = HunchesWriter(hunches_path=tmp_path / "hunches.jsonl")
    _emit(w, "smell A")
    fb = FeedbackWriter(feedback_path=tmp_path / "feedback.jsonl")
    fb.write_explicit("h-0001", "good", "2026-04-14T12:05:00Z")
    w.write_status_change(
        hunch_id="h-0001", new_status="surfaced",
        ts="2026-04-14T12:10:00Z", by="hook:async_delivery",
    )
    snap = read_snapshot(tmp_path)
    assert snap.display_status_for("h-0001", snap.records[0]) == "delivered"


# ---------------------------------------------------------------------------
# read_snapshot
# ---------------------------------------------------------------------------

def test_read_snapshot_empty_replay_dir(tmp_path):
    snap = read_snapshot(tmp_path)
    assert snap.records == []
    assert snap.labels == {}


def test_read_snapshot_merges_hunches_and_feedback(tmp_path):
    w = HunchesWriter(hunches_path=tmp_path / "hunches.jsonl")
    _emit(w, "smell A")
    _emit(w, "smell B")

    fb = FeedbackWriter(feedback_path=tmp_path / "feedback.jsonl")
    fb.write_explicit("h-0001", "good", "2026-04-14T12:05:00Z")

    snap = read_snapshot(tmp_path)
    assert [r.hunch_id for r in snap.records] == ["h-0001", "h-0002"]
    assert snap.labels == {"h-0001": "good"}


def test_read_snapshot_reflects_status_change(tmp_path):
    w = HunchesWriter(hunches_path=tmp_path / "hunches.jsonl")
    _emit(w, "smell A")
    w.write_status_change(
        hunch_id="h-0001",
        new_status="surfaced",
        ts="2026-04-14T12:10:00Z",
        by="hook:user_prompt_submit",
    )
    snap = read_snapshot(tmp_path)
    assert len(snap.records) == 1
    assert snap.records[0].status == "surfaced"


# ---------------------------------------------------------------------------
# PanelSnapshot.visible
# ---------------------------------------------------------------------------

def test_visible_shows_only_active_by_default(tmp_path):
    """Default view shows pending + approved, hides dismissed/delivered/skipped."""
    w = HunchesWriter(hunches_path=tmp_path / "hunches.jsonl")
    _emit(w, "smell A")  # h-0001 -> pending (no label)
    _emit(w, "smell B")  # h-0002 -> dismissed (labeled bad)
    _emit(w, "smell C")  # h-0003 -> approved (labeled good, still pending)
    _emit(w, "smell D")  # h-0004 -> delivered (surfaced)
    _emit(w, "smell E")  # h-0005 -> skipped
    fb = FeedbackWriter(feedback_path=tmp_path / "feedback.jsonl")
    fb.write_explicit("h-0002", "bad", "2026-04-14T12:05:00Z")
    fb.write_explicit("h-0003", "good", "2026-04-14T12:06:00Z")
    fb.write_explicit("h-0005", "skip", "2026-04-14T12:07:00Z")
    w.write_status_change(
        hunch_id="h-0004", new_status="surfaced",
        ts="2026-04-14T12:08:00Z", by="hook:async_delivery",
    )

    snap = read_snapshot(tmp_path)
    visible = snap.visible(show_all=False)
    assert [r.hunch_id for r in visible] == ["h-0001", "h-0003"]


def test_visible_all_includes_everything(tmp_path):
    w = HunchesWriter(hunches_path=tmp_path / "hunches.jsonl")
    _emit(w, "smell A")
    _emit(w, "smell B")
    fb = FeedbackWriter(feedback_path=tmp_path / "feedback.jsonl")
    fb.write_explicit("h-0001", "bad", "2026-04-14T12:05:00Z")

    snap = read_snapshot(tmp_path)
    visible = snap.visible(show_all=True)
    assert [r.hunch_id for r in visible] == ["h-0001", "h-0002"]


def test_visible_returns_independent_list(tmp_path):
    w = HunchesWriter(hunches_path=tmp_path / "hunches.jsonl")
    _emit(w, "smell A")
    snap = read_snapshot(tmp_path)
    out = snap.visible(show_all=True)
    out.clear()
    # snap.records should not have been mutated.
    assert len(snap.records) == 1


# ---------------------------------------------------------------------------
# PanelSnapshot.counts
# ---------------------------------------------------------------------------

def test_counts_distinguish_display_statuses(tmp_path):
    w = HunchesWriter(hunches_path=tmp_path / "hunches.jsonl")
    _emit(w, "smell A")  # h-0001 -> stays pending
    _emit(w, "smell B")  # h-0002 -> delivered (surfaced)
    _emit(w, "smell C")  # h-0003 -> approved (labeled good, still pending)
    _emit(w, "smell D")  # h-0004 -> dismissed (labeled bad)
    w.write_status_change(
        hunch_id="h-0002",
        new_status="surfaced",
        ts="2026-04-14T12:10:00Z",
        by="hook:user_prompt_submit",
    )
    fb = FeedbackWriter(feedback_path=tmp_path / "feedback.jsonl")
    fb.write_explicit("h-0003", "good", "2026-04-14T12:15:00Z")
    fb.write_explicit("h-0004", "bad", "2026-04-14T12:16:00Z")

    snap = read_snapshot(tmp_path)
    counts = snap.counts()
    assert counts == {"pending": 1, "delivered": 1, "approved": 1, "dismissed": 1}


def test_counts_empty_snapshot():
    snap = PanelSnapshot(records=[], labels={})
    assert snap.counts() == {}


# ---------------------------------------------------------------------------
# read_max_tick_seq / liveness cue
# ---------------------------------------------------------------------------

def test_read_max_tick_seq_missing_file(tmp_path):
    assert read_max_tick_seq(tmp_path / "nope.jsonl") == 0


def test_read_max_tick_seq_empty_file(tmp_path):
    p = tmp_path / "conv.jsonl"
    p.write_text("")
    assert read_max_tick_seq(p) == 0


def test_read_max_tick_seq_returns_highest(tmp_path):
    w = ReplayBufferWriter(replay_dir=tmp_path)
    for i in range(3):
        w.append_events(
            [{"type": "text", "timestamp": f"2026-04-14T12:00:0{i}Z",
              "role": "user", "content": f"m{i}"}],
            project_roots=[str(tmp_path)],
        )
    assert read_max_tick_seq(w.conversation_path) == 3


def test_read_max_tick_seq_tolerates_malformed(tmp_path):
    p = tmp_path / "conv.jsonl"
    p.write_text('garbage\n{"tick_seq": 5}\n{"tick_seq": 2}\n')
    assert read_max_tick_seq(p) == 5


def test_snapshot_exposes_max_tick_seq(tmp_path):
    w = ReplayBufferWriter(replay_dir=tmp_path)
    w.append_events(
        [{"type": "text", "timestamp": "2026-04-14T12:00:00Z",
          "role": "user", "content": "hi"}],
        project_roots=[str(tmp_path)],
    )
    snap = read_snapshot(tmp_path)
    assert snap.max_tick_seq == 1


# ---------------------------------------------------------------------------
# module surface
# ---------------------------------------------------------------------------

def test_run_returns_1_when_textual_missing(tmp_path, monkeypatch):
    """If textual import fails, run() should report cleanly and exit 1."""
    import builtins
    import hunch.panel as panel

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("textual"):
            raise ImportError("simulated missing textual")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    rc = panel.run(replay_dir=tmp_path, poll_s=1.0)
    assert rc == 1
