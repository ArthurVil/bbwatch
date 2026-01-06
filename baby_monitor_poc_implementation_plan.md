# 🍼 Baby Monitor PoC – Implementation Plan

This document describes a **fast, efficient, fully FOSS Proof of Concept (PoC)** for a baby monitor built with:
- Raspberry Pi 4
- USB webcam + microphone
- Android smartphone on the same Wi‑Fi

The PoC prioritizes **speed of implementation, reliability, and clarity**, with a clean upgrade path toward real‑time ML inference.

---

## 🎯 PoC Goals (Definition of Done)

The PoC is considered complete when:

1. ✅ Live video from the Raspberry Pi is viewable on an Android phone (VLC or web browser).
2. ✅ Audio is continuously recorded into short `.wav` segments with sliding window overlap.
3. ✅ A Python script detects baby‑cry‑like audio using bandpass filtering + energy detection.
4. ✅ **Visual alerts** are displayed as overlays on the video stream:
   - 🔴 **Red overlay** → Cry / loud noise detected
   - 🔵 **Blue overlay** → Signal lost / system error
5. ✅ Disk space is managed automatically (max 1 GB, configurable).
6. ✅ Everything runs locally on the same Wi‑Fi using only FOSS tools.

---

## 🧱 System Architecture (PoC)

```
┌─────────────────────────────────────────────────────────────────┐
│                        RASPBERRY PI 4                          │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  USB Camera ─┬─► go2rtc ──────────────────► VLC / Browser       │
│              │      ▲                          (video)          │
│  USB Mic(s) ─┘      │                                           │
│       │             │ overlay control                           │
│       ▼             │ (red/blue/none)                           │
│  ffmpeg ────► .wav segments ──► Python detector ────────────────┘
│  (3s sliding)           │              │
│                         ▼              ▼
│                   disk manager    status.json
│                   (max 1GB)       (read by overlay)
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

---

## 📦 Components Overview

| Component | Tool | Reason |
|-----------|------|--------|
| Video + audio streaming | **go2rtc** | Zero‑config WebRTC + RTSP, supports overlays |
| UI | **VLC / Browser** | No custom app needed |
| Audio capture | **ffmpeg** | Reliable segmentation with overlap |
| Cry detection | **Python + scipy** | Bandpass filter + RMS energy |
| Visual alerts | **FFmpeg drawtext/overlay** or **go2rtc exec** | Overlay on video stream |
| Disk management | **Python** | Delete empty segments, enforce 1 GB limit |
| Startup | Manual → systemd | Incremental deployment |

---

## � Alert System Design

### Trigger Sources

Alerts are triggered by **either**:
1. **Sound detection** (PoC): Audio RMS in baby-cry frequency band exceeds threshold
2. **Motion detection** (Post-PoC): Frame difference exceeds threshold

### Hysteresis Triggering

To prevent rapid on/off oscillation when intensity hovers around the threshold:

```
intensity
    ▲
    │       ┌──────────┐  
    │       │  ALERT   │  trigger_high (0.03)
    │ ──────┼──────────┼───────────────────
    │       │          │  
    │       │  STABLE  │  trigger_low (0.015)
    │ ──────┴──────────┴───────────────────
    │                     
    └─────────────────────────────► time
    
    Alert ACTIVATES when intensity rises ABOVE trigger_high
    Alert CLEARS when intensity falls BELOW trigger_low
```

### Alert Actions

When an alert **activates** (intensity crosses `trigger_high` from below):

1. **🔔 Visual Overlay** → Red tint on video stream (immediate feedback)
2. **📸 Screenshot Capture** → Save JPEG frame at peak detection
3. **🎬 Clip Recording** → Start recording N seconds of video
4. **📊 Log to Timeline** → Store event timestamp + intensity for daily chart

When alert **clears** (intensity crosses `trigger_low` from above):

1. **Visual Overlay** → Remove red tint (return to normal)
2. **Clip Recording** → Finalize and save video clip
3. **Log to Timeline** → Mark event end timestamp

### State Machine

```python
class AlertState(Enum):
    IDLE = "idle"           # No alert, overlay = none
    TRIGGERED = "triggered" # Alert active, overlay = red, recording
    COOLDOWN = "cooldown"   # Recently triggered, waiting before next record

# State transitions
IDLE + (intensity > trigger_high) → TRIGGERED
TRIGGERED + (intensity < trigger_low) → COOLDOWN  
COOLDOWN + (time > cooldown_s) → IDLE
```

### Alert Outputs

| Output | Location | Purpose |
|--------|----------|---------|
| Visual overlay | Video stream | Real-time feedback when watching |
| Screenshot | `~/bbwatch_data/screenshots/` | Quick reference of alert moment |
| Video clip | `~/bbwatch_data/clips/` | Review what triggered the alert |
| Timeline data | SQLite DB | Daily activity chart generation |

---

## �🔧 Configuration File

`~/baby_poc/config.yaml`

```yaml
# Audio capture
audio:
  segment_duration_s: 3
  overlap_s: 1
  sample_rate: 16000
  channels: 1

# Detection thresholds
detection:
  bandpass_low_hz: 250
  bandpass_high_hz: 800
  rms_threshold: 0.02  # Tune based on environment
  min_active_ratio: 0.3  # 30% of segment must be active

# Alert system with hysteresis
# Alert triggers when intensity EXCEEDS trigger_high
# Alert clears when intensity DROPS BELOW trigger_low
# This prevents rapid on/off oscillation
alerts:
  trigger_high: 0.03      # Trigger alert when RMS > this
  trigger_low: 0.015      # Clear alert when RMS < this (hysteresis)
  status_file: "~/baby_poc/status.json"
  cooldown_s: 5           # Min seconds between alert recordings
  
  # Recording on alert
  record_clip_s: 10       # Seconds of video to save on alert
  screenshot_on_peak: true
  clips_dir: "~/baby_poc/clips"
  screenshots_dir: "~/baby_poc/screenshots"

# Disk management
storage:
  wav_dir: "~/baby_poc/wav_segments"
  max_size_mb: 1024
  delete_empty_segments: true
```

---

## 🧩 Phase 1 — Environment Setup

### 1.1 Install system packages

```bash
sudo apt update
sudo apt install -y \
  ffmpeg \
  v4l-utils \
  alsa-utils \
  python3-pip \
  python3-venv
