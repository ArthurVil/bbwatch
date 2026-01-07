# bbwatch LLM Context

## Project Identity
**Name**: bbwatch
**Purpose**: FOSS Baby Monitor for Raspberry Pi.
**Tech Stack**: Python 3.11+, Docker, Pydantic, FFmpeg, SciPy.

## Critical Constraints
1.  **Strict Error Handling**: Do **not** use silent `try-except` blocks. If a dependency (like `arecord`) is missing, crash with a clear `RuntimeError`.
2.  **No Magic Numbers**: All configurable values (thresholds, durations) MUST come from `bbwatch.config.BBWatchConfig`.
3.  **Type Safety**: All code must pass `mypy --strict` (or equivalent high strictness). No `Any` unless absolutely necessary.
4.  **Hardware Abstraction**: Code should run in Docker. Use `scripts/demo.py` to verify hardware access on host.

## Directory Map
- `bbwatch/`: Core package.
    - `detector.py`: DSP logic.
    - `config.py`: Pydantic models.
- `scripts/`: Standalone utilities (must align with `bbwatch/` logic).
- `docs/`: Human-readable documentation.
- `docker/`: Build environments.

## Common Tasks
- **Lint**: `make lint`
- **Test**: `make test`
- **Demo**: `make docker-demo`
