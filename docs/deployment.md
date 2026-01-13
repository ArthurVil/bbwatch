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

## 5. Deployment Verification

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
