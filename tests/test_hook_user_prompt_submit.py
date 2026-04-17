"""Tests for the UserPromptSubmit hook handler."""

from __future__ import annotations

import json
from pathlib import Path

from hunch.critic.protocol import Hunch, TriggeringRefs
from hunch.hook.user_prompt_submit import handle_user_prompt_submit
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


def test_pending_hunch_is_injected_and_marked_surfaced(tmp_path):
    replay = tmp_path / "replay"
    replay.mkdir()
    hunches_path = replay / "hunches.jsonl"

    writer = HunchesWriter(hunches_path=hunches_path)
    hid = _emit(writer, "calibration drift", "3× discrepancy between runs.")

    result = handle_user_prompt_submit(b"{}", replay, now_iso="2026-04-14T12:05:00Z")

    assert result.stdout != ""
    payload = json.loads(result.stdout)
    assert payload["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert hid in ctx
    assert "calibration drift" in ctx
    assert "3×" in ctx
    assert "<hunch-injection>" in ctx

    # Status-change event appended; hunch now surfaced.
    records = read_current_hunches(hunches_path)
    assert len(records) == 1
    assert records[0].status == "surfaced"
    assert records[0].history[0]["by"] == "hook:user_prompt_submit"


def test_surfaced_hunches_do_not_re_surface(tmp_path):
    replay = tmp_path / "replay"
    replay.mkdir()
    hunches_path = replay / "hunches.jsonl"
    writer = HunchesWriter(hunches_path=hunches_path)
    _emit(writer, "smell A")

    handle_user_prompt_submit(b"{}", replay, now_iso="2026-04-14T12:05:00Z")
    result2 = handle_user_prompt_submit(b"{}", replay, now_iso="2026-04-14T12:06:00Z")

    # Second call should see no pending hunches and inject nothing.
    assert result2.stdout == ""


def test_new_hunch_after_surfacing_still_fires(tmp_path):
    replay = tmp_path / "replay"
    replay.mkdir()
    hunches_path = replay / "hunches.jsonl"
    writer = HunchesWriter(hunches_path=hunches_path)

    _emit(writer, "smell A")
    handle_user_prompt_submit(b"{}", replay, now_iso="2026-04-14T12:05:00Z")

    _emit(writer, "smell B")
    result = handle_user_prompt_submit(b"{}", replay, now_iso="2026-04-14T12:06:00Z")

    assert result.stdout != ""
    payload = json.loads(result.stdout)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert "smell B" in ctx
    assert "smell A" not in ctx  # already surfaced


def test_malformed_hunches_file_does_not_crash(tmp_path):
    replay = tmp_path / "replay"
    replay.mkdir()
    (replay / "hunches.jsonl").write_text("not valid json\n{broken\n")
    result = handle_user_prompt_submit(b"{}", replay)
    # read_current_hunches skips bad lines, so we just get empty_continue.
    assert result.stdout == ""
    assert result.exit_code == 0


def test_exception_in_handler_returns_empty_continue(tmp_path, monkeypatch):
    # Simulate something breaking deep in the handler. The contract is
    # that Claude Code's prompt continues unaffected — no crash, no error.
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
    # Guards the Critic-voice choice in critic_v0.md: the injection
    # must read as observation, not as instruction.
    replay = tmp_path / "replay"
    replay.mkdir()
    writer = HunchesWriter(hunches_path=replay / "hunches.jsonl")
    _emit(writer, "some smell")

    result = handle_user_prompt_submit(b"{}", replay, now_iso="2026-04-14T12:00:00Z")
    ctx = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]

    assert "not instructions for you" in ctx
    assert "Scientist" in ctx
