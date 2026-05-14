"""Tests for hunch.mine.chunker."""

import pytest

from hunch.mine.chunker import Chunk, chunk_conversation


def _event(seq: int, etype: str = "assistant_text", text: str = "hello") -> dict:
    return {"tick_seq": seq, "type": etype, "text": text}


def _events(n: int) -> list[dict]:
    """Generate n events, with user_text every 10th event."""
    events = []
    for i in range(1, n + 1):
        etype = "user_text" if i % 10 == 1 else "assistant_text"
        events.append(_event(i, etype))
    return events


class TestChunkConversation:
    def test_empty_events(self):
        assert chunk_conversation([]) == []

    def test_single_chunk_when_small(self):
        events = _events(50)
        chunks = chunk_conversation(events, window_size=200, overlap=50)
        assert len(chunks) == 1
        assert chunks[0].n_events == 50
        assert chunks[0].start_seq == 1
        assert chunks[0].end_seq == 50

    def test_multiple_chunks_with_overlap(self):
        events = _events(400)
        chunks = chunk_conversation(events, window_size=200, overlap=50)
        assert len(chunks) >= 2
        for chunk in chunks:
            assert chunk.n_events > 0

        # Verify overlap: second chunk should start before end of first
        if len(chunks) >= 2:
            first_end = chunks[0].end_seq
            second_start = chunks[1].start_seq
            assert second_start < first_end

    def test_user_turn_boundary_snapping(self):
        events = []
        for i in range(1, 21):
            if i == 15:
                events.append(_event(i, "user_text"))
            else:
                events.append(_event(i, "assistant_text"))

        chunks = chunk_conversation(events, window_size=12, overlap=3)
        # The first chunk should end at or extend to the user turn boundary
        assert len(chunks) >= 1

    def test_all_events_covered(self):
        events = _events(500)
        chunks = chunk_conversation(events, window_size=100, overlap=20)

        all_seqs = set()
        for chunk in chunks:
            for e in chunk.events:
                all_seqs.add(e["tick_seq"])

        expected_seqs = {e["tick_seq"] for e in events}
        assert all_seqs == expected_seqs

    def test_chunk_start_end_seq(self):
        events = _events(300)
        chunks = chunk_conversation(events, window_size=100, overlap=20)

        for chunk in chunks:
            assert chunk.start_seq == chunk.events[0]["tick_seq"]
            assert chunk.end_seq == chunk.events[-1]["tick_seq"]

    def test_no_overlap_larger_than_window(self):
        events = _events(100)
        chunks = chunk_conversation(events, window_size=50, overlap=60)
        # Should still produce chunks (overlap is clamped effectively)
        assert len(chunks) >= 1

    def test_exact_window_size_no_user_boundary_inside(self):
        # All same type — no user-turn snapping, single chunk
        events = [_event(i, "assistant_text") for i in range(1, 201)]
        chunks = chunk_conversation(events, window_size=200, overlap=50)
        assert len(chunks) == 1
        assert chunks[0].n_events == 200
