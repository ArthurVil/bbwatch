# Raspberry Pi 5 + Pi Camera Module 3 Setup Guide

This guide walks through setting up bbwatch on Raspberry Pi 5 with Pi Camera Module 3 for automated cry detection and video streaming.

## Prerequisites

- **Raspberry Pi 5** (4GB or 8GB RAM)
- **Raspberry Pi Camera Module 3** (standard or wide angle)
- **Micro SD card** — 64GB recommended (for WAV segment storage)
- **Raspberry Pi OS 64-bit** — latest version (kernel 6.x supports libcamera natively)
- **Ribbon cable** — properly seated in the camera connector
- **Ethernet or WiFi** — for remote access and stream viewing
- **Adequate cooling** — RPi5 runs warm; consider a heatsink or case with fan

## System Setup

### 1. Flash Raspberry Pi OS 64-bit

Use Raspberry Pi Imager:
1. Download from https://www.raspberrypi.com/software/
2. Select "Raspberry Pi 5" → "Raspberry Pi OS (64-bit)" → your SD card
3. Configure WiFi, hostname, and SSH in the advanced settings (optional but recommended)
4. Flash and boot

### 2. Connect Pi Camera

1. Power off the Pi
2. Open the camera connector (small ribbon tab on board)
3. Insert the ribbon cable *gold contacts facing the board*, ensuring it seats fully
4. Gently close the ribbon tab — do not force it
5. Power on

### 3. Install System Packages

```bash
sudo apt update
sudo apt install libcamera-tools python3-picamera2 ffmpeg -y
```

Verify libcamera is working:
```bash
libcamera-hello --list-cameras
```

Expected output (Pi Camera Module 3):
```
Available cameras
0 : imx708 [4608x2592 10-bit RGGB]
```

If you see an error like "Cannot access camera. File exists" or device busy, the ribbon cable may not be properly seated — recheck and reboot.

## Docker Setup & Deployment

### 1. Clone the repository (if not already done)

```bash
git clone https://github.com/your-username/bbwatch.git
cd bbwatch
git checkout feat/pi-camera-rpi5  # or current working branch
```

### 2. Install go2rtc on the host

go2rtc must run **natively on the Pi**, not in Docker: the CSI camera needs the
host libcamera stack, and no stock go2rtc image includes rpicam support
(the containerized attempt fails with `unsupported scheme: rpicam:0`).

```bash
# Single static binary
curl -sL -o /tmp/go2rtc \
  https://github.com/AlexxIT/go2rtc/releases/download/v1.9.14/go2rtc_linux_arm64
sudo install -m 755 /tmp/go2rtc /usr/local/bin/go2rtc

# Config + systemd unit (adjust mic device, FIFO path and User= first)
sudo cp deploy/go2rtc-host.yaml /etc/go2rtc.yaml
sudo cp deploy/go2rtc.service /etc/systemd/system/go2rtc.service
sudo systemctl daemon-reload && sudo systemctl enable --now go2rtc
```

The camera source is `exec:rpicam-vid … -o -` at 1080p@10fps; audio is
captured from ALSA directly (no PulseAudio required).

### 3. Start bbwatch

```bash
docker compose -f docker/docker-compose.yml up -d bbwatch
```

bbwatch runs in Docker and reaches the host go2rtc via
`rtsp://host.docker.internal:8554`; the overlay FIFO is shared through the
repo's `data/` bind mount that host go2rtc reads.

## Operating go2rtc (systemd)

go2rtc runs as a native systemd unit on the Pi (`/etc/systemd/system/go2rtc.service`,
installed from `deploy/go2rtc.service`), completely independent of the Docker
`bbwatch` container. Use standard `systemctl`/`journalctl` for day-to-day
operation — none of this requires touching Docker.

### Start / stop / restart / status

```bash
sudo systemctl start go2rtc
sudo systemctl stop go2rtc
sudo systemctl restart go2rtc
sudo systemctl status go2rtc
```

### Enable / disable at boot

`enable --now` (done once during install, see above) both starts go2rtc
immediately and makes it start on every boot.

```bash
sudo systemctl enable go2rtc     # start automatically on boot
sudo systemctl disable go2rtc    # stop starting on boot (does not stop a running instance)
```

### Logs

