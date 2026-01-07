# bbwatch Development Guide

## Setup

### Requirements
- Linux (Debian/Ubuntu recommended) or macOS
- Python 3.11+
- Docker & Docker Compose
- `ffmpeg`, `libsndfile1`, `pulseaudio-utils` (for audio demos)

### Installation
```bash
# Clone the repo
git clone https://github.com/ArthurVil/bbwatch.git
cd bbwatch

# Install dev dependencies
make install-dev
```

## Workflow

### Running Tests
We use `pytest` for unit and integration testing.
```bash
make test
```

### Linting & Types
We enforce strict typing (`mypy`) and linting (`ruff`).
```bash
make lint
```
> **Note**: The CI pipeline will fail if there are any linting errors or missing type annotations.

### Docker Development
To test in a clean environment that mirrors the Raspberry Pi:
```bash
make docker-build
make docker-test
```

## Demos
Scripts in `scripts/` allow isolating components:
- `python scripts/demo.py`: Audio detection feedback loop.
- `python scripts/demo_motion.py`: Webcam motion detection (requires local display/X11).

## Release Process
1. Bump version in `bbwatch/__init__.py`.
2. Commit and Tag: `git tag vX.Y.Z`.
3. Push tags: `git push origin vX.Y.Z`.
4. GitHub Actions will automatically build the multi-arch Docker images.
