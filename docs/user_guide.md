# bbwatch User Guide

## Running bbwatch

### Using Docker (Full Stack)
Deployment is managed via Docker Compose. This runs both the `bbwatch` logic and the `go2rtc` streaming server.

> [!IMPORTANT]
> **Hardware Configuration**: The streaming server (`go2rtc`) ignores `config.yaml`. You must configure audio/video devices for streaming directly in `docker/go2rtc.yaml` (e.g., changing `hw:1,0` to match your hardware).

```bash
cd docker
docker-compose up
```

Access the video feed at [http://localhost:1984](http://localhost:1984).

### Remote Access (Wi-Fi)
To access the stream from another device (e.g., smartphone) on the same Wi-Fi:

1.  **Find your IP address**:
    ```bash
    hostname -I | awk '{print $1}'
    ```
2.  **Open in Browser**:
    Go to `http://<YOUR_IP>:1984` (e.g., `http://192.168.1.15:1984`).

### Manual Run
```bash
bbwatch --config /path/to/config.yaml
```

## Configuration Reference (`config.yaml`)

### `audio` section
- `segment_duration_s`: Length of audio chunk to analyze (default: `1.0`).
- `overlap_s`: Overlap between chunks (default: `0.5`). Higher overlap = smoother detection but more CPU.
- `sample_rate`: Audio sample rate. `48000` recommended for modern webcams.
- `device_index`: ALSA device string (e.g., `plughw:1,0`). Use `arecord -l` to find yours.

### `detection` section
- `bandpass_low_hz` / `bandpass_high_hz`: Frequency range to analyze (250-2000Hz captures most cries).
- `rms_threshold`: Volume threshold (0.0-1.0). Lower = more sensitive to quiet cries.
- `min_active_ratio`: Percentage of the segment that must be "loud" to trigger an alert.
- `window_ms`: Sub-window size for RMS calculation (e.g., `100.0` ms).
- `silence_threshold`: RMS level below which a segment is considered "silent" and discarded.

### `alerts` section
- `trigger_high`: Hysteresis upper bound. Alert starts when intensity > this.
- `trigger_low`: Hysteresis lower bound. Alert stops when intensity < this.
- `cooldown_s`: Minimum time (seconds) before a new alert can trigger.

### `storage` section
- `max_size_mb`: Disk quota for recordings.
- `delete_empty_segments`: If `true`, deletes silent clips immediately to save space.