```

### 1.2 Create project structure

```bash
mkdir -p ~/baby_poc/{wav_segments,logs}
cd ~/baby_poc
python3 -m venv .venv
source .venv/bin/activate
```

### 1.3 Install Python dependencies

```bash
pip install \
  watchdog \
  numpy \
  scipy \
  soundfile \
  pyyaml \
  pydantic
```

---

## 🔍 Phase 2 — Hardware Detection

### 2.1 Detect and log available devices at startup

Create `~/baby_poc/bbwatch/hardware.py`:

```python
"""Hardware detection and logging for baby monitor."""

import subprocess
import logging
from dataclasses import dataclass

LOGGER = logging.getLogger(__name__)


@dataclass
class AudioDevice:
    card: int
    device: int
    name: str
    alsa_id: str  # e.g., "hw:1,0"


@dataclass
class VideoDevice:
    path: str  # e.g., "/dev/video0"
    name: str
    capabilities: list[str]


def detect_audio_devices() -> list[AudioDevice]:
    """Detect all available ALSA audio capture devices."""
    result = subprocess.run(
        ["arecord", "-l"],
        capture_output=True,
        text=True
    )
    devices = []
    for line in result.stdout.splitlines():
        if line.startswith("card"):
            # Parse: "card 1: Device [USB Audio], device 0: USB Audio [USB Audio]"
            parts = line.split(":")
            card = int(parts[0].split()[1])
            device = int(parts[1].split()[1].rstrip(","))
            name = line.split("[")[1].split("]")[0] if "[" in line else "Unknown"
            devices.append(AudioDevice(
                card=card,
                device=device,
                name=name,
                alsa_id=f"hw:{card},{device}"
            ))
    return devices


def detect_video_devices() -> list[VideoDevice]:
    """Detect all available V4L2 video devices."""
    result = subprocess.run(
        ["v4l2-ctl", "--list-devices"],
        capture_output=True,
        text=True
    )
    devices = []
    current_name = ""
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.endswith(":"):
            current_name = line.rstrip(":")
        elif line.startswith("/dev/video"):
            devices.append(VideoDevice(
                path=line,
                name=current_name,
                capabilities=[]  # Extended later if needed
            ))
    return devices


def log_detected_hardware() -> tuple[list[AudioDevice], list[VideoDevice]]:
    """Detect and log all hardware at startup."""
    audio_devices = detect_audio_devices()
    video_devices = detect_video_devices()

    LOGGER.info("=== Detected Audio Devices ===")
    for dev in audio_devices:
        LOGGER.info(f"  [{dev.alsa_id}] {dev.name}")

    LOGGER.info("=== Detected Video Devices ===")
    for dev in video_devices:
        LOGGER.info(f"  [{dev.path}] {dev.name}")

    if not audio_devices:
        LOGGER.warning("No audio capture devices found!")
    if not video_devices:
        LOGGER.warning("No video capture devices found!")

    return audio_devices, video_devices
```

---

## 🎥 Phase 3 — Video Streaming with Overlay Support

### 3.1 Install go2rtc

```bash
wget https://github.com/AlexxIT/go2rtc/releases/latest/download/go2rtc_linux_arm64 -O go2rtc
chmod +x go2rtc
sudo mv go2rtc /usr/local/bin/
```

### 3.2 Configuration with audio input

`/etc/go2rtc.yaml`

```yaml
streams:
  # Main stream: video + audio from webcam
  babycam:
    - "ffmpeg:device?video=/dev/video0&audio=hw:1,0#video=h264#audio=opus"

  # Alternative: raw stream for lower latency
  babycam_raw:
    - "v4l2:/dev/video0"

api:
  listen: ":1984"

webrtc:
  listen: ":8555"
```

> **Note:** Adjust `hw:1,0` based on output from Phase 2 hardware detection.

### 3.3 Overlay mechanism

For visual alerts, we use **FFmpeg filter injection** via go2rtc's exec source:

`/etc/go2rtc.yaml` (extended):

```yaml
streams:
  # Base stream without overlay
  babycam_raw:
    - "ffmpeg:device?video=/dev/video0&audio=hw:1,0"

  # Stream with dynamic overlay based on status file
  babycam:
    - "exec:ffmpeg -i /dev/video0 -f alsa -i hw:1,0 \
        -vf \"drawbox=x=0:y=0:w=iw:h=ih:color=red@0.3:t=fill:enable='eq(ld(0), 1)':e='st(0, gte(time, 0))' \" \
        -c:v libx264 -preset ultrafast -tune zerolatency \
        -c:a aac -f rtsp rtsp://localhost:8554/babycam_overlay"
```

> **Simplified approach for PoC:** We'll use a status-polling overlay with periodic refresh, implemented in Phase 5.

### 3.4 Run and test

```bash
go2rtc serve
```

**Access from Android:**
- Browser: `http://<PI_IP>:1984`
- VLC: `rtsp://<PI_IP>:8554/babycam`

✔️ *Video + audio working = first milestone*

---

## 🎤 Phase 4 — Audio Capture with Sliding Window

### 4.1 Sliding window segmentation

To achieve 3s segments with 1s overlap, we run **two ffmpeg processes** offset by 1 second, or use a more elegant pipe-based approach.

**Simple approach (PoC):**

```bash
# Terminal 1: segments at t=0, t=3, t=6, ...
ffmpeg -f alsa -i hw:1,0 \
  -ac 1 -ar 16000 \
  -f segment -segment_time 3 -segment_format wav \
  ~/baby_poc/wav_segments/seg_a_%05d.wav &

# Terminal 2: segments at t=1, t=4, t=7, ... (start 1s later)
sleep 1
ffmpeg -f alsa -i hw:1,0 \
  -ac 1 -ar 16000 \
  -f segment -segment_time 3 -segment_format wav \
  ~/baby_poc/wav_segments/seg_b_%05d.wav &

# Terminal 3: segments at t=2, t=5, t=8, ... (start 2s later)
sleep 2
ffmpeg -f alsa -i hw:1,0 \
  -ac 1 -ar 16000 \
  -f segment -segment_time 3 -segment_format wav \
  ~/baby_poc/wav_segments/seg_c_%05d.wav &
```

