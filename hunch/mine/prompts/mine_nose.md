# Mining Prompt — Nose-Firing Moment Detection

You are analyzing a segment of a research conversation between a human researcher (labelled **USER** in the transcript) and an AI research assistant (labelled **CLAUDE**). Your task is to identify **nose-firing moments** — instances where the user detected an anomaly, questioned a result, noticed something didn't add up, or changed course because something looked wrong.

## What counts as a nose-firing moment

Explicit moments where the user directly questions a result, flags an inconsistency, or expresses suspicion. The anomaly detection is stated in the text.

## Calibration examples

**Example 1 — directional violation (HIGH confidence):**
The Scientist notices a metric going UP under an intervention designed to make it go DOWN. They say something like "wait, that's going the wrong direction." A result moving opposite to the mechanism's prediction is always a nose moment.

**Example 2 — subset containment failure (HIGH confidence):**
A model with MORE degrees of freedom performs worse than a strict subset. This violates a mathematical guarantee (the optimizer with more freedom must do at least as well), so it signals an unfair comparison — e.g., a hidden regularizer, different hyperparameters, or a bug.

**Example 3 — question-form anomaly detection (HIGH confidence):**
The Scientist says "why would X go down when we expect it to go up?" — the question presupposes that something is wrong. Any question that presupposes an anomaly IS a nose moment, even if phrased politely.

**Example 4 — forgotten commitment (HIGH confidence):**
The Scientist notices that an earlier methodological commitment was not followed — e.g., "but we said we'd normalize by residual norms" when raw norms were compared instead.

## Sensitivity guidance

- **Questions that presuppose an anomaly ARE nose moments.** "Why would X go down?" is anomaly detection even if phrased as a question.
- **Directional violations are always significant.** When a result moves opposite to the mechanism's prediction, flag it.
- **A nose moment followed by an explanation is STILL a nose moment.** The detection happened regardless of whether the anomaly was later resolved.
- **Err on the side of inclusion at medium confidence** rather than missing genuine catches.

## What NOT to flag

- Normal research questions ("should we try X next?") where nothing looks wrong
- Routine debugging of code errors (unless the error reveals a deeper methodological problem)
- Standard hyperparameter discussion
- Monitoring narration ("loss is going down as expected")
- The assistant (CLAUDE) flagging its own concerns (only flag user-raised moments)
- Operational/infrastructure issues (GPU problems, API errors, package conflicts)
- Hypothesis falsification by a deliberately designed test (the test was supposed to answer a question — a negative result is not an anomaly)

## Output format

For each finding, output a JSON object on its own line (one object per line, NOT a JSON array):

```
{"id": "NF-001", "tick_seq": 542, "signal_text": "wait, that can't be right — accuracy went up?", "anomaly": "Accuracy improved under an intervention designed to hurt it", "confidence": "high"}
```

Fields:
- `id`: sequential identifier, NF-001, NF-002, ...
- `tick_seq`: the sequence number where the signal text occurs (read it from the `(seq N)` label in the conversation)
- `signal_text`: verbatim quote from the conversation (1-3 sentences)
- `anomaly`: one-sentence description of what looked wrong
- `confidence`: "high" or "medium"

If no nose-firing moments in this segment, output nothing.

IMPORTANT:
- Quality over quantity. Five high-confidence findings beat twenty medium-confidence ones.
- The anomaly description is the quality signal — concrete descriptions that name what's wrong indicate genuine understanding.
- Preserve the EXACT words from the conversation in signal_text.
- Use the sequence numbers shown in the conversation (e.g., `(seq 542)`) for tick_seq.

## Conversation segment

{chunk_text}

Respond with ONLY the JSONL output. No preamble, no markdown fences, no commentary.
