#!/usr/bin/env bash
# Upload the bbwatch outbox (data/outbox/{clips,screenshots}) to Google Drive
# via rclone, deleting each outbox file once it is safely uploaded.
#
# bbwatch only puts files in the outbox when alerts.actions.destination is
# "cloud" or "both" in config.yaml (with "both" they are hard links, so the
# local copies in data/clips and data/screenshots stay). With "local", or with
# no screenshot/clip action, the outbox stays empty and this does nothing.
#
# Runs on the host (not in the bbwatch container) from bbwatch-upload.timer.
# Opt-in: see "Remote upload" in .claude/rules/reliability.md.
#
#   BBWATCH_DATA_DIR       bbwatch data dir holding outbox/ (default ~/bbwatch/data)
#   BBWATCH_UPLOAD_REMOTE  rclone destination (default gdrive:bbwatch)
set -uo pipefail

DATA_DIR="${BBWATCH_DATA_DIR:-$HOME/bbwatch/data}"
REMOTE="${BBWATCH_UPLOAD_REMOTE:-gdrive:bbwatch}"
status=0

for kind in clips screenshots; do
    src="$DATA_DIR/outbox/$kind"
    [ -d "$src" ] || continue

    # In-progress clips are hidden ".HHMMSS.mp4.part" files: never upload them.
    # --min-age also skips a screenshot that ffmpeg may still be writing.
    if ! rclone move "$src" "$REMOTE/$kind" \
        --exclude '.*' --exclude '*.part' \
        --min-age 10s \
        --retries 3 --low-level-retries 10 \
        --log-level INFO --stats 0; then
        echo "ERROR: upload of $kind to $REMOTE/$kind failed" >&2
        status=1
    fi

    # Prune emptied day folders. Not rclone's --delete-empty-src-dirs: bbwatch
    # creates today's folder 1-2 s before ffmpeg opens the clip file in it.
    find "$src" -mindepth 1 -type d -empty -mmin +60 -delete

    pending=$(find "$src" -type f ! -name '.*' | wc -l)
    echo "$kind: $pending file(s) waiting locally"
done

exit "$status"
