# Raspberry Pi Deployment Guide

This guide covers setting up **bbwatch** on a fresh Raspberry Pi (RPi 4 or 5 recommended).

## 1. Prerequisites
- **Hardware**: Raspberry Pi 4 (2GB+), USB Webcam (with mic).
- **OS**: Raspberry Pi OS (64-bit) Lite or Desktop.
    - *Lite is recommended for headless appliance performance.*

## 2. System Preparation

1.  **Update System**:
    ```bash
    sudo apt update && sudo apt upgrade -y
    ```

2.  **Install Docker**:
    The official convenience script is the easiest way:
    ```bash
    curl -fsSL https://get.docker.com | sh
    ```

3.  **Configure Permissions**:
    Add your user (`pi` usually) to necessary groups for hardware access:
    ```bash
    sudo usermod -aG docker,video,audio $USER
    # Log out and back in for changes to take effect!
    exit
    ```

4.  **Verify Hardware**:
    Connect your USB camera. Check device presence:
    ```bash
    ls -l /dev/video*
    ls -l /dev/snd/
    ```

## 3. Installation

1.  **Clone Repository**:
    ```bash
    git clone https://github.com/ArthurVil/bbwatch.git
    cd bbwatch
    ```

2.  **Identify Hardware Devices**:
    We provide a utility to list devices. You might need python/poetry, OR just rely on `arecord` manually.
    
    *Using built-in utility (requires python3-venv):*
    ```bash
    sudo apt install python3-venv -y
    make install
    make devices
    ```
    
    *Simple manual check:*
    ```bash
    # Audio
    arecord -L | grep plughw
    # Video
    v4l2-ctl --list-devices
    ```

3.  **Configure**:
    Copy the sample config:
    ```bash
    cp config.yaml.example config.yaml
    ```
    
    Edit `docker/go2rtc.yaml` if your camera supports different formats (default is specific to typical webcams, you might need to change `/dev/video0` or `input_format`).

## 4. Running

Start the application stack (detached mode):

```bash
make up
# OR
docker compose -f docker/docker-compose.yml up -d
```

## 5. Alert Recordings & Google Drive Upload (optional)

When an alert triggers (sustained cry **or** sustained motion), bbwatch records
the `babycam` stream (video + overlay + mic audio) until the alert clears,
capped at `alerts.record_max_s` (default 5 min) per file:

```
data/clips/YYYY-MM-DD/HHMMSS.mp4        # finished clips
data/clips/YYYY-MM-DD/.HHMMSS.mp4.part  # clip being recorded (hidden)
data/screenshots/YYYY-MM-DD/HHMMSS.jpg
```

Tune with `alerts.motion_triggers_alert`, `alerts.motion_min_s` and
`alerts.record_max_s` in `config.yaml`.

To view recordings from anywhere, a host-side systemd timer uploads finished
files to Google Drive with `rclone move` every minute (local copies are deleted
once uploaded; during a network outage they wait on the SD card and upload
later). It is **opt-in** and runs outside the bbwatch container.

1.  Configure an rclone remote named `gdrive` for the user that owns `~/bbwatch`
    (`rclone config`, type `drive`), and check it: `rclone lsd gdrive:`.
2.  Install and start the timer (edit `User=`/paths in the `.service` if your
    user is not `kanai`):
    ```bash
    sudo cp deploy/bbwatch-upload.service deploy/bbwatch-upload.timer /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now bbwatch-upload.timer
    ```
3.  Check it:
    ```bash
    systemctl list-timers bbwatch-upload.timer
    journalctl -u bbwatch-upload -n 50
    rclone lsl gdrive:bbwatch/clips
    ```

Files land in `gdrive:bbwatch/clips/<date>/` and `gdrive:bbwatch/screenshots/<date>/`.
Drive-side retention is not managed — delete old days from Drive yourself.

Do not point bbwatch at an `rclone mount` instead: writing live recordings to a
FUSE-mounted Drive loses footage whenever the network drops.

## 6. Deployment Verification

1.  **Check Logs**:
    ```bash
    make logs
    ```
    Look for: `Overlay loop alive` and `Baby monitor started successfully`.

2.  **Access Stream**:
    Open VLC or a browser:
    - Stream: `rtsp://<RPI_IP>:8554/babycam`
    - WebRTC: `http://<RPI_IP>:1984`
    
    *Note: If using WebRTC on a different network, you may need to configure ICE servers in go2rtc.yaml.*

3.  **Troubleshooting**:
    - **Frozen Stream?** Check voltage (undervoltage throttles USB).
    - **No Audio?** Verify ALSA device string in `docker/go2rtc.yaml`.