This gives us a new segment to analyze **every 1 second** with 3s of context.

### 4.2 Capture wrapper script

Create `~/baby_poc/bbwatch/capture.py` to manage the ffmpeg processes and handle graceful shutdown.

✔️ *Sliding window audio capture working = second milestone*

---

## 🔊 Phase 5 — Cry Detection with Bandpass Filter

### 5.1 Detection algorithm

```python
"""Cry detection using bandpass filter + RMS energy."""

import numpy as np
from scipy.signal import butter, sosfilt
import soundfile as sf
import logging

LOGGER = logging.getLogger(__name__)


def butter_bandpass(lowcut: float, highcut: float, fs: int, order: int = 4) -> np.ndarray:
    """Design a Butterworth bandpass filter."""
    nyq = 0.5 * fs
    low = lowcut / nyq
    high = highcut / nyq
    sos = butter(order, [low, high], btype="band", output="sos")
    return sos


def detect_cry(
    wav_path: str,
    lowcut: float = 250.0,
    highcut: float = 800.0,
    rms_threshold: float = 0.02,
    min_active_ratio: float = 0.3,
) -> tuple[bool, float, float]:
    """Detect baby cry in audio segment.

    Args:
        wav_path: Path to .wav file
        lowcut: Low frequency cutoff (Hz) for bandpass
        highcut: High frequency cutoff (Hz) for bandpass
        rms_threshold: RMS threshold for activity detection
        min_active_ratio: Minimum ratio of active frames to trigger alert

    Returns:
        Tuple of (is_cry_detected, filtered_rms, active_ratio)
    """
    # Load audio
    audio, sr = sf.read(wav_path)

    if len(audio) == 0:
        return False, 0.0, 0.0

    # Apply bandpass filter (baby cry range: ~250-800 Hz)
    sos = butter_bandpass(lowcut, highcut, sr)
    filtered = sosfilt(sos, audio)

    # Compute RMS in windows
    window_size = int(sr * 0.1)  # 100ms windows
    n_windows = len(filtered) // window_size
    rms_values = []

    for i in range(n_windows):
        window = filtered[i * window_size : (i + 1) * window_size]
        rms = np.sqrt(np.mean(window**2))
        rms_values.append(rms)

    if not rms_values:
        return False, 0.0, 0.0

    # Calculate metrics
    filtered_rms = np.mean(rms_values)
    active_frames = sum(1 for rms in rms_values if rms > rms_threshold)
    active_ratio = active_frames / len(rms_values)

    is_cry = active_ratio >= min_active_ratio

    LOGGER.debug(
        f"Detection: rms={filtered_rms:.4f}, active_ratio={active_ratio:.2f}, cry={is_cry}"
    )

    return is_cry, filtered_rms, active_ratio


def is_segment_empty(
    wav_path: str,
    silence_threshold: float = 0.001,
) -> bool:
    """Check if a segment is essentially silent (for deletion)."""
    audio, _ = sf.read(wav_path)
    if len(audio) == 0:
        return True
    rms = np.sqrt(np.mean(audio**2))
    return rms < silence_threshold
```

### 5.2 Directory watcher with status updates

```python
"""Watch for new audio segments and run detection."""

import json
import time
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import logging

LOGGER = logging.getLogger(__name__)


class SegmentHandler(FileSystemEventHandler):
    """Handle new .wav segment files."""

    def __init__(self, config: dict, status_file: Path):
        self.config = config
        self.status_file = status_file
        self.last_alert_time = 0
        self.cooldown = config.get("alert_cooldown_s", 5)

    def on_created(self, event):
        if not event.src_path.endswith(".wav"):
            return

        # Wait for file to be fully written
        time.sleep(0.5)

        wav_path = event.src_path

        # Check if empty (delete if configured)
        if is_segment_empty(wav_path):
            if self.config.get("delete_empty_segments", True):
                Path(wav_path).unlink()
                LOGGER.debug(f"Deleted empty segment: {wav_path}")
            return

        # Run detection
        is_cry, rms, ratio = detect_cry(
            wav_path,
            lowcut=self.config.get("bandpass_low_hz", 250),
            highcut=self.config.get("bandpass_high_hz", 800),
            rms_threshold=self.config.get("rms_threshold", 0.02),
            min_active_ratio=self.config.get("min_active_ratio", 0.3),
        )

        # Update status file (read by overlay)
        current_time = time.time()
        status = {
            "timestamp": current_time,
            "cry_detected": is_cry,
            "rms": rms,
            "active_ratio": ratio,
            "last_segment": Path(wav_path).name,
        }

        if is_cry and (current_time - self.last_alert_time) > self.cooldown:
            LOGGER.warning(f"[ALERT] Cry detected in {Path(wav_path).name}")
            self.last_alert_time = current_time
            status["alert_active"] = True
        else:
            status["alert_active"] = False

        self.status_file.write_text(json.dumps(status, indent=2))
```

✔️ *Detection with bandpass filter working = third milestone*

---

## 📣 Phase 6 — Visual Overlay Alerts

### 6.1 Status file format

`~/baby_poc/status.json` (updated by detector):

```json
{
  "timestamp": 1704488400.0,
  "cry_detected": true,
  "alert_active": true,
  "rms": 0.045,
  "active_ratio": 0.67,
  "last_segment": "seg_a_00042.wav",
  "system_status": "ok"
}
```

### 6.2 Overlay strategy (PoC approach)

For the PoC, we use a **status polling overlay** via go2rtc + FFmpeg:

1. A Python process monitors `status.json`
2. When alert state changes, it updates an **overlay image** (transparent PNG with red/blue tint)
3. go2rtc's FFmpeg source composites this overlay onto the video

**Overlay images:**
- `~/baby_poc/overlays/none.png` — fully transparent
- `~/baby_poc/overlays/red.png` — red tint at 30% opacity
- `~/baby_poc/overlays/blue.png` — blue tint at 30% opacity

**go2rtc config with overlay:**

```yaml
streams:
  babycam:
    - "exec:ffmpeg -i /dev/video0 -f alsa -i hw:1,0 \
        -i ~/baby_poc/overlays/current.png \
        -filter_complex '[0:v][2:v]overlay=0:0' \
        -c:v libx264 -preset ultrafast -tune zerolatency \
        -c:a aac -f flv rtmp://localhost/live/babycam"
```

