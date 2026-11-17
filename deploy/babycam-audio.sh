#!/usr/bin/env bash
# babycam audio for go2rtc: USB mic -> +10 dB -> G.711 mu-law -> RTSP.
#
#   exec:/home/kanai/bbwatch/deploy/babycam-audio.sh {output}
#
# The camera's USB mic sometimes stops delivering samples while ALSA still
# reports the capture as RUNNING (hw_ptr frozen). FFmpeg then waits forever:
# no error, no exit. When audio and video shared one FFmpeg, that froze the
# live video too (seen 5 times on 2026-11-17). Now audio runs on its own, and
# this wrapper watches FFmpeg's progress: if the audio clock stops advancing
# for STALL_S, it kills FFmpeg and exits, so go2rtc starts a fresh capture
# (reopening the device has always brought the mic back).
#
# setpriv --pdeathsig KILL: FFmpeg dies with this wrapper even if go2rtc
# SIGKILLs it, so an orphan can never keep the (single-open) mic busy.
#
#   BABYCAM_MIC          ALSA device (default plughw:CARD=Camera,DEV=0)
#   BABYCAM_MIC_STALL_S  seconds without new audio before restarting (default 5)
set -uo pipefail

OUTPUT="${1:?usage: babycam-audio.sh <rtsp output url>}"
DEVICE="${BABYCAM_MIC:-plughw:CARD=Camera,DEV=0}"
STALL_S="${BABYCAM_MIC_STALL_S:-5}"

# volume=10dB before the 8-bit mu-law encode so quiet sounds keep their
# resolution (the mic's hardware gain is already at max); alimiter stops loud
# cries from clipping. bbwatch's alerts.trigger_high/low are set for this gain.
coproc FFMPEG {
    exec setpriv --pdeathsig KILL ffmpeg -hide_banner -v error -nostdin \
        -stats_period 1 -progress pipe:1 \
        -fflags nobuffer -flags low_delay -f alsa -i "$DEVICE" \
        -af 'volume=10dB,alimiter=limit=0.9:level=0' \
        -c:a pcm_mulaw -ar 8000 -ac 1 -f rtsp -rtsp_transport tcp "$OUTPUT"
}
# Saved now: bash unsets FFMPEG / FFMPEG_PID as soon as the coproc exits
FFMPEG_FD="${FFMPEG[0]}"
PID="$FFMPEG_PID"
trap 'kill -9 "$PID" 2>/dev/null' EXIT
trap 'exit 0' TERM INT

last=""
since=$SECONDS
while true; do
    IFS= read -r -t "$STALL_S" line <&"$FFMPEG_FD"
    rc=$?
    if ((rc > 128)); then # read timed out: ffmpeg stopped reporting
        echo "babycam-audio: no progress from ffmpeg for ${STALL_S}s - restarting the capture" >&2
        exit 1
    elif ((rc != 0)); then # EOF: ffmpeg exited by itself (error already on stderr)
        wait "$PID"
        exit $?
    fi
    case "$line" in
    out_time_us=*)
        if [ "$line" != "$last" ]; then
            last="$line"
            since=$SECONDS
        elif ((SECONDS - since >= STALL_S)); then
            echo "babycam-audio: mic delivered no audio for ${STALL_S}s - restarting the capture" >&2
            exit 1
        fi
        ;;
    esac
done
