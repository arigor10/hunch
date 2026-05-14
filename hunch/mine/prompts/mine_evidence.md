You are helping build a ground-truth dataset for evaluating a research critic system. You have access to a research conversation (between a Scientist and an AI Assistant) and project documentation snapshots.

## Your task

A research "nose moment" was identified: at tick_seq {signal_seq}, the human researcher noticed something wrong. Your job is to find **where in the conversation the evidence appeared that would have allowed a careful critic to raise this concern BEFORE the researcher did** (i.e., before tick_seq {signal_seq}).

## The finding

- **Signal text:** {signal_text}
- **Anomaly:** {anomaly}

## What you need to find

Search the conversation and docs for:
1. **Where the key concepts/claims relevant to this anomaly first appeared** — specific tick_seqs. Grep for relevant terms in conversation.jsonl.
2. **Where the evidence accumulated** that would make the anomaly detectable — which turns, which docs.
3. **The earliest tick_seq at which a critic could have noticed the gap, contradiction, or untested assumption.**
4. **Any project docs** (in project_docs/) that contain relevant evidence.

Important:
- The conversation is in conversation.jsonl (JSONL, one event per line). Each event has: tick_seq, type, timestamp, text (for text events), path (for artifact events).
- Use Grep to search conversation.jsonl for relevant terms. Use Read to examine specific sections. The file may be large (thousands of lines) — do NOT try to read it all at once.
- Project docs are in project_docs/ — these are snapshots as they existed at the time of the finding.
- Focus on evidence BEFORE tick_seq {signal_seq}. The goal is to find what a critic watching the conversation could have cited.

## Output

Return a JSON object with:
- evidence_tick_seqs: list of key tick_seqs where evidence accumulated
- earliest_raisable: the tick_seq after which a critic could first have raised this
- artifacts: list of doc paths that contain relevant evidence (relative to project_docs/)
- evidence_summary: 2-3 sentences describing the evidence chain
- smell: a one-line hunch title (short, specific, like a PR title)
- description: 3-5 sentence hunch description written from a critic's perspective ("The conversation established X at turn N, but by turn M...")