### 6.3 Signal loss detection

The detector also monitors for:
- No new `.wav` files for > 10 seconds → **blue overlay** (capture failure)
- go2rtc process not responding → **blue overlay** (stream failure)

```python
def check_system_health(wav_dir: Path, timeout_s: float = 10.0) -> str:
    """Check if audio capture is healthy."""
    wav_files = sorted(wav_dir.glob("*.wav"), key=lambda p: p.stat().st_mtime)
    if not wav_files:
        return "no_audio"

    latest_mtime = wav_files[-1].stat().st_mtime
    if time.time() - latest_mtime > timeout_s:
        return "stale_audio"

    return "ok"
```

✔️ *Visual alerts working = fourth milestone*

---

## 💾 Phase 7 — Disk Management

### 7.1 Storage manager

```python
"""Manage disk usage for audio segments."""

import os
from pathlib import Path
import logging

LOGGER = logging.getLogger(__name__)


def get_directory_size_mb(path: Path) -> float:
    """Get total size of directory in MB."""
    total = sum(f.stat().st_size for f in path.glob("*.wav") if f.is_file())
    return total / (1024 * 1024)


def enforce_storage_limit(wav_dir: Path, max_size_mb: float = 1024.0) -> int:
    """Delete oldest segments to stay under storage limit.

    Args:
        wav_dir: Directory containing .wav segments
        max_size_mb: Maximum allowed size in MB

    Returns:
        Number of files deleted
    """
    deleted = 0
    current_size = get_directory_size_mb(wav_dir)

    if current_size <= max_size_mb:
        return 0

    # Sort by modification time (oldest first)
    wav_files = sorted(wav_dir.glob("*.wav"), key=lambda p: p.stat().st_mtime)

    for wav_file in wav_files:
        if current_size <= max_size_mb * 0.9:  # Target 90% to avoid frequent cleanup
            break

        file_size_mb = wav_file.stat().st_size / (1024 * 1024)
        wav_file.unlink()
        current_size -= file_size_mb
        deleted += 1
        LOGGER.debug(f"Deleted old segment: {wav_file.name}")

    if deleted > 0:
        LOGGER.info(f"Storage cleanup: deleted {deleted} files, now at {current_size:.1f} MB")

    return deleted
```

### 7.2 Periodic cleanup

Run cleanup every 60 seconds in the main detector loop, or as a separate thread.

✔️ *Disk management working = fifth milestone*

---

## 🧪 Phase 8 — Validation Checklist

| Test | Expected Result |
|------|-----------------|
| Video on phone | < 1s latency |
| Audio in video stream | Audible in VLC |
| Hardware detection | All devices logged at startup |
| Audio segments | New file every ~1s (sliding window) |
| Silence | No alerts, segments deleted |
| Cry / loud voice in 250-800 Hz | Red overlay appears |
| Capture failure | Blue overlay appears |
| Disk usage | Never exceeds 1 GB |
| Process restart | Continues cleanly |

---

## 📁 Project Structure

```
~/baby_poc/
├── .venv/                    # Python virtual environment
├── bbwatch/                  # Python package
│   ├── __init__.py
│   ├── config.py             # Configuration loading (Pydantic)
│   ├── hardware.py           # Device detection
│   ├── capture.py            # FFmpeg process management
│   ├── detector.py           # Cry detection logic
│   ├── storage.py            # Disk management
│   ├── overlay.py            # Overlay image generation
│   └── main.py               # Entry point
├── config.yaml               # User configuration
├── overlays/                 # Overlay images
│   ├── none.png
│   ├── red.png
│   ├── blue.png
│   └── current.png           # Symlink to active overlay
├── wav_segments/             # Audio segments (managed)
├── logs/                     # Application logs
└── status.json               # Current detection status
```

---

## 🚦 Explicitly Out of Scope for the PoC

- ❌ ROS2
- ❌ Machine learning models (using simple DSP instead)
- ❌ Cloud connectivity
- ❌ WAN / NAT traversal
- ❌ Mobile app (using VLC/browser)
- ❌ Audio/video synchronization (beyond go2rtc defaults)
- ❌ Multi-room support

This is intentional to keep the PoC fast and robust.

---

## 🔜 Post‑PoC Upgrade Path (Next Phases)

### Phase 1: Core Improvements
1. Replace disk‑based `.wav` with **GStreamer appsink** for zero-disk latency
2. Add baby‑cry classifier (ONNX / TFLite) for better accuracy
3. Replace overlay mechanism with WebRTC data channel for instant updates

### Phase 1.5: Hardware Evolution 📷
4. **Night Vision Upgrade**
   - Initial PoC: Standard USB Webcam (RGB + Mic)
   - Upgrade: **RGB-IR Camera** (e.g., RPi NoIR or USB IR cam) for robust night monitoring.


### Phase 2: Timeline & Screenshots 📸
> **User Request**: Every 12h, generate a 24h timeline visualization + capture screenshots at peaks.

4. **Activity Timeline Generator**
   - Generate PNG/SVG every 12 hours showing last 24h
   - Line 1: Audio intensity (RMS over time)
   - Line 2: Motion detection intensity (requires frame diff implementation)
   - X-axis: time, Y-axis: intensity
   - Save to `~/bbwatch_data/timelines/YYYY-MM-DD_HH.png`
   - **Dataset Collection Mode**: Option to save raw data (labeled audio segments + video frames) to build a training set for future ML models.


5. **Motion Detection** (needed for timeline)
   - Simple frame differencing using OpenCV
   - Store motion intensity alongside audio metrics
   - Threshold configurable in `config.yaml`

6. **Peak Screenshot Capture**
   - When audio or motion exceeds threshold, capture JPEG from video stream
   - Store in `~/bbwatch_data/screenshots/YYYY-MM-DD_HH-MM-SS.jpg`
   - Link screenshots to timeline events

7. **Data Persistence**
   - SQLite database for metrics history (audio RMS, motion intensity, timestamps)
   - Configurable retention (e.g., 7 days)
   - Query API for timeline generation

