#!/bin/bash
# scripts/deploy.sh
# Deploy bbwatch to Raspberry Pi

set -e

# Configuration
RPI_HOST="${RPI_HOST:-babypi.local}"
RPI_USER="${RPI_USER:-pi}"
IMAGE_NAME="bbwatch:latest"

echo "========================================"
echo "bbwatch Deployment to Raspberry Pi"
echo "========================================"
echo "Target: ${RPI_USER}@${RPI_HOST}"
echo ""

# Check if Docker buildx is available
if ! docker buildx version &> /dev/null; then
    echo "ERROR: Docker buildx is required for cross-platform builds"
    echo "Run: docker buildx create --use"
    exit 1
fi

echo "=== Building ARM64 image ==="
docker buildx build \
    --platform linux/arm64 \
    -f docker/Dockerfile.rpi \
    -t $IMAGE_NAME \
    --load \
    .

echo ""
echo "=== Saving image ==="
docker save $IMAGE_NAME | gzip > /tmp/bbwatch.tar.gz
echo "Image size: $(du -h /tmp/bbwatch.tar.gz | cut -f1)"

echo ""
echo "=== Transferring to Raspberry Pi ==="
scp /tmp/bbwatch.tar.gz ${RPI_USER}@${RPI_HOST}:/tmp/

echo ""
echo "=== Loading image on Raspberry Pi ==="
ssh ${RPI_USER}@${RPI_HOST} "docker load < /tmp/bbwatch.tar.gz"

echo ""
echo "=== Stopping existing container ==="
ssh ${RPI_USER}@${RPI_HOST} "docker stop bbwatch 2>/dev/null || true"
ssh ${RPI_USER}@${RPI_HOST} "docker rm bbwatch 2>/dev/null || true"

echo ""
echo "=== Creating data directory ==="
ssh ${RPI_USER}@${RPI_HOST} "mkdir -p ~/bbwatch/data"

echo ""
echo "=== Starting new container ==="
ssh ${RPI_USER}@${RPI_HOST} "docker run -d \
    --name bbwatch \
    --restart unless-stopped \
    --device /dev/video0 \
    --device /dev/snd \
    --group-add audio \
    --group-add video \
    -v /home/${RPI_USER}/bbwatch/config.yaml:/app/config.yaml:ro \
    -v /home/${RPI_USER}/bbwatch/data:/app/wav_segments \
    -p 1984:1984 \
    -p 8554:8554 \
    $IMAGE_NAME"

echo ""
echo "=== Checking container status ==="
sleep 2
ssh ${RPI_USER}@${RPI_HOST} "docker ps --filter name=bbwatch"

echo ""
echo "========================================"
echo "Deployment complete!"
echo "========================================"
echo ""
echo "Access points:"
echo "  - go2rtc UI: http://${RPI_HOST}:1984"
echo "  - RTSP stream: rtsp://${RPI_HOST}:8554/babycam"
echo ""
echo "Logs: ssh ${RPI_USER}@${RPI_HOST} docker logs -f bbwatch"
echo ""
