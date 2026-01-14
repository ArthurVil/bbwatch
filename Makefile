.PHONY: help install install-dev test lint format clean docker-build docker-test docker-demo docker-demo-video docker-demo-motion docker-shell docker-build-rpi devices up down logs restart

# Export host variables from config.yaml for docker-compose
ifneq ("$(wildcard config.yaml)","")
export HOST_VIDEO_DEVICE ?= $(shell python3 -c 'import yaml; print(yaml.safe_load(open("config.yaml")).get("host", {}).get("video_device", "/dev/video0"))' 2>/dev/null)
export HOST_AUDIO_SOURCE ?= $(shell python3 -c 'import yaml; print(yaml.safe_load(open("config.yaml")).get("host", {}).get("pulse_source", ""))' 2>/dev/null)
else
export HOST_VIDEO_DEVICE ?= /dev/video0
export HOST_AUDIO_SOURCE ?=
endif

# Default target
help:
	@echo "BBWatch Development Commands"
	@echo ""
	@echo "Development:"
	@echo "  make install       Install dependencies (poetry)"
	@echo "  make install-dev   Install dev dependencies"
	@echo "  make test          Run all tests with coverage"
	@echo "  make lint          Run ruff + mypy"
	@echo "  make format        Auto-format code with ruff"
	@echo "  make clean         Remove build artifacts"
	@echo ""
	@echo "Docker Compose Shortcuts:"
	@echo "  make up            Start services (detached)"
	@echo "  make down          Stop services"
	@echo "  make logs          View logs (follow)"
	@echo "  make restart       Restart services"
	@echo ""
	@echo "Docker Dev:"
	@echo "  make docker-build      Build development image"
	@echo "  make docker-test       Run tests in Docker"
	@echo "  make docker-demo       Run audio demo with mic passthrough"
	@echo "  make docker-demo-video  Run video demo with webcam (requires X11)"
	@echo "  make docker-demo-motion Run motion detection demo (requires X11)"
	@echo "  make docker-shell      Interactive shell in container"
	@echo "  make docker-build-rpi  Cross-compile ARM64 image for Raspberry Pi"
	@echo ""
	@echo "Hardware:"
	@echo "  make devices       List available audio/video devices"

# ============================================================================
# Development
# ============================================================================

install:
	poetry install --only main

install-dev:
	poetry install

test:
	poetry run pytest tests/ -v --tb=short --cov=bbwatch --cov-report=term-missing

test-unit:
	poetry run pytest tests/unit/ -v --tb=short

test-cov:
	poetry run pytest tests/ -v --tb=short --cov=bbwatch --cov-report=html

lint:
	poetry run ruff check bbwatch/ tests/
	poetry run mypy bbwatch/ --ignore-missing-imports

format-unsafe:
	poetry run ruff check bbwatch/ tests/ --fix --unsafe-fixes

format:
	poetry run ruff check bbwatch/ tests/ --fix
	poetry run ruff format bbwatch/ tests/

clean:
	rm -rf build/ dist/ *.egg-info/ .pytest_cache/ .coverage htmlcov/
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete

# ============================================================================
# Docker Compose Shortcuts
# ============================================================================

up:
	docker compose -f docker/docker-compose.yml up -d

down:
	docker compose -f docker/docker-compose.yml down

logs:
	docker compose -f docker/docker-compose.yml logs -f

restart:
	docker compose -f docker/docker-compose.yml restart

# ============================================================================
# Docker Dev
# ============================================================================

DOCKER_IMAGE := bbwatch
DOCKER_TAG := dev

docker-build:
	docker build -t $(DOCKER_IMAGE):$(DOCKER_TAG) -f docker/Dockerfile.dev .

docker-test: docker-build
	docker run --rm $(DOCKER_IMAGE):$(DOCKER_TAG) pytest tests/ -v --tb=short

docker-demo: docker-build
	@echo "Starting audio demo with microphone passthrough..."
	@echo "Press Ctrl+C to stop"
	docker run --rm -it \
		--privileged \
		--device /dev/snd:/dev/snd \
		--device /dev/bus/usb:/dev/bus/usb \
		--group-add audio \
		-e PA_ALSA_PLUGHW=1 \
		-e PULSE_SERVER=unix:/run/user/$(shell id -u)/pulse/native \
		-e PULSE_COOKIE=/tmp/pulse-cookie \
		-v /run/user/$(shell id -u)/pulse:/run/user/$(shell id -u)/pulse:ro \
		-v $(HOME)/.config/pulse/cookie:/tmp/pulse-cookie:ro \
		-v $(PWD):/app \
		$(DOCKER_IMAGE):$(DOCKER_TAG) \
		python3 scripts/demo.py --loop

docker-demo-video: docker-build
	@echo "Starting video demo with webcam passthrough..."
	@echo "Requires X11 forwarding: export DISPLAY and mount /tmp/.X11-unix"
	docker run --rm -it \
		--privileged \
		--device /dev/video0:/dev/video0 \
		--device /dev/snd:/dev/snd \
		--device /dev/bus/usb:/dev/bus/usb \
		--group-add video \
		--group-add audio \
		-e DISPLAY=$(DISPLAY) \
		-e PA_ALSA_PLUGHW=1 \
		-v /tmp/.X11-unix:/tmp/.X11-unix \
		-v $(PWD):/app \
		$(DOCKER_IMAGE):$(DOCKER_TAG) \
		python3 scripts/demo_video.py

docker-demo-motion: docker-build
	@echo "Starting motion detection demo with webcam passthrough..."
	@echo "Requires X11 forwarding: export DISPLAY and mount /tmp/.X11-unix"
	docker run --rm -it \
		--privileged \
		--device /dev/video0:/dev/video0 \
		--device /dev/bus/usb:/dev/bus/usb \
		--group-add video \
		-e DISPLAY=$(DISPLAY) \
		-v /tmp/.X11-unix:/tmp/.X11-unix \
		-v $(PWD):/app \
		$(DOCKER_IMAGE):$(DOCKER_TAG) \
		python3 scripts/demo_motion.py

docker-shell: docker-build
	docker run --rm -it \
		--privileged \
		--device /dev/snd:/dev/snd \
		--device /dev/video0:/dev/video0 \
		--device /dev/bus/usb:/dev/bus/usb \
		--group-add audio \
		--group-add video \
		-e DISPLAY=$(DISPLAY) \
		-e PA_ALSA_PLUGHW=1 \
		-v /tmp/.X11-unix:/tmp/.X11-unix \
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
	@poetry run python3 scripts/list_devices.py
