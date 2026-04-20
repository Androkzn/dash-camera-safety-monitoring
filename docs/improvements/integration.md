# Integration improvements — FE↔BE↔cloud

**Scope.** The seam between the React SPA, the FastAPI edge backend, and the optional HMAC-signed cloud receiver. Transports, auth tiers, contracts, observability of the integration plane.

**Anchor files.** [server.py](../../road_safety/server.py) · [security.py](../../road_safety/security.py) · [edge_publisher.py](../../road_safety/integrations/edge_publisher.py) · [cloud/receiver.py](../../cloud/receiver.py) · [frontend/src/hooks/useSSE.ts](../../frontend/src/hooks/useSSE.ts) · [frontend/src/lib/api.ts](../../frontend/src/lib/api.ts) · [frontend/src/types.ts](../../frontend/src/types.ts).

**Date.** 2026-04-18.

---

## TL;DR

1. **Fix B1 (unauthenticated watchdog DELETE) today.** Real defect.
2. **Generate FE types from `/openapi.json`.** Stops silent contract drift cold; fastest credibility win in the codebase.
3. **Last-Event-ID resumption on SSE.** The browser already sends the header; the server never emits `id:` lines, so reconnects lose data. ~50 LOC fix.
4. **W3C Trace Context end-to-end** (FE → BE → perception thread → Anthropic). Without it you cannot answer "why did this event take 9 seconds".
5. **JWT for admin auth + per-user audit attribution.** Static shared token cannot rotate, cannot revoke, cannot attribute. Acceptable for a demo, embarrassing for an audit.
6. **slowapi rate limit on `/chat` and `/api/agents/*`.** Both are unauthenticated and call the LLM. One scraper burns the Anthropic budget in minutes.

---

## P0 bugs found during review

### B1 `[H]` Watchdog DELETE / POST-delete are unauthenticated

