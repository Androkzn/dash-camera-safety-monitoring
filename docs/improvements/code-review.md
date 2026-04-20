# Code Review — Type Safety & Code Quality

Scope: Python backend (`road_safety/`) and frontend TypeScript (`frontend/`).
Date: 2026-04-18.

---

## TL;DR

- **Python has no enforced type safety.** No `mypy`/`pyright` config, no CI gate, and `make lint` is just `py_compile`. Event dicts flow through the whole pipeline untyped.
- **Frontend type safety is solid.** `strict: true`, `noUncheckedIndexedAccess: true`, no stray `any`.
- The highest-leverage fix is introducing a typed event schema (TypedDict or Pydantic) and wiring `mypy --strict` into CI.

---

## Ranked findings

| # | Severity | Issue | Evidence |
|---|----------|-------|----------|
| 1 | Critical | No project-level type checker config | [pyproject.toml](../../pyproject.toml), [Makefile](../../Makefile) |
| 2 | Critical | Event/template dicts lack schemas; `dict[str, Any]` is pervasive | [llm.py:771](../../road_safety/services/llm.py#L771), [settings_db.py:193](../../road_safety/services/settings_db.py#L193) |
| 3 | High | Numpy frames passed without types on hot path | [detection.py:927](../../road_safety/core/detection.py#L927), [redact.py:80](../../road_safety/services/redact.py#L80) |
| 4 | High | Bare `except Exception` in LLM failover masks SDK bugs | [llm.py](../../road_safety/services/llm.py) |
| 5 | High | In-place frame mutation is undocumented | [redact.py:151](../../road_safety/services/redact.py#L151) |
| 6 | High | No async/concurrency tests despite heavy asyncio use | [tests/](../../tests/) |
| 7 | Medium | Event dict mutates through 5+ stages without validation | [server.py](../../road_safety/server.py), [llm.py](../../road_safety/services/llm.py) |
| 8 | Medium | `server.py` is 3,206 lines; tight coupling to 15+ modules | [server.py](../../road_safety/server.py) |
| 9 | Medium | `_blur_roi()` does not clamp bbox coords | [redact.py:80](../../road_safety/services/redact.py#L80) |
| 10 | Low | Plate salt falls back to per-process ephemeral value | [llm.py](../../road_safety/services/llm.py) |

---

## 1. Type safety (the headline)

### What's missing

- No `[tool.mypy]` / `[tool.pyright]` block in [pyproject.toml](../../pyproject.toml).
- No `py.typed` marker at the package root.
- `make lint` runs `py_compile` only — it catches syntax errors, not type errors.
- `.claude/rules/python.md` asks for "type hints on public functions" but nothing enforces it.

### Where it hurts

**Event dicts are the lingua franca of the pipeline:**

```
detect → qualify → narrate → enrich → redact → emit
```

Each stage reads fields, adds fields, and sometimes strips fields (plate scrubbing in `enrich_event()`). None of it is typed. A renamed field fails silently downstream.

**Service boundaries return `dict[str, Any]`:**
- [services/impact.py:95](../../road_safety/services/impact.py#L95) — `to_dict()` → `dict[str, Any]`
- [services/settings_db.py:193,241,325](../../road_safety/services/settings_db.py#L193) — all template getters return `dict[str, Any] | None`
- [services/registry.py:351](../../road_safety/services/registry.py#L351) — `road_summary()` returns untyped dict
- [services/llm.py:476,771](../../road_safety/services/llm.py#L476) — `narrate_event(event: dict)` and `enrich_event(event: dict, ...)` accept any dict shape

**Hot-path numpy arrays are untyped:**
- [core/detection.py:927](../../road_safety/core/detection.py#L927) — `detect_frame(model, frame, ...)` — `frame` should be `NDArray[np.uint8]`
- [core/detection.py:1073](../../road_safety/core/detection.py#L1073) — `draw_thumbnail(frame, ...)`
- [services/redact.py:80](../../road_safety/services/redact.py#L80) — `_blur_roi(frame, ...)`
- [services/redact.py:151](../../road_safety/services/redact.py#L151) — `redact_for_egress(frame, detections)`

### Proposed plan (in order)

1. **Introduce a typed event schema first.** This is where the payoff is largest because every stage of the pipeline touches it.

   ```python
   # road_safety/schemas.py
   from typing import TypedDict, NotRequired
   from datetime import datetime

   class EventDict(TypedDict):
       event_id: str
       timestamp: datetime
       vehicle_id: str
       road_id: str
       driver_id: str
       risk_level: str               # "low" | "medium" | "high" | "critical"
       ttc_sec: float | None
       primary_track_id: int
       secondary_track_id: int | None
       plate_hash: NotRequired[str]  # present after enrich_event
       narration: NotRequired[str]
       thumbnail_path: NotRequired[str]
       public_thumbnail_path: NotRequired[str]
   ```

   Then change `narrate_event(event: dict)` → `narrate_event(event: EventDict)` and let mypy find the 10+ places that pass the wrong shape.

2. **Add mypy config — start loose, tighten over time.**
   ```toml
   [tool.mypy]
   python_version = "3.10"
   warn_unused_ignores = true
   warn_redundant_casts = true
   # Start here; tighten per-module as you go
   disallow_untyped_defs = false

   [[tool.mypy.overrides]]
   module = "road_safety.schemas"
   disallow_untyped_defs = true
   strict = true
   ```
   Add `make typecheck` → `mypy road_safety/`. Wire into CI and pre-commit.

3. **Type the boundaries next** — public functions in `services/`, route handlers, and the perception hot path. Leave internal helpers for later.

4. **Add numpy typing on frame params** — `from numpy.typing import NDArray`. This is a 30-minute change with immediate payoff because frame shape/dtype mistakes are a common runtime bug.

5. **Replace `dict[str, Any]` returns with Pydantic models** at service boundaries. Pydantic validates at runtime too, which is a bonus at egress points (Slack, webhook, cloud receiver).

### What **not** to do

- Don't turn on `--strict` globally on day one. You'll get 1,000+ errors and ignore them all. Tighten per-module.
- Don't replace all dicts with Pydantic at once. Start with the event dict — it's the hottest path.

---

## 2. Error handling at boundaries

- [services/llm.py](../../road_safety/services/llm.py) — `_complete()` uses bare `except Exception` to failover between Anthropic and Azure. This masks `ImportError`, `AttributeError`, and any programming bug that should crash loudly.
- [core/stream.py:22](../../road_safety/core/stream.py#L22) — background OS thread feeds frames into an asyncio queue with no timeout on `queue.put()` and no heartbeat. If the loop stalls, the thread blocks forever.
- [services/redact.py:80](../../road_safety/services/redact.py#L80) — `_blur_roi()` does not clamp `x1/y1/x2/y2` to frame bounds. A negative or out-of-bounds bbox crashes the whole pipeline.

**Fix for `_blur_roi`:**
```python
h, w = frame.shape[:2]
x1 = max(0, min(int(x1), w))
y1 = max(0, min(int(y1), h))
x2 = max(x1, min(int(x2), w))
y2 = max(y1, min(int(y2), h))
```

---

## 3. Implicit contracts / mutation

- [services/redact.py:151](../../road_safety/services/redact.py#L151) — `redact_for_egress(frame, detections)` mutates `frame` in place. Callers that reuse the frame get the redacted version unexpectedly.
- [services/llm.py](../../road_safety/services/llm.py) — `_hash_and_strip_plate()` mutates the event dict in place. The side effect is the point (privacy invariant), but the signature doesn't document it.

**Rule of thumb:** if a function mutates its input, either (a) take the mutation out of the signature and return a new object, or (b) prefix the function with `_mutate_` and put a one-line docstring noting the side effect.

---

## 4. Testing gaps

Current coverage (by file size, rough proxy):
- `tests/test_core.py` (326 lines) — TTC math, risk classification. Good.
- `tests/test_services.py` (503 lines) — registry, redaction, templates. Broad but shallow.
- `tests/test_api.py` (77 lines) — minimal endpoint coverage.

**Not covered:**
- End-to-end perception pipeline (stream → detect → enrich → emit).
- Circuit breaker state machine in `llm.py` (3 failures → 60s open).
- Async/concurrency paths — `pytest-asyncio` is installed but barely used.
- Stream failure modes (ffmpeg crash, HLS 404, webcam disconnect).
- Redaction integration (does the `_public` thumbnail actually omit plate regions?).

---

## 5. Architecture risks

### `server.py` is a god module

3,206 lines, imports from 15+ submodules, orchestrates the full lifecycle. `StreamSlot` mixes state management, HTTP routes, and perception. Extracting a `PerceptionOrchestrator` and moving routes to `api/` would halve the size and make integration tests possible without mocking the whole app.

### Event dict mutation pipeline has no validation checkpoints

By the time an event reaches `_emit_event`, it has passed through 5 mutation stages. A missing field produces a `null` downstream instead of a loud failure. Adding `EventDict` validation at ingest and egress (proposal 1 above) fixes this.

### Settings store callback pattern is loose

[settings_store.py](../../road_safety/settings_store.py) registers subscribers by string name with no type-checked callback signature. Warm-reload of e.g. `TRACK_HISTORY_LEN` depends on the subscriber running; there's no guarantee of propagation.

---

## What I'd say in the interview

> "Yes, I noted the missing type safety. My plan would be to:
>
> 1. Introduce a `TypedDict` or Pydantic model for the event that flows through the perception pipeline — that's the single highest-leverage change because every stage mutates that dict untyped today.
> 2. Wire `mypy` into `pyproject.toml` and CI, starting loose and tightening per-module so the team isn't buried in 1,000 errors on day one.
> 3. Type the boundaries next — service interfaces, route handlers, and numpy frame parameters on the hot path.
> 4. Replace `dict[str, Any]` returns in `services/` with Pydantic models so we get validation at egress points (Slack, cloud receiver) for free.
>
> I'd **not** flip `--strict` on globally on day one — that produces a pile of ignored errors. Tighten per-module so each module that passes strict stays strict."

---

## Appendix: Frontend

[frontend/tsconfig.json](../../frontend/tsconfig.json) — `strict: true`, `noUncheckedIndexedAccess: true`, `noFallthroughCasesInSwitch: true`. No `any` in `frontend/src/`. Type safety here is not the weak link.
