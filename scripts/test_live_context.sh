#!/usr/bin/env bash
# Manual test script for the live context web viewer.
#
# Creates a temp replay dir with mock conversation events, hunches,
# and feedback, then launches hunch panel (which starts the web server).
#
# Test steps:
#   1. Panel launches. Note the "context viewer: http://localhost:5556" notification.
#   2. Open http://localhost:5556 in a browser.
#   3. You should see 3 hunches in the left panel with status badges.
#   4. Click a hunch — the conversation context should scroll to the trigger window.
#   5. h-0002 should show as "approved" (green badge) and "(edited)".
#   6. h-0003 should show as "dismissed" (red badge).
#   7. Label buttons should be hidden; a "label in TUI" hint should appear.
#   8. In the TUI, select a hunch and press 'o' — your browser should open
#      to that hunch's context.
#   9. Try pressing 'g' on h-0001 in the TUI, then check that the web UI
#      updates to "approved" within ~3 seconds.
#
# After quitting (q in TUI), the web server stops automatically.

set -e

TMPDIR=$(mktemp -d /tmp/hunch-live-ctx-XXXXXX)
REPLAY="$TMPDIR/replay"
mkdir -p "$REPLAY/artifacts"

echo "=== Creating mock replay data in $REPLAY ==="

python3 -c "
import json
from pathlib import Path

replay = Path('$REPLAY')

# --- conversation.jsonl: simulate a research dialogue ---
events = [
    {'tick_seq': 1, 'type': 'user_text', 'text': 'Let me start by setting up the experiment. I want to compare 4-bit and 8-bit quantization on Llama-3.1-8B.', 'timestamp': '2026-05-27T10:00:00Z'},
    {'tick_seq': 2, 'type': 'assistant_text', 'text': 'I will set up the comparison. Let me create the configuration files for both quantization levels and prepare the evaluation pipeline.', 'timestamp': '2026-05-27T10:00:30Z'},
    {'tick_seq': 3, 'type': 'assistant_text', 'text': 'I have created configs/quant_4bit.toml and configs/quant_8bit.toml. Both use the same base model (Llama-3.1-8B) with identical training hyperparameters. The only difference is the quantization level. Let me now run the 4-bit variant first.', 'timestamp': '2026-05-27T10:01:00Z'},
    {'tick_seq': 4, 'type': 'user_text', 'text': 'Good. Also make sure we log the memory usage during training.', 'timestamp': '2026-05-27T10:02:00Z'},
    {'tick_seq': 5, 'type': 'assistant_text', 'text': 'Added memory logging via torch.cuda.max_memory_allocated(). Results will be written to results/memory_profile.json after each epoch. Starting the 4-bit run now.', 'timestamp': '2026-05-27T10:02:30Z'},
    {'tick_seq': 6, 'type': 'assistant_text', 'text': 'The 4-bit run completed. Loss converged to 0.42 after 3 epochs. Peak memory: 6.2 GB. Starting the 8-bit run now with the same seed.', 'timestamp': '2026-05-27T10:10:00Z'},
    {'tick_seq': 7, 'type': 'assistant_text', 'text': 'The 8-bit run completed. Loss converged to 0.38 after 3 epochs. Peak memory: 9.8 GB. Interesting - the 8-bit model achieved lower loss but the difference might not be significant.', 'timestamp': '2026-05-27T10:18:00Z'},
    {'tick_seq': 8, 'type': 'user_text', 'text': 'Let me look at the learning curves. Can you plot them side by side?', 'timestamp': '2026-05-27T10:19:00Z'},
    {'tick_seq': 9, 'type': 'assistant_text', 'text': 'Here are the learning curves. I notice something odd: the 4-bit curve shows a sharp dip at epoch 2 step 450 that recovers by step 500. This could be a gradient instability from the lower precision. The 8-bit curve is smooth throughout.', 'timestamp': '2026-05-27T10:20:00Z'},
    {'tick_seq': 10, 'type': 'user_text', 'text': 'That dip is concerning. Let me check the gradient norms.', 'timestamp': '2026-05-27T10:21:00Z'},
    {'tick_seq': 11, 'type': 'assistant_text', 'text': 'The gradient norms confirm it: there is a spike to 145.0 at step 448 in the 4-bit run (normal range is 0.5-2.0). The gradient clipping threshold was set to 1.0 but it seems the spike happened within a single step before clipping could take effect. This is a known issue with 4-bit training on certain attention layers.', 'timestamp': '2026-05-27T10:22:00Z'},
    {'tick_seq': 12, 'type': 'user_text', 'text': 'OK, let me now switch to evaluating the models on our benchmark.', 'timestamp': '2026-05-27T10:25:00Z'},
    {'tick_seq': 13, 'type': 'assistant_text', 'text': 'Running the evaluation suite on both checkpoints. The benchmark includes: MMLU (5-shot), HellaSwag (10-shot), and our custom domain-specific eval.', 'timestamp': '2026-05-27T10:26:00Z'},
    {'tick_seq': 14, 'type': 'assistant_text', 'text': 'Results are in. 4-bit: MMLU 62.1, HellaSwag 74.3, Custom 58.9. 8-bit: MMLU 63.8, HellaSwag 75.1, Custom 61.2. The 8-bit model is consistently better by 1-2 points, which aligns with the lower training loss. However, I should note that I used the same evaluation seeds as the training seeds - this could introduce a subtle bias.', 'timestamp': '2026-05-27T10:35:00Z'},
    {'tick_seq': 15, 'type': 'user_text', 'text': 'Wait, are you saying the eval seeds match the training seeds? That could contaminate the results.', 'timestamp': '2026-05-27T10:36:00Z'},
    {'tick_seq': 16, 'type': 'assistant_text', 'text': 'You are right to flag that. Let me re-run with different eval seeds. Using seed=42 for training and seed=123 for evaluation this time.', 'timestamp': '2026-05-27T10:37:00Z'},
]

