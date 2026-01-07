# bbwatch Architecture

## Overview
**bbwatch** is a local-first baby monitor that processes audio and video on-device (Raspberry Pi). It avoids cloud latency and privacy risks by performing all Digital Signal Processing (DSP) and motion analysis locally.

## High-Level Data Flow

```mermaid
graph TD
    subgraph Hardware
        Mic[USB Microphone]
        Cam[USB Webcam]
    end

    subgraph "Docker / Host System"
        direction TB
        
        Capture[Capture Module\n(FFmpeg/PulseAudio)]
        Detector[Detector Module\n(DSP / Bandpass + RMS)]
        Logic{Alert Logic\nState Machine}
        
        Mic -->|Audio Stream| Capture
        Capture -->|WAV Segments| Detector
        Detector -->|Analysis Metrics| Logic
        
        Logic -->|Verified Cry| AlertMgr[Alert Manager]
        Logic -->|Silence| Storage[Storage Manager]
        
        Cam -->|Video Stream| Go2RTC[Go2RTC Server]
        AlertMgr -->|Overlay Status| Go2RTC
    end

    subgraph Clients
        Web[Web Browser]
        Phone[Mobile Device]
    end

    Go2RTC -->|WebRTC/RTSP| Web
    Go2RTC -->|WebRTC/RTSP| Phone
```

## Core Components

### 1. Audio Pipeline (`bbwatch.capture`, `bbwatch.detector`)
- **Capture**: Uses `ffmpeg` or `sounddevice` to define sliding windows of audio (default 1.0s).
- **Detection**: Applies a Butterworth bandpass filter (default 250-2000Hz) to isolate cry frequencies. Calculates Root Mean Square (RMS) energy to detect "loud" events within this band.
- **Why DSP?**: Lightweight and effective for loud, tonal baby cries. No heavy ML models required.

### 2. Alert System (`bbwatch.alert`)
- **Hysteresis**: Prevents flickering alerts. Trigger high threshold starts an alert; trigger low threshold stops it.
- **Cool-down**: Prevents spamming notifications.

### 3. Video & Overlay
- **Go2RTC**: Handles low-latency streaming.
- **Overlays**: Generated as transparent images/HTML layered over the video feed to indicate status (Green = OK, Red = Cry).

### 4. Storage Management
- **Circular Buffer**: Maintains a fixed disk usage (e.g., 1GB). Oldest non-event segments are deleted first.