```bash
journalctl -u go2rtc -f              # follow live
journalctl -u go2rtc -n 200          # last 200 lines
journalctl -u go2rtc --since "10 min ago"
```

`deploy/go2rtc.service` sets `Restart=always`, so a crashing stream (e.g. the
`rpicam-vid` or `ffmpeg` exec process behind a stream dying) restarts
automatically within `RestartSec=5`. When troubleshooting, check the ~50-100
lines around a restart rather than only the very last line — the useful error
(camera busy, bad device path, missing FIFO reader) usually appears just
before the restart, not after.

### Check it's listening

```bash
ss -tlnp | grep -E '8554|1984'
```

- **`:8554`** — RTSP. Used internally by the `raw_video`/`babycam` composite
  pipeline and by bbwatch's motion detector; also reachable externally (e.g.
  `vlc rtsp://<pi-ip>:8554/babycam`).
- **`:1984`** — go2rtc's HTTP API and built-in web UI (stream viewer, health
  checks).

If neither port shows up, go2rtc isn't running or crashed on startup — check
`sudo systemctl status go2rtc` first.

### Editing the config — two separate config surfaces

> [!IMPORTANT]
> bbwatch's `config.yaml` (Docker side) is bind-mounted into the container —
> editing it and running `make restart` picks up changes immediately. go2rtc's
> config at `/etc/go2rtc.yaml` is a **plain copy** installed onto the host
> filesystem, not bind-mounted from the repo — editing it has **no effect**
> until you explicitly restart the systemd unit. This asymmetry is the most
> common cause of "I changed the config but nothing happened."

```bash
sudo nano /etc/go2rtc.yaml
sudo systemctl restart go2rtc
journalctl -u go2rtc -f     # confirm the streams came back up cleanly
```

If you edit `deploy/go2rtc-host.yaml` in the repo and want it to take effect
on the Pi, re-copy it over the installed config, then restart:

```bash
sudo cp deploy/go2rtc-host.yaml /etc/go2rtc.yaml
sudo systemctl restart go2rtc
```

### Config variants: with mic vs. video-only

`deploy/go2rtc-host.yaml` ships with a `device_audio` stream (ALSA mic via
`exec: ffmpeg … -f alsa`) that the `babycam` composite mixes in with `-c:a
copy`. On a camera-only deployment (no mic wired up, or a deliberate
video-only setup — see `BBWATCH_AUDIO_SOURCE=disabled` in `docker/.env`,
described in `docker-compose.yml`), a hand-edited variant may be installed
instead that drops the `device_audio:` stream entirely and the corresponding
second `-i` / `-c:a copy` in the `babycam` composite.

To check which variant is currently installed on the Pi:

```bash
grep -A1 'device_audio:' /etc/go2rtc.yaml
```

No output means the video-only variant is installed. Keep this in sync with
the bbwatch side: going video-only in `/etc/go2rtc.yaml` without also setting
`BBWATCH_AUDIO_SOURCE=disabled` in `docker/.env` leaves bbwatch retrying a
mic-shaped RTSP audio stream that no longer carries audio.

### Troubleshooting

**Service fails to start, or restarts continuously**
- Check whether the camera is held by another process: `sudo systemctl stop
  go2rtc` then run `libcamera-hello --list-cameras` by hand — if that also
  fails with "Cannot access camera", something else (a stray go2rtc process,
  a manual `rpicam-vid` test, a leftover container) still holds the lock. Kill
  it, then restart go2rtc.
- Verify the `rpicam-vid` binary exists and is on `PATH`: `which rpicam-vid`.
  `deploy/go2rtc-host.yaml` assumes `rpicam-vid`; older/alternate images may
  only ship `libcamera-vid`, which needs the stream's `exec:` command updated
  to match.
- Read the actual startup error from the journal: `journalctl -u go2rtc -n 100`.

**Stream 404s or is empty in VLC / the web UI**
- go2rtc surfaces its `exec:` subprocess's stderr as `WRN` log lines — check
  `journalctl -u go2rtc -n 100` around the time of the failed request for the
  real ffmpeg/rpicam-vid error (bad device path, unsupported resolution,
  etc.), not just a generic "stream not found".
- If specifically the `babycam` composite stream 404s or hangs, confirm the
  overlay FIFO exists and bbwatch's `OverlayGenerator` is running — go2rtc's
  ffmpeg process blocks opening the FIFO input until a writer connects (see
  [CLAUDE.md](../CLAUDE.md) "Key constraints").