### Phase 3: Notifications & Remote Access
8. Trigger video recording on alert (save clips)
9. Push notifications via ntfy.sh or similar
10. Secure remote access (WireGuard + HTTPS)
11. Home Assistant integration
12. **Baby Tracking (DeepSort)**
    - Implement lightweight visual tracking (OpenCV + DeepSort or similar) on RPi.
    - Track baby movement and posture within the crib.


---

## 🧠 Key Design Rationale

- **go2rtc** removes all WebRTC complexity and handles audio+video muxing
- **ffmpeg sliding window** gives us 1s response time with 3s context
- **Bandpass filter (250-800 Hz)** targets baby cry fundamental frequency
- **Disk I/O is acceptable for PoC** and simplifies debugging
- **Visual overlays** provide immediate feedback without custom apps
- **Status file** decouples detection from visualization
- Each component is independently replaceable and testable

---

## ✅ Final PoC Deliverables

1. Live baby video + audio stream to Android phone
2. Hardware auto-detection with startup logging
3. Sliding window audio capture (3s segments, 1s overlap)
4. Cry detection using bandpass filter + energy analysis
5. Visual alerts (red = cry, blue = system error)
6. Automatic disk management (max 1 GB)
7. Clean migration path to real‑time inference

---

## � Development Environment (Docker)

### Why Docker for Development?

- **Consistency**: Same environment on dev machine and RPi
- **No host pollution**: Dependencies isolated from your system
- **Cross-platform**: Develop on Linux/macOS/Windows, deploy to ARM
- **Reproducibility**: `Dockerfile` documents exact setup

### Project Structure (Updated)

```
bbwatch/
├── .github/
│   └── workflows/
│       └── ci.yml                # GitHub Actions CI
├── docker/
│   ├── Dockerfile.dev            # Development image (x86_64)
│   ├── Dockerfile.rpi            # Raspberry Pi image (arm64)
│   └── docker-compose.yml        # Local dev with fake devices
├── bbwatch/                      # Python package
│   ├── __init__.py
│   ├── config.py
│   ├── hardware.py
│   ├── capture.py
│   ├── detector.py
│   ├── storage.py
│   ├── overlay.py
│   └── main.py
├── tests/
│   ├── conftest.py               # Pytest fixtures
│   ├── fixtures/                 # Test audio samples
│   │   ├── silence_3s.wav
│   │   ├── baby_cry_3s.wav
│   │   ├── adult_speech_3s.wav
│   │   └── white_noise_3s.wav
│   ├── unit/                     # Fast, isolated tests
│   │   ├── test_detector.py
│   │   ├── test_storage.py
│   │   └── test_config.py
│   ├── integration/              # Component interaction tests
│   │   ├── test_capture_detector.py
│   │   └── test_overlay_status.py
│   └── functional/               # End-to-end reliability tests
│       ├── test_long_running.py
│       ├── test_recovery.py
│       └── test_resource_limits.py
├── scripts/
│   ├── deploy.sh                 # Deploy to Raspberry Pi
│   ├── generate_test_audio.py    # Create test fixtures
│   └── calibrate.py              # Threshold calibration tool
├── config.yaml
├── pyproject.toml
├── requirements.txt
├── requirements-dev.txt
└── README.md
```

### Dockerfile.dev (Development)

```dockerfile
# docker/Dockerfile.dev
FROM python:3.11-slim-bookworm

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    alsa-utils \
    v4l-utils \
    libsndfile1 \
    pulseaudio \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python dependencies
COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-dev.txt

# Copy source
COPY . .

# Install package in editable mode
RUN pip install -e .

# Default command: run tests
CMD ["pytest", "-v", "--tb=short"]
```

### Dockerfile.rpi (Raspberry Pi Production)

```dockerfile
# docker/Dockerfile.rpi
FROM python:3.11-slim-bookworm

# System dependencies (ARM64-native)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    alsa-utils \
    v4l-utils \
    libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY bbwatch/ ./bbwatch/
COPY config.yaml ./

# Run as non-root
RUN useradd -m bbwatch && chown -R bbwatch:bbwatch /app
USER bbwatch

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import json; s=json.load(open('/app/status.json')); exit(0 if s.get('system_status')=='ok' else 1)"

CMD ["python", "-m", "bbwatch.main"]
```

### docker-compose.yml (Local Development)

```yaml
# docker/docker-compose.yml
version: '3.8'

services:
  bbwatch:
    build:
      context: ..
      dockerfile: docker/Dockerfile.dev
    volumes:
      - ../:/app
      - ./test_data:/app/wav_segments
    environment:
      - BBWATCH_ENV=development
      - BBWATCH_FAKE_HARDWARE=true
    ports:
      - "1984:1984"   # go2rtc web UI
      - "8554:8554"   # RTSP
    # For real hardware access (optional):
    # devices:
    #   - /dev/video0:/dev/video0
    #   - /dev/snd:/dev/snd
    command: pytest -v tests/

  # Fake audio generator for testing
  audio_generator:
    build:
      context: ..
      dockerfile: docker/Dockerfile.dev
    volumes:
      - ./test_data:/app/wav_segments
    command: python scripts/generate_test_audio.py --output /app/wav_segments --loop
```

### Development Commands

```bash
# Build development image
docker compose -f docker/docker-compose.yml build

# Run all tests
docker compose -f docker/docker-compose.yml run --rm bbwatch pytest -v

# Run specific test category
docker compose -f docker/docker-compose.yml run --rm bbwatch pytest tests/unit/ -v
docker compose -f docker/docker-compose.yml run --rm bbwatch pytest tests/functional/ -v

# Interactive development shell
docker compose -f docker/docker-compose.yml run --rm bbwatch bash

# Run with fake audio for manual testing
docker compose -f docker/docker-compose.yml up
```

---

## 🧪 Testing Strategy

### Testing Philosophy

> **This system watches a baby. It must be trustworthy.**

We implement **defense in depth** with multiple test layers:

| Layer | Purpose | Speed | Coverage |
|-------|---------|-------|----------|
| **Unit** | Verify algorithms in isolation | Fast (ms) | Detection logic, config parsing |
| **Integration** | Component interactions | Medium (s) | Capture→Detector, Status→Overlay |
| **Functional** | Real-world reliability | Slow (min) | Long-running, crash recovery, resource limits |

### Unit Tests

Fast, isolated tests for core algorithms.

