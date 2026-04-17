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
from hunch.panel import PanelSnapshot, read_max_tick_seq, read_snapshot


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

def test_visible_hides_labeled_by_default(tmp_path):
    w = HunchesWriter(hunches_path=tmp_path / "hunches.jsonl")
    _emit(w, "smell A")
    _emit(w, "smell B")
    fb = FeedbackWriter(feedback_path=tmp_path / "feedback.jsonl")
    fb.write_explicit("h-0001", "bad", "2026-04-14T12:05:00Z")

    snap = read_snapshot(tmp_path)
    visible = snap.visible(show_labeled=False)
    assert [r.hunch_id for r in visible] == ["h-0002"]


def test_visible_all_includes_labeled(tmp_path):
    w = HunchesWriter(hunches_path=tmp_path / "hunches.jsonl")
    _emit(w, "smell A")
    _emit(w, "smell B")
    fb = FeedbackWriter(feedback_path=tmp_path / "feedback.jsonl")
    fb.write_explicit("h-0001", "bad", "2026-04-14T12:05:00Z")

    snap = read_snapshot(tmp_path)
    visible = snap.visible(show_labeled=True)
    assert [r.hunch_id for r in visible] == ["h-0001", "h-0002"]


def test_visible_returns_independent_list(tmp_path):
    w = HunchesWriter(hunches_path=tmp_path / "hunches.jsonl")
    _emit(w, "smell A")
    snap = read_snapshot(tmp_path)
    out = snap.visible(show_labeled=True)
    out.clear()
    # snap.records should not have been mutated.
    assert len(snap.records) == 1


# ---------------------------------------------------------------------------
# PanelSnapshot.counts
# ---------------------------------------------------------------------------

def test_counts_distinguish_pending_surfaced_labeled(tmp_path):
    w = HunchesWriter(hunches_path=tmp_path / "hunches.jsonl")
    _emit(w, "smell A")  # h-0001 -> stays pending
    _emit(w, "smell B")  # h-0002 -> will be surfaced
    _emit(w, "smell C")  # h-0003 -> will be labeled (still pending status-wise)
    w.write_status_change(
        hunch_id="h-0002",
        new_status="surfaced",
        ts="2026-04-14T12:10:00Z",
        by="hook:user_prompt_submit",
    )
    fb = FeedbackWriter(feedback_path=tmp_path / "feedback.jsonl")
    fb.write_explicit("h-0003", "good", "2026-04-14T12:15:00Z")

    snap = read_snapshot(tmp_path)
    counts = snap.counts()
    assert counts == {"pending": 2, "surfaced": 1, "labeled": 1}


def test_counts_empty_snapshot():
    snap = PanelSnapshot(records=[], labels={})
    assert snap.counts() == {"pending": 0, "surfaced": 0, "labeled": 0}


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
