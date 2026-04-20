# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Common commands

- `python start.py` — one-command launcher: builds the React frontend, runs the pytest suite, starts `road_safety.server:app` via uvicorn on port 8000, waits for `/api/live/status`, then opens the admin UI in the browser.
- `python start.py --skip-tests` — skip the test run (fastest iteration loop).
- `python start.py --cloud` — also start the cloud receiver (`cloud.receiver:app`) on port 8001.
- `python start.py --no-browser --port 8000` — headless start.
- `make test` / `pytest tests/ -v` — full test suite.
- `pytest tests/test_core.py::test_name -v` — run a single test.
- `make lint` — cheap syntax check (`py_compile` on `server.py`, `config.py`, `start.py`); there is no formatter or type-checker wired up.
- `cd frontend && npm run build` — TypeScript + Vite production build into `frontend/dist/`. `start.py` does this automatically before launching the server.
- `cd frontend && npm run dev` — Vite dev server (only needed when iterating on frontend separate from the Python server).
- `docker compose up --build` / `make docker-up` — containerized run; `--profile cloud` or `make docker-up-cloud` adds the receiver.

The server is served from the built `frontend/dist/` (see `STATIC_DIR` in `road_safety/config.py`), so backend-only changes do **not** require rebuilding the frontend. If `frontend/dist/` is missing, the static-files mount fails at boot — run `cd frontend && npm run build` first (or `python start.py`, which builds it for you).

Python dependencies live in a local `.venv`; `start.py` prefers `.venv/bin/python` over the system interpreter. Install with `pip install -e ".[dev]"`.

## Architecture

This is a two-process system: an **edge node** (the main `road_safety.server`) that runs heavy perception on-device, and an optional **cloud receiver** (`cloud/receiver.py`, port 8001) that ingests HMAC-signed batched events into SQLite. Only typed JSON events + redacted thumbnails cross the wire — never raw frames or plate text. See `docs/architecture.md` for the full diagram and bandwidth math.

### Conflict-detection pipeline (the hot path)

Each frame flows through an independent stack of gates in `road_safety/core/` and `road_safety/server.py::_run_loop`. A real conflict satisfies all gates; noise fails early:

1. `StreamReader` pulls frames (HLS, file, webcam, or RTSP) at `TARGET_FPS` (default 2 fps).
2. `detect_frame` (`core/detection.py`) runs YOLOv8 + ByteTrack.
3. `TrackHistory` maintains per-track trailing windows for TTC math.
4. `EgoMotionEstimator` (`core/egomotion.py`) computes optical-flow ego-speed proxy.
5. `SceneContextClassifier` (`core/context.py`) tags urban/highway/parking and rescales thresholds.
6. `find_interactions` → depth-gate → convergence-angle → ego-relative-motion → multi-gate TTC (`estimate_pair_ttc` / `estimate_ttc_sec`).
7. `QualityMonitor` (`core/quality.py`) suppresses low-confidence events when the camera is degraded.
8. `Episode` accumulates peak risk across frames; sustained-risk downgrade demotes peaks not supported over ≥2 frames / ≥1s.
9. `_emit_event` redacts thumbnails, optionally narrates via LLM, broadcasts over SSE, tier-dispatches to Slack, and publishes to cloud.

**Do not short-circuit these gates to "improve" detection** — each one exists to kill a specific class of false positive that was causing alert fatigue. If you change a gate, run the integration tests in `tests/test_core.py`.

### Privacy invariant (non-obvious)

`enrich_event()` in [road_safety/services/llm.py](road_safety/services/llm.py) hashes the plate and strips `plate_text`/`plate_state` from the returned dict **before** it reaches any in-memory event buffer. `server.py` retains an egress `pop()` as defence in depth, but the primary invariant — **no raw plate text in any buffer** — is enforced at ingest, not at egress. Any new code path that touches vision-enrichment output must preserve this. Dual thumbnails (internal + public) are produced by `services/redact.py::write_thumbnails`; shared channels must only use the `_public` variant.

### LLM layer is enrichment, not critical path

Detection works with zero LLM calls. The LLM layer has multi-provider failover (Anthropic ↔ Azure OpenAI), a client-side token-bucket rate budget, a circuit breaker (3 failures → 60s open), self-consistency ALPR (two calls at different temps, null on disagreement), and cost/latency tracking in [services/llm_obs.py](road_safety/services/llm_obs.py). External ALPR is gated by `ROAD_ALPR_MODE` (default `off`). When adding LLM calls, route them through the existing `llm.py` helpers so they inherit all of this.

### Package layout