```python
# tests/unit/test_detector.py
"""Unit tests for cry detection algorithm."""

import pytest
import numpy as np
from pathlib import Path

from bbwatch.detector import detect_cry, is_segment_empty, butter_bandpass


class TestBandpassFilter:
    """Tests for bandpass filter design."""

    def test_bandpass_coefficients_valid(self):
        """Filter coefficients should be finite and stable."""
        sos = butter_bandpass(250, 800, fs=16000, order=4)
        assert np.all(np.isfinite(sos))
        assert sos.shape[0] == 4  # 4th order = 4 second-order sections

    def test_bandpass_attenuates_low_frequencies(self):
        """Frequencies below cutoff should be attenuated."""
        fs = 16000
        sos = butter_bandpass(250, 800, fs=fs)

        # Generate 100 Hz sine (should be attenuated)
        t = np.linspace(0, 1, fs)
        low_freq = np.sin(2 * np.pi * 100 * t)
        filtered = sosfilt(sos, low_freq)

        assert np.std(filtered) < np.std(low_freq) * 0.1  # >90% attenuation


class TestCryDetection:
    """Tests for cry detection logic."""

    @pytest.fixture
    def fixtures_dir(self) -> Path:
        return Path(__file__).parent.parent / "fixtures"

    def test_silence_not_detected_as_cry(self, fixtures_dir):
        """Silent audio should never trigger an alert."""
        is_cry, rms, ratio = detect_cry(fixtures_dir / "silence_3s.wav")
        assert is_cry is False
        assert rms < 0.001
        assert ratio < 0.1

    def test_baby_cry_detected(self, fixtures_dir):
        """Baby cry audio should trigger an alert."""
        is_cry, rms, ratio = detect_cry(fixtures_dir / "baby_cry_3s.wav")
        assert is_cry is True
        assert ratio > 0.3

    def test_adult_speech_not_detected(self, fixtures_dir):
        """Adult speech should NOT trigger (different frequency profile)."""
        is_cry, rms, ratio = detect_cry(
            fixtures_dir / "adult_speech_3s.wav",
            lowcut=250,
            highcut=800,
        )
        # Adult fundamental is typically 85-180 Hz (male) or 165-255 Hz (female)
        # Should have reduced energy in baby cry band
        assert is_cry is False or ratio < 0.5

    def test_white_noise_not_detected(self, fixtures_dir):
        """Broadband noise should not reliably trigger."""
        is_cry, rms, ratio = detect_cry(fixtures_dir / "white_noise_3s.wav")
        # White noise has energy everywhere, but ratio should be lower
        # because it's distributed, not concentrated in cry band
        assert ratio < 0.7  # May need tuning


class TestSegmentEmpty:
    """Tests for empty segment detection."""

    def test_silent_segment_marked_empty(self, fixtures_dir):
        """Silent segments should be marked for deletion."""
        assert is_segment_empty(fixtures_dir / "silence_3s.wav") is True

    def test_audio_segment_not_empty(self, fixtures_dir):
        """Segments with audio should not be deleted."""
        assert is_segment_empty(fixtures_dir / "baby_cry_3s.wav") is False
```

### Integration Tests

Test component interactions.

```python
# tests/integration/test_capture_detector.py
"""Integration tests for capture → detection pipeline."""

import pytest
import tempfile
import time
from pathlib import Path
from unittest.mock import Mock, patch

from bbwatch.capture import AudioCapture
from bbwatch.detector import SegmentHandler


class TestCaptureDetectorIntegration:
    """Test that captured segments are processed correctly."""

    def test_new_segment_triggers_detection(self, tmp_path):
        """When a new .wav appears, detector processes it."""
        status_file = tmp_path / "status.json"
        handler = SegmentHandler(
            config={"delete_empty_segments": False},
            status_file=status_file,
        )

        # Simulate file creation event
        test_wav = tmp_path / "segment_001.wav"
        # Copy a fixture
        shutil.copy("tests/fixtures/baby_cry_3s.wav", test_wav)

        event = Mock()
        event.src_path = str(test_wav)

        handler.on_created(event)

        # Status file should be updated
        assert status_file.exists()
        status = json.loads(status_file.read_text())
        assert "cry_detected" in status
        assert status["cry_detected"] is True

    def test_empty_segment_deleted_when_configured(self, tmp_path):
        """Empty segments should be deleted if configured."""
        status_file = tmp_path / "status.json"
        handler = SegmentHandler(
            config={"delete_empty_segments": True},
            status_file=status_file,
        )

        test_wav = tmp_path / "segment_002.wav"
        shutil.copy("tests/fixtures/silence_3s.wav", test_wav)

        event = Mock()
        event.src_path = str(test_wav)

        handler.on_created(event)

        # File should be deleted
        assert not test_wav.exists()
```

### Functional Tests (Reliability)

Critical tests for production reliability.

