"""Tests for the `hunch bank tombstone` CLI command."""

from __future__ import annotations

import argparse
from pathlib import Path

from hunch.bank.reader import read_bank
from hunch.bank.sync import _now_ts
from hunch.bank.writer import BankWriter
from hunch.cli import _cmd_bank_tombstone


def _seed_bank(tmp_path: Path, run: str = "myrun") -> Path:
    bank_path = tmp_path / ".hunch" / "bank" / "hunch_bank.jsonl"
    w = BankWriter(bank_path)
    bid = w.allocate_id()
    w.write_entry(bid, "smell x", "desc x", run, "h-1", _now_ts(), bookmark_now=5)
    return bank_path


def _ns(tmp_path: Path, run: str, *, reason: str = "", yes: bool = True):
    return argparse.Namespace(
        bank_command="tombstone",
        project_dir=tmp_path,
        run=run,
        reason=reason,
        yes=yes,
    )


def test_tombstone_writes_event(tmp_path, capsys):
    bank_path = _seed_bank(tmp_path)
    rc = _cmd_bank_tombstone(_ns(tmp_path, "myrun", reason="debug run"))
    assert rc == 0
    state = read_bank(bank_path)
    assert "myrun" in state.tombstoned_runs
    out = capsys.readouterr().out
    assert "not_displayable" in out  # the explanation was printed
    assert "debug run" in out


def test_tombstone_idempotent(tmp_path, capsys):
    _seed_bank(tmp_path)
    assert _cmd_bank_tombstone(_ns(tmp_path, "myrun")) == 0
    rc = _cmd_bank_tombstone(_ns(tmp_path, "myrun"))
    assert rc == 0
    assert "already tombstoned" in capsys.readouterr().out


def test_tombstone_unknown_run_errors(tmp_path, capsys):
    _seed_bank(tmp_path)
    rc = _cmd_bank_tombstone(_ns(tmp_path, "ghost"))
    assert rc == 1
    assert "not found" in capsys.readouterr().err


def test_tombstone_no_bank_errors(tmp_path, capsys):
    rc = _cmd_bank_tombstone(_ns(tmp_path, "myrun"))
    assert rc == 1
    assert "no bank" in capsys.readouterr().err


def test_tombstone_non_tty_without_yes_aborts(tmp_path, capsys, monkeypatch):
    bank_path = _seed_bank(tmp_path)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    rc = _cmd_bank_tombstone(_ns(tmp_path, "myrun", yes=False))
    assert rc == 1
    assert "not a TTY" in capsys.readouterr().err
    # Nothing was written.
    assert "myrun" not in read_bank(bank_path).tombstoned_runs


def test_tombstone_knows_run_from_bank_runs_copy(tmp_path):
    """A run present only as a bank/runs copy (no synced entry) is tombstonable."""
    bank_path = tmp_path / ".hunch" / "bank" / "hunch_bank.jsonl"
    bank_path.parent.mkdir(parents=True)
    # An empty bank stream, but a run copy on disk.
    bank_path.write_text("")
    (bank_path.parent / "runs" / "copyonly").mkdir(parents=True)
    (bank_path.parent / "runs" / "copyonly" / "hunches.jsonl").write_text("")
    rc = _cmd_bank_tombstone(_ns(tmp_path, "copyonly"))
    assert rc == 0
    assert "copyonly" in read_bank(bank_path).tombstoned_runs
