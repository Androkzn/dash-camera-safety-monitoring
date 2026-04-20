---
name: python
description: Python conventions for road_safety/, cloud/, tests/, start.py
type: rules
paths:
  - "**/*.py"
---

# Python conventions

- Python 3.10+. Use type hints on public functions and dataclasses.
- Imports: stdlib → third-party → local, separated by blank lines. Do not auto-add `from __future__ import annotations`.
- **Paths**: never compute `Path(__file__).parent` in modules. Import paths from [road_safety/config.py](road_safety/config.py) — single source of truth.
- **Logging**: use `logging.getLogger(__name__)`, not `print`. Structured fields preferred (`extra={...}`).
- **Async**: server is FastAPI/uvicorn. Long CPU work goes through `run_in_threadpool` or a worker thread; never block the event loop.
- **Error handling**: prefer narrow `except` clauses. Bubble unexpected errors to the watchdog ([road_safety/services/watchdog.py](road_safety/services/watchdog.py)) — do not swallow.

## Hot-path rules (perception pipeline)

- Do not short-circuit conflict-detection gates in [road_safety/core/](road_safety/core/) — each kills a specific false-positive class.
- Do not add LLM calls outside the `services/llm.py` wrappers — bypasses failover, rate budget, circuit breaker, and cost tracking.
- Do not leak raw plate text. Scrub at ingest in `enrich_event()` ([road_safety/services/llm.py](road_safety/services/llm.py)), not just at egress.

## Tests

- `pytest tests/ -v` for full suite; `pytest tests/test_core.py::name -v` for one.
- Any change to a detection gate requires `tests/test_core.py` to pass.
- Use `pytest-asyncio` for async tests; mark with `@pytest.mark.asyncio`.

## Lint

- `make lint` only does `py_compile` on three entrypoints — there is no formatter / type checker wired up. Don't introduce one without asking.
