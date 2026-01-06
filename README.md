# bbwatch 🍼

**FOSS Baby Monitor with cry detection and visual alerts**

A fully open-source baby monitor built for Raspberry Pi with USB webcam. Features real-time cry detection using DSP (no cloud, no ML required), visual overlay alerts, and automatic disk management.

## Features

- 🎥 **Live video streaming** via WebRTC/RTSP (go2rtc)
- 🔊 **Cry detection** using bandpass filter (250-800 Hz) + energy analysis
- 🔴 **Visual alerts** - red overlay for cry, blue for system errors
- 💾 **Automatic disk management** - 1GB limit with oldest-first cleanup
- 🔒 **Fully local** - no cloud, no accounts, no data leaves your network
- 🐳 **Docker support** - develop anywhere, deploy to RPi

## Quick Start

### Development (Docker)

```bash
# Clone and enter directory
git clone https://github.com/bbwatch/bbwatch.git
cd bbwatch

# Run tests
docker compose -f docker/docker-compose.yml run --rm bbwatch pytest -v

# Interactive shell
docker compose -f docker/docker-compose.yml run --rm bbwatch bash
```

### Development (Local)

```bash
# Create virtual environment
python -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -e ".[dev]"

# Generate test fixtures
python scripts/generate_test_audio.py

# Run tests
pytest -v
```

### Raspberry Pi Deployment

```bash
# Set your Pi's hostname
export RPI_HOST=babypi.local

# Deploy
./scripts/deploy.sh
```

## Configuration

Copy `config.yaml` and adjust as needed:

```yaml
audio:
  segment_duration_s: 3.0    # Audio segment length
  overlap_s: 1.0             # Overlap for sliding window

detection:
  bandpass_low_hz: 250.0     # Baby cry low frequency
  bandpass_high_hz: 800.0    # Baby cry high frequency
  rms_threshold: 0.02        # Energy threshold
  min_active_ratio: 0.3      # % of segment with activity

storage:
  max_size_mb: 1024.0        # Max disk usage (1GB)
  delete_empty_segments: true
```

## Architecture

```
USB Camera ─┬─► go2rtc ──────────────────► VLC / Browser
            │      ▲
USB Mic ────┘      │ overlay control
     │             │
     ▼             │
  ffmpeg ──► .wav ──► Python detector ────┘
```

## Project Structure

```
bbwatch/
├── bbwatch/              # Python package
│   ├── config.py         # Pydantic configuration
│   ├── hardware.py       # Device detection
│   ├── capture.py        # FFmpeg audio capture
│   ├── detector.py       # Cry detection
│   ├── storage.py        # Disk management
│   ├── overlay.py        # Video overlay
│   └── main.py           # Entry point
├── docker/               # Docker files
├── tests/                # Test suite
├── scripts/              # Deployment scripts
└── config.yaml           # Default configuration
```

## Viewing the Stream

- **Browser**: `http://<PI_IP>:1984`
- **VLC**: `rtsp://<PI_IP>:8554/babycam`
- **Android VLC**: Open network stream → enter RTSP URL

## Requirements

### Raspberry Pi
- Raspberry Pi 4 (2GB+ RAM recommended)
- USB webcam with microphone
- Docker installed

### Development
- Python 3.10+
- FFmpeg
- Docker (optional)

## License

MIT License - see [LICENSE](LICENSE)

## Contributing

Contributions welcome! Please open an issue first to discuss changes.