[server.py:1609](../../road_safety/server.py#L1609) and [server.py:1618](../../road_safety/server.py#L1618):

```python
@app.delete("/api/watchdog/findings")
def watchdog_delete_findings(clear_all: bool = False):
    if clear_all:
        removed = watchdog_delete(indices=None)
        return {"deleted": removed}
    ...

@app.post("/api/watchdog/findings/delete")
async def watchdog_delete_selected(request: Request):
    body = await request.json()
    keys: list[str] = body.get("keys", [])
    ...
```

No `_require_admin` call. Compare with [server.py:1426](../../road_safety/server.py#L1426) (`/api/road/vehicle/{vehicle_id}`) which correctly gates with `_require_admin(request, "road vehicle detail")`.

**Impact.** Any unauthenticated origin reachable on the listening port — including any browser tab on a localhost demo, or any host on the LAN in a fleet pilot — can wipe operator state with a single request. The CSRF analysis below assumes header-bearer auth; with no auth at all, even a `<form action="/api/watchdog/findings?clear_all=true" method="POST">` (after the small change to add a POST alias) would work cross-origin.

**Fix.**
```python
@app.delete("/api/watchdog/findings")
def watchdog_delete_findings(request: Request, clear_all: bool = False):
    _require_admin(request, "watchdog findings clear")
    ...

@app.post("/api/watchdog/findings/delete")
async def watchdog_delete_selected(request: Request):
    _require_admin(request, "watchdog findings delete")
    ...
```

**Effort.** 10 minutes. Add an integration test that asserts 401 without bearer.

**Citation.** OWASP ASVS V4.2 Operations Authorization — https://owasp.org/www-project-application-security-verification-standard/.

---

## 1 — Transport: SSE / WebSocket / HTTP/2 / WebTransport

### R1.1 `[H]` Implement Last-Event-ID resumption

The HTML5 SSE spec requires the user agent to send `Last-Event-ID` on reconnect when the prior stream emitted `id:` lines, and the server SHOULD resume from there. [useSSE.ts](../../frontend/src/hooks/useSSE.ts) constructs `new EventSource(url)` correctly (the browser auto-attaches the header), but [server.py:1061](../../road_safety/server.py#L1061) never emits `id:`. Result: every disconnect (Wi-Fi flap, tab background, nginx idle close, Cloudflare ~100s timeout) drops up to `SSE_REPLAY_COUNT` of context, and the client can sometimes show duplicates because dedup is in-memory by `event_id` rather than by stream cursor.

**Adoption.**
- Maintain a monotonic `state.event_seq` (already implicit in `recent_events` ordering — formalize it).
- Emit `f"id: {seq}\nevent: safety_event\ndata: {json}\n\n"`.
- On request entry, read `request.headers.get("last-event-id")`, slice `recent_events` from that seq forward.
- FE: nothing to change (EventSource handles `Last-Event-ID` natively).

**Trade-off.** Requires a per-seq ring; cheap (~few KB).

**Citation.** WHATWG HTML §9.2 Server-Sent Events — https://html.spec.whatwg.org/multipage/server-sent-events.html#concept-event-stream-last-event-id.

### R1.2 `[H]` Heartbeat-based stale-connection detection on the FE

Today the SSE hook reconnects only on `onerror`. A silently stalled connection (laptop sleep, idle proxy kill) won't fire `onerror` until the next byte attempts to flow. Add a heartbeat timeout:

```ts
// useSSE.ts
const lastBeatRef = useRef(Date.now());
useEffect(() => {
  const id = setInterval(() => {
    if (Date.now() - lastBeatRef.current > 30_000) {
      esRef.current?.close();
      connect();
    }
  }, 5_000);
  return () => clearInterval(id);
}, []);
// in onmessage handler:
lastBeatRef.current = Date.now();
```

Pair with a backend `: heartbeat\n\n` comment every 15s — already partially present (verify cadence).

**Trade-off.** None meaningful.

**Citation.** Mozilla SSE guide — https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events.

### R1.3 `[H]` Keep SSE — do not switch to WebSocket

For this app traffic is server→client only with server-side termination logic that already lives in `gen()`. WebSockets buy nothing and cost framing complexity, no auto-reconnect, HTTP/2 multiplexing problems behind L7 proxies. WebTransport over HTTP/3 is interesting only when you need unreliable datagrams; events here are causal. Document the decision so reviewers know you considered the alternatives.

**Citation.** MDN comparison — https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events.

### R1.4 `[M]` Proxy buffering hardening

[server.py:1084](../../road_safety/server.py#L1084) sets `X-Accel-Buffering: no` — correct for nginx. For Cloudflare-fronted deployments add a Configuration Rule "Cache: bypass; Disable Buffering: on" on the SSE paths, or use a Workers route. Also pin `retry: 3000\n\n` in the keepalive — Safari ignores `retry:` shorter than 1000ms in older versions.

**Citations.** nginx `X-Accel-Buffering` — https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_buffering · Cloudflare configuration rules — https://developers.cloudflare.com/rules/configuration-rules/.

### R1.5 `[L]` MJPEG → fragmented MP4 / WebCodecs at fleet scale

[VideoFeed.tsx](../../frontend/src/components/admin/VideoFeed.tsx) uses `<img src="/admin/video_feed">` against a `multipart/x-mixed-replace` stream at ~2.5 fps. Fine for demo (zero JS, browser handles it). For fleet-scale dashboards move to fMP4 over `<video>` with MSE, or WebCodecs `VideoDecoder` — 5-10× bandwidth reduction at equal perceived quality and gives you per-frame overlay control.

**Trade-off.** WebCodecs is Chromium-first (Safari 17+). Don't migrate until you need overlays or bandwidth.

**Citation.** WebCodecs API — https://developer.mozilla.org/en-US/docs/Web/API/WebCodecs_API.

---

## 2 — End-to-end type safety

### R2.1 `[H]` Generate FE types from FastAPI's `/openapi.json`

[frontend/src/types.ts](../../frontend/src/types.ts) re-declares `SafetyEvent`, `LiveStatus`, `WatchdogFinding`, etc. — ~200 lines of TypeScript that mirror Pydantic shapes that don't yet exist (most BE handlers return raw `dict`). Every BE change risks silent drift; FE type errors only surface when a field renders empty.

**Adoption.**
1. Convert handler return types to Pydantic v2 models (see [backend.md §14](./backend.md#r14-h-pydantic-v2-models-for-events-payloads-sse-frames)).
2. Declare `response_model=` on every route — FastAPI emits accurate OpenAPI 3.1 for free.
3. Add codegen:
   ```bash
   npm i -D openapi-typescript
   npm i openapi-fetch
   curl -s http://localhost:8000/openapi.json | npx openapi-typescript -o frontend/src/types/api.gen.ts
   ```
4. Replace `lib/api.ts`'s `fetchJson<T>` with `createClient<paths>({ baseUrl: "" })` from `openapi-fetch` (~1 KB runtime).
5. Add a `make types` step and a CI guard that diffs `api.gen.ts`.

**Trade-off.** Forces every endpoint to declare `response_model`. That's the point — it surfaces every untyped surface.

**Citations.** FastAPI OpenAPI — https://fastapi.tiangolo.com/reference/openapi/ · openapi-typescript — https://openapi-ts.dev/ · openapi-fetch — https://openapi-ts.dev/openapi-fetch/.

### R2.2 `[H]` Runtime validation of SSE payloads (Zod or Valibot)

[useSSE.ts](../../frontend/src/hooks/useSSE.ts) does `JSON.parse(ev.data) as T` — an unchecked cast that lies the moment the BE adds, renames, or drops a field. For a stream the BE controls, validation closes the loop:

```ts
import * as v from "valibot";

const SafetyEventSchema = v.object({
  event_id: v.string(),
  risk_level: v.picklist(["high", "medium", "low"]),
  ts_start: v.number(),
  // …
});

const result = v.safeParse(SafetyEventSchema, JSON.parse(ev.data));
if (result.success) onMessage(result.output);
else reportSchemaError(result.issues);
```

Valibot (~3 KB tree-shaken) wins on bundle size vs Zod (~12 KB) for FE-only projects.

**Combine with R2.1.** Either generate Valibot/Zod schemas from OpenAPI (`openapi-zod-client`) or hand-write them adjacent to generated types.

**Citations.** Valibot — https://valibot.dev/ · Zod — https://zod.dev/ · openapi-zod-client — https://github.com/astahmer/openapi-zod-client.

### R2.3 `[M]` Pydantic v2 discriminated unions for SSE payloads

The `/stream/events` channel multiplexes `safety_event`, `perception_state`, possibly future `drift_alert`. Today the FE branches on a free-form `_meta` string. Use Pydantic v2 `Discriminator` + `Field(discriminator="kind")` so OpenAPI emits a proper `oneOf` and FE codegen produces a tagged union — TS narrowing works without manual guards.

**Citation.** https://docs.pydantic.dev/latest/concepts/unions/#discriminated-unions.

---

## 3 — Contract testing

### R3.1 `[H]` Schemathesis against the live FastAPI app

Schemathesis reads `/openapi.json` and property-tests every endpoint (status codes, schema conformance, header behavior, stateful sequences). Catches the exact bug class R2.1 prevents — drift between Pydantic models and what handlers actually return.

```bash
pip install schemathesis
schemathesis run http://localhost:8000/openapi.json --checks all --hypothesis-deadline=2000
```

Add `tests/test_contract.py` that boots the app via `TestClient` and feeds Schemathesis the `app` directly (no network, fast in CI).

**Citation.** https://schemathesis.readthedocs.io/.

### R3.2 `[M]` Snapshot test of `/openapi.json`

Single-file diff guard. `tests/test_openapi_snapshot.py` writes `openapi.snap.json` once; subsequent runs assert `paths` equality. Forces a reviewer to acknowledge any contract change. Cheaper than Pact for a single FE↔BE pair.

### R3.3 `[L]` Pact only if a 2nd consumer appears

Pact shines with multiple consumers of the same provider. Schemathesis + snapshot is sufficient until a mobile or partner consumer arrives.

---

## 4 — Auth tier modernization

### R4.1 `[H]` Move admin auth to short-lived JWT + refresh, asymmetric signing

Today `ROAD_ADMIN_TOKEN` is a static shared secret in env. [security.py:36](../../road_safety/security.py) uses `secrets.compare_digest` (timing-safe — good). Drawbacks: no audit attribution (every admin action is "the admin"), no rotation without a redeploy, no per-user revocation. The audit log loses all forensic value the moment two operators share the token (which they will).

**Adoption.**
1. `pip install pyjwt[crypto]`. Sign with EdDSA (RFC 8032), 15-min access tokens, 7-day single-use refresh.
2. Claims: `sub`, `kid`, `aud=road-admin`, `tenant_id` (forward-compat with R15.1).
3. Keep the static `ROAD_ADMIN_TOKEN` as a "break-glass" service token behind a feature flag.
4. Audit log writes the verified `sub` instead of "admin".
5. Publish JWKS at `/.well-known/jwks.json` so rotation is a `kid` bump.

**Trade-off.** Key management. Use a JWKS file + cron-rotated kid; defer to an external IdP (R4.2) for human users.

**Citations.** RFC 7519 JWT — https://datatracker.ietf.org/doc/html/rfc7519 · RFC 8725 JWT BCP — https://datatracker.ietf.org/doc/html/rfc8725 · OWASP JWT cheat sheet — https://cheatsheetseries.owasp.org/cheatsheets/JSON_Web_Token_for_Java_Cheat_Sheet.html.

### R4.2 `[M]` OIDC for human admins

Fleet ops need SSO. Push it to an external IdP rather than home-grow. WorkOS / Auth0 / Clerk give SAML/SCIM out of the box for enterprise customers; Authentik / Keycloak are self-hosted. The FastAPI side becomes a pure resource server validating the IdP's JWT (R4.1).

**Citations.** WorkOS SSO — https://workos.com/docs/sso · Authentik — https://goauthentik.io/.

### R4.3 `[M]` WebAuthn / passkeys for the admin UI

Once OIDC is in place, layer WebAuthn at the IdP. For an ops UI accessed from fixed laptops, passkeys eliminate phishing. WebAuthn Level 3 is current.

**Citations.** W3C WebAuthn 3 — https://www.w3.org/TR/webauthn-3/ · passkeys.dev — https://passkeys.dev/.

### R4.4 `[L]` mTLS for edge → cloud — see R8.4

---

## 5 — CSRF / cookie strategy

### R5.1 `[H]` Document and enforce header-only auth as policy

Bearer in `Authorization` header is **not** auto-attached by the browser; cross-origin and cross-tab `<form>` submissions cannot leak it. Keep it that way: never put admin tokens in cookies. The single fix is B1 (actually require the token on watchdog mutations).

**Citation.** OWASP CSRF cheat sheet — https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html#use-of-custom-request-headers.

### R5.2 `[M]` If R4.2 ever ships browser cookies

`__Host-session=...; Path=/; Secure; HttpOnly; SameSite=Lax` + double-submit token for state-changing requests. Hidden `<meta name="csrf-token">` mirror.

**Citations.** `__Host-` cookie prefix — https://developer.mozilla.org/en-US/docs/Web/HTTP/Cookies#cookie_prefixes · SameSite (RFC 6265bis) — https://datatracker.ietf.org/doc/html/draft-ietf-httpbis-rfc6265bis.

---

## 6 — Rate limiting & abuse

### R6.1 `[H]` slowapi — per-token + per-IP buckets, tight `/chat` budget

[server.py:1088](../../road_safety/server.py#L1088) `/chat` is unauthenticated and hits the LLM. A scraper burns the Anthropic quota in minutes. [services/agents.py](../../road_safety/services/agents.py) endpoints have no per-endpoint limit beyond the token bucket inside `services/llm.py`.

```python
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(
    key_func=lambda r: r.headers.get("Authorization") or get_remote_address(r),
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_handler)

@app.post("/chat")
@limiter.limit("10/minute;200/day")
async def chat(...): ...

@app.post("/api/agents/coaching")
@limiter.limit("5/minute;100/day")
async def api_agent_coaching(...): ...
```

Return `429` with `Retry-After:` per RFC 6585.

**Trade-off.** slowapi defaults to in-memory; for multi-worker uvicorn use the Redis backend.

**Citations.** slowapi — https://slowapi.readthedocs.io/ · RFC 6585 §4 — https://datatracker.ietf.org/doc/html/rfc6585#section-4.

### R6.2 `[M]` Cloudflare WAF or equivalent in front

Public deployments should not expose uvicorn directly. Cloudflare's free plan covers TLS, basic WAF, bot-fight, and edge rate-limiting. **Critical for this project:** disable proxy buffering on the SSE paths (R1.4).

**Citation.** Cloudflare configuration rules — https://developers.cloudflare.com/rules/configuration-rules/.

---

## 7 — Distributed tracing

### R7.1 `[H]` W3C Trace Context, FE → BE → cloud → Anthropic

Without traces you cannot answer "why did `event_id=X` take 9 seconds end-to-end?" Adopt:

- **FE.** `@opentelemetry/sdk-trace-web` + `@opentelemetry/instrumentation-fetch`. `propagators: [new W3CTraceContextPropagator()]` so every `fetch` and the `EventSource` URL carry `traceparent`.
- **BE.** `opentelemetry-instrumentation-fastapi` auto-injects per-request spans; `opentelemetry-instrumentation-httpx` propagates outbound (the [edge_publisher.py](../../road_safety/integrations/edge_publisher.py) `httpx.AsyncClient`).
- **Cloud receiver.** Same FastAPI instrumentation; the `traceparent` header threads through.
- **LLM.** OpenInference Anthropic instrumentation — gives spans with token counts, model, cache-read/write, cost. https://github.com/Arize-ai/openinference

**Citations.** W3C Trace Context — https://www.w3.org/TR/trace-context/ · OTel Python — https://opentelemetry.io/docs/languages/python/instrumentation/ · OTel JS Web — https://opentelemetry.io/docs/languages/js/instrumentation/.

### R7.2 `[H]` Manual context propagation into the perception thread

The frame loop runs in a `threading.Thread` (StreamReader). OTel's context is `contextvars`-based and **does not** flow into raw threads. When the thread schedules work back onto the loop:

```python
# capture in the thread when the work is enqueued
ctx = otel_context.get_current()
loop.call_soon_threadsafe(lambda: otel_context.attach(ctx) and emit_event(ev))
```

Spans for `detect_frame`, `enrich_event`, `narrate_event` then thread-link back to the originating frame trace.

**Citation.** https://opentelemetry.io/docs/languages/python/instrumentation/#manually-propagating-context.

---

## 8 — HMAC hardening

### R8.1 `[H]` Bounded replay cache on the receiver

[cloud/receiver.py](../../cloud/receiver.py) validates `X-Road-Timestamp` ±300 s but does not check a nonce. Inside the 300 s window the same body+sig replays cleanly. Add:

```sql
CREATE TABLE IF NOT EXISTS seen_nonces (
  nonce TEXT PRIMARY KEY,
  seen_at INTEGER NOT NULL
);
```

Insert-or-fail; sweep older than 600 s. Reject duplicates with 401.

If [edge_publisher.py](../../road_safety/integrations/edge_publisher.py) doesn't already include a nonce field, add one (`uuid4().hex`) to the signed payload.

**Citation.** OWASP API4:2023 — https://owasp.org/API-Security/editions/2023/en/0xa4-unrestricted-resource-consumption/.

### R8.2 `[H]` Sign over `(method, path, content-sha256, ts, nonce)` — adopt RFC 9421

The current canonicalization (`f"{ts}.{body}"`) is vulnerable to path-confusion if a future operator points the publisher at a different endpoint on the same host. Move to RFC 9421 HTTP Message Signatures with `Content-Digest: sha-256=:base64:` (RFC 9530) — modern replacement for ad-hoc HMAC, supported by all major HTTP libs.

**Citations.** RFC 9421 HTTP Message Signatures — https://datatracker.ietf.org/doc/html/rfc9421 · RFC 9530 Digest Fields — https://datatracker.ietf.org/doc/html/rfc9530.

### R8.3 `[M]` Per-vehicle keys via HKDF + `kid` header

Single fleet-wide secret means one compromised edge box rotates the world. Derive per-vehicle keys via HKDF (RFC 5869) from a master; include `X-Road-Kid: <vehicle_id>` so the receiver looks up the right key. Rotation = bump master, derive new sub-keys, push to fleet.

**Citation.** RFC 5869 HKDF — https://datatracker.ietf.org/doc/html/rfc5869.

### R8.4 `[M]` mTLS as the durable upgrade path

When the cloud is your own service (not a webhook target), mTLS with per-vehicle client certs replaces HMAC, gets transport-layer identity, and integrates with service-mesh policy. RFC 8705 — https://datatracker.ietf.org/doc/html/rfc8705.

### R8.5 `[L]` Tighten thumbnail token entropy

[edge_publisher.py:47](../../road_safety/integrations/edge_publisher.py#L47): `mac.hexdigest()[:32]` truncates a 256-bit HMAC to 128 bits. NIST FIPS 198-1 allows truncation down to 128 bits, so this is **acceptable**, not a bug — but for a 60-second TTL link the cost of returning the full 64-char hex is zero. Drop the slice.

---

## 9 — Edge → cloud delivery semantics

### R9.1 `[H]` Formalize the outbox pattern

The JSONL queue + HMAC + cloud `INSERT OR IGNORE` is a textbook **transactional outbox + idempotent consumer = effectively-once** delivery. Document it as such in [docs/architecture.md](../architecture.md). Two gaps:

- **No monotonic per-vehicle sequence.** Add `vehicle_seq INTEGER NOT NULL` increment-on-enqueue. Cloud stores it; out-of-order replays become detectable.
- **No watermark on cloud side.** Track `MAX(vehicle_seq)` per vehicle in `vehicle_watermarks`. Anything older than `watermark - K` is "late-arriving" and should be flagged in `/stats`, not silently merged.

**Citations.** Microservices.io outbox — https://microservices.io/patterns/data/transactional-outbox.html · Kleppmann, *Designing Data-Intensive Applications* Ch. 11 — https://dataintensive.net/.

### R9.2 `[M]` Bound the queue and shed load explicitly

[edge_publisher.py](../../road_safety/integrations/edge_publisher.py) appends without a hard cap. A 24-hour cloud outage on a busy vehicle blows disk. Add a max bytes/lines cap; on overflow, drop oldest with a counter (`dropped_oldest_total`) or compress + rotate.

**Citation.** Google SRE Book ch. 21 "Handling Overload" — https://sre.google/sre-book/handling-overload/.

### R9.3 `[M]` Move queue to SQLite WAL instead of JSONL

Race-tolerant under multi-worker uvicorn, supports indexed `attempts` + `next_attempt_at`, gives crash-consistent semantics for free:

```sql
CREATE TABLE outbox(
  id INTEGER PRIMARY KEY,
  vehicle_seq INTEGER NOT NULL,
  body BLOB NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at INTEGER NOT NULL
);
```

**Citation.** SQLite WAL — https://www.sqlite.org/wal.html.

---

## 10 — SSE reliability under proxies

### R10.1 `[H]` Integration test for the proxy matrix

Once R1.1 lands, add a regression test:

```python
def test_sse_replays_after_disconnect(client):
    with client.stream("GET", "/stream/events", headers={"Last-Event-ID": "5"}) as r:
        first = next(parse_sse(r))
        assert int(first["id"]) > 5
```

### R10.2 `[M]` Visibility-aware reconnect on the FE

Pause `EventSource` on `document.visibilityState === "hidden"` for >5 min; resume on visible. Saves your server an idle subscriber per backgrounded tab and cuts client CPU/network on background tabs to ~0.

**Citation.** https://developer.mozilla.org/en-US/docs/Web/API/Page_Visibility_API.

### R10.3 `[M]` Pin `retry: 3000` in keepalive

Older Safari ignores `retry:` shorter than 1000ms. Standardize on 3000ms in the keepalive comment.

---

## 11 — Versioning & deprecation

### R11.1 `[M]` Move under `/api/v1`, leave SSE at `/stream/v1/events`

Today every path is unversioned. The first time `SafetyEvent.risk_level` evolves from string to object, every deployed FE breaks.

- URL versioning: `/api/v1/live/events`.
- SSE event-type evolution: emit `event: safety_event.v1`; later add `event: safety_event.v2`. EventSource consumers register specific listeners.
- `Sunset:` header (RFC 8594) on `/api/v1/*` once `v2` lands — https://datatracker.ietf.org/doc/html/rfc8594.
- `Deprecation:` header per the IETF draft — https://datatracker.ietf.org/doc/html/draft-ietf-httpapi-deprecation-header.

### R11.2 `[L]` Single-origin deploy bounds version skew

FE and BE always advance together → version skew is bounded to the rollout window. Document in [CLAUDE.md](../../CLAUDE.md).

---

## 12 — Health, readiness, metrics

### R12.1 `[H]` Split `/healthz` (liveness) from `/readyz` (readiness)

K8s convention; useful even on bare metal. Liveness = "process alive, no deps checked". Readiness = "deps green, ok to route traffic":

```python
@app.get("/healthz")
def healthz(): return {"status": "ok"}

@app.get("/readyz")
def readyz():
    checks = {
      "model": state.model is not None,
      "stream": state.stream_reader and state.stream_reader.is_alive(),
      "llm": llm_circuit_closed_or_disabled(),
      "cloud": _cloud_recently_acked(),  # only if publisher enabled
    }
    code = 200 if all(checks.values()) else 503
    return JSONResponse(checks, status_code=code)
```

**Citation.** K8s probes — https://kubernetes.io/docs/concepts/configuration/liveness-readiness-startup-probes/.

### R12.2 `[H]` Prometheus `/metrics` endpoint

Two lines via `prometheus-fastapi-instrumentator`. Exposes p50/p95/p99 per route, in-flight gauge, request-size histograms — instant Grafana dashboards.

**Citation.** https://github.com/trallnag/prometheus-fastapi-instrumentator.

---

## 13 — Integration-plane observability

### R13.1 `[H]` Custom metrics

Above the FastAPI defaults, add:

- `road_sse_subscribers{stream="events|detections"}` — Gauge.
- `road_outbox_depth` — Gauge over the queue length.
- `road_outbox_dropped_total{reason}` — Counter.
- `road_cloud_ingest_dedup_total` — Counter (cloud `INSERT OR IGNORE` ratio).
- `road_llm_token_cost_usd_total{model}` — Counter from [services/llm_obs.py](../../road_safety/services/llm_obs.py).
- `road_event_e2e_seconds` — Histogram of `cloud.received_at - edge.event.wall_time` (requires synced clocks).

### R13.2 `[M]` Grafana dashboard JSON checked into `ops/`

Single `grafana-cli dashboards import` step. Reviewers can render the dashboard locally.

**Citation.** https://grafana.com/docs/grafana/latest/dashboards/build-dashboards/.

---

## 14 — Privacy plumbing

### R14.1 `[H]` Audit `sub` after JWT lands

Once R4.1 ships, every privileged read logs the verified `sub` (not the static "admin"), the `kid`, the `event_id`, IP, user-agent. CCPA §1798.130 (records of disclosures) and BIPA §15(c) effectively require this for biometric-derived data; license plates plausibly qualify in some jurisdictions.

**Citations.** CCPA — https://oag.ca.gov/privacy/ccpa · BIPA — https://www.ilga.gov/legislation/ilcs/ilcs3.asp?ActID=3004.

### R14.2 `[H]` Lower thumbnail signing TTL and bind to `sub`

[edge_publisher.py:42](../../road_safety/integrations/edge_publisher.py#L42) `_THUMB_TTL_SEC = 15 * 60` is long. Cut to 60 s and add `sub` into the signing input so a leaked link can't be replayed by a different identity:

```python
mac = hmac.new(secret, f"{name}.{expiry}.{sub}".encode(), hashlib.sha256)
```

### R14.3 `[M]` Formalize the DSAR endpoint

Today `X-DSAR-Token` gates `/thumbnails`. GDPR Art. 15 (right of access) and Art. 17 (erasure) need a structured endpoint:

- `GET /api/dsar/{subject_ref}` → JSON of all derived data tied to that subject.
- `POST /api/dsar/{subject_ref}/erase` → cascading delete: cloud rows, thumbnails, [feedback.jsonl](../../data/), AL samples.
- Emit a tombstone to the cloud receiver so federated copies erase too.

**Citations.** GDPR Art. 30 — https://gdpr-info.eu/art-30-gdpr/ · GDPR Art. 17 — https://gdpr-info.eu/art-17-gdpr/.

---

## 15 — Multi-tenant readiness

### R15.1 `[M]` `tenant_id` claim → propagated to every query

After R4.1, every protected route resolves `request.state.tenant_id` from the verified JWT. Every cloud SQL query gets a `WHERE tenant_id = ?`. Per-tenant rate-limit bucket in slowapi (R6.1).

### R15.2 `[M]` Move `cloud.db` to Postgres at scale

SQLite is single-writer. The receiver's `INSERT OR IGNORE` loop holds the write lock per batch — fine to ~50 events/s, painful at fleet scale. Postgres `INSERT ... ON CONFLICT DO NOTHING` is the drop-in replacement. Partition by `(tenant_id, vehicle_id)`.

**Citations.** SQLite WAL limits — https://www.sqlite.org/wal.html · Postgres ON CONFLICT — https://www.postgresql.org/docs/current/sql-insert.html#SQL-ON-CONFLICT.

### R15.3 `[L]` Vehicle-id sharding

Once Postgres lands, `(tenant_id, vehicle_id)` becomes the shard key. Citus / pg_partman.

---

## 16 — Compliance plumbing

### R16.1 `[H]` Wire `compliance/audit.py` to dual-emit (JSONL + OTel event)

Today `audit.log("chat_query", query[:200])` is a flat append. Make it dual-emit: structured JSONL line **and** an OTel `Event` on the active span. This gives Article 30 records joinable with the trace from R7.

### R16.2 `[H]` Right-to-erasure cascade test

`tests/test_erasure_cascade.py` that (a) creates a synthetic event, (b) calls `/api/dsar/.../erase`, (c) asserts cloud row gone, thumbnail file gone, audit entry tombstoned, AL sample gone, edge outbox scrubbed if not yet flushed. The test is the spec.

### R16.3 `[M]` Retention semantics on responses

Surface `Retention-Period: P30D` (Internet-Draft `draft-ietf-httpapi-data-retention-policy`) on responses that include personal data, so downstream caches honor the policy.

---

## What I would NOT recommend

- **Switch from SSE to WebSockets / WebTransport.** Uni-directional event push is exactly what SSE was designed for; WebSockets cost framing complexity, no auto-reconnect, HTTP/2 multiplexing problems behind L7 proxies. Document the choice (R1.3).
- **GraphQL gateway.** Three pages and ~25 endpoints don't justify the schema-stitching cost. REST + OpenAPI codegen (R2.1) gives you the type safety without the resolver overhead.
- **gRPC for edge → cloud.** HMAC over HTTPS is fine and traverses any corporate proxy; gRPC adds binary debugging difficulty for marginal latency gain at 50 events/min/vehicle.
- **Service mesh on the edge.** Linkerd/Istio on a Jetson is ~500 MB of overhead for a single-process app. Revisit only when the cloud side scales out.
- **Pact contract testing.** Schemathesis + OpenAPI snapshot (R3.1, R3.2) covers the same risk for one consumer at a fraction of the cognitive overhead.

---

## 90-day phased rollout

### Weeks 1-2 — bug fixes + cheapest credibility wins
- **B1** require_admin on watchdog mutations · 10 min
- **R8.5** drop the `[:32]` slice on the thumb token · 5 min
- **R6.1** slowapi on `/chat` and `/api/agents/*` · 4 h
- **R12.2** Prometheus `/metrics` · 1 h

### Weeks 3-4 — contracts
- **R2.1** Pydantic response_models on every route + `openapi-typescript` codegen · 3 d
- **R2.2** Valibot validation in `useSSE` · 1 d
- **R3.1** Schemathesis in CI · 0.5 d
- **R3.2** OpenAPI snapshot test · 0.5 d

### Weeks 5-6 — SSE protocol hardening
- **R1.1** Last-Event-ID resumption · 1 d
- **R1.2** FE heartbeat-stall detection · 0.5 d
- **R1.4** proxy-buffering hardening · 0.25 d
- **R10.1** SSE replay regression test · 0.25 d
- **R10.2** Visibility-aware reconnect · 0.5 d

### Weeks 7-8 — observability
- **R12.1** `/readyz` separate · 0.5 d
- **R7.1** OTel + W3C trace context (FE + BE + cloud + Anthropic) · 2 d
- **R7.2** manual context propagation into perception thread · 1 d
- **R13.1** custom metrics · 1 d

### Weeks 9-10 — auth modernization
- **R4.1** JWT + refresh + JWKS · 3 d
- **R14.1** audit `sub` migration · 0.5 d
- **R14.2** thumb TTL + sub binding · 1 h
- **R5.1** document the header-only auth policy in [CLAUDE.md](../../CLAUDE.md) · 15 min

### Weeks 11-12 — compliance + edge↔cloud durability
- **R8.1** nonce replay cache on receiver · 0.5 d
- **R9.1** outbox seq + watermark · 2 d
- **R14.3** DSAR endpoint · 1 d
- **R16.2** erasure cascade test · 1 d
- **R16.1** audit → OTel events · 0.5 d

### Defer past 90 days
- R1.5 (MJPEG → fMP4), R4.2/R4.3 (OIDC + WebAuthn), R8.2 (RFC 9421), R8.3 (per-vehicle HKDF), R9.3 (SQLite-backed outbox), R11.1 (URL versioning), R15.x (multi-tenancy), R16.3 (retention header).

---

## File → recommendation map

| File | Recommendations |
|------|------------------|
| [server.py](../../road_safety/server.py) | B1 (auth gap) · R1.1 (SSE id:) · R1.4 (X-Accel) · R6.1 (slowapi) · R7.1 (OTel) · R12.1 (readyz) · R16.1 (audit→OTel) |
| [security.py](../../road_safety/security.py) | R4.1 (JWT) |
| [services/llm.py](../../road_safety/services/llm.py) | R7.1 (OpenInference) · R6.1 (per-endpoint limits) |
| [edge_publisher.py](../../road_safety/integrations/edge_publisher.py) | R8.1 (nonce) · R8.2 (RFC 9421) · R8.3 (HKDF) · R8.5 (token entropy) · R9.1 (outbox seq) · R14.2 (TTL+sub) |
| [cloud/receiver.py](../../cloud/receiver.py) | R8.1 (replay cache) · R9.1 (watermark) · R12.1 (readyz) · R15.2 (Postgres) |
| [useSSE.ts](../../frontend/src/hooks/useSSE.ts) | R1.2 (heartbeat) · R2.2 (Valibot) · R10.2 (visibility) · R10.3 (retry) |
| [lib/api.ts](../../frontend/src/lib/api.ts) | R2.1 (openapi-fetch) |
| [types.ts](../../frontend/src/types.ts) | R2.1 (replaced by codegen) |
| [vite.config.ts](../../frontend/vite.config.ts) | R7.1 (OTel browser SDK) |
| (new) `tests/test_contract.py` | R3.1 (Schemathesis) |
| (new) `tests/test_openapi_snapshot.py` | R3.2 |
| (new) `tests/test_erasure_cascade.py` | R16.2 |
| (new) `ops/grafana/*.json` | R13.2 |
