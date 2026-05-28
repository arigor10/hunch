"""Tests for the UserPromptSubmit hook handler."""

from __future__ import annotations

import json
from pathlib import Path

from hunch.critic.protocol import Hunch, TriggeringRefs
from hunch.hook.user_prompt_submit import handle_user_prompt_submit
from hunch.journal.feedback import FeedbackWriter
from hunch.journal.hunches import HunchesWriter, read_current_hunches


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


def _label_good(replay: Path, hunch_id: str) -> None:
    fw = FeedbackWriter(feedback_path=replay / "feedback.jsonl")
    fw.write_explicit(hunch_id=hunch_id, label="good", ts="2026-04-14T12:01:00Z")


def test_empty_replay_dir_returns_empty_continue(tmp_path):
    result = handle_user_prompt_submit(b"{}", tmp_path / "replay")
    assert result.stdout == ""
    assert result.exit_code == 0


def test_no_pending_hunches_returns_empty_continue(tmp_path):
    replay = tmp_path / "replay"
    replay.mkdir()
    (replay / "hunches.jsonl").write_text("")  # file exists, empty
    result = handle_user_prompt_submit(b"{}", replay)
    assert result.stdout == ""


def test_pending_but_unlabeled_hunch_not_injected(tmp_path):
    """A pending hunch without a 'good' label is NOT injected."""
    replay = tmp_path / "replay"
    replay.mkdir()
    writer = HunchesWriter(hunches_path=replay / "hunches.jsonl")
    _emit(writer, "calibration drift")

    result = handle_user_prompt_submit(b"{}", replay)
    assert result.stdout == ""


