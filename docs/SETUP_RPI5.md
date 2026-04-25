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

### 2. Build ARM64 Docker image

```bash
make docker-build-rpi
```

This cross-compiles the bbwatch image for ARM64. It will take ~5 minutes on your build host.

### 3. Deploy to RPi5

On the Raspberry Pi:
```bash
# Pull the built image (substitute your registry)
docker pull your-registry/bbwatch:rpi-latest

# Or build directly on the Pi (takes longer but uses exact hardware):
make docker-build-rpi
```

### 4. Start the stack

```bash
docker compose up -d
```

This starts both `bbwatch` (cry detection) and `go2rtc` (video streaming).

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

No manual config changes are needed. The `config.yaml` defaults work out-of-the-box:

- Audio is pulled from the Docker container's PulseAudio sink (from host if configured)
- Video is auto-detected and routed to motion detection via RTSP
- go2rtc automatically starts the `rpicam:0` source on container startup

### Optional: Adjust resolution

If RPi5 is running hot or you want lower bandwidth, reduce resolution in `docker/go2rtc.yaml`:

```yaml
device_video:
  - rpicam:0#width=1280&height=720&fps=30&codec=h264
```

Restart: `docker compose restart go2rtc`

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
