.PHONY: help install install-dev test lint format clean docker-build docker-test docker-demo docker-shell docker-build-rpi devices

# Default target
help:
	@echo "BBWatch Development Commands"
	@echo ""
	@echo "Development:"
	@echo "  make install       Install runtime dependencies"
	@echo "  make install-dev   Install dev dependencies (includes test, lint)"
	@echo "  make test          Run all tests"
	@echo "  make lint          Run ruff + mypy"
	@echo "  make format        Auto-format code with ruff"
	@echo "  make clean         Remove build artifacts"
	@echo ""
	@echo "Docker:"
	@echo "  make docker-build      Build development image"
	@echo "  make docker-test       Run tests in Docker"
	@echo "  make docker-demo       Run audio demo with mic passthrough"
	@echo "  make docker-demo-video Run video demo with webcam (requires X11)"
	@echo "  make docker-shell      Interactive shell in container"
	@echo "  make docker-build-rpi  Cross-compile ARM64 image for Raspberry Pi"
	@echo ""
	@echo "Hardware:"
	@echo "  make devices       List available audio/video devices"

# ============================================================================
# Development
# ============================================================================

install:
	python3 -m pip install -e .

install-dev:
	python3 -m pip install -e ".[dev]"
	python3 -m pip install sounddevice opencv-python

test:
	python3 -m pytest tests/ -v --tb=short

test-unit:
	python3 -m pytest tests/unit/ -v --tb=short

test-cov:
	python3 -m pytest tests/ -v --tb=short --cov=bbwatch --cov-report=html

lint:
	python3 -m ruff check bbwatch/ tests/
	python3 -m mypy bbwatch/ --ignore-missing-imports

format:
	python3 -m ruff check bbwatch/ tests/ --fix
	python3 -m ruff format bbwatch/ tests/

clean:
	rm -rf build/ dist/ *.egg-info/ .pytest_cache/ .coverage htmlcov/
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete

# ============================================================================
# Docker
# ============================================================================

DOCKER_IMAGE := bbwatch
DOCKER_TAG := dev

docker-build:
	docker build -t $(DOCKER_IMAGE):$(DOCKER_TAG) -f docker/Dockerfile.dev .

docker-test: docker-build
	docker run --rm $(DOCKER_IMAGE):$(DOCKER_TAG) python3 -m pytest tests/ -v --tb=short

docker-demo: docker-build
	@echo "Starting audio demo with microphone passthrough..."
	@echo "Press Ctrl+C to stop"
	docker run --rm -it \
		--device /dev/snd:/dev/snd \
		--group-add audio \
		-v $(PWD):/app \
		$(DOCKER_IMAGE):$(DOCKER_TAG) \
		python3 scripts/demo.py --loop

docker-demo-video: docker-build
	@echo "Starting video demo with webcam passthrough..."
	@echo "Requires X11 forwarding: export DISPLAY and mount /tmp/.X11-unix"
	docker run --rm -it \
		--device /dev/video0:/dev/video0 \
		--group-add video \
		-e DISPLAY=$(DISPLAY) \
		-v /tmp/.X11-unix:/tmp/.X11-unix \
		-v $(PWD):/app \
		$(DOCKER_IMAGE):$(DOCKER_TAG) \
		python3 scripts/demo_video.py

docker-shell: docker-build
	docker run --rm -it \
		-v $(PWD):/app \
		$(DOCKER_IMAGE):$(DOCKER_TAG) \
		/bin/bash

docker-build-rpi:
	docker buildx build \
		--platform linux/arm64 \
		-t $(DOCKER_IMAGE):rpi \
		-f docker/Dockerfile.rpi \
		--load \
		.

# ============================================================================
# Hardware
# ============================================================================

devices:
	@python3 scripts/list_devices.py
