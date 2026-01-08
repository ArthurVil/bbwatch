#!/bin/bash
set -e

# Fast-track: Create pipe immediately to satisfy go2rtc race condition
echo "Setting up overlay pipe..."
mkdir -p /app/data/overlays
PIPE_PATH="/app/data/overlays/overlay.pipe"

if [ ! -p "$PIPE_PATH" ]; then
    # Clean up if it exists as a normal file/dir
    if [ -e "$PIPE_PATH" ]; then rm -f "$PIPE_PATH"; fi
    mkfifo "$PIPE_PATH"
fi
chmod 666 "$PIPE_PATH"

# Background loop to feed the pipe - start immediately
# Redirecting to pipe OUTSIDE the loop keeps the file descriptor open
# preventing EOFs from killing the FFmpeg input stream.
(
    while true; do
        if [ -e /app/data/overlays/current.rgba ]; then
            cat /app/data/overlays/current.rgba
        else
            # Avoid tight loop if file missing, but don't crash the pipe
            sleep 0.1
        fi
        sleep 1
    done
) > "$PIPE_PATH" &

# Create initial healthy status to prevent Blue Screen on startup
echo "Initializing status..."
# Use python to get valid timestamp
python3 -c 'import json, time; from pathlib import Path; Path("/app/data/status.json").write_text(json.dumps({"timestamp": time.time(), "system_status": "ok", "alert_active": False}))'

# Pre-generate overlay images (now safe to take a few seconds)
echo "Pre-generating overlay images..."
python3 -c 'from bbwatch.overlay import generate_overlay_set, update_current_overlay; from pathlib import Path; d=Path("/app/data/overlays"); generate_overlay_set(d); update_current_overlay(d, Path("/app/data/status.json"))'

# Start the main application
echo "Starting bbwatch..."
exec python3 -m bbwatch.main
