# Backend improvements — FastAPI · YOLOv8 · LLM

**Scope.** The Python edge service: FastAPI HTTP surface, the perception loop (YOLOv8 + ByteTrack + OpenCV + ego-motion), the LLM layer (Anthropic / Azure OpenAI), the agent orchestration, the compliance/audit/retention plane, and the edge→cloud publisher.

**Anchor files.** [road_safety/server.py](../../road_safety/server.py) (1,535 LOC) · [services/llm.py](../../road_safety/services/llm.py) · [services/llm_obs.py](../../road_safety/services/llm_obs.py) · [services/agents.py](../../road_safety/services/agents.py) · [services/drift.py](../../road_safety/services/drift.py) · [core/stream.py](../../road_safety/core/stream.py) · [core/detection.py](../../road_safety/core/detection.py) · [config.py](../../road_safety/config.py) · [integrations/edge_publisher.py](../../road_safety/integrations/edge_publisher.py) · [compliance/audit.py](../../road_safety/compliance/audit.py).

**Date.** 2026-04-18.

---

## TL;DR

1. **Adopt Anthropic prompt caching on `ENRICH_SYSTEM` and `NARRATION_SYSTEM`.** Already wired on the chat corpus but not on the dominant per-event input blocks. ~85% reduction on input-token cost on the busy path.
2. **Audit the thread → asyncio bridge.** `_HAIKU_BUCKET._lock = asyncio.Lock()` is loop-bound; the perception thread must use `asyncio.run_coroutine_threadsafe` against a captured loop, not `asyncio.run` or `loop.call_soon_threadsafe`.
3. **Decompose [server.py](../../road_safety/server.py) (1,535 LOC) into APIRouters with a `lifespan`-managed factory.** Currently routes, orchestration, identity resolution, and middleware all share one module.
4. **OpenTelemetry traces + Prometheus metrics.** Replace the ad-hoc in-memory `LLMObserver` ring with OTel spans + a `/metrics` exporter. EU AI Act traceability obligation aligns with this.
5. **Pydantic-settings + fail-fast config.** Refuse to boot in `ENV=prod` without `THUMB_SIGNING_SECRET`, `ROAD_ADMIN_TOKEN`, etc. Today's silent defaults attribute events to `unidentified_vehicle_<host>`.
6. **EU AI Act risk register and model card.** This system is borderline high-risk under Annex III §6(d); ship the docs before pilots.
7. **YOLO accelerator selection + multi-source load governance** (R4 + R-PERF). The 2026-04-19 perf incident showed `yolov8s.pt` × 6 stream slots × CPU-only inference saturated uvicorn at ~205 % CPU; auto-device selection (CUDA → MPS → CPU) and the per-slot `detection_enabled` toggle are shipped, but auto-shedding, a detecting-slot cap, shared MJPEG fan-out, and a `perf.cpu_saturated` watchdog rule remain.

---

## P0 bugs found during review

### B2 `[H]` `_HAIKU_BUCKET._lock = asyncio.Lock()` is loop-bound

