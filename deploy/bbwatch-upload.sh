#!/usr/bin/env bash
# Upload finished bbwatch clips and screenshots to Google Drive via rclone,
# deleting each local copy once it is safely uploaded.
#
# Runs on the host (not in the bbwatch container) from bbwatch-upload.timer.
# Opt-in: see "Remote upload" in .claude/rules/reliability.md.
#
# The switch lives in config.yaml (`upload.enabled`, `upload.remote`); with
# upload.enabled false (the default) this script does nothing and exits 0.
#
#   BBWATCH_CONFIG         config file (default ~/bbwatch/config.yaml)
#   BBWATCH_DATA_DIR       data dir holding clips/ and screenshots/ (default ~/bbwatch/data)
#   BBWATCH_UPLOAD_REMOTE  rclone destination, overrides upload.remote
set -uo pipefail

CONFIG="${BBWATCH_CONFIG:-$HOME/bbwatch/config.yaml}"
DATA_DIR="${BBWATCH_DATA_DIR:-$HOME/bbwatch/data}"

# Prints "<enabled> <remote>"; an unreadable config is an error, not "off".
if ! settings=$(python3 - "$CONFIG" <<'EOF'
import sys, yaml
with open(sys.argv[1]) as f:
    upload = (yaml.safe_load(f) or {}).get("upload") or {}
print("true" if upload.get("enabled") is True else "false", upload.get("remote") or "gdrive:bbwatch")
EOF
); then
    echo "ERROR: cannot read upload settings from $CONFIG" >&2
    exit 1
fi
read -r enabled config_remote <<<"$settings"

if [ "$enabled" != "true" ]; then
    echo "Upload disabled (upload.enabled is false in $CONFIG): nothing to do"
    exit 0
fi

REMOTE="${BBWATCH_UPLOAD_REMOTE:-$config_remote}"
status=0

for kind in clips screenshots; do
    src="$DATA_DIR/$kind"
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
