"""Tests for the async stop-delivery hook handler."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from hunch.critic.protocol import Hunch, TriggeringRefs
from hunch.hook.stop_delivery import handle_stop_delivery, _find_deliverable
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
        ts="2026-05-26T12:00:00Z",
        emitted_by_tick=1,
        bookmark_prev=0,
        bookmark_now=1,
    )
    return hid


def _label_good(replay: Path, hunch_id: str) -> None:
    fw = FeedbackWriter(feedback_path=replay / "feedback.jsonl")
    fw.write_explicit(hunch_id=hunch_id, label="good", ts="2026-05-26T12:01:00Z")


def _setup_replay(tmp_path: Path) -> Path:
    replay = tmp_path / "replay"
    replay.mkdir()
    return replay


class TestFindDeliverable:
    def test_no_replay_dir(self, tmp_path):
        assert _find_deliverable(tmp_path / "nonexistent") == []

    def test_no_hunches_file(self, tmp_path):
        replay = _setup_replay(tmp_path)
        assert _find_deliverable(replay) == []

    def test_pending_unlabeled_not_deliverable(self, tmp_path):
        replay = _setup_replay(tmp_path)
        writer = HunchesWriter(hunches_path=replay / "hunches.jsonl")
        _emit(writer, "some smell")
        assert _find_deliverable(replay) == []

    def test_pending_good_is_deliverable(self, tmp_path):
        replay = _setup_replay(tmp_path)
        writer = HunchesWriter(hunches_path=replay / "hunches.jsonl")
        hid = _emit(writer, "drift detected")
        _label_good(replay, hid)
        result = _find_deliverable(replay)
        assert len(result) == 1
        assert result[0].hunch_id == hid

    def test_bad_labeled_not_deliverable(self, tmp_path):
        replay = _setup_replay(tmp_path)
        writer = HunchesWriter(hunches_path=replay / "hunches.jsonl")
        hid = _emit(writer, "false alarm")
        fw = FeedbackWriter(feedback_path=replay / "feedback.jsonl")
        fw.write_explicit(hunch_id=hid, label="bad", ts="2026-05-26T12:01:00Z")
        assert _find_deliverable(replay) == []

    def test_already_surfaced_not_deliverable(self, tmp_path):
        replay = _setup_replay(tmp_path)
        writer = HunchesWriter(hunches_path=replay / "hunches.jsonl")
        hid = _emit(writer, "old smell")
        _label_good(replay, hid)
        writer.write_status_change(
            hunch_id=hid, new_status="surfaced",
            ts="2026-05-26T12:02:00Z", by="hook:stop_delivery",
        )
        assert _find_deliverable(replay) == []

    def test_multiple_good_hunches_all_deliverable(self, tmp_path):
        replay = _setup_replay(tmp_path)
        writer = HunchesWriter(hunches_path=replay / "hunches.jsonl")
        hid_a = _emit(writer, "smell A")
        hid_b = _emit(writer, "smell B")
        _label_good(replay, hid_a)
        _label_good(replay, hid_b)
        result = _find_deliverable(replay)
        assert len(result) == 2


class TestHandleStopDelivery:
    def test_immediate_delivery_exits_2(self, tmp_path, capsys):
        """Pre-seeded approved hunch is found on first poll, exits code 2."""
        replay = _setup_replay(tmp_path)
        writer = HunchesWriter(hunches_path=replay / "hunches.jsonl")
        hid = _emit(writer, "calibration drift", "3x discrepancy.")
        _label_good(replay, hid)

        rc = handle_stop_delivery(replay, poll_interval=0.01)
        assert rc == 2

        captured = capsys.readouterr()
        assert "<hunch-injection>" in captured.err
        assert "calibration drift" in captured.err
        assert hid in captured.err

    def test_marks_surfaced_on_delivery(self, tmp_path, capsys):
        replay = _setup_replay(tmp_path)
        writer = HunchesWriter(hunches_path=replay / "hunches.jsonl")
        hid = _emit(writer, "smell X")
        _label_good(replay, hid)

        handle_stop_delivery(replay, poll_interval=0.01)

        records = read_current_hunches(replay / "hunches.jsonl")
        assert len(records) == 1
        assert records[0].status == "surfaced"
        assert records[0].history[0]["by"] == "hook:stop_delivery"

    def test_batches_multiple_hunches(self, tmp_path, capsys):
        replay = _setup_replay(tmp_path)
        writer = HunchesWriter(hunches_path=replay / "hunches.jsonl")
        hid_a = _emit(writer, "smell A", "desc A")
        hid_b = _emit(writer, "smell B", "desc B")
        _label_good(replay, hid_a)
        _label_good(replay, hid_b)

        rc = handle_stop_delivery(replay, poll_interval=0.01)
        assert rc == 2

        captured = capsys.readouterr()
        assert "smell A" in captured.err
        assert "smell B" in captured.err

        records = read_current_hunches(replay / "hunches.jsonl")
        assert all(r.status == "surfaced" for r in records)

    def test_already_surfaced_not_redelivered(self, tmp_path, capsys):
        """Hunch surfaced by a previous delivery is not picked up again."""
        replay = _setup_replay(tmp_path)
        writer = HunchesWriter(hunches_path=replay / "hunches.jsonl")
        hid = _emit(writer, "old smell")
        _label_good(replay, hid)

        handle_stop_delivery(replay, poll_interval=0.01)
        capsys.readouterr()  # discard first delivery output

        # Second hunch emitted and approved
        hid_b = _emit(writer, "new smell")
        _label_good(replay, hid_b)

        rc = handle_stop_delivery(replay, poll_interval=0.01)
        assert rc == 2

        captured = capsys.readouterr()
        assert "new smell" in captured.err
        assert "old smell" not in captured.err

    def test_polls_until_hunch_appears(self, tmp_path, capsys):
        """Hook waits and polls, then finds an approved hunch."""
        replay = _setup_replay(tmp_path)
        writer = HunchesWriter(hunches_path=replay / "hunches.jsonl")

        def _approve_after_delay():
            time.sleep(0.15)
            hid = _emit(writer, "delayed smell")
            _label_good(replay, hid)

        t = threading.Thread(target=_approve_after_delay)
        t.start()

        rc = handle_stop_delivery(replay, poll_interval=0.05)
        t.join()
        assert rc == 2

        captured = capsys.readouterr()
        assert "delayed smell" in captured.err

    def test_transient_error_does_not_kill_watcher(self, tmp_path):
        """A transient read error is retried, not fatal."""
        replay = _setup_replay(tmp_path)
        writer = HunchesWriter(hunches_path=replay / "hunches.jsonl")

        call_count = {"n": 0}
        original_find = _find_deliverable

        def _flaky(replay_dir):
            call_count["n"] += 1
            if call_count["n"] <= 2:
                raise RuntimeError("simulated torn read")
            hid = _emit(writer, "recovered smell")
            _label_good(replay, hid)
            return original_find(replay_dir)

        import hunch.hook.stop_delivery as mod
        old = mod._find_deliverable
        mod._find_deliverable = _flaky
        try:
            rc = handle_stop_delivery(replay, poll_interval=0.01)
        finally:
            mod._find_deliverable = old

        assert rc == 2
        assert call_count["n"] >= 3

    def test_injection_framing_matches_ups_hook(self, tmp_path, capsys):
        """The injection text uses the same framing as UserPromptSubmit."""
        replay = _setup_replay(tmp_path)
        writer = HunchesWriter(hunches_path=replay / "hunches.jsonl")
        hid = _emit(writer, "test smell")
        _label_good(replay, hid)

        handle_stop_delivery(replay, poll_interval=0.01)
        captured = capsys.readouterr()

        assert "not instructions for you" in captured.err
        assert "Scientist" in captured.err
        assert "</hunch-injection>" in captured.err


class TestFileLock:
    def test_second_watcher_exits_immediately(self, tmp_path):
        """Only one watcher polls at a time; latecomers exit 0."""
        import fcntl
        from hunch.hook.stop_delivery import _LOCK_FILENAME

        replay = _setup_replay(tmp_path)
        lock_path = replay / _LOCK_FILENAME
        lock_fd = open(lock_path, "w")
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        try:
            rc = handle_stop_delivery(replay, poll_interval=0.01, max_wait=1.0)
            assert rc == 0
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()

    def test_concurrent_subprocess_watchers_no_duplicate(self, tmp_path):
        """Three concurrent watchers: only one delivers, others exit or idle."""
        replay = _setup_replay(tmp_path)
        writer = HunchesWriter(hunches_path=replay / "hunches.jsonl")
        hid = _emit(writer, "concurrent smell")
        _label_good(replay, hid)

        procs = [
            subprocess.Popen(
                ["hunch", "hook", "stop-delivery", "--replay-dir", str(replay)],
                stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            )
            for _ in range(3)
        ]

        # Wait a bit for the winner to deliver, then kill stragglers
        # (losers that acquired the lock after the winner released it
        # will poll finding nothing — that's correct but slow)
        time.sleep(3)
        for p in procs:
            try:
                p.wait(timeout=1)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()

        exit_codes = [p.returncode for p in procs]
        assert exit_codes.count(2) == 1, (
            f"Expected exactly 1 delivery (exit 2), got: {exit_codes}"
        )


class TestSubprocessIntegration:
    """Run the hook as a real subprocess — tests CLI wiring + polling."""

    def test_hook_process_delivers_preseeded_hunch(self, tmp_path):
        replay = _setup_replay(tmp_path)
        writer = HunchesWriter(hunches_path=replay / "hunches.jsonl")
        hid = _emit(writer, "subprocess smell", "found via subprocess")
        _label_good(replay, hid)

        result = subprocess.run(
            ["hunch", "hook", "stop-delivery", "--replay-dir", str(replay)],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 2
        assert "<hunch-injection>" in result.stderr
        assert "subprocess smell" in result.stderr

        records = read_current_hunches(replay / "hunches.jsonl")
        assert records[0].status == "surfaced"

    def test_hook_process_delivers_after_delay(self, tmp_path):
        """Hook polls, then a hunch appears after a short delay."""
        replay = _setup_replay(tmp_path)
        writer = HunchesWriter(hunches_path=replay / "hunches.jsonl")

        proc = subprocess.Popen(
            ["hunch", "hook", "stop-delivery", "--replay-dir", str(replay)],
            stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        )

        time.sleep(0.3)
        hid = _emit(writer, "delayed subprocess smell")
        _label_good(replay, hid)

        proc.wait(timeout=15)
        assert proc.returncode == 2
        assert "delayed subprocess smell" in proc.stderr.read()


class TestInitRegistration:
    def test_init_registers_async_stop_hook(self, tmp_path):
        from hunch.init import init_project

        init_project(tmp_path)
        settings = json.loads(
            (tmp_path / ".claude" / "settings.local.json").read_text()
        )
        stop_hooks = settings["hooks"]["Stop"]

        commands = []
        for group in stop_hooks:
            for hook in group["hooks"]:
                commands.append(
                    (hook["command"], hook.get("asyncRewake", False))
                )

        assert ("hunch hook stop", False) in commands
        assert ("hunch hook stop-delivery", True) in commands

    def test_init_idempotent_does_not_duplicate(self, tmp_path):
        from hunch.init import init_project

        init_project(tmp_path)
        init_project(tmp_path)
        settings = json.loads(
            (tmp_path / ".claude" / "settings.local.json").read_text()
        )
        stop_hooks = settings["hooks"]["Stop"]
        all_commands = []
        for group in stop_hooks:
            for hook in group["hooks"]:
                all_commands.append(hook["command"])

        assert all_commands.count("hunch hook stop-delivery") == 1


# ---------------------------------------------------------------------------
# E2E test — runs a real Claude Haiku call to verify hooks fire.
#
# Skipped by default. Run with:
#   pytest tests/test_hook_stop_delivery.py -k e2e --run-e2e -v
#
# Tests the UserPromptSubmit delivery path end-to-end (hooks fire,
# hunch is surfaced, Claude sees the injection context).
#
# The asyncRewake delivery path requires a long-lived interactive
# Claude session which can't be driven programmatically (Claude Code's
# TUI doesn't work with pexpect). To test asyncRewake manually:
#
#   1. mkdir /tmp/hunch-e2e-manual && cd /tmp/hunch-e2e-manual
#   2. hunch init
#   3. python3 -c "
#      from hunch.journal.hunches import HunchesWriter
#      from hunch.critic.protocol import Hunch, TriggeringRefs
#      w = HunchesWriter('.hunch/replay/hunches.jsonl')
#      hid = w.allocate_id()
#      w.write_emit(Hunch('test smell','test desc',TriggeringRefs()),
#                   hid,'2026-01-01T00:00:00Z',1,bookmark_prev=0,bookmark_now=1)
#      print(f'Created {hid}')
#      "
#   4. Start Claude:  claude --model claude-haiku-4-5-20251001
#   5. Send any message (e.g., "say hello")
#   6. In another terminal:  hunch label <hid> good
#   7. Within ~5 seconds, Claude should respond with the hunch injection.
# ---------------------------------------------------------------------------

def _claude_available() -> bool:
    try:
        r = subprocess.run(["claude", "--version"], capture_output=True, timeout=5)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


@pytest.mark.e2e
@pytest.mark.skipif(not _claude_available(), reason="claude CLI not installed")
class TestHookDeliveryE2E:
    """E2E: run real claude -p, verify hooks fire and hunches are surfaced.

    Uses Haiku for minimal cost (~$0.001 per run).
    """

    def test_ups_delivery_via_claude_p(self, tmp_path):
        """Pre-seed an approved hunch, run claude -p, verify UPS delivery."""
        project_dir = tmp_path / "e2e_project"
        project_dir.mkdir()
        replay = project_dir / ".hunch" / "replay"
        replay.mkdir(parents=True)

        subprocess.run(["git", "init"], cwd=str(project_dir),
                        capture_output=True, check=True)

        from hunch.init import init_project
        init_project(project_dir)

        writer = HunchesWriter(hunches_path=replay / "hunches.jsonl")
        hid = _emit(writer, "E2E test smell", "Automated test hunch.")
        _label_good(replay, hid)

        settings_path = project_dir / ".claude" / "settings.local.json"
        settings = json.loads(settings_path.read_text())
        for group in settings["hooks"]["Stop"]:
            for hook in group["hooks"]:
                if "stop-delivery" in hook.get("command", ""):
                    hook["command"] = "timeout 10 hunch hook stop-delivery"
        settings_path.write_text(json.dumps(settings, indent=2))

        result = subprocess.run(
            [
                "claude", "-p", "Reply with exactly: E2E_OK",
                "--model", "claude-haiku-4-5-20251001",
                "--output-format", "json",
            ],
            cwd=str(project_dir),
            capture_output=True, text=True, timeout=120,
        )
        assert result.returncode == 0, (
            f"claude failed (rc={result.returncode}):\n"
            f"stderr: {result.stderr[:500]}\n"
            f"stdout: {result.stdout[:500]}"
        )

        records = read_current_hunches(replay / "hunches.jsonl")
        surfaced = [r for r in records if r.status == "surfaced"]
        assert len(surfaced) == 1
        assert surfaced[0].hunch_id == hid
        assert surfaced[0].history[0]["by"] == "hook:user_prompt_submit"

    def test_async_rewake_delivery_via_claude_p(self, tmp_path):
        """Approve a hunch AFTER Claude starts — tests asyncRewake path.

        1. Write hunch (not yet approved)
        2. Start claude -p (UPS finds nothing, Claude responds)
        3. Stop fires → async watcher starts polling
        4. From another thread, approve the hunch
        5. Watcher finds it → exits code 2 → Claude wakes up
        6. Hunch should be surfaced by hook:stop_delivery
        """
        project_dir = tmp_path / "e2e_project_async"
        project_dir.mkdir()
        replay = project_dir / ".hunch" / "replay"
        replay.mkdir(parents=True)

        subprocess.run(["git", "init"], cwd=str(project_dir),
                        capture_output=True, check=True)

        from hunch.init import init_project
        init_project(project_dir)

        writer = HunchesWriter(hunches_path=replay / "hunches.jsonl")
        hid = _emit(writer, "async rewake smell", "Should be delivered via asyncRewake.")

        # Short timeout on stop-delivery so the second watcher
        # (after rewake) exits quickly
        settings_path = project_dir / ".claude" / "settings.local.json"
        settings = json.loads(settings_path.read_text())
        for group in settings["hooks"]["Stop"]:
            for hook in group["hooks"]:
                if "stop-delivery" in hook.get("command", ""):
                    hook["command"] = "timeout 20 hunch hook stop-delivery"
        settings_path.write_text(json.dumps(settings, indent=2))

        # Start claude -p non-blocking
        proc = subprocess.Popen(
            [
                "claude", "-p", "Reply with exactly: ASYNC_TEST",
                "--model", "claude-haiku-4-5-20251001",
                "--output-format", "json",
            ],
            cwd=str(project_dir),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

        # Wait for Claude to respond and the async watcher to start,
        # then approve the hunch
        def _approve_after_delay():
            time.sleep(8)
            _label_good(replay, hid)

        t = threading.Thread(target=_approve_after_delay)
        t.start()

        proc.wait(timeout=90)
        t.join()

        records = read_current_hunches(replay / "hunches.jsonl")
        surfaced = [r for r in records if r.status == "surfaced"]
        assert len(surfaced) == 1, (
            f"Expected 1 surfaced hunch, got {len(surfaced)}. "
            f"Statuses: {[r.status for r in records]}"
        )
        assert surfaced[0].hunch_id == hid
        assert surfaced[0].history[0]["by"] == "hook:stop_delivery"
