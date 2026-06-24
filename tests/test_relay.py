"""Tests for hunch/relay.py — the parked-Claude tmux relay (mocked tmux)."""

from __future__ import annotations

from hunch import relay
from hunch.critic.protocol import Hunch, TriggeringRefs
from hunch.journal.append import append_json_line
from hunch.journal.feedback import FeedbackWriter
from hunch.journal.hunches import HunchesWriter, read_current_hunches


def _emit(writer: HunchesWriter, smell: str, description: str = "") -> str:
    hid = writer.allocate_id()
    writer.write_emit(
        hunch=Hunch(smell=smell, description=description, triggering_refs=TriggeringRefs()),
        hunch_id=hid, ts="2026-04-14T12:00:00Z",
        emitted_by_tick=1, bookmark_prev=0, bookmark_now=1,
    )
    return hid


def _setup(tmp_path, *, parked: bool, approve: bool = True):
    replay = tmp_path / "replay"
    replay.mkdir()
    conv = replay / "conversation.jsonl"
    append_json_line(conv, {"tick_seq": 1, "type": "user_text", "timestamp": "t"})
    # The last event decides parked-ness.
    append_json_line(conv, {
        "tick_seq": 2,
        "type": "claude_stopped" if parked else "assistant_text",
        "timestamp": "t",
    })
    hw = HunchesWriter(hunches_path=replay / "hunches.jsonl")
    hid = _emit(hw, "ordering inconsistency", "rows out of order")
    if approve:
        FeedbackWriter(feedback_path=replay / "feedback.jsonl").write_explicit(
            hid, "good", "2026-04-14T12:02:00Z"
        )
    return replay, hid


def _patch_tmux(monkeypatch, *, in_tmux=True, research="%1", cur="%2", send=None):
    monkeypatch.setattr(relay, "in_tmux", lambda: in_tmux)
    monkeypatch.setattr(
        relay, "window_roles", lambda: ({"research": research} if research else {})
    )
    monkeypatch.setattr(relay, "current_pane_id", lambda: cur)
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        relay, "send_text_to_pane", send or (lambda pane, text: sent.append((pane, text)))
    )
    return sent


def _status(replay, hid):
    recs = {r.hunch_id: r for r in read_current_hunches(replay / "hunches.jsonl")}
    return recs[hid]


def test_relay_delivers_when_parked(tmp_path, monkeypatch):
    replay, hid = _setup(tmp_path, parked=True)
    sent = _patch_tmux(monkeypatch)

    assert relay.relay_pending(replay) == relay.RELAYED
    # Typed into the research pane, carrying the hunch id as its marker.
    assert sent and sent[0][0] == "%1"
    assert hid in sent[0][1]
    # Marked surfaced by panel:relay.
    rec = _status(replay, hid)
    assert rec.status == "surfaced"
    assert rec.history[-1]["by"] == "panel:relay"


def test_relay_noop_when_not_parked(tmp_path, monkeypatch):
    replay, hid = _setup(tmp_path, parked=False)
    sent = _patch_tmux(monkeypatch)
    assert relay.relay_pending(replay) == relay.NOT_PARKED
    assert sent == []
    assert _status(replay, hid).status == "pending"  # untouched


def test_relay_not_in_tmux(tmp_path, monkeypatch):
    replay, _ = _setup(tmp_path, parked=True)
    _patch_tmux(monkeypatch, in_tmux=False)
    assert relay.relay_pending(replay) == relay.NOT_IN_TMUX


def test_relay_no_research_pane(tmp_path, monkeypatch):
    replay, _ = _setup(tmp_path, parked=True)
    _patch_tmux(monkeypatch, research=None)
    assert relay.relay_pending(replay) == relay.NO_RESEARCH_PANE


def test_relay_skips_when_research_is_current_pane(tmp_path, monkeypatch):
    replay, _ = _setup(tmp_path, parked=True)
    _patch_tmux(monkeypatch, research="%1", cur="%1")
    assert relay.relay_pending(replay) == relay.NO_RESEARCH_PANE


def test_relay_nothing_to_deliver_when_unapproved(tmp_path, monkeypatch):
    replay, _ = _setup(tmp_path, parked=True, approve=False)
    _patch_tmux(monkeypatch)
    assert relay.relay_pending(replay) == relay.NOTHING_TO_DELIVER


def test_relay_rolls_back_on_send_failure(tmp_path, monkeypatch):
    replay, hid = _setup(tmp_path, parked=True)

    def boom(pane, text):
        raise relay.RelayError("tmux died")

    _patch_tmux(monkeypatch, send=boom)

    assert relay.relay_pending(replay) == relay.FAILED
    # Rolled back to pending — a failed relay is never recorded as delivered.
    rec = _status(replay, hid)
    assert rec.status == "pending"
    assert rec.history[-1]["by"] == "panel:relay-failed"


# ---------------------------------------------------------------------------
# Parked detection — robust to the live-ingest event stream
# ---------------------------------------------------------------------------

def test_parked_is_robust_to_trailing_output_after_stop(tmp_path):
    # The live `hunch run` ingest appends assistant_text / artifact_edit AFTER
    # the Stop hook's claude_stopped (trailing output from the finished turn).
    # Claude is still parked — claude_stopped is the most recent turn boundary.
    replay = tmp_path / "replay"
    replay.mkdir()
    conv = replay / "conversation.jsonl"
    for e in [
        {"type": "user_text", "tick_seq": 1},
        {"type": "assistant_text", "tick_seq": 2},
        {"type": "claude_stopped", "tick_seq": 3},
        {"type": "artifact_edit", "tick_seq": 3},   # trailing, same finished turn
        {"type": "assistant_text", "tick_seq": 4},  # trailing
    ]:
        append_json_line(conv, e)
    assert relay._claude_parked(replay) is True


def test_not_parked_when_a_new_user_turn_follows_the_stop(tmp_path):
    replay = tmp_path / "replay"
    replay.mkdir()
    conv = replay / "conversation.jsonl"
    for e in [
        {"type": "claude_stopped", "tick_seq": 1},
        {"type": "user_text", "tick_seq": 2},       # a new turn opened
        {"type": "assistant_text", "tick_seq": 3},  # Claude is now working
    ]:
        append_json_line(conv, e)
    assert relay._claude_parked(replay) is False


def test_not_parked_while_processing_a_just_injected_hunch(tmp_path):
    # Claude is still working through a hunch we just relayed/delivered — the
    # hunch_injection opens the turn. A second approval must NOT relay into it
    # (the bug found live: approving while Claude processed the prior hunch).
    replay = tmp_path / "replay"
    replay.mkdir()
    conv = replay / "conversation.jsonl"
    for e in [
        {"type": "claude_stopped", "tick_seq": 1},
        {"type": "hunch_injection", "tick_seq": 2},   # relayed hunch → Claude working
        {"type": "assistant_text", "tick_seq": 3},
    ]:
        append_json_line(conv, e)
    assert relay._claude_parked(replay) is False


def test_not_parked_with_no_stop_event(tmp_path):
    replay = tmp_path / "replay"
    replay.mkdir()
    conv = replay / "conversation.jsonl"
    append_json_line(conv, {"type": "user_text", "tick_seq": 1})
    append_json_line(conv, {"type": "assistant_text", "tick_seq": 2})
    assert relay._claude_parked(replay) is False