```python
# tests/functional/test_long_running.py
"""Functional tests for long-running reliability."""

import pytest
import time
import threading
import psutil
from pathlib import Path


class TestLongRunning:
    """Tests that simulate extended operation."""

    @pytest.mark.slow
    def test_memory_stable_over_1000_segments(self, tmp_path):
        """Memory usage should not grow unboundedly."""
        from bbwatch.detector import SegmentHandler

        handler = SegmentHandler(
            config={"delete_empty_segments": True},
            status_file=tmp_path / "status.json",
        )

        process = psutil.Process()
        initial_memory = process.memory_info().rss

        # Process 1000 segments
        for i in range(1000):
            test_wav = tmp_path / f"segment_{i:05d}.wav"
            shutil.copy("tests/fixtures/baby_cry_3s.wav", test_wav)

            event = Mock()
            event.src_path = str(test_wav)
            handler.on_created(event)

            # Clean up to simulate real usage
            if test_wav.exists():
                test_wav.unlink()

        final_memory = process.memory_info().rss
        memory_growth = (final_memory - initial_memory) / (1024 * 1024)  # MB

        # Memory should not grow more than 50 MB
        assert memory_growth < 50, f"Memory grew by {memory_growth:.1f} MB"

    @pytest.mark.slow
    def test_no_file_descriptor_leak(self, tmp_path):
        """File descriptors should not leak over time."""
        from bbwatch.detector import SegmentHandler

        handler = SegmentHandler(
            config={"delete_empty_segments": True},
            status_file=tmp_path / "status.json",
        )

        process = psutil.Process()
        initial_fds = process.num_fds()

        for i in range(500):
            test_wav = tmp_path / f"segment_{i:05d}.wav"
            shutil.copy("tests/fixtures/silence_3s.wav", test_wav)

            event = Mock()
            event.src_path = str(test_wav)
            handler.on_created(event)

        final_fds = process.num_fds()

        # Should not leak more than 5 file descriptors
        assert final_fds - initial_fds < 5


# tests/functional/test_recovery.py
"""Tests for crash recovery and error handling."""

import pytest
import signal
import subprocess
import time


class TestRecovery:
    """Tests for system recovery from failures."""

    def test_recovers_from_missing_wav_dir(self, tmp_path):
        """System should recreate wav_segments if deleted."""
        from bbwatch.main import BabyMonitor

        wav_dir = tmp_path / "wav_segments"
        wav_dir.mkdir()

        monitor = BabyMonitor(config_path=tmp_path / "config.yaml")

        # Simulate directory deletion
        shutil.rmtree(wav_dir)

        # Monitor should detect and recover
        monitor._check_directories()

        assert wav_dir.exists()

    def test_recovers_from_corrupt_status_file(self, tmp_path):
        """System should handle corrupt status.json gracefully."""
        from bbwatch.detector import SegmentHandler

        status_file = tmp_path / "status.json"
        status_file.write_text("{ invalid json !")

        handler = SegmentHandler(
            config={},
            status_file=status_file,
        )

        # Should not crash, should overwrite with valid status
        test_wav = tmp_path / "segment.wav"
        shutil.copy("tests/fixtures/silence_3s.wav", test_wav)

        event = Mock()
        event.src_path = str(test_wav)

        # Should not raise
        handler.on_created(event)

        # Status should now be valid JSON
        status = json.loads(status_file.read_text())
        assert "timestamp" in status

    def test_handles_ffmpeg_crash(self, tmp_path):
        """System should restart capture if ffmpeg dies."""
        from bbwatch.capture import AudioCapture

        capture = AudioCapture(
            device="hw:1,0",
            output_dir=tmp_path,
            segment_duration=3,
        )

        capture.start()
        time.sleep(1)

        # Kill ffmpeg
        capture._process.kill()
        time.sleep(2)

        # Capture should detect and restart
        assert capture.is_running()

        capture.stop()


# tests/functional/test_resource_limits.py
"""Tests for resource limit enforcement."""

import pytest
from pathlib import Path


class TestResourceLimits:
    """Tests for disk and memory limits."""

    def test_disk_limit_enforced(self, tmp_path):
        """Disk usage should never exceed configured limit."""
        from bbwatch.storage import StorageManager

        manager = StorageManager(
            wav_dir=tmp_path,
            max_size_mb=10,  # Small limit for testing
        )

        # Create files that would exceed limit
        for i in range(100):
            wav_file = tmp_path / f"segment_{i:05d}.wav"
            # Create 1 MB file
            wav_file.write_bytes(b"\x00" * (1024 * 1024))

            manager.enforce_limit()

        # Total size should be under limit
        total_size = sum(f.stat().st_size for f in tmp_path.glob("*.wav"))
        assert total_size < 10 * 1024 * 1024

    def test_oldest_files_deleted_first(self, tmp_path):
        """When cleaning up, oldest files should be deleted first."""
        from bbwatch.storage import StorageManager
        import time

        manager = StorageManager(wav_dir=tmp_path, max_size_mb=2)

        # Create files with different ages
        files = []
        for i in range(5):
            wav_file = tmp_path / f"segment_{i:05d}.wav"
            wav_file.write_bytes(b"\x00" * (512 * 1024))  # 512 KB each
            files.append(wav_file)
            time.sleep(0.1)  # Ensure different mtimes

        # Total: 2.5 MB, limit: 2 MB
        manager.enforce_limit()

        # Oldest file should be deleted, newest should remain
        assert not files[0].exists()  # Oldest
        assert files[-1].exists()      # Newest
```

### Test Fixtures Generation

```python
# scripts/generate_test_audio.py
"""Generate test audio fixtures for testing."""

import numpy as np
import soundfile as sf
from pathlib import Path
import argparse


def generate_silence(path: Path, duration_s: float = 3.0, sr: int = 16000):
    """Generate silent audio file."""
    samples = np.zeros(int(duration_s * sr), dtype=np.float32)
    sf.write(path, samples, sr)


def generate_baby_cry(path: Path, duration_s: float = 3.0, sr: int = 16000):
    """Generate synthetic baby cry (fundamental 400-600 Hz with harmonics)."""
    t = np.linspace(0, duration_s, int(duration_s * sr))

    # Fundamental frequency modulation (crying is not monotone)
    f0 = 450 + 100 * np.sin(2 * np.pi * 2 * t)  # 450 Hz ± 100 Hz

    # Generate harmonics
    signal = np.zeros_like(t)
    for harmonic in [1, 2, 3, 4]:
        signal += (1 / harmonic) * np.sin(2 * np.pi * f0 * harmonic * t)

    # Add amplitude envelope (crying is pulsed)
    envelope = 0.5 + 0.5 * np.sin(2 * np.pi * 3 * t)  # 3 Hz pulsing
    signal *= envelope

    # Normalize
    signal = signal / np.max(np.abs(signal)) * 0.8

    sf.write(path, signal.astype(np.float32), sr)


def generate_adult_speech(path: Path, duration_s: float = 3.0, sr: int = 16000):
    """Generate synthetic adult speech (fundamental 100-200 Hz)."""
    t = np.linspace(0, duration_s, int(duration_s * sr))

    # Lower fundamental for adult voice
    f0 = 150 + 30 * np.sin(2 * np.pi * 0.5 * t)  # 150 Hz ± 30 Hz

    signal = np.zeros_like(t)
    for harmonic in [1, 2, 3, 4, 5]:
        signal += (1 / harmonic) * np.sin(2 * np.pi * f0 * harmonic * t)

    signal = signal / np.max(np.abs(signal)) * 0.7
    sf.write(path, signal.astype(np.float32), sr)


def generate_white_noise(path: Path, duration_s: float = 3.0, sr: int = 16000):
    """Generate white noise."""
    samples = np.random.randn(int(duration_s * sr)).astype(np.float32)
    samples = samples / np.max(np.abs(samples)) * 0.3
    sf.write(path, samples, sr)


def main():
    parser = argparse.ArgumentParser(description="Generate test audio fixtures")
    parser.add_argument("--output", type=Path, default=Path("tests/fixtures"))
    parser.add_argument("--loop", action="store_true", help="Continuously generate")
    args = parser.parse_args()

    args.output.mkdir(parents=True, exist_ok=True)

    generate_silence(args.output / "silence_3s.wav")
    generate_baby_cry(args.output / "baby_cry_3s.wav")
    generate_adult_speech(args.output / "adult_speech_3s.wav")
    generate_white_noise(args.output / "white_noise_3s.wav")

    print(f"Generated fixtures in {args.output}")


if __name__ == "__main__":
    main()
```

