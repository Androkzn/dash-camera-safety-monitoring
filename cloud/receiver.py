"""Cloud receiver — ingests HMAC-signed event batches from the edge node.

This is **deliberately a separate FastAPI app** from the edge server. The edge
node and the cloud receiver run on different hosts with different trust
boundaries, scaling profiles, and secrets. The split enforces the security
boundary in code rather than by convention.

Run it standalone::

    uvicorn cloud.receiver:app --port 8001

Or via the unified launcher::

    python start.py --cloud

Responsibility (narrow by design):
    1. Accept ``POST /ingest/events`` batches from
       ``road_safety.integrations.edge_publisher.EdgePublisher``.
    2. Reject anything whose HMAC signature doesn't validate.
    3. Idempotently persist accepted events into a SQLite file
       (``data/cloud.db``) keyed by ``event_id``.
    4. Provide a couple of read-only dashboards (``/events``, ``/stats``)
       guarded by a separate bearer token.

Key invariants to preserve when extending this module:

    * **HMAC verification rejects tampered batches.** The server refuses to
      boot without ``ROAD_CLOUD_HMAC_SECRET`` (see ``_require_secret``). An
      ingest endpoint that accepts unsigned traffic would be worse than a
      disabled one because the ops team might not notice the silent data
      corruption.
    * **``INSERT OR IGNORE`` provides idempotent ingest.** The edge
      publisher retries on transient failure, so duplicates arrive
      routinely. SQLite's "ignore on conflict" makes retries safe —
      identical ``event_id`` rows are silently dropped with no error. The
      accepted/duplicate counts are reported back so the edge knows which
      of its sends were actually new data.
    * **Timestamp window rejects replays.** Signatures are also bound to
      the timestamp header; old requests (outside ±5 min) are refused even
      with a valid HMAC because an attacker who captured a valid request
      could otherwise re-send it forever.
    * **SQLite on purpose.** A real analytics stack (Postgres, BigQuery,
      Snowflake...) would be fine but this receiver is intentionally
      *small* and *deployable anywhere* — single-binary, zero-infra. For
      demo / POC / small-fleet deployments it's plenty; larger fleets swap
      in a backing store without changing the wire format.

Python concepts used here (quick glossary):

    * ``@asynccontextmanager`` + ``lifespan=`` — FastAPI's startup/shutdown
      hook. Code before ``yield`` runs when the app boots; code after runs
      at shutdown. Replaces the deprecated ``@app.on_event("startup")``.
    * ``@app.post("/path")`` / ``@app.get("/path")`` — route decorators.
      The decorated ``async def`` handler is called for each matching
      request; FastAPI injects typed parameters (``request: Request`` etc.)
      automatically.
    * ``sqlite3`` — stdlib database module. ``connect(path)`` opens the DB
      (file is auto-created). ``execute(sql, params)`` runs a statement;
      ``commit()`` persists. The ``with conn:`` context manager commits on
      success, rolls back on exception.
    * ``hmac`` / ``hashlib`` — stdlib crypto primitives. ``hmac.new(key,
      msg, hashlib.sha256).hexdigest()`` computes a signature that proves
      the message wasn't modified by anyone who doesn't hold ``key``.
    * ``hmac.compare_digest`` — constant-time comparison; see the matching
      comment in ``road_safety/security.py``.
    * ``try: ... except X: ... finally: ...`` — Python's exception handling.
      ``finally`` always runs (useful for cleanup). We mostly use narrow
      ``except`` clauses here because each failure mode maps to a specific
      HTTP response.
"""

from __future__ import annotations

# Stdlib: crypto primitives, JSON parsing, filesystem, SQLite, time, logging.
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

# FastAPI + its Request type for reading headers/body in route handlers.
from fastapi import FastAPI, HTTPException, Request

# We deliberately share the bearer-token helper with the edge node so both
# apps enforce auth identically. See ``road_safety/security.py`` for why
# the comparison is constant-time and how "fail closed" is implemented.
from road_safety.security import require_bearer_token