**Stream freezes/goes blank after restarting bbwatch (but bbwatch's own logs look fine)**
- Restarting bbwatch alone breaks the overlay composite. go2rtc's `babycam`
  ffmpeg process reads the overlay FIFO as one of its inputs; when
  bbwatch's old process exits, its writer fd closes, ffmpeg sees EOF on
  that input, and — unlike a fresh startup, where it blocks waiting for a
  writer — it does **not** resume reading once bbwatch reopens the pipe.
  The video composite stalls silently: bbwatch's `LATENCY overlay` log
  line will show `dropped=` climbing toward 100% (render succeeds, but the
  write-readiness check never finds the pipe writable again), while every
  other bbwatch log line looks completely healthy — nothing in bbwatch's
  own process is actually broken.
- Fix: restart go2rtc too. `make restart` (run from the repo root) does
  both together for exactly this reason — prefer it over
  `docker compose restart bbwatch` alone whenever you restart bbwatch for
  any reason (picking up a code change, a config edit, testing a fix).
- To confirm this is what's happening rather than something else: `docker
  compose -f docker/docker-compose.yml logs --tail=20 bbwatch | grep
  LATENCY.*overlay` — a `dropped=` percentage stuck near 100% across
  several consecutive lines is the signature.

**Port already in use**
- `ss -tlnp | grep -E '8554|1984'` shows what's currently bound. A stray
  manually-started go2rtc process, or a Docker-based go2rtc left over from
  before switching to host-native mode, are the usual culprits. If the
  systemd-managed instance is already up and healthy
  (`systemctl status go2rtc`), just stop the extra stray process rather than
  restarting the service.

## Verification

### Check device detection

```bash
make devices
```

Expected output:
```
=== AUDIO DEVICES ===
[hw:0,0] Built-in Audio

=== VIDEO DEVICES ===
=== PICAMERA ===
[rpicam:0] Pi Camera 0 (imx708)
```

If the Pi Camera is **not listed**:
- Check ribbon cable is fully seated
- Run `libcamera-hello --list-cameras` directly
- Check kernel logs: `dmesg | grep -i imx708`

### Access the live stream

Navigate to:
```
http://<pi-hostname>:1984
```

Or if on the Pi itself:
```
http://localhost:1984
```

Click **"babycam"** to view the live video stream with cry detection overlay.

## How Auto-Detection Works

When bbwatch starts on RPi5:

1. **Device Discovery** — `detect_picamera_devices()` calls `libcamera-hello --list-cameras`
2. **Parse Output** — Extracts camera index (e.g., `0`) and name (e.g., `imx708`)
3. **Create Device Path** — Maps to `VideoDevice(path="rpicam:0", ...)`
4. **Preference Ranking** — `get_preferred_video_device()` selects rpicam over USB cameras
5. **RTSP Restream** — `main.py` converts `rpicam:0` → `rtsp://localhost:8554/raw_video`

**Why RTSP?** OpenCV cannot open `rpicam:0` directly — the hardware is locked by go2rtc (which holds the camera and re-exposes it via RTSP). The MotionDetector consumes the RTSP restream instead.

## Configuration

- Video comes from go2rtc's host-native `rpicam-vid` exec stream
  (`deploy/go2rtc-host.yaml`'s `device_video`), not a Docker-managed
  camera source.
- Audio comes from ALSA directly (`device_audio`'s `ffmpeg -f alsa`), not
  PulseAudio — see "Config variants: with mic vs. video-only" above for
  the camera-only case.
- bbwatch (in Docker) reaches both via go2rtc's RTSP streams; motion
  detection uses `raw_video`, `config.yaml`'s `alerts.stream_url` uses
  `babycam`.

### Optional: Adjust resolution

Resolution is set in `deploy/go2rtc-host.yaml`'s `device_video` stream
(`rpicam-vid --width/--height`), not in a `docker/go2rtc.yaml` — see
"Editing the config — two separate config surfaces" above: changing the
*installed* `/etc/go2rtc.yaml` requires re-copying and restarting, not
just editing the repo file.

Also update `alerts.overlay_width`/`overlay_height` in `config.yaml` and
the matching `-video_size`/`-framerate` in the `babycam` stream's `exec:`
command to the same resolution — see
[docs/latency.md](latency.md#pipeline-3--overlay-compositing) for why they
must all agree.

Restart: `make restart` (restarts both bbwatch and go2rtc — see the
"Stream freezes/goes blank" troubleshooting entry above for why both are
needed).

## Stream URLs

After startup, the following streams are available:

| Stream | URL | Purpose |
|--------|-----|---------|
| **babycam** (overlaid) | `rtsp://localhost:8554/babycam` | Web UI, shows cry detection status |
| **raw_video** | `rtsp://localhost:8554/raw_video` | Motion detection input (internal) |

Use VLC to test the RTSP streams:
```bash
vlc rtsp://localhost:8554/babycam
```

## Storage Management

WAV segments and alert clips are stored in the `data_dir` (default: `/app/data/`):

```
data_dir/
├── audio_segments/     # Rolling WAV buffer (auto-rotates, max 1GB)
├── alerts/
│   ├── screenshots/    # Cry detection screenshot JPEGs
│   └── clips/          # Cry detection clip MP4s
└── overlays/           # FIFO pipe for video overlay
```

The storage manager automatically deletes oldest WAV segments when max size is reached. Alert clips and screenshots are kept indefinitely — clean them manually as needed.

## Troubleshooting

### Pi Camera not detected

**Symptom:** `make devices` shows no PICAMERA section

**Solutions:**
1. Verify ribbon cable is fully seated (power off, reseat, reboot)
2. Check kernel logs: `dmesg | tail -50 | grep -i camera`
3. Try `libcamera-hello --list-cameras` directly
4. If error "Cannot access camera: File exists", another process has the lock — restart: `systemctl reboot`

### Motion detection offline

**Symptom:** No motion indicator on web UI

**Solutions:**
1. Check go2rtc is running: `docker ps | grep go2rtc`
2. Test RTSP stream in VLC: `vlc rtsp://localhost:8554/raw_video`
3. Check bbwatch logs: `docker logs bbwatch`
4. Restart stack: `docker compose down && docker compose up -d`

### High CPU or thermal throttling

**Solutions:**
1. Reduce resolution in `go2rtc.yaml` (see "Configuration" above)
2. Reduce `alerts.overlay_fps` in `config.yaml` from 5 to 2–3
3. Add active cooling (heatsink or case with fan)
4. Monitor temp: `vcgencmd measure_temp` (target: <80°C)

### Stream URL not accessible from network

**Symptom:** Can access `localhost:1984` on Pi, but not from phone/computer

**Solutions:**
1. Check firewall allows port 1984 (or use mDNS hostname)
2. Use mDNS name instead of IP: `http://<pi-hostname>.local:1984` (if set in Pi Imager)
3. Forward port via router if accessing from outside network (security warning: opens stream to internet)
4. Use VPN instead of port forwarding for remote access

### PulseAudio audio not working (Docker audio input)

**Symptom:** `make devices` shows no audio devices

**Solutions:**
1. Ensure PulseAudio is running on host: `pactl info`
2. Check volume levels: `alsamixer`
3. Verify Docker has PulseAudio socket mounted in `docker-compose.yml` (should be auto-configured)
4. Restart PulseAudio: `systemctl --user restart pulseaudio`

## Testing without Real Hardware (CI/Development)

Use the `--fake-hardware` flag to bypass device detection on non-RPi hosts:

```bash
# Run tests locally
poetry run pytest tests/ --fake-hardware

# Or start dev server with fake audio/video (generates synthetic data)
poetry run python bbwatch/main.py --fake-hardware
```

This is useful for development and CI pipelines where RPi5 hardware is unavailable.

## Next Steps

1. Let cry detection run for a few days to collect audio baseline
2. Adjust `detection.rms_threshold` in `config.yaml` if false positives occur
3. Set up remote access if needed (port forward, VPN, or cloud reverse tunnel)
4. Monitor disk usage — WAV segments auto-rotate but alert clips persist

## Resources

- [libcamera Documentation](https://libcamera.org/)
- [go2rtc GitHub](https://github.com/AlexxIT/go2rtc)
- [Raspberry Pi Camera Documentation](https://www.raspberrypi.com/documentation/accessories/camera.html)
- bbwatch [CLAUDE.md](../CLAUDE.md) — architecture and development guide