### CI Pipeline

```yaml
# .github/workflows/ci.yml
name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install system dependencies
        run: |
          sudo apt-get update
          sudo apt-get install -y ffmpeg libsndfile1

      - name: Install Python dependencies
        run: |
          pip install -r requirements.txt -r requirements-dev.txt
          pip install -e .

      - name: Generate test fixtures
        run: python scripts/generate_test_audio.py

      - name: Run unit tests
        run: pytest tests/unit/ -v --tb=short

      - name: Run integration tests
        run: pytest tests/integration/ -v --tb=short

      - name: Run functional tests (slow)
        run: pytest tests/functional/ -v --tb=short -m "not slow"

  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install ruff mypy
      - run: ruff check bbwatch/ tests/
      - run: mypy bbwatch/ --ignore-missing-imports
```

---

## 🚀 Deployment to Raspberry Pi

### Deployment Strategy

1. **Build**: Create Docker image on dev machine (cross-compile for ARM64)
2. **Transfer**: Push image to RPi via SSH or registry
3. **Run**: Start container with hardware access

### Deploy Script

```bash
#!/bin/bash
# scripts/deploy.sh

set -e

RPI_HOST="${RPI_HOST:-babypi.local}"
RPI_USER="${RPI_USER:-pi}"
IMAGE_NAME="bbwatch:latest"

echo "=== Building ARM64 image ==="
docker buildx build \
    --platform linux/arm64 \
    -f docker/Dockerfile.rpi \
    -t $IMAGE_NAME \
    --load \
    .

echo "=== Saving image ==="
docker save $IMAGE_NAME | gzip > /tmp/bbwatch.tar.gz

echo "=== Transferring to Raspberry Pi ==="
scp /tmp/bbwatch.tar.gz ${RPI_USER}@${RPI_HOST}:/tmp/

echo "=== Loading image on Raspberry Pi ==="
ssh ${RPI_USER}@${RPI_HOST} "docker load < /tmp/bbwatch.tar.gz"

echo "=== Stopping existing container ==="
ssh ${RPI_USER}@${RPI_HOST} "docker stop bbwatch || true"
ssh ${RPI_USER}@${RPI_HOST} "docker rm bbwatch || true"

echo "=== Starting new container ==="
ssh ${RPI_USER}@${RPI_HOST} "docker run -d \
    --name bbwatch \
    --restart unless-stopped \
    --device /dev/video0 \
    --device /dev/snd \
    -v /home/pi/bbwatch/config.yaml:/app/config.yaml:ro \
    -v /home/pi/bbwatch/data:/app/wav_segments \
    -p 1984:1984 \
    -p 8554:8554 \
    $IMAGE_NAME"

echo "=== Deployment complete ==="
echo "Access at: http://${RPI_HOST}:1984"
```

### First-Time Raspberry Pi Setup

```bash
# On the Raspberry Pi (via SSH)

# Install Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker pi

# Create data directory
mkdir -p ~/bbwatch/data

# Install go2rtc (runs outside container for now)
wget https://github.com/AlexxIT/go2rtc/releases/latest/download/go2rtc_linux_arm64 -O go2rtc
chmod +x go2rtc
sudo mv go2rtc /usr/local/bin/

# Create go2rtc config
cat > /etc/go2rtc.yaml << 'EOF'
streams:
  babycam:
    - "ffmpeg:device?video=/dev/video0&audio=hw:1,0#video=h264#audio=opus"
api:
  listen: ":1984"
EOF

# Create systemd services
sudo systemctl enable docker
# (Additional systemd units for go2rtc and bbwatch)
```

### Smoke Test After Deployment

```bash
# scripts/smoke_test.sh

#!/bin/bash
set -e

RPI_HOST="${RPI_HOST:-babypi.local}"

echo "=== Checking container status ==="
ssh pi@$RPI_HOST "docker ps | grep bbwatch"

echo "=== Checking health endpoint ==="
curl -s http://$RPI_HOST:1984/ | head -5

echo "=== Checking status file ==="
ssh pi@$RPI_HOST "docker exec bbwatch cat /app/status.json"

echo "=== Checking logs for errors ==="
ssh pi@$RPI_HOST "docker logs bbwatch --tail 20 2>&1 | grep -i error || echo 'No errors found'"

echo "=== Smoke test passed ==="
```

---

## 📋 Implementation Order

| Step | Phase | Description | Est. Time |
|------|-------|-------------|-----------|
| 1 | Setup | Project structure, pyproject.toml, requirements | 20 min |
| 2 | Docker | Dockerfile.dev, docker-compose.yml | 20 min |
| 3 | Config | Pydantic config model | 15 min |
| 4 | Hardware | Device detection module | 30 min |
| 5 | Detection | Bandpass filter + RMS detector | 45 min |
| 6 | Storage | Disk management + cleanup | 30 min |
| 7 | Capture | FFmpeg sliding window wrapper | 45 min |
| 8 | Overlay | Status file + image generation | 45 min |
| 9 | Main | Entry point + watchdog integration | 30 min |
| 10 | Tests | Unit + integration tests | 1 hour |
| 11 | CI | GitHub Actions workflow | 15 min |
| 12 | Deploy | RPi deployment script | 20 min |
| **Total** | | | **~6 hours** |

---

**Status:** 🚧 Implementation in progress
```
