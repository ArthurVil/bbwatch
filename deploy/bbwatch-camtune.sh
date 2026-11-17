#!/usr/bin/env bash
# Apply a night image profile to the USB camera while it is in IR mode.
#
# The camera switches to IR (grayscale) by itself in the dark. Its IR image
# is washed out: backlight compensation lifts the shadows to mid-grey (black
# at Y=54/255) and sharpening amplifies sensor grain. The NIGHT profile below
# fixes that in the camera's own processing (zero latency, zero CPU in the
# go2rtc pipeline). The DAY profile is the camera's defaults, so the color
# view is left exactly as it was.
#
# Mode detection: an IR frame has zero saturation, a color frame does not.
# Every CHECK_INTERVAL_S this samples a few frames of the raw camera stream
# and reconciles the camera controls with the profile for the detected mode.
# Controls are re-applied whenever they differ (camera re-plugged or reset),
# and only mode changes are logged.
#
# Runs on the host from bbwatch-camtune.service. The profiles also change the
# image bbwatch's motion detection sees (raw_video comes from the same camera).
#
#   CAMTUNE_DEVICE        camera device (default /dev/video0)
#   CAMTUNE_STREAM        stream to sample (default rtsp://127.0.0.1:8554/raw_video)
#   CHECK_INTERVAL_S      seconds between checks (default 20)
#   CAMTUNE_NIGHT / CAMTUNE_DAY  "control=value ..." profiles (defaults below)
set -uo pipefail

DEVICE="${CAMTUNE_DEVICE:-/dev/video0}"
STREAM="${CAMTUNE_STREAM:-rtsp://127.0.0.1:8554/raw_video}"
INTERVAL="${CHECK_INTERVAL_S:-20}"

# Calibrated 2026-11-17 on the crib scene in IR: black 54 -> 21, highlights
# 252 -> 240 (no clipping), same bitrate (less sharpening grain offsets the
# added contrast).
NIGHT="${CAMTUNE_NIGHT:-brightness=-24 contrast=40 gamma=100 sharpness=1 backlight_compensation=0}"
DAY="${CAMTUNE_DAY:-brightness=0 contrast=32 gamma=100 sharpness=3 backlight_compensation=1}"

# Saturation (0-~180) hysteresis: IR is exactly 0, a lit room is well above.
IR_BELOW=0.5
COLOR_ABOVE=1.5

mode=""

# Mean saturation of a few frames; empty if the stream could not be read.
sample_saturation() {
    timeout 15 ffmpeg -hide_banner -loglevel error -rtsp_transport tcp -i "$STREAM" \
        -an -frames:v 5 -vf "signalstats,metadata=print:file=-" -f null - 2>/dev/null |
        awk -F= '/SATAVG/ {s += $2; n++} END {if (n) printf "%.2f", s / n}'
}

# Set the controls of a profile that differ from the camera's current values.
apply_profile() {
    local profile="$1" ctrl want have changed=""
    for ctrl in $profile; do
        want="${ctrl#*=}"
        have=$(v4l2-ctl -d "$DEVICE" -C "${ctrl%%=*}" 2>/dev/null | awk '{print $2}')
        if [ "$have" != "$want" ]; then
            v4l2-ctl -d "$DEVICE" -c "$ctrl" || echo "ERROR: could not set $ctrl on $DEVICE" >&2
            changed+=" $ctrl"
        fi
    done
    [ -n "$changed" ] && echo "Applied$changed"
    return 0
}

while true; do
    sat=$(sample_saturation)
    if [ -z "$sat" ]; then
        echo "WARNING: could not sample $STREAM; keeping the current camera settings" >&2
    else
        new="$mode"
        if awk -v s="$sat" -v t="$IR_BELOW" 'BEGIN {exit !(s < t)}'; then
            new=night
        elif awk -v s="$sat" -v t="$COLOR_ABOVE" 'BEGIN {exit !(s > t)}'; then
            new=day
        fi
        if [ -n "$new" ]; then
            [ "$new" != "$mode" ] && echo "Camera in $([ "$new" = night ] && echo IR || echo color) mode (saturation $sat): $new profile"
            mode="$new"
            if [ "$mode" = night ]; then apply_profile "$NIGHT"; else apply_profile "$DAY"; fi
        fi
    fi
    sleep "$INTERVAL"
done