# Per-module logger. All log lines carry ``logger="cloud_receiver"`` in
# the JSON output, making them filterable in log aggregators.
logger = logging.getLogger("cloud_receiver")

# ``Path(__file__).resolve().parent.parent`` = project root.
# The cloud receiver technically lives in its own package (``cloud/``) but
# shares the root with the edge node for convenience in this monorepo.
# In a real deployment this receiver would be in its own repo/container and
# not reach sideways into ``road_safety.*`` at all.
ROOT = Path(__file__).resolve().parent.parent
# DB location, env-overridable for test fixtures and alternate mount points.
DB_PATH = Path(os.getenv("ROAD_CLOUD_DB", ROOT / "data" / "cloud.db"))
# Acceptable clock skew between edge and cloud (seconds). Signatures older
# than this (or more than this in the future) are rejected to prevent
# replay attacks. 5 minutes is lenient enough for NTP drift on fresh VMs
# yet tight enough to bound the replay window.
TIMESTAMP_WINDOW_SEC = 300
# Read-side auth token, distinct from the HMAC secret used for ingest. A
# dashboard user should be able to list events without holding the signing
# key. Unset → read endpoints return 503.
CLOUD_READ_TOKEN = os.getenv("ROAD_CLOUD_READ_TOKEN")

# SQL schema executed by ``_init_db`` on every boot.
# ``CREATE ... IF NOT EXISTS`` makes the boot idempotent — if the tables
# already exist the statement is a no-op, so we can safely re-run without
# touching migrations.
#
# Columns:
#   event_id    TEXT PRIMARY KEY — globally unique id from the edge. UNIQUE
#                                   constraint is what powers "INSERT OR
#                                   IGNORE" dedup.
#   received_at INTEGER           — Unix epoch seconds recorded by this
#                                   receiver when the row landed.
#   source      TEXT              — which edge node sent the event. Used
#                                   for per-fleet filtering in ``/events``.
#   payload     TEXT              — the raw event JSON, stored verbatim.
#                                   SQLite's ``json_extract`` lets us query
#                                   nested fields without a migration.
#   verdict     TEXT              — optional human-review outcome, NULL
#                                   until a reviewer labels the event.
SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id     TEXT PRIMARY KEY,
    received_at  INTEGER NOT NULL,
    source       TEXT NOT NULL,
    payload      TEXT NOT NULL,
    verdict      TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_received_at ON events(received_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_source       ON events(source);
"""


def _require_secret() -> str:
    """Read the HMAC secret from the environment, or refuse to start.

    Called from the FastAPI lifespan hook at boot. Making this a hard
    failure — rather than a log warning — prevents the classic mistake of
    deploying an unsigned-accepting ingest endpoint into production.

    Returns:
        The secret string.

    Raises:
        RuntimeError: ``ROAD_CLOUD_HMAC_SECRET`` is unset or empty. The
            process exits with a clear message instead of silently booting.
    """
    secret = os.getenv("ROAD_CLOUD_HMAC_SECRET")
    if not secret:
        raise RuntimeError(
            "ROAD_CLOUD_HMAC_SECRET is required. Refusing to start an ingest "
            "endpoint without a signing key."
        )
    return secret


def _connect() -> sqlite3.Connection:
    """Open a new SQLite connection, creating the parent dir on demand.

    A fresh connection is opened per request (at request scope) rather than
    reused as a singleton — SQLite connections are not safe to share across
    threads by default, and this pattern is plenty fast for the receiver's
    modest workload.

    Returns:
        A connection with ``row_factory = sqlite3.Row`` so rows can be
        accessed by column name (``row["event_id"]``) instead of tuple
        index, which improves readability in queries.
    """
    # ``mkdir(parents=True, exist_ok=True)`` — create the ``data/``
    # directory if it's missing; do nothing if it's already there. Idiomatic
    # "ensure-dir" one-liner.
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db() -> None:
    """Apply :data:`SCHEMA` to the database. Idempotent.

    Uses ``executescript`` (not ``execute``) because the schema contains
    multiple statements separated by semicolons. ``with _connect() as
    conn`` auto-commits on clean exit and rolls back on exception.
    """
    with _connect() as conn:
        conn.executescript(SCHEMA)
        conn.commit()


# -------------------------------------------------------------------------------------


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """FastAPI lifespan hook: runs at startup and shutdown.

    The lifespan replaces the older ``@app.on_event("startup")`` / ``...
    ("shutdown")`` hooks. Code before ``yield`` runs once at boot; code
    after ``yield`` (none here) runs on graceful shutdown.

    We stash the shared secret on ``app.state`` so every request handler
    can read it without hitting ``os.getenv`` on the hot path. State set
    here survives for the life of the process.
    """
    # Fail loud on misconfig — see ``_require_secret`` docstring.
    app.state.shared_secret = _require_secret()
    _init_db()
    logger.info("cloud_receiver ready; db=%s", DB_PATH)
    yield


# The FastAPI app is module-level so ``uvicorn cloud.receiver:app`` can
# locate it by import path. ``title`` shows up in the auto-generated
# OpenAPI docs.
app = FastAPI(title="Cloud Receiver", lifespan=_lifespan)


def _require_read_access(request: Request) -> None:
    """Guard read-only dashboard endpoints with ``ROAD_CLOUD_READ_TOKEN``.

    Thin wrapper so every read route uses the identical error messages
    and realm label. See :func:`road_safety.security.require_bearer_token`.
    """
    require_bearer_token(
        request,
        CLOUD_READ_TOKEN,
        realm="cloud read",
        env_var="ROAD_CLOUD_READ_TOKEN",
    )


# -------------------------------------------------------------------------------------


def _verify_signature(secret: str, ts_header: str | None, sig_header: str | None, body: bytes) -> None:
    """Verify HMAC-SHA256 signature on an incoming ingest request.

    Protocol:
        The edge publisher computes::

            signature = "sha256=" + hex(HMAC_SHA256(secret, f"{ts}.{body}"))

        and sends both ``ts`` (as ``X-Road-Timestamp``) and ``signature``
        (as ``Signature``) with the request. Binding the timestamp into
        the signed message is what prevents replay with an outdated ``ts``.

    Args:
        secret: The shared HMAC key loaded at boot.
        ts_header: Value of the ``X-Road-Timestamp`` header (Unix seconds
            as a string).
        sig_header: Value of the ``Signature`` header including the
            ``sha256=`` prefix.
        body: Raw request body bytes. We sign the bytes — not the parsed
            JSON — so re-serialisation doesn't break verification.

    Returns:
        ``None`` on success.

    Raises:
        HTTPException(401): Missing headers, non-integer timestamp,
            timestamp outside the ±5 min window, or signature mismatch.
            All four map to the same status intentionally: don't leak which
            step failed to a would-be attacker.
    """
    if not ts_header or not sig_header:
        raise HTTPException(status_code=401, detail="missing signature headers")
    try:
        # Timestamps arrive as strings (HTTP headers are always strings).
        # ``int(...)`` raises ``ValueError`` on garbage input, which we
        # catch and translate to 401.
        ts = int(ts_header)
    except ValueError:
        # ``raise X from None`` suppresses the implicit exception-chain
        # display ("During handling of the above exception..."); clients
        # don't need to see our internal parse errors.
        raise HTTPException(status_code=401, detail="bad timestamp") from None
    now = int(time.time())
    # ``abs(...)`` handles both past and future skew in a single check.
    if abs(now - ts) > TIMESTAMP_WINDOW_SEC:
        raise HTTPException(status_code=401, detail="timestamp outside window")

    # Recompute the signature locally and constant-time compare. Binding
    # ``ts`` into the message prefix means the same body signed at
    # different seconds produces a different HMAC — so an attacker cannot
    # replay an old valid request beyond the timestamp window.
    msg = f"{ts}.".encode() + body
    expected = "sha256=" + hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig_header):
        raise HTTPException(status_code=401, detail="bad signature")


# -------------------------------------------------------------------------------------


@app.post("/ingest/events")
async def ingest_events(request: Request) -> dict[str, int]:
    """Accept a batch of events from the edge publisher.

    Happy path:
        1. Read the raw body bytes (once; can't re-read ``request.body()``).
        2. Verify HMAC signature + timestamp window.
        3. Parse body as JSON (``{"source": "...", "events": [...]}``).
        4. Iterate events, ``INSERT OR IGNORE`` each into SQLite.
        5. Return per-batch counts so the edge can log them.

    Returns:
        A dict with three counters::

            {
                "accepted":   int,  # new rows inserted
                "duplicates": int,  # rows skipped because event_id existed
                "rejected":   int,  # malformed events (not a dict or no id)
            }

        The edge publisher uses these to update its own observability.

    Raises:
        HTTPException(401): Signature / timestamp invalid (see
            :func:`_verify_signature`).
        HTTPException(400): Body is not JSON, or ``events`` is not a list.
    """
    body = await request.body()
    secret = request.app.state.shared_secret
    _verify_signature(
        secret,
        request.headers.get("X-Road-Timestamp"),
        request.headers.get("Signature"),
        body,
    )
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        # ``from None`` — hide the underlying parse exception; we've already
        # translated it into a user-visible HTTP error.
        raise HTTPException(status_code=400, detail="invalid json") from None

    events = payload.get("events")
    # Source can come in via the payload itself or as a header. Either is
    # fine; header is a convenience for edge nodes that reuse a generic
    # batch body template.
    source = payload.get("source") or request.headers.get("X-Road-Source") or "unknown"
    if not isinstance(events, list):
        raise HTTPException(status_code=400, detail="events must be list")

    accepted = 0
    duplicates = 0
    rejected = 0
    now = int(time.time())

    # Open one connection for the whole batch so all inserts share a
    # transaction; ``conn.commit()`` at the end flushes them atomically.
    # If anything raises mid-loop, the ``with`` block rolls back — no
    # partial batches in the DB.
    with _connect() as conn:
        for ev in events:
            # Defensive validation — the HMAC proves the batch wasn't
            # tampered with *in transit*, but it doesn't validate that the
            # edge didn't send us garbage in the first place.
            if not isinstance(ev, dict):
                rejected += 1
                continue
            event_id = ev.get("event_id")
            if not event_id or not isinstance(event_id, str):
                rejected += 1
                continue
            # ``INSERT OR IGNORE`` — if a row with the same PRIMARY KEY
            # (event_id) already exists, silently skip the insert rather
            # than raising a constraint violation. This is what makes
            # retries safe.
            # ``?`` placeholders are sqlite3's parameterised form; the
            # driver escapes values so SQL injection is impossible.
            cur = conn.execute(
                "INSERT OR IGNORE INTO events(event_id, received_at, source, payload) "
                "VALUES (?, ?, ?, ?)",
                (event_id, now, source, json.dumps(ev, separators=(",", ":"), default=str)),
            )
            # ``rowcount == 1`` → the INSERT actually inserted a row.
            # ``rowcount == 0`` → the IGNORE kicked in (duplicate event_id).
            if cur.rowcount == 1:
                accepted += 1
            else:
                duplicates += 1
        conn.commit()

    return {"accepted": accepted, "duplicates": duplicates, "rejected": rejected}


# -------------------------------------------------------------------------------------


@app.get("/events")
async def list_events(request: Request, limit: int = 100, risk_level: str | None = None) -> dict[str, Any]:
    """List recent events, newest first. Bearer-token protected.

    Args:
        request: FastAPI request, used for auth header access.
        limit: Max events to return. Clamped to [1, 500] to protect the
            server from accidental ``?limit=1000000`` queries.
        risk_level: Optional filter on the nested ``risk_level`` field
            inside each event payload. Uses SQLite's ``json_extract``
            so we don't need a dedicated column.

    Returns:
        ``{"events": [...], "count": N}`` — each event dict has two extra
        keys added by the receiver: ``_received_at`` (cloud timestamp) and
        ``_source`` (which edge node).

    Raises:
        HTTPException(401/403/503): Forwarded from :func:`_require_read_access`.
    """
    _require_read_access(request)
    # Clamp to a safe range. ``max(1, ...)`` prevents 0 or negative;
    # ``min(..., 500)`` prevents multi-megabyte responses.
    limit = max(1, min(limit, 500))
    # Build the query dynamically to support the optional filter without
    # resorting to ``WHERE 1=1``-style hacks. Params go in a parallel list
    # so we still use parameterised queries (no string interpolation).
    q = "SELECT event_id, received_at, source, payload FROM events"
    params: list[Any] = []
    if risk_level:
        q += " WHERE json_extract(payload, '$.risk_level') = ?"
        params.append(risk_level)
    q += " ORDER BY received_at DESC LIMIT ?"
    params.append(limit)

    with _connect() as conn:
        rows = conn.execute(q, params).fetchall()

    events = []
    for r in rows:
        try:
            ev = json.loads(r["payload"])
        except json.JSONDecodeError:
            # Stored payload is somehow corrupt. Don't fail the whole
            # listing — surface a marker so the dashboard can flag it.
            ev = {"_parse_error": True}
        # Annotate with receiver-side metadata. Underscore prefix signals
        # "server-added, not part of the original event".
        ev["_received_at"] = r["received_at"]
        ev["_source"] = r["source"]
        events.append(ev)
    return {"events": events, "count": len(events)}


# -------------------------------------------------------------------------------------


@app.get("/stats")
async def stats(request: Request) -> dict[str, Any]:
    """Return aggregate counters for the dashboard. Bearer-token protected.

    All three queries run in one connection so the counts are consistent
    relative to each other (same snapshot of the DB).

    Returns:
        ``{
            "total":            total event count,
            "last_received_at": most recent row's timestamp or None,
            "per_risk_level":   {"low": n, "medium": n, ...},
            "per_event_type":   {"near_miss": n, ...},
        }``

    Raises:
        HTTPException(401/403/503): Forwarded from :func:`_require_read_access`.
    """
    _require_read_access(request)
    with _connect() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM events").fetchone()["c"]
        last = conn.execute(
            "SELECT MAX(received_at) AS ts FROM events"
        ).fetchone()["ts"]
        # ``json_extract`` reaches into the stored JSON text. Cheaper than
        # normalising into a dedicated column, at the cost of full-table
        # scans when there's no functional index — fine for our volumes.
        per_risk_rows = conn.execute(
            "SELECT json_extract(payload, '$.risk_level') AS rl, COUNT(*) AS c "
            "FROM events GROUP BY rl"
        ).fetchall()
        per_type_rows = conn.execute(
            "SELECT json_extract(payload, '$.event_type') AS et, COUNT(*) AS c "
            "FROM events GROUP BY et"
        ).fetchall()

    # Dict comprehensions unpack the row objects into ``{key: count}`` maps.
    # NULL values (events missing the field) collapse to ``"unknown"`` so
    # the dashboard always has a printable key.
    return {
        "total": total,
        "last_received_at": last,
        "per_risk_level": {(r["rl"] or "unknown"): r["c"] for r in per_risk_rows},
        "per_event_type": {(r["et"] or "unknown"): r["c"] for r in per_type_rows},
    }


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness probe used by load balancers / ``start.py``.

    No auth, no DB access — must stay trivial so that k8s readiness probes,
    ALB health checks, and :func:`start.wait_for_health` all get a cheap,
    reliable signal that the process is responsive.
    """
    return {"status": "ok"}
