#!/bin/bash
set -euo pipefail

SCRIPT="$(cd "$(dirname "$0")/.." && pwd)/run/filter_savedclips_window.py"
TMPDIR="$(mktemp -d)"
LIST="$TMPDIR/sentry_files"
EXP="$TMPDIR/expected"
REMOVED="$TMPDIR/removed"
EXP_REMOVED="$TMPDIR/expected_removed"

cleanup() {
  rm -rf "$TMPDIR"
}
trap cleanup EXIT

cat > "$LIST" <<DATA
SavedClips/2026-01-01_12-00-00/2026-01-01_12-00-00-front.mp4
SavedClips/2026-01-01_12-00-00/2026-01-01_12-01-00-front.mp4
SavedClips/2026-01-01_12-00-00/2026-01-01_12-05-00-front.mp4
SavedClips/2026-01-01_12-00-00/event.json
SavedClips/2026-01-01_13-00-00/2026-01-01_13-00-00-front.mp4
SavedClips/2026-01-01_13-00-00/2026-01-01_13-02-00-front.mp4
SentryClips/2026-01-01_12-00-00/2026-01-01_12-00-00-front.mp4
DATA

python3 "$SCRIPT" --list "$LIST" --minutes 3 --removed-list "$REMOVED"

cat > "$EXP" <<DATA
SavedClips/2026-01-01_12-00-00/2026-01-01_12-05-00-front.mp4
SavedClips/2026-01-01_12-00-00/event.json
SavedClips/2026-01-01_13-00-00/2026-01-01_13-00-00-front.mp4
SavedClips/2026-01-01_13-00-00/2026-01-01_13-02-00-front.mp4
SentryClips/2026-01-01_12-00-00/2026-01-01_12-00-00-front.mp4
DATA

cat > "$EXP_REMOVED" <<DATA
SavedClips/2026-01-01_12-00-00/2026-01-01_12-00-00-front.mp4
SavedClips/2026-01-01_12-00-00/2026-01-01_12-01-00-front.mp4
DATA

diff -u "$EXP" "$LIST"
diff -u "$EXP_REMOVED" "$REMOVED"

echo "filter-savedclips-window-test: OK"