with open(replay / 'conversation.jsonl', 'w') as f:
    for ev in events:
        f.write(json.dumps(ev) + '\n')
print(f'  wrote {len(events)} conversation events')

# --- hunches.jsonl: 3 hunches at different points ---
from hunch.journal.hunches import HunchesWriter
from hunch.critic.protocol import Hunch, TriggeringRefs

w = HunchesWriter(hunches_path=replay / 'hunches.jsonl')

# h-0001: pending (not yet labeled) — gradient instability
hid1 = w.allocate_id()
w.write_emit(
    Hunch(
        smell='Gradient spike in 4-bit run may invalidate comparison',
        description='The gradient norm spike to 145.0 at step 448 suggests the 4-bit model experienced training instability. The final loss (0.42 vs 0.38) comparison may be misleading if the model did not fully recover from this event.',
        triggering_refs=TriggeringRefs(),
    ),
    hunch_id=hid1,
    ts='2026-05-27T10:22:30Z',
    emitted_by_tick=1,
    bookmark_prev=8,
    bookmark_now=11,
)
print(f'  created {hid1} (pending)')

# h-0002: will be labeled good (approved) + edited
hid2 = w.allocate_id()
w.write_emit(
    Hunch(
        smell='Eval seed contamination',
        description='The evaluation used the same random seeds as training (seed=42). This creates a risk of data ordering bias - the model may perform better on examples it saw in a particular order during training.',
        triggering_refs=TriggeringRefs(),
    ),
    hunch_id=hid2,
    ts='2026-05-27T10:35:30Z',
    emitted_by_tick=2,
    bookmark_prev=12,
    bookmark_now=14,
)
print(f'  created {hid2} (will be approved+edited)')

# h-0003: will be labeled bad (dismissed)
hid3 = w.allocate_id()
w.write_emit(
    Hunch(
        smell='Memory difference between 4-bit and 8-bit is expected',
        description='The 6.2 GB vs 9.8 GB memory difference is consistent with the theoretical 2x ratio for 4-bit vs 8-bit quantization. This is not a concern.',
        triggering_refs=TriggeringRefs(),
    ),
    hunch_id=hid3,
    ts='2026-05-27T10:20:30Z',
    emitted_by_tick=3,
    bookmark_prev=5,
    bookmark_now=7,
)
print(f'  created {hid3} (will be dismissed)')

# --- feedback.jsonl: labels + an edit ---
from hunch.journal.feedback import FeedbackWriter
fw = FeedbackWriter(feedback_path=replay / 'feedback.jsonl')

# Label h-0002 as good (approved)
fw.write_explicit(hid2, 'good', '2026-05-27T10:36:00Z')

# Edit h-0002's smell
fw.write_edit(
    hunch_id=hid2,
    original_smell='Eval seed contamination',
    original_description='The evaluation used the same random seeds as training (seed=42). This creates a risk of data ordering bias - the model may perform better on examples it saw in a particular order during training.',
    edited_smell='Eval seed matches training seed — potential data ordering bias',
    edited_description='The evaluation used the same random seeds as training (seed=42). This creates a risk of data ordering bias. The Scientist already caught this (tick 15) but it is worth flagging explicitly as it affects the validity of the 4-bit vs 8-bit comparison.',
    ts='2026-05-27T10:36:30Z',
)

# Label h-0003 as bad (dismissed)
fw.write_explicit(hid3, 'bad', '2026-05-27T10:37:00Z')

print('  wrote feedback: h-0002 approved+edited, h-0003 dismissed')

# --- artifacts.jsonl (empty but present for validation) ---
(replay / 'artifacts.jsonl').touch()
print('  touched artifacts.jsonl')

print()
print(f'=== Replay dir: {replay}')
print()
"

echo ""
echo "=== Launching hunch panel ==="
echo "  The web context viewer starts automatically."
echo "  Open http://localhost:5556 in your browser."
echo ""
echo "  Test: click hunches in the web UI, check statuses, press 'o' in TUI."
echo ""

hunch panel --replay-dir "$REPLAY"

echo ""
echo "Temp dir: $TMPDIR (delete with: rm -rf $TMPDIR)"