- `road_safety/core/` — perception: detection, stream, egomotion, quality, context.
- `road_safety/services/` — LLM, redaction, drift, watchdog, agents, registry, digest, test_runner.
- `road_safety/compliance/` — `audit.py` (audit log) and `retention.py` (hourly retention sweeps).
- `road_safety/integrations/` — `edge_publisher.py` (HMAC batched delivery), `slack.py`, `fnol.py`.
- `road_safety/api/feedback.py` — feedback routes (others live directly in `server.py`).
- `road_safety/config.py` — **single source of truth** for paths and env vars. Every module imports from here; never compute `Path(__file__).parent` in modules.
- `road_safety/logging.py` — JSON-line logger setup (`setup()` called once from the FastAPI lifespan hook). Deliberately has no dependency on `config.py` so it can import early in bootstrap. `ROAD_LOG_FORMAT=text` switches to human-readable output for local dev.
- `road_safety/security.py` — shared `require_bearer_token()` helper used by both the edge server and the cloud receiver. Constant-time token comparison, fail-closed on unset env var (503), 401/403 on missing/wrong token. Use this for any new admin-tier endpoint instead of rolling a fresh auth check.
- `tools/` — offline utilities: `analyze.py` (batch event extraction from a video file), `eval_detect.py` (detection precision/recall harness), `eval_enrich.py` (LLM enrichment scorer). See [tools/README.md](tools/README.md).
- `cloud/receiver.py` — separate FastAPI app; verifies HMAC, dedupes by `event_id` (`INSERT OR IGNORE`), stores in `data/cloud.db`.
- `frontend/` — React 19 + Vite + TypeScript + react-router. Pages: `AdminPage` (live detections), `DashboardPage` (fleet overview), `MonitoringPage` (incident-queue watchdog).

### Auth model

Three tiers of access, enforced in `server.py`:

- **Public** — SSE stream, public thumbnails, dashboard.
- **`X-DSAR-Token`** (env: `ROAD_DSAR_TOKEN`) — unredacted thumbnails. Denied attempts are audit-logged.
- **`Authorization: Bearer <ROAD_ADMIN_TOKEN>`** — `/api/audit`, `/api/llm/*`, `/api/road/*`, `/api/agents/*`, `/api/retention/*`.

When adding endpoints that read sensitive state, pick the right tier and audit-log through `road_safety/compliance/audit.py`.

### Fleet identity

Every event carries `vehicle_id`, `road_id`, `driver_id` sourced from env (`ROAD_VEHICLE_ID` / `ROAD_ID` / `ROAD_DRIVER_ID`). On startup without these, the server logs a warning and falls back to `unidentified_*_<hostname>` — events will not attribute to a real fleet. The driver safety-score model (`services/registry.py`) decays on a schedule controlled by `ROAD_SCORE_DECAY_INTERVAL_SEC` (set `0` to disable).

### Live video transport (admin grid)

The multi-source admin grid (`frontend/src/components/admin/MultiSourceGrid.tsx`)
renders one live tile per perception source via `StreamImage`
(`frontend/src/components/admin/StreamImage.tsx`). It picks one of two server
endpoints based on the page protocol:

- **HTTPS → MJPEG** (`GET /admin/video_feed/{id}`, `multipart/x-mixed-replace`).
  One persistent connection per tile; the server pushes each freshly-encoded
  JPEG with no polling latency floor.
- **HTTP → polling** (`GET /admin/frame/{id}`, single JPEG every ~400 ms).
  Used in local dev where uvicorn speaks HTTP/1.1 directly and the browser's
  6-concurrent-connections-per-host cap would deadlock MJPEG once you have
  more than ~4 tiles open alongside SSE.

**Deploy implication**: any production deployment with ≥6 streams **must**
front uvicorn with an HTTP/2 reverse proxy (nginx, Caddy, Cloudflare, ALB).
HTTP/2 multiplexes all streams over one TCP connection, dissolving the
6-conn cap. TLS termination at the proxy is what flips the frontend into
MJPEG mode automatically — no client config needed.

Operators can override the auto-detection at build time via the Vite env
var `VITE_ROAD_VIDEO_TRANSPORT=mjpeg|poll` (useful for h2c-cleartext
deployments or for forcing polling during transport debugging). The
server keeps both endpoints live regardless, so `/admin/frame/{id}` is
also available for one-shot snapshots and tests.

### Watchdog

`services/watchdog.py` groups repeated errors into fingerprinted incidents with impact + likely cause + owner + evidence + debug commands. The design goal is an **incident queue**, not a log-tail wall of red; preserve this when extending it.

## Things to avoid

- **Don't compute paths manually** — import from `road_safety/config.py`.
- **Don't leak raw plate text** — scrub at ingest in `enrich_event()`, not just at egress.
- **Don't add LLM calls outside the `services/llm.py` wrappers** — you'll bypass failover, rate budget, circuit breaker, and cost tracking.
- **Don't widen an agent's tool set past 5** — `services/agents.py` enforces this deliberately (tool-overload hallucination grows past ~5 tools).
- **Don't remove conflict-detection gates to "catch more"** — each gate targets a specific false-positive class; loosen thresholds per-scene via `AdaptiveThresholds` instead.
