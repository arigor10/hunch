#!/usr/bin/env bash
# Manual test script for the hunch edit feature.
#
# Creates a temp directory with two mock hunches, then opens hunch panel.
#
# In the panel:
#   1. Select h-0001 and press 'e' to edit.
#   2. Two fields appear: "Smell" (top) and "Description" (bottom).
#      Modify either or both, then press Ctrl+S.
#   3. Verify the detail pane shows "(edited)" and the edited text.
#   4. Press 'g' to approve. Verify status changes to "approved".
#   5. Check feedback.jsonl for the edit event:
#      cat $REPLAY_DIR/feedback.jsonl | python3 -m json.tool
#
# After quitting (q), the script prints the feedback.jsonl contents.

set -e

TMPDIR=$(mktemp -d /tmp/hunch-edit-test-XXXXXX)
REPLAY="$TMPDIR/replay"
mkdir -p "$REPLAY"

echo "=== Creating mock hunches in $REPLAY ==="

python3 -c "
from hunch.journal.hunches import HunchesWriter
from hunch.critic.protocol import Hunch, TriggeringRefs

w = HunchesWriter(hunches_path='$REPLAY/hunches.jsonl')

hid1 = w.allocate_id()
w.write_emit(
    Hunch(
        smell='Calibration drift detected in exp_042',
        description='The loss curve shows a 3x discrepancy between runs A and B. This may indicate a hyperparameter mismatch or data contamination between the two training configurations.',
        triggering_refs=TriggeringRefs(),
    ),
    hunch_id=hid1,
    ts='2026-05-27T10:00:00Z',
    emitted_by_tick=1,
    bookmark_prev=0,
    bookmark_now=10,
)
print(f'Created {hid1}')

hid2 = w.allocate_id()
w.write_emit(
    Hunch(
        smell='Eval metric plateau since tick 40',
        description='The BLEU score has been flat at 0.32 for the last 15 ticks despite continued training. Consider checking if the learning rate schedule is still active.',
        triggering_refs=TriggeringRefs(),
    ),
    hunch_id=hid2,
    ts='2026-05-27T10:01:00Z',
    emitted_by_tick=2,
    bookmark_prev=10,
    bookmark_now=20,
)
print(f'Created {hid2}')
"

echo ""
echo "=== Launching hunch panel ==="
echo "  Replay dir: $REPLAY"
echo ""
echo "  Try: select a hunch, press 'e' to edit, Ctrl+S to save, 'g' to approve"
echo ""

hunch panel --replay-dir "$REPLAY"

echo ""
echo "=== feedback.jsonl contents ==="
if [ -f "$REPLAY/feedback.jsonl" ]; then
    cat "$REPLAY/feedback.jsonl" | python3 -c "
import sys, json
for line in sys.stdin:
    line = line.strip()
    if line:
        print(json.dumps(json.loads(line), indent=2))
        print()
"
else
    echo "(no feedback written)"
fi

echo ""
echo "=== hunches.jsonl status ==="
python3 -c "
from hunch.journal.hunches import read_current_hunches
for r in read_current_hunches('$REPLAY/hunches.jsonl'):
    print(f'{r.hunch_id}  status={r.status}  smell={r.smell[:60]}')
"

echo ""
echo "Temp dir: $TMPDIR (delete with: rm -rf $TMPDIR)"
