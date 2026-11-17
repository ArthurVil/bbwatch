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

## 5. Alert Actions, Recordings & Google Drive Upload (optional)

An alert triggers on a cry **or** on motion: `alerts.motion_min_frames` frames
(default 3) within `alerts.motion_window_s` (1 s) changing more than
`motion.motion_threshold_percent` (1 %) of the image. The live overlay always
shows it. What else happens is chosen at startup in `config.yaml`:

```yaml
alerts:
  actions:
    screenshot: false     # one JPEG when an alert triggers
    clip: false           # record the babycam stream until the alert clears
    destination: local    # local | cloud | both
```

The default (both off) is overlay only: no files, FFmpeg never runs. Apply a
change with `make restart`. Env vars override for one run, e.g.
`BBWATCH_ALERTS__ACTIONS__CLIP=true` (add it to the `environment:` list in
`docker/docker-compose.yml`). The startup log shows the result:
`Alert actions: overlay only (no screenshots, no clips)`.

A clip (video + overlay + mic audio) runs until the alert clears, capped at
`alerts.record_max_s` (default 5 min) per file and lasting at least
`alerts.record_min_s` (default 15 s).

| destination | where files go |
|---|---|
| `local` | `data/clips/`, `data/screenshots/` — kept on the SD card (no automatic cleanup) |
| `cloud` | `data/outbox/{clips,screenshots}/` — moved to Drive by the upload timer, then deleted locally |
| `both`  | local, plus a hard link in `data/outbox/` (no extra space) for the upload timer |

```
data/clips/YYYY-MM-DD/HHMMSS.mp4        # finished clips (outbox/clips/... for cloud)
data/clips/YYYY-MM-DD/.HHMMSS.mp4.part  # clip being recorded (hidden)
data/screenshots/YYYY-MM-DD/HHMMSS.jpg
```

Tune with `motion.motion_threshold_percent`, `alerts.motion_min_frames`,
`alerts.motion_window_s`, `alerts.record_min_s` and
`alerts.record_max_s` in `config.yaml`.

For `cloud`/`both`, a host-side systemd timer empties `data/outbox/` to Google
Drive with `rclone move` every minute (during a network outage files wait on
the SD card and upload later). It is **opt-in** and runs outside the bbwatch
container; with `destination: local` it finds nothing to upload.

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

## 6. Night Image Profile (USB IR camera)

The USB camera switches to IR (grayscale) by itself in the dark. Its default
IR image is washed out (shadows at mid-grey, sharpening grain). A host service
applies a night profile in the camera itself while it is in IR mode, and
restores the defaults (color view unchanged) as soon as the image is in color
again. No latency or CPU is added to the streaming pipeline.

| profile | brightness | contrast | sharpness | backlight_compensation |
|---|---|---|---|---|
| night (IR) | -24 | 40 | 1 | 0 |
| day (color, camera defaults) | 0 | 32 | 3 | 1 |

Mode is detected every 20 s from the saturation of a few `raw_video` frames
(IR is exactly 0). Settings are re-applied if the camera is re-plugged.

```bash
sudo cp deploy/bbwatch-camtune.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bbwatch-camtune.service
journalctl -u bbwatch-camtune -n 20                  # mode changes
v4l2-ctl -d /dev/video0 -C brightness,contrast,sharpness,backlight_compensation
```

Tune the profiles with `CAMTUNE_NIGHT` / `CAMTUNE_DAY` (`Environment=` in the
unit). The profile also changes the image motion detection sees; switching
profiles is one whole-frame change, at the moment the light changes anyway.
Stopping the service puts the camera back on its defaults.

## 7. Deployment Verification

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
    - **Sound drops out for a few seconds?** The camera's USB mic sometimes
      stops delivering samples. babycam's audio is a separate go2rtc source
      (`deploy/babycam-audio.sh`) that restarts the capture after 5 s without
      audio, so the video never freezes with it. Each restart shows in
      `journalctl -u go2rtc` as `error=EOF url="exec:.../babycam-audio.sh"`.