def test_good_labeled_hunch_is_injected_and_marked_surfaced(tmp_path):
    replay = tmp_path / "replay"
    replay.mkdir()
    hunches_path = replay / "hunches.jsonl"

    writer = HunchesWriter(hunches_path=hunches_path)
    hid = _emit(writer, "calibration drift", "3x discrepancy between runs.")
    _label_good(replay, hid)

    result = handle_user_prompt_submit(b"{}", replay, now_iso="2026-04-14T12:05:00Z")

    assert result.stdout != ""
    payload = json.loads(result.stdout)
    assert payload["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert hid in ctx
    assert "calibration drift" in ctx
    assert "<hunch-injection>" in ctx

    records = read_current_hunches(hunches_path)
    assert len(records) == 1
    assert records[0].status == "surfaced"
    assert records[0].history[0]["by"] == "hook:user_prompt_submit"


def test_bad_labeled_hunch_not_injected(tmp_path):
    replay = tmp_path / "replay"
    replay.mkdir()
    writer = HunchesWriter(hunches_path=replay / "hunches.jsonl")
    hid = _emit(writer, "false alarm")
    fw = FeedbackWriter(feedback_path=replay / "feedback.jsonl")
    fw.write_explicit(hunch_id=hid, label="bad", ts="2026-04-14T12:01:00Z")

    result = handle_user_prompt_submit(b"{}", replay)
    assert result.stdout == ""


def test_surfaced_hunches_do_not_re_inject(tmp_path):
    """A surfaced hunch is not re-delivered as <hunch-injection>, but may
    be reminded as <hunch-reminder> if unacknowledged."""
    replay = tmp_path / "replay"
    replay.mkdir()
    hunches_path = replay / "hunches.jsonl"
    writer = HunchesWriter(hunches_path=hunches_path)
    hid = _emit(writer, "smell A")
    _label_good(replay, hid)

    handle_user_prompt_submit(b"{}", replay, now_iso="2026-04-14T12:05:00Z")
    result2 = handle_user_prompt_submit(b"{}", replay, now_iso="2026-04-14T12:06:00Z")

    if result2.stdout:
        ctx = json.loads(result2.stdout)["hookSpecificOutput"]["additionalContext"]
        assert "<hunch-injection>" not in ctx
        assert "<hunch-reminder>" in ctx
    # Either empty (no reminder yet) or a reminder — never a re-injection


def test_new_good_hunch_after_surfacing_still_fires(tmp_path):
    """A new approved hunch is injected even when an older one is surfaced.
    The older one may appear in a <hunch-reminder>, but not in <hunch-injection>."""
    replay = tmp_path / "replay"
    replay.mkdir()
    hunches_path = replay / "hunches.jsonl"
    writer = HunchesWriter(hunches_path=hunches_path)

    hid_a = _emit(writer, "smell A")
    _label_good(replay, hid_a)
    handle_user_prompt_submit(b"{}", replay, now_iso="2026-04-14T12:05:00Z")

    hid_b = _emit(writer, "smell B")
    _label_good(replay, hid_b)
    result = handle_user_prompt_submit(b"{}", replay, now_iso="2026-04-14T12:06:00Z")

    assert result.stdout != ""
    payload = json.loads(result.stdout)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert "<hunch-injection>" in ctx
    assert "smell B" in ctx
    # smell A may appear in a reminder block, but not in the injection
    injection_part = ctx.split("<hunch-injection>")[1].split("</hunch-injection>")[0]
    assert "smell A" not in injection_part


def test_malformed_hunches_file_does_not_crash(tmp_path):
    replay = tmp_path / "replay"
    replay.mkdir()
    (replay / "hunches.jsonl").write_text("not valid json\n{broken\n")
    result = handle_user_prompt_submit(b"{}", replay)
    assert result.stdout == ""
    assert result.exit_code == 0


def test_exception_in_handler_returns_empty_continue(tmp_path, monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(
        "hunch.hook.user_prompt_submit.read_current_hunches", _boom
    )

    replay = tmp_path / "replay"
    replay.mkdir()
    (replay / "hunches.jsonl").write_text("")

    result = handle_user_prompt_submit(b"{}", replay)
    assert result.stdout == ""
    assert result.exit_code == 0


def test_injection_framing_is_information_not_command(tmp_path):
    replay = tmp_path / "replay"
    replay.mkdir()
    writer = HunchesWriter(hunches_path=replay / "hunches.jsonl")
    hid = _emit(writer, "some smell")
    _label_good(replay, hid)

    result = handle_user_prompt_submit(b"{}", replay, now_iso="2026-04-14T12:00:00Z")
    ctx = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]

    assert "not instructions for you" in ctx
    assert "Scientist" in ctx


# ---------------------------------------------------------------------------
# Acknowledgment lifecycle tests
# ---------------------------------------------------------------------------


def _surface_hunch(replay: Path, hid: str) -> None:
    """Simulate a hunch being delivered (pending → surfaced)."""
    writer = HunchesWriter(hunches_path=replay / "hunches.jsonl")
    writer.write_status_change(
        hunch_id=hid, new_status="surfaced",
        ts="2026-04-14T12:02:00Z", by="hook:stop_delivery",
    )


def _write_conversation_events(replay: Path, n: int) -> None:
    """Write n synthetic conversation events to produce tick_seq values."""
    from hunch.journal.append import append_json_line
    conv_path = replay / "conversation.jsonl"
    for i in range(1, n + 1):
        append_json_line(conv_path, {"tick_seq": i, "type": "assistant_text", "text": f"turn {i}"})


class TestHunchReminder:
    def test_surfaced_unacknowledged_gets_reminder(self, tmp_path):
        """First UPS after surfacing sends a <hunch-reminder>."""
        replay = tmp_path / "replay"
        replay.mkdir()
        writer = HunchesWriter(hunches_path=replay / "hunches.jsonl")
        hid = _emit(writer, "gradient spike")
        _label_good(replay, hid)
        _surface_hunch(replay, hid)

        result = handle_user_prompt_submit(b"{}", replay, now_iso="2026-04-14T12:10:00Z")
        assert result.stdout != ""
        ctx = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        assert "<hunch-reminder>" in ctx
        assert "gradient spike" in ctx
        assert hid in ctx
        assert 'Re h-XXXX:' in ctx

    def test_acknowledged_hunch_not_reminded(self, tmp_path):
        """A hunch with a response event is not included in reminders."""
        replay = tmp_path / "replay"
        replay.mkdir()
        writer = HunchesWriter(hunches_path=replay / "hunches.jsonl")
        hid = _emit(writer, "stale data")
        _label_good(replay, hid)
        _surface_hunch(replay, hid)

        fw = FeedbackWriter(feedback_path=replay / "feedback.jsonl")
        fw.write_response(hid, "Corrected the numbers.", "2026-04-14T12:05:00Z")

        result = handle_user_prompt_submit(b"{}", replay, now_iso="2026-04-14T12:10:00Z")
        assert result.stdout == ""

    def test_reminder_writes_reminder_event(self, tmp_path):
        """Reminder writes a channel='reminder' event to feedback.jsonl."""
        replay = tmp_path / "replay"
        replay.mkdir()
        writer = HunchesWriter(hunches_path=replay / "hunches.jsonl")
        hid = _emit(writer, "seed issue")
        _label_good(replay, hid)
        _surface_hunch(replay, hid)
        _write_conversation_events(replay, 5)

        handle_user_prompt_submit(b"{}", replay, now_iso="2026-04-14T12:10:00Z")

        from hunch.journal.feedback import read_hunch_reminders
        reminders = read_hunch_reminders(replay / "feedback.jsonl")
        assert hid in reminders
        assert reminders[hid] == 5  # max tick_seq

    def test_reminder_not_repeated_within_interval(self, tmp_path):
        """After a reminder, next UPS within N turns doesn't re-remind."""
        replay = tmp_path / "replay"
        replay.mkdir()
        writer = HunchesWriter(hunches_path=replay / "hunches.jsonl")
        hid = _emit(writer, "memory issue")
        _label_good(replay, hid)
        _surface_hunch(replay, hid)
        _write_conversation_events(replay, 5)

        # First UPS: reminder issued
        handle_user_prompt_submit(b"{}", replay, now_iso="2026-04-14T12:10:00Z")

        # Second UPS with only a few more turns (< REMINDER_INTERVAL_TURNS)
        _write_conversation_events(replay, 3)  # now at tick_seq 8
        result = handle_user_prompt_submit(b"{}", replay, now_iso="2026-04-14T12:15:00Z")
        assert result.stdout == ""

    def test_reminder_repeats_after_n_turns(self, tmp_path):
        """After REMINDER_INTERVAL_TURNS, the reminder fires again."""
        from hunch.hook.user_prompt_submit import REMINDER_INTERVAL_TURNS

        replay = tmp_path / "replay"
        replay.mkdir()
        writer = HunchesWriter(hunches_path=replay / "hunches.jsonl")
        hid = _emit(writer, "drift concern")
        _label_good(replay, hid)
        _surface_hunch(replay, hid)
        _write_conversation_events(replay, 5)

        # First reminder
        handle_user_prompt_submit(b"{}", replay, now_iso="2026-04-14T12:10:00Z")

        # Enough turns pass
        _write_conversation_events(replay, REMINDER_INTERVAL_TURNS + 5)
        result = handle_user_prompt_submit(b"{}", replay, now_iso="2026-04-14T13:00:00Z")
        assert result.stdout != ""
        ctx = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        assert "<hunch-reminder>" in ctx
        assert "drift concern" in ctx

    def test_full_lifecycle_approve_deliver_remind_ack(self, tmp_path):
        """Integration: emit → approve → deliver → remind → ack → no remind."""
        replay = tmp_path / "replay"
        replay.mkdir()
        hunches_path = replay / "hunches.jsonl"
        feedback_path = replay / "feedback.jsonl"
        writer = HunchesWriter(hunches_path=hunches_path)

        # 1. Emit + approve
        hid = _emit(writer, "seed contamination", "Eval seed matches training seed.")
        _label_good(replay, hid)

        # 2. Deliver via UPS (pending → surfaced)
        r1 = handle_user_prompt_submit(b"{}", replay, now_iso="2026-04-14T12:05:00Z")
        assert "<hunch-injection>" in json.loads(r1.stdout)["hookSpecificOutput"]["additionalContext"]
        records = read_current_hunches(hunches_path)
        assert records[0].status == "surfaced"

        # 3. Next UPS: reminder fires (surfaced, unacknowledged)
        _write_conversation_events(replay, 3)
        r2 = handle_user_prompt_submit(b"{}", replay, now_iso="2026-04-14T12:10:00Z")
        ctx2 = json.loads(r2.stdout)["hookSpecificOutput"]["additionalContext"]
        assert "<hunch-reminder>" in ctx2
        assert "<hunch-injection>" not in ctx2
        assert hid in ctx2

        # 4. Researcher acknowledges (simulating parser + writer)
        fw = FeedbackWriter(feedback_path=feedback_path)
        fw.write_response(hid, "Re-ran with different eval seeds. Fixed.", "2026-04-14T12:15:00Z")

        # 5. Next UPS: no reminder (acknowledged)
        _write_conversation_events(replay, 5)
        r3 = handle_user_prompt_submit(b"{}", replay, now_iso="2026-04-14T12:20:00Z")
        assert r3.stdout == ""

        # 6. display_status shows "acknowledged"
        from hunch.panel import display_status
        from hunch.journal.feedback import read_hunch_responses
        records = read_current_hunches(hunches_path)
        responses = read_hunch_responses(feedback_path)
        ds = display_status(records[0], "good", acknowledged=hid in responses)
        assert ds == "acknowledged"

    def test_injection_and_reminder_coexist(self, tmp_path):
        """New injection + old reminder appear together in additionalContext."""
        replay = tmp_path / "replay"
        replay.mkdir()
        writer = HunchesWriter(hunches_path=replay / "hunches.jsonl")

        hid_old = _emit(writer, "old smell")
        _label_good(replay, hid_old)
        _surface_hunch(replay, hid_old)

        hid_new = _emit(writer, "new smell")
        _label_good(replay, hid_new)

        result = handle_user_prompt_submit(b"{}", replay, now_iso="2026-04-14T12:10:00Z")
        assert result.stdout != ""
        ctx = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        assert "<hunch-injection>" in ctx
        assert "<hunch-reminder>" in ctx
        assert "new smell" in ctx
        assert "old smell" in ctx