[services/llm.py](../../road_safety/services/llm.py#L85) instantiates an `asyncio.Lock` at import time. Locks bind to the loop running when first awaited; on Python 3.10+ awaiting from a different loop raises `RuntimeError: ... bound to a different event loop`. The perception thread then schedules narration/enrichment against the lifespan loop — but if any path (`asyncio.run(narrate_event(...))`) opens a fresh loop, the bucket awakens broken.

**Fix.** Capture the loop in `lifespan`:
```python
async def lifespan(app: FastAPI):
    state.loop = asyncio.get_running_loop()
    ...
    yield
```
Replace any in-thread `asyncio.run(coro)` with:
```python
fut = asyncio.run_coroutine_threadsafe(coro, state.loop)
fut.add_done_callback(lambda f: handle(f.result()))
```
For non-coroutine notifications, `state.loop.call_soon_threadsafe(queue.put_nowait, item)` against an `asyncio.Queue` consumed by an async task.

Long-term: replace ad-hoc threading with `anyio.from_thread.start_blocking_portal()` + `portal.call(coro)`. anyio is already a FastAPI dependency.

**Effort.** 2-4 h to audit every cross-thread call site.

**Citations.** https://docs.python.org/3/library/asyncio-task.html#asyncio.run_coroutine_threadsafe · https://anyio.readthedocs.io/en/stable/threads.html.

### B3 `[H]` `state.recent_events` mutated in perception thread without a lock

[server.py:883-885](../../road_safety/server.py#L883):
```python
state.recent_events.append(event)
if len(state.recent_events) > MAX_RECENT_EVENTS:
    state.recent_events.pop(0)
```
runs in the perception thread. SSE handlers and `/api/live/events` concurrently iterate via `list(state.recent_events)` ([server.py:996, 1195](../../road_safety/server.py#L996)). Each individual op is GIL-protected, but the **read+slice composite** (`state.recent_events[-SSE_REPLAY_COUNT:]`) is not — a `pop(0)` racing a slice can produce torn data, and a slow consumer can hand back stale lengths.

**Fix.** Wrap mutations and reads with a single `threading.Lock` on `state.events_lock`, or replace the list with `collections.deque(maxlen=MAX_RECENT_EVENTS)` (deque ops are GIL-atomic for `append`/`popleft`, no manual cap needed). For SSE replay, snapshot under the lock:
```python
with state.events_lock:
    snapshot = list(state.recent_events)[-SSE_REPLAY_COUNT:]
```

**Effort.** 1 h. Add a stress test (1k append/sec while 10 SSE consumers iterate).

---

## P0 — strategic (do this quarter)

### R1 `[H]` Anthropic prompt caching on `ENRICH_SYSTEM` and `NARRATION_SYSTEM`

[services/llm.py:440](../../road_safety/services/llm.py#L440) already applies `cache_control: {"type": "ephemeral"}` to the `CORPUS_TEXT` block in `chat()`. The much larger `ENRICH_SYSTEM` (~700 tokens, sent on every detection — twice due to self-consistency) and `NARRATION_SYSTEM` are **not** cached.

**Why this project.** At 2 fps with risk events firing in bursts, enrichment alone runs 20-50 calls/min on a busy intersection; each one re-pays the static instruction tokens. Cached input bills at 0.10× base (Haiku 4.5: $0.08/MTok cached vs $0.80/MTok base — 90% reduction). Cache writes cost 1.25× base; break-even is ~2 reads per write inside the 5-minute TTL. The bursty event pattern easily exceeds that.

**Adoption.** In `_complete_anthropic`, accept `system` as a list of blocks and at call sites build:
```python
system = [{
    "type": "text",
    "text": ENRICH_SYSTEM,
    "cache_control": {"type": "ephemeral"},
}]
```
For `chat()`, add a second cache breakpoint on `SYSTEM_INSTRUCTIONS` so corpus and instructions invalidate independently.

**Telemetry.** Surface `usage.cache_creation_input_tokens` and `usage.cache_read_input_tokens` in `LLMRecord` ([services/llm_obs.py:46](../../road_safety/services/llm_obs.py#L46)). Without this you can't verify cache hits.

**Trade-off.** Five-minute idle invalidates the cache; next event pays the 1.25× write penalty. Acceptable.

**Citations.** Prompt caching — https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching · Pricing — https://docs.anthropic.com/en/release-notes/api.

### R2 `[H]` Decompose `server.py` into APIRouters + `lifespan` factory

`server.py` is 1,535 lines mixing route handlers, perception orchestration, identity resolution, SSE state, and middleware. Gripes the audit caught: routes execute module-level setup at import time (`setup_logging()`, identity resolution, EdgePublisher start, drift loop, retention loop, watchdog start), making test startup expensive and order non-deterministic.

**Adoption.**
- Create `road_safety/api/{events.py, llm.py, watchdog.py, admin.py, sse.py, road.py, agents.py}`. Each defines `router = APIRouter(prefix="/api/...", tags=["..."])`.
- Keep `server.py` as a thin assembly:
  ```python
  app = FastAPI(lifespan=lifespan)
  app.include_router(events.router)
  app.include_router(llm.router)
  ...
  ```
- Move existing module-level setup into `@asynccontextmanager async def lifespan(app)`.
- Convert the global `state` dict into a dependency: `def get_state() -> AppState: return _state`, injected via `Annotated[AppState, Depends(get_state)]`. Tests can inject a fake.

**Trade-off.** ~1 day refactor; risk of regressing the SSE replay buffer if globals move without care. Migrate one router per PR; lock SSE last.

**Citations.** Bigger applications — https://fastapi.tiangolo.com/tutorial/bigger-applications/ · Lifespan — https://fastapi.tiangolo.com/advanced/events/ · Dependencies — https://fastapi.tiangolo.com/tutorial/dependencies/.

### R3 `[H]` OpenTelemetry traces + Prometheus metrics; replace `LLMObserver`

[services/llm_obs.py](../../road_safety/services/llm_obs.py) is a clean in-process ring (2k records) but cannot answer "why did event X take 4s end-to-end across detect → enrich → narrate → SSE?" Without spans you have no causal trace. For an EU AI Act high-risk system (R10), traceability is a formal Article 12 obligation. Prometheus exposes the things you most want on edge: FPS, frame queue depth, circuit-breaker state, Haiku bucket level, watchdog incident rate.

**Adoption.**
```bash
pip install opentelemetry-distro \
  opentelemetry-instrumentation-fastapi \
  opentelemetry-instrumentation-httpx \
  openinference-instrumentation-anthropic \
  prometheus-fastapi-instrumentator
```
In `lifespan`: configure an OTLP exporter (Phoenix / Tempo / Honeycomb), `OpenInferenceAnthropicInstrumentor().instrument()`, and `Instrumentator().instrument(app).expose(app, "/metrics")`.

Wrap perception stages with `tracer.start_as_current_span("detect"|"track"|"ttc"|"enrich"|"narrate")`. Keep `LLMObserver` for the dashboard tile but back it with an OTel metric reader; Grafana then renders FPS, p95 latency, cache-hit ratio, cost burn.

**Trade-off.** ~150 MB of extra Python deps and an OTLP collector to run. On a Jetson Nano this is non-trivial — use a sidecar collector if RAM is tight; skip Phoenix on-device, export remotely.

**Citations.** OTel Python — https://opentelemetry.io/docs/languages/python/automatic/ · OpenInference (LLM semantic conventions) — https://github.com/Arize-ai/openinference · Langfuse + OTel — https://langfuse.com/docs/integrations/opentelemetry/example-python · prometheus-fastapi-instrumentator — https://github.com/trallnag/prometheus-fastapi-instrumentator.

### R4 `[H]` YOLOv8 accelerator selection + export + half precision for the deployment target

`load_model()` in [core/detection.py](../../road_safety/core/detection.py) historically called `YOLO(path)` with no `.to(device)`, which silently pinned inference to **CPU on every host** — including dev Macs and any deployment without explicit cuda env. Combined with N concurrent stream slots (each running YOLO at `ROAD_TARGET_FPS`), this was the dominant cause of perceived UI lag during multi-source demos.

**P0 perf incident (2026-04-19).** With `ROAD_STREAM_SOURCES` set to 6 live HLS URLs and detection enabled on all, `uvicorn` saturated at **~205 % CPU** (≈ two full M-series cores), starving the asyncio event loop and making SSE / MJPEG / page navigation feel laggy. Root cause: 6 streams × 2 fps × `yolov8s.pt` on CPU = 12 inferences/sec single-process. **Fix that landed:** auto-select the best available accelerator in `load_model()` (CUDA → MPS → CPU) with a `ROAD_YOLO_DEVICE` override and a defensive fallback. Post-fix: same 6 streams, same model — uvicorn drops to **~54 % CPU** (≈ 4× faster) on Apple Silicon via MPS. See `road_safety/core/detection.py::load_model`.

**Why this still matters even with auto-device.** At 2 fps a Jetson Orin Nano runs `yolov8s` PyTorch FP32 at ~25-30 ms/frame; TensorRT FP16 brings it under 10 ms, freeing CPU/GPU headroom for the Farneback ego-motion pass and reducing the chance of a queued frame backlog when Anthropic is slow. MPS / CUDA in PyTorch eager mode is a floor, not a ceiling.

**Adoption (remaining work).**
- Add `tools/export_model.py`: `--target {jetson, intel-cpu, mac-coreml, x86-onnx}` runs `model.export(format=...)`. Pin Ultralytics version in `pyproject.toml`.
- Ultralytics `YOLO()` already handles `.engine` / `.onnx` / `.pt` / `.openvino` / `.mlpackage` from the file extension — branch is automatic.
- Per-target: TensorRT (Jetson), OpenVINO (Intel CPU), CoreML (Mac demos), ONNX (portable).
- Surface the resolved device in `/api/admin/health` and `/api/live/status` so operators (and the watchdog) can detect a silent CPU-fallback regression.
- Add a startup smoke check: if `device == "cpu"` AND `len(state.slots) > 2`, log a `WARNING` recommending MPS/CUDA or `detection_enabled=false` on N-2 slots (cross-references R-PERF below).

**Trade-off.** Per-target export becomes a build artifact. TensorRT engines are tied to the exact GPU + driver — cannot ship one binary across mixed Jetson generations. MPS occasionally has op-coverage gaps in newer Ultralytics releases; the `ROAD_YOLO_DEVICE=cpu` escape hatch covers the regression case.

**Citations.** Ultralytics export — https://docs.ultralytics.com/modes/export/ · TensorRT — https://docs.ultralytics.com/integrations/tensorrt/ · OpenVINO — https://docs.ultralytics.com/integrations/openvino/ · PyTorch MPS — https://pytorch.org/docs/stable/notes/mps.html.

### R-PERF `[H]` Multi-source load governance: per-slot detection toggle + runtime CPU guard

The same incident exposed a structural gap: the multi-source slot manager ([core/stream.py](../../road_safety/core/stream.py) + the `StreamSlot` lifecycle in [server.py](../../road_safety/server.py)) had no operator-controlled way to keep watching a camera *without* paying YOLO cost on it, and no runtime brake when N slots × FPS × per-frame cost exceeded available compute. The system would just degrade everything (UI included) until the operator manually paused tiles.

**What landed.** A per-slot `detection_enabled: bool` flag on `StreamSlot` plus `POST /api/live/sources/{id}/detection?enabled=...`. When false, `_on_frame` still encodes the raw JPEG into the MJPEG buffer (preview keeps working) but short-circuits before YOLO / quality / scene / episode logic. Frontend gets a per-tile checkbox and bulk Select/Clear-all controls (`MultiSourceGrid.tsx`). This is the operator-facing escape valve.

**What's still recommended.**

1. **Auto-shedding policy.** Sample uvicorn process CPU + per-slot `frames_processed / wall_seconds` over a rolling 30 s window. When CPU > 85 % AND the slowest slot's effective FPS drops below `0.5 * ROAD_TARGET_FPS`, automatically toggle `detection_enabled=false` on the **least-recently-focused** slot. The `focusedId` plumbed through `MultiSourceGrid` → `AdminPage` is the natural priority signal and, since 2026-04-19, is persisted to `localStorage.road_admin_focused_id` with a `admin-focused-id-changed` custom event — so any server-side shedder can read the operator's intent via an eventually-reported `POST /api/live/focus` (not yet implemented; today the signal is frontend-local but durable across reloads and consumed by the `SettingsPage` Live Preview). See [frontend.md](./frontend.md). Emit an audit event so the operator sees what was shed and why.
2. **Cap on simultaneous detection slots.** A `ROAD_MAX_DETECTING_SLOTS` env (default ≈ N-1 where N = available perf cores) refuses new `detection_enabled=true` requests once the cap is reached, with a 409 carrying the suggested slot to disable. Prevents the operator from re-saturating the box after a shed.
3. **MJPEG fan-out cost.** Each tile in the browser opens its own `/admin/video_feed/{id}` MJPEG connection; six concurrent multipart streams pin the renderer process too (we saw Cursor's renderer at ~76 % during the incident). Add a server-side **shared encoder** per slot (encode the JPEG once, broadcast bytes to all subscribers via an `asyncio.Queue` per consumer) and downscale + drop-to-keyframe for the minimized tiles. The new "focused vs minimized" tile state from the frontend (`tileMini` class) is the right hint: minimized tiles can be served at e.g. 160 px wide / 1 fps with no perception cost.
4. **Watchdog rule.** Add a finding category `perf.cpu_saturated` triggered when uvicorn CPU > 90 % for > 60 s, surfacing the offending slot list and quoting the auto-shed action (or the manual one if shedding is disabled).

**Why this project.** Multi-source perception is now first-class (see `ROAD_STREAM_SOURCES` parsing in [config.py](../../road_safety/config.py) and the `StreamSlot` registry in `server.py`), so "operator added a sixth stream and the box melted" is a foreseeable failure mode, not an exotic one. The detection toggle is the manual fix; (1)–(4) above keep the system useful when the operator forgets.

**Effort.** ~1 day for auto-shed + cap + watchdog rule. Shared MJPEG encoder is ~2 days but pays back as soon as more than 4 slots are configured.

**Trade-off.** Auto-shedding is opinionated — some operators will want to be told, not auto-corrected. Make it opt-in via `ROAD_AUTOSHED=true`, default off; surface the suggestion in the watchdog regardless.

**Citations.** psutil per-process CPU — https://psutil.readthedocs.io/en/latest/#psutil.Process.cpu_percent · Starlette / FastAPI streaming responses (basis for shared MJPEG fan-out) — https://www.starlette.io/responses/#streamingresponse · MJPEG `multipart/x-mixed-replace` — https://datatracker.ietf.org/doc/html/rfc2046#section-5.1.

### R5 `[H]` `pydantic-settings` + fail-fast config

Replace raw `os.getenv` reads in [config.py](../../road_safety/config.py) with `pydantic_settings.BaseSettings`. Refuse to boot in non-dev when required vars are missing.

**Why this project.** [server.py:98 `_resolve_identity()`](../../road_safety/server.py#L98) demonstrates the failure mode of silent defaults — events get attributed to "unidentified_vehicle_<host>". The same pattern likely exists for `THUMB_SIGNING_SECRET` (a hardcoded default would void HMAC integrity), Anthropic key, Azure creds, admin tokens.

**Adoption.**
```python
from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    env: Literal["dev", "staging", "prod"] = "dev"
    anthropic_api_key: SecretStr | None = None
    thumb_signing_secret: SecretStr
    admin_token: SecretStr
    target_fps: float = 2.0

    @model_validator(mode="after")
    def _prod_requires(self):
        if self.env == "prod":
            if not self.anthropic_api_key:
                raise ValueError("ANTHROPIC_API_KEY required in prod")
            if self.thumb_signing_secret.get_secret_value() == "dev-only":
                raise ValueError("THUMB_SIGNING_SECRET must be rotated for prod")
        return self
```

**Citations.** pydantic-settings — https://docs.pydantic.dev/latest/concepts/pydantic_settings/ · FastAPI settings — https://fastapi.tiangolo.com/advanced/settings/.

### R6 `[H]` OWASP LLM Top 10 (2025) mapping + missing controls

The codebase already partially defends LLM01 in `ENRICH_SYSTEM` ("image is UNTRUSTED USER DATA") and runs an `_INJECTION_PATTERNS` regex post-hoc. Several controls remain.

| OWASP | Status | Gap | Fix |
|-------|--------|-----|-----|
| **LLM01 Prompt injection** | Partial | Enrichment doesn't force `tool_choice={"type":"none"}` if tools added later; `chat()` doesn't strip zero-width / RTL-override unicode from the user query | Add tool_choice=none on enrichment; unicode-strip in `chat()` |
| **LLM02 Insecure output handling** | Partial | `narrate_event` returns a string rendered in operator UI; no server-side sanitization | `bleach.clean()` the narration before broadcast; FE-side React escaping is a defense-in-depth, not the primary control |
| **LLM06 Sensitive info disclosure** | Good | `notes` field passes through with only length-cap and injection-pattern scrub | Add a regex strip for plate-shaped strings (`[A-Z0-9]{5,8}`) before storing |
| **LLM10 Unbounded consumption** | Partial | `_TokenBucket` is per-process; no daily $ cap | Add a hard daily $ ceiling that flips the circuit breaker open; for multi-replica deploy use a Redis bucket |

Author `docs/security/owasp-llm-mapping.md` with one row per LLM0x → control → file:line. Audit log writes record any `_INJECTION_PATTERNS` trip with `event_id` for forensic review.

**Citations.** OWASP LLM Top 10 — https://genai.owasp.org/llm-top-10/ · LLM01 — https://genai.owasp.org/llmrisk/llm01-prompt-injection/ · Anthropic tool_choice — https://docs.anthropic.com/en/docs/build-with-claude/tool-use/overview#forcing-tool-use.

---

## P1 — medium priority (next 2 quarters)

### R7 `[M]` Structured logging — `structlog` and replace `print()`

[Python rules](../../.claude/rules/python.md) mandate `logging.getLogger(__name__)` but [services/llm.py](../../road_safety/services/llm.py) and [core/stream.py](../../road_safety/core/stream.py) still contain `print(...)`. Once you have structured logs, the OTel log exporter (R3) ships them to Loki / Datadog with span-correlation IDs.

**Adoption.** Extend [road_safety/logging.py](../../road_safety/logging.py) to configure `structlog` with a JSON renderer in prod and a console renderer in dev. Lint rule (after R15): forbid `print` outside `start.py` / `tools/`.

**Citation.** https://www.structlog.org/en/stable/.

### R8 `[M]` Model serving graduation path — stay in-process; document the trigger

At 2 fps single-camera, **stay in-process**. Triton / BentoML / Ray Serve / KServe pay off when you have multiple cameras per host, multiple model variants (A/B), or remote inference clients. For an edge box with one stream, the IPC cost dominates the latency budget.

**When to graduate.**
- **NVIDIA Triton** — graduate here when you need >1 model on the same GPU with dynamic batching (e.g., a road-condition classifier alongside YOLO). Sub-2ms scheduling overhead; supports TensorRT / ONNX / PyTorch in one process.
- **BentoML** — best dev-ergonomics for Python-first teams; runs on a single edge box without K8s. Use if one Bento bundle should pack YOLO + scene classifier + a future drowsiness model.
- **Ray Serve** — only if you're already on Ray.
- **KServe** — cluster-native; ignore until you move off-edge.

**Adoption.** No code change today. Add `docs/adr/0001-model-serving.md` with the trigger conditions: ">1 GPU model" or ">1 camera per host" → migrate to Triton over gRPC localhost.

**Citations.** Triton — https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/index.html · BentoML — https://docs.bentoml.com/.

### R9 `[M]` Agent maturation — evals, `tool_choice`, parallel tools

Two short-term wins on [services/agents.py](../../road_safety/services/agents.py) before considering a framework migration:

- **Tool-call correctness eval set.** YAML of `(transcript, expected_tool_calls)` scored by `pytest`. Without this you cannot ship agent changes safely.
- **`tool_choice` forcing.** For the investigation agent, force the first tool with `tool_choice={"type":"tool","name":"query_events"}` so it can't short-circuit to a hallucinated answer on turn 1.
- **Parallel tool calls.** Anthropic Claude 4 supports parallel tool use; the report agent (which gathers compliance + recent events + LLM stats) can do 3 calls in one turn instead of 3 sequential turns. ~3× latency reduction on report generation.

**When to migrate to a framework.** If agent count grows past 5 or you need long-horizon memory, evaluate Anthropic's Agent SDK (Managed Agents) and LangGraph. Anthropic Agent SDK is lighter and aligns with your existing SDK.

**Adoption.**
- `tests/test_agents_eval.py` with 20 transcripts; CI gates on ≥18/20 tool-call match.
- In `services/agents.py`, batch tool execution: gather all `tool_use` blocks from one response, run them in `asyncio.gather`, feed all `tool_result` blocks back in one user turn.

**Citations.** Tool use — https://docs.anthropic.com/en/docs/build-with-claude/tool-use/overview · Parallel tool use — https://docs.anthropic.com/en/docs/build-with-claude/tool-use/parallel-tool-use · Agent SDK — https://docs.anthropic.com/en/api/agent-sdk.

### R10 `[M]` EU AI Act risk register and operational compliance docs

This project is **borderline high-risk** under the EU AI Act. Annex III §6(d) covers "AI systems intended to be used for risk assessment in real-time of natural persons in the context of road traffic". A system that flags driver behavior for fleet operations and writes to an audit log used in disciplinary processes likely qualifies. If sold into EU operators, the obligations apply regardless of where the edge box sits.

**Concrete obligations to map.**
- **Art. 9 — Risk management.** `docs/aia/risk-register.md` listing failure modes (false-negative collision, false-positive plate read, drift past threshold).
- **Art. 10 — Data governance.** Datasheet for the YOLOv8 model (COCO + any fine-tune set). The shipped `yolov8s.pt` has no datasheet — author one.
- **Art. 11 + 12 — Technical docs + logging.** Audit trail exists; add a model card per deployed model (R11) and ensure logs are tamper-evident (HMAC-chained JSONL or append-only S3 Object Lock when off-edge).
- **Art. 13 — Transparency.** Mark narration text as AI-generated to operators. Add `source: "claude-haiku-4-5"` on every narrated SSE event.
- **Art. 14 — Human oversight.** Already partially designed (operator copilot, feedback loop). Document the override path: operators flag any event as false-positive ([api/feedback.py](../../road_safety/api/feedback.py)).
- **Art. 15 — Accuracy / robustness / cybersecurity.** Drift monitor (R11) is the metric. Publish a "minimum acceptable mAP@0.5 = X" gate that pages an operator when violated.
- **Art. 72 — Post-market monitoring.** Drift + AL + retraining loop closure (R11).

**Adoption.** Author `docs/aia/{risk-register, model-card, dpia, post-market-plan}.md` once; review quarterly.

**Citations.** Art. 6 — https://artificialintelligenceact.eu/article/6/ · Annex III — https://artificialintelligenceact.eu/annex/3/ · Art. 9 — https://artificialintelligenceact.eu/article/9/ (and following articles to 15).

### R11 `[M]` Model registry + drift A/B + retraining loop closure

[services/drift.py](../../road_safety/services/drift.py) (547 LOC) computes drift signals and an active-learning sampler — but the export currently goes nowhere ([README](../../README.md) mentions it; [challenges.md §5](../challenges.md) acknowledges the loop is not closed). Without registry + retraining trigger this is observability theatre.

**Adoption.**
- **MLflow** for the registry (open-source, single-binary, SQLite-backed — fits the edge ethos). `models:/yolo-fleet/Production` URI handles deploy hand-off.
- **Evidently** or **NannyML** for drift signals beyond what `services/drift.py` already does.
- **Shadow deploys.** Load `yolo-fleet@Production` and `yolo-fleet@Candidate` in `core/detection.py`, run both per frame, log per-class divergence. Promote candidate when shadow agreement ≥ 0.95 and false-negative rate against operator feedback ≤ baseline.
- **AL loop closure.** Export bundle → labeling tool (Label Studio / CVAT) → fine-tune job → registry → shadow → promote.

**Trade-off.** MLflow + Evidently each add ~80 MB of deps. On Jetson, run them off-edge in a fleet-management cloud service; the edge box only ships drift metrics + AL exports.

**Citations.** MLflow registry — https://mlflow.org/docs/latest/model-registry.html · Evidently — https://docs.evidentlyai.com/ · NannyML — https://docs.nannyml.com/.

### R12 `[M]` Anthropic SDK 2025+ features

| Feature | Adopt? | Rationale |
|---------|--------|-----------|
| Prompt caching | **Yes — R1** | Dominant cost lever |
| Extended thinking | Selective | Investigation agent only — narration/enrichment must stay fast |
| `tool_choice` forcing | **Yes — R9** | Bound first action of investigation/report agents |
| Batch API (Message Batches) | **Yes — for AL backfill** | Async re-score of AL candidates at 50% discount; perfect for the export-to-nowhere queue (R11) |
| Files API | **Yes — for chat corpus** | Replace inline corpus block with a server-side file reference; survives sessions, prunes prompt size |
| Streaming with cache hits | Selective | Enable for `chat()` only; narration is too short to benefit |
| Computer use | N/A | Out of scope |
| Vision | Already used | Confirm `media_type` matches actual JPEG quality from [services/redact.py](../../road_safety/services/redact.py) |

**Citations.** Extended thinking — https://docs.anthropic.com/en/docs/build-with-claude/extended-thinking · Batch processing — https://docs.anthropic.com/en/docs/build-with-claude/batch-processing · Files — https://docs.anthropic.com/en/docs/build-with-claude/files.

### R13 `[M]` Pydantic v2 models for events, payloads, SSE frames

The event dict shape is implicit, defined by `core/detection.build_event_summary` and mutated by `enrich_event`, narration, redaction, edge publisher. Replace dicts with `class SafetyEvent(BaseModel)`. Use `model_dump(exclude_none=True)` for SSE wire format and HMAC-signed batches.

**Why this project.** Pydantic models give you `response_model=SafetyEvent` on the SSE endpoint, OpenAPI export consumed by the cloud receiver, and contract tests against `/openapi.json` ([integration.md R3.1](./integration.md#r31-h-schemathesis-against-the-live-fastapi-app)).

**Adoption.** Define `road_safety/models.py` with `SafetyEvent`, `EnrichedEvent`, `NarratedEvent` (composition not inheritance). Migrate one site at a time; keep the dict path until the cloud receiver consumes the new schema.

**Trade-off.** Pydantic v2 validation costs ~50 µs per event; negligible at 2 fps.

**Citations.** Pydantic — https://docs.pydantic.dev/latest/ · response_model — https://fastapi.tiangolo.com/tutorial/response-model/.

### R14 `[M]` Wire ruff + pyright in CI

The [Python rules](../../.claude/rules/python.md) say "make lint only does py_compile … Don't introduce one without asking." This is the **ask**. The absence of types in a 6,500-LOC codebase mixing asyncio + threads + numpy is an active risk. The B2 cross-loop concern is exactly the bug class a careful annotation catches.

**Adoption.** Phased:
- **Phase a.** `ruff format` and `ruff check --select=E,F,I,B,UP,SIM` in CI as warning.
- **Phase b.** `pyright --strict` on `services/llm.py` and `core/stream.py` first; expand quarterly.
- Pin versions in `pyproject.toml` `[project.optional-dependencies].dev`.

**Trade-off.** First pass adds ~200 small fixes. Run `ruff format` once, commit; then `ruff check --fix`, commit; then incremental annotations.

**Citations.** Ruff — https://docs.astral.sh/ruff/ · Pyright — https://microsoft.github.io/pyright/.

### R15 `[M]` Constant-time HMAC + replay-protected batch delivery

Verify [edge_publisher.py](../../road_safety/integrations/edge_publisher.py) and [cloud/receiver.py](../../cloud/receiver.py) use `hmac.compare_digest`, not `==`. (Spot-checked: receiver looks correct via `secrets.compare_digest`; verify edge side.) Then add nonce tracking + canonicalized signing — see [integration.md §8](./integration.md#8--hmac-hardening) for the full plan.

**Citation.** https://docs.python.org/3/library/hmac.html#hmac.compare_digest.

---

## P2 — lower priority (when calmer)

### R16 `[L]` Test improvements

- **Contract test against `/openapi.json`** — see [integration.md R3.1](./integration.md#r31-h-schemathesis-against-the-live-fastapi-app).
- **Golden-frame fixtures.** Three labeled JPEGs in `tests/fixtures/frames/` covering: (a) two converging vehicles, (b) pedestrian near-miss, (c) clean scene. `tests/test_core.py::test_detect_golden_frames` runs the perception pipeline end-to-end with `enrich`/`narrate` mocked. Catches detection-gate regressions immediately.
- **Hypothesis property tests** for `core/detection.estimate_ttc_sec` — pure arithmetic; property tests over `(relative_velocity, distance)` ranges catch divide-by-zero and sign-flip bugs the operator-gathered cases miss.
- **Mutation testing.** `mutmut run --paths-to-mutate=road_safety/core/detection.py`. Mutation score < 60% means weak detection tests. Run quarterly, not in CI.
- **`httpx.AsyncClient` for endpoint tests.** Required for SSE testing; `TestClient` is sync.

**Citations.** Hypothesis — https://hypothesis.readthedocs.io/ · mutmut — https://mutmut.readthedocs.io/ · httpx async — https://www.python-httpx.org/async/.

### R17 `[L]` Reliability & graceful shutdown

- **Idempotency tokens** on `/api/feedback` POST so a flaky operator double-tap doesn't duplicate. `Idempotency-Key: <uuid>`; reject within 24h.
- **Graceful shutdown.** `lifespan` cleanup must `stream_reader.stop()`, drain SSE queues, flush JSONL outbound queue with `os.fsync`, and join LLM in-flight tasks (5s timeout).
- **Systemd `Restart=on-failure`** for bare metal; `restart: unless-stopped` in `docker-compose.yml`.
- **JSONL durability.** POSIX append is atomic up to PIPE_BUF (4 KB). Events larger than that need an explicit lock or per-record `fsync`. Confirm record sizes; if any approach 4 KB, switch to length-prefix framing.

### R18 `[L]` `/healthz` vs `/readyz` (k8s pattern)

See [integration.md R12.1](./integration.md#r121-h-split-healthz-liveness-from-readyz-readiness).

### R19 `[L]` Dockerfile polish

- **Multi-stage** `python:3.12-slim` builder → distroless or chainguard `python` runtime. Today's image likely ~1.5 GB; staged + distroless reaches ~400 MB.
- **Buildkit cache mounts:** `RUN --mount=type=cache,target=/root/.cache/pip pip install ...` — 5-10× rebuild speedup.
- **Multi-arch:** `docker buildx build --platform linux/amd64,linux/arm64`.
- **Non-root user:** `USER 65532`.
- **Pre-baked YOLO export:** copy `yolov8s.engine` (per-target) into the image; do not export at first boot.

**Citations.** Buildkit cache — https://docs.docker.com/build/cache/ · Distroless — https://github.com/GoogleContainerTools/distroless.

### R20 `[L]` Slack / PagerDuty for watchdog escalation

[services/watchdog.py](../../road_safety/services/watchdog.py) is mature (1,068 LOC). Confirm escalation: WARN → log only, ERROR → Slack ([integrations/slack.py](../../road_safety/integrations/slack.py)), CRITICAL → PagerDuty (add `integrations/pagerduty.py`). Tag every page with `event_id` + trace_id (from R3) so on-call jumps to the OTel trace.

---

## What I would NOT recommend

- **Migrate to gRPC for internal services.** Single-process FastAPI is the right shape; gRPC adds binary debugging difficulty for a service serving 50 RPM peak.
- **Switch from JSONL to Kafka for the outbound queue.** Kafka on edge is ~1 GB RAM minimum and a Zookeeper / KRaft cluster. The JSONL outbox + HMAC + cloud `INSERT OR IGNORE` already gives effectively-once semantics — see [integration.md R9.1](./integration.md#r91-h-formalize-the-outbox-pattern).
- **Replace YOLOv8 with a transformer-based detector** (RT-DETR, GroundingDINO). At 2 fps the YOLO speed advantage is irrelevant; the win would be open-vocabulary detection. Not worth the 5-10× compute hit unless customer requirements drive it.
- **LangChain or LangGraph today.** The custom 5-tool agents are well-bounded. Migrate only if agent count grows past 5 or long-horizon memory becomes a requirement (R9).
- **Move `state` into Redis.** Single-process; in-process is faster and simpler. Redis becomes useful only when multi-replica deploy lands (R6.1's rate-limit backend).

---

## 90-day phased rollout

### Weeks 1-2 — bug fixes + highest-leverage cost win
- **B2** loop bridge audit · 4 h
- **B3** events lock or deque · 1 h
- **R1** prompt caching on ENRICH/NARRATION + telemetry · 0.5 d
- **R5** pydantic-settings + fail-fast · 0.5 d
- **R15** verify edge HMAC compare_digest · 1 h

### Weeks 3-6 — architecture + observability
- **R2** server.py router split + lifespan factory · 5 d (largest mechanical refactor)
- **R3** OTel + Prometheus · 2 d
- **R7** structlog migration · 1 d
- (paired with [integration.md R7.1, R12.2](./integration.md))

### Weeks 7-9 — perception + MLOps
- **R4** YOLO export pipeline + per-target Docker · 2 d
- **R11** MLflow registry + AL loop closure planning · 3 d
- **R13** Pydantic event models · 2 d

### Weeks 10-12 — compliance + agents
- **R10** EU AI Act docs (risk register, model card, DPIA, post-market plan) · 3 d
- **R6** OWASP LLM Top 10 mapping + missing controls · 2 d
- **R9** agent eval set + parallel tools + tool_choice · 2 d
- **R16** golden-frame tests + hypothesis on TTC · 1 d

### Defer past 90 days
- R8 (Triton — only when 2nd model lands), R14 (linter — needs explicit project sign-off per Python rules), R12 (Files API, Batch API — when loop closure ships), R17/R18/R19/R20 (polish).

---

## File → recommendation map

| File | Recommendations |
|------|-----------------|
| [server.py](../../road_safety/server.py) | B3 · R2 · R3 · R-PERF (auto-shed, slot cap, watchdog rule, shared MJPEG) |
| [services/llm.py](../../road_safety/services/llm.py) | B2 · R1 · R6 · R7 · R12 |
| [services/llm_obs.py](../../road_safety/services/llm_obs.py) | R1 (cache token telemetry) · R3 |
| [services/agents.py](../../road_safety/services/agents.py) | R9 · R12 |
| [services/drift.py](../../road_safety/services/drift.py) | R10 (Art. 15) · R11 |
| [services/registry.py](../../road_safety/services/registry.py) | R11 (MLflow integration) |
| [core/stream.py](../../road_safety/core/stream.py) | B2 · R7 · R-PERF |
| [core/detection.py](../../road_safety/core/detection.py) | R4 (auto-device shipped; export work pending) · R16 (golden frames, hypothesis) |
| [config.py](../../road_safety/config.py) | R5 · R-PERF (`ROAD_YOLO_DEVICE`, `ROAD_MAX_DETECTING_SLOTS`, `ROAD_AUTOSHED`) |
| [integrations/edge_publisher.py](../../road_safety/integrations/edge_publisher.py) | R15 (and see [integration.md §8](./integration.md#8--hmac-hardening)) |
| [Dockerfile](../../Dockerfile) | R19 |
| [pyproject.toml](../../pyproject.toml) | R14 (ruff + pyright deps) |
| [tests/](../../tests/) | R16 (contract, golden, hypothesis, mutmut) |
| (new) `road_safety/models.py` | R13 |
| (new) `docs/aia/` | R10 |
| (new) `docs/adr/0001-model-serving.md` | R8 |
| (new) `docs/security/owasp-llm-mapping.md` | R6 |

---

**Closing.** The codebase makes architecturally sound choices for an edge demo: in-process state, append-only JSONL, single-process FastAPI, careful redaction at the LLM boundary, real circuit breaker. The improvement surface is mostly *observability*, *compliance*, and *cost optimization* — not foundational rework. Highest-ROI change: **R1 (prompt caching)**. Highest-risk-reducing change: **B2 (loop bridge audit)**. Do those first.
