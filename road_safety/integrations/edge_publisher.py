"""Edge-side event publisher.

Responsibility
--------------
Ship safety events from this edge node up to a central cloud receiver in a
way that is (a) durable across crashes, (b) bandwidth-efficient, (c)
tamper-evident, and (d) privacy-preserving. This is the ONLY outbound path
for event data from the edge; Slack notifications are a separate,
human-readable channel.

How it works at a glance
------------------------
1. ``enqueue(event, thumb_path)`` — append the event as a single JSON line
   to ``data/outbound_queue.jsonl`` (a durable FIFO on local disk).
2. A background task (``run_forever``) wakes every ``flush_interval_sec``
   and calls ``flush_once`` to:
     - read up to ``batch_size`` lines off the front of the queue,
     - strip edge-only fields (anything prefixed ``_``),
     - optionally attach a signed public-thumbnail URL,
     - HMAC-SHA256 sign the serialized batch,
     - POST to ``ROAD_CLOUD_ENDPOINT`` over HTTPS,
     - on 2xx, truncate the queue to the remainder,
     - on 5xx / 408 / 429 / transport error, retain the batch and back off
       exponentially.

PRIVACY INVARIANTS (load-bearing — do not weaken)
-------------------------------------------------
- **Raw frames never leave this process.** Only JSON event records and
  (optionally) URLs pointing at already-redacted thumbnails ever go out.
- **Raw plate text never leaves this process.** ``services/llm.enrich_event``
  salt-hashes plate strings at ingest; egress is defence-in-depth only.
- **Only the ``_public`` (redacted) thumbnail URL is ever emitted.** The
  ``thumbnail_url`` field is built by ``build_thumbnail_url`` and is only
  populated when ``ROAD_EDGE_PUBLIC_URL`` is configured. Without that env
  var, the ``_thumbnail_path`` internal field is silently dropped from the
  egress payload — the cloud receives a text-only event.
- **Every batch is HMAC-SHA256 signed.** The cloud receiver rejects batches
  whose signature does not match, which means a tampered or forged batch
  cannot inject events into the cloud store.

Environment variables read by this module
-----------------------------------------
- ``ROAD_CLOUD_ENDPOINT``      — full HTTPS URL of the cloud receiver's
                                 ingest endpoint. If unset, publisher is
                                 disabled and no network traffic is made.
- ``ROAD_CLOUD_HMAC_SECRET``   — shared secret used to HMAC-sign batches and
                                 thumbnail URLs. If unset, publisher is
                                 disabled. Must match the secret configured
                                 on the cloud receiver.
- ``ROAD_EDGE_PUBLIC_URL``     — public base URL at which this edge node's
                                 ``/thumbnails/...`` endpoint is reachable
                                 from the cloud side. If unset, public
                                 thumbnail URLs are omitted (privacy-safe
                                 default).
- ``ROAD_EDGE_NODE_ID``        — free-form identifier for this node.
                                 Forwarded verbatim as the ``source`` field
                                 in each batch and as the ``X-Road-Source``
                                 HTTP header.

Files this module reads and writes
----------------------------------
- ``data/outbound_queue.jsonl`` (read + write) — durable FIFO of pending
  events. Lines are never mutated in place: on flush we read the file,
  take a prefix, POST it, and on success rewrite the file with only the
  remainder. The file is created if missing.

Threat model (summary)
----------------------
TLS provides confidentiality. HMAC-SHA256 over ``f"{timestamp}.{body}"``
provides integrity and authenticity. A ~300s timestamp window bounds
replay (enforced on the cloud side). The cloud side also dedupes on
``event_id`` via SQLite ``INSERT OR IGNORE``.

This module does NO network I/O at import time. Everything is lazy / async.
"""

# ``from __future__ import annotations`` makes every type annotation in this
# file evaluated lazily as a string, which lets us use newer-style union
# syntax like ``str | None`` on older Pythons and avoids import-time
# circularity problems.
from __future__ import annotations

# Standard-library imports (stdlib → third-party → local, blank line between
# groups, per project convention):
import asyncio      # event-loop primitives; this module is async end-to-end
import hashlib      # SHA-256 digest used for thumbnail content hashes
import hmac         # keyed-MAC primitive used for batch + URL signing
import json         # serialize events to/from JSON lines
import logging      # structured logger (prefer this over print)
import os           # read environment variables via os.getenv
import secrets      # cryptographically-secure random (used for nonce)
import time         # unix timestamps for expiry + signing
from dataclasses import dataclass  # tiny POD class for backoff state
from pathlib import Path           # OO filesystem paths — no string-joining
from typing import Any             # ``Any`` = "escape hatch" static type

# Third-party. ``httpx`` is an async-capable HTTP client (think requests,
# but awaitable). We use its ``AsyncClient`` context manager so connections
# are pooled and cleanly closed.
import httpx

# One named logger per module. Callers configure handlers centrally; we
# never ``print`` from production code paths.
logger = logging.getLogger("edge_publisher")

# ============================================================================
# Section: presigned thumbnail URLs
# ----------------------------------------------------------------------------
# Goal: let the cloud receiver LAZILY fetch a thumbnail image, without us
# having to upload to S3/Azure Blob, and without exposing the thumbnail to
# the wider internet indefinitely.
#
# In a real deployment the edge node would either
#   (a) upload the redacted thumb to object storage and return a presigned
#       GET URL, or
#   (b) serve it itself over a short-lived signed URL.
# We do option (b): the URL points at the edge node's own
# ``/thumbnails/{name}`` endpoint with a ``token`` query param derived from
# HMAC(secret, name|expiry). The edge node's route checks the token before
# returning the bytes. Cloud fetches lazily, only if a human actually opens
# the event.
#
# PRIVACY NOTE: only the REDACTED (``_public``) thumbnail is ever linked
# this way. The caller passes the public path into ``enqueue``; the
# original (un-redacted) image stays behind on the edge node and is never
# referenced from a URL that the cloud sees.
# ============================================================================

# TTL for the short-lived signed URL. 15 minutes is a balance:
#   - short enough to limit accidental leak via log capture / screenshots,
#   - long enough that an operator clicking a Slack link five minutes after
#     an alert still sees the image (no race with cloud indexing lag).
# The URL encodes this expiry so the edge node can reject late requests.
_THUMB_TTL_SEC = 15 * 60


def _sign_thumb_token(secret: str, name: str, expiry: int) -> str:
    """Compute an HMAC-SHA256 token that authorizes one specific thumbnail
    for one specific expiry time.

    HMAC is a keyed message authentication code: given the same key and
    message you always get the same digest, but without the key an
    attacker cannot forge a digest for a new (name, expiry) pair. We sign
    ``"{name}.{expiry}"`` so that swapping either field invalidates the
    token.

    Args:
        secret: shared secret (same as ``ROAD_CLOUD_HMAC_SECRET`` by
            default) — must be kept out of logs.
        name: thumbnail filename (e.g. ``evt_abc_public.jpg``).
        expiry: unix timestamp past which the token is no longer valid.

    Returns:
        A 32-character hex string. We truncate from the 64-char SHA-256
        output because 128 bits is plenty of unpredictability for a URL
        token and short tokens keep URLs readable. The algorithm is
        SHA-256 because it's ubiquitous, fast, and well-audited; MD5 and
        SHA-1 are NOT acceptable for new code.
    """
    # ``str.encode()`` converts a Python str into UTF-8 bytes; ``hmac.new``
    # wants bytes for both the key and the message.
    mac = hmac.new(secret.encode(), f"{name}.{expiry}".encode(), hashlib.sha256)
    return mac.hexdigest()[:32]


def build_thumbnail_url(
    edge_base_url: str, secret: str, thumb_path: Path, now: int | None = None
) -> tuple[str, str]:
    """Build a short-lived signed URL for a redacted thumbnail.

    Args:
        edge_base_url: public base URL at which this edge node is reachable
            (e.g. ``https://edge-42.example.com``). Trailing slash is
            stripped to keep URL construction deterministic.
        secret: shared HMAC secret — same secret the edge's
            ``/thumbnails/{name}`` route uses to verify tokens.
        thumb_path: filesystem path to the REDACTED (public) thumbnail.
            Must already exist for the SHA-256 content hash to be
            computed; if it is missing we return an empty hash rather than
            failing (cloud can still fetch the URL later).
        now: unix timestamp, useful to inject for deterministic tests.
            Defaults to ``int(time.time())``.

    Returns:
        Tuple ``(thumbnail_url, thumbnail_sha256)``. The URL carries both
        the expiry and the signature as query parameters. The SHA-256 hex
        digest is an integrity check the cloud can use to detect truncation
        or mid-flight tampering of the image bytes.
    """
    # ``a or b`` returns ``a`` unless it is "falsy" (0, None, empty). This
    # idiom is why callers can pass ``now=None`` to mean "use real time".
    now = now or int(time.time())
    expiry = now + _THUMB_TTL_SEC
    name = thumb_path.name  # ``Path.name`` gives just the filename, no dir
    token = _sign_thumb_token(secret, name, expiry)
    # f-strings (``f"..."``) interpolate ``{expression}`` into a string at
    # runtime. Much clearer than .format() or % formatting.
    url = f"{edge_base_url.rstrip('/')}/thumbnails/{name}?exp={expiry}&token={token}"
    # ``Path.read_bytes()`` returns the file contents as a ``bytes`` object.
    # We hash it so the cloud can verify nothing mutated the file in flight.
    sha = hashlib.sha256(thumb_path.read_bytes()).hexdigest() if thumb_path.exists() else ""
    return url, sha


# ============================================================================
# Section: publisher
# ----------------------------------------------------------------------------
# The ``EdgePublisher`` class owns (1) the on-disk queue, (2) the async
# flush loop, and (3) the exponential backoff state used between flushes.
# ============================================================================


# ``@dataclass`` is a decorator that auto-generates __init__, __repr__, and
# __eq__ from the class body's typed attributes. In other words,
# ``_BackoffState(delay=2.0)`` Just Works without us writing boilerplate.
# We use it here because this is a pure state-bag, no fancy behaviour.
@dataclass
class _BackoffState:
    """Exponential-backoff counter shared across flush attempts.

    Doubles the delay on each failure up to a ceiling, then resets to the
    starting value on any success. Kept as a tiny class rather than two
    loose floats so future tweaks (jitter, reset-after-N-success, etc.)
    have one obvious home.
    """

    # Field with a default value. ``float`` is a type hint for documentation
    # and tooling — Python does not enforce it at runtime.
    delay: float = 1.0
    # Upper bound chosen because beyond ~60s the useful failure signal
    # moves from "network hiccup" to "operator paging"; we'd rather alert
    # loud than sleep long.
    max_delay: float = 60.0

    def next(self) -> float:
        """Return the CURRENT delay and double it for next time (capped).

        Why return the current value before doubling? So the first call
        yields 1s (a short, forgiving first retry), the second 2s, then
        4s, 8s, 16s, 32s, 60s, 60s … Classic capped exponential backoff.
        """
        d = self.delay
        # ``min(a, b)`` prevents the delay from growing past ``max_delay``.
        self.delay = min(self.delay * 2.0, self.max_delay)
        return d

    def reset(self) -> None:
        """Call after a successful flush so the next failure starts fresh."""
        self.delay = 1.0


class EdgePublisher:
    """Durable outbound queue + batched HMAC-signed HTTPS delivery.

    Lifecycle
    ---------
    - Construct once in ``server.py`` at startup. Construction is cheap and
      does no network I/O; it just ensures ``data/`` exists and creates an
      empty queue file if needed.
    - ``await enqueue(event, thumb_path)`` is called from the hot path
      whenever a safety event is emitted. Appends to disk; never blocks
      on the network.
    - A single long-lived task (``await publisher.run_forever()``) drives
      periodic flushes. Cancelling that task (app shutdown) cleanly stops
      the loop.

    State
    -----
    - ``queue_path``:   durable FIFO on disk (one JSON line per event).
    - ``_lock``:        an ``asyncio.Lock`` protecting concurrent access to
                        the queue file. Only one task may read/rewrite it
                        at a time.
    - ``_backoff``:     retry-delay state (see ``_BackoffState`` above).
    """

    def __init__(
        self,
        endpoint_url: str | None = None,
        shared_secret: str | None = None,
        queue_path: Path = Path("data/outbound_queue.jsonl"),
        batch_size: int = 20,
        flush_interval_sec: float = 10.0,
        edge_base_url: str | None = None,
        source_name: str | None = None,
    ) -> None:
        """Wire up publisher configuration; do NOT perform network I/O here.

        Args:
            endpoint_url: override for ``ROAD_CLOUD_ENDPOINT``. Tests pass
                this explicitly; production code leaves it None so the env
                var wins.
            shared_secret: override for ``ROAD_CLOUD_HMAC_SECRET``.
            queue_path: where to keep the on-disk FIFO. Relative paths are
                resolved against the current working directory; production
                callers pass an absolute path built from ``config.DATA_DIR``.
            batch_size: max events per POST. 20 chosen as a balance between
                per-request overhead (TLS handshake, JSON framing) and
                latency of the slowest event in a batch — if we bundle too
                many, one late event delays the whole group; too few and
                we chatter.
            flush_interval_sec: wake-up cadence for ``run_forever``. 10s is
                a conservative default: short enough that incidents surface
                near-real-time, long enough to actually accumulate a batch
                during low-traffic periods.
            edge_base_url: override for ``ROAD_EDGE_PUBLIC_URL`` — the
                public URL of this edge node, used to build thumbnail
                links. When empty, thumbnail URLs are omitted (privacy-safe
                default — see module docstring).
            source_name: override for ``ROAD_EDGE_NODE_ID`` — stamped onto
                every outgoing batch.
        """
        # The ``or`` pattern means "if caller passed None, fall back to env
        # var, else stay None and disable the publisher".
        self.endpoint_url = endpoint_url or os.getenv("ROAD_CLOUD_ENDPOINT")
        self.shared_secret = shared_secret or os.getenv("ROAD_CLOUD_HMAC_SECRET")
        # ``Path(x)`` accepts strings, Path objects, or os.PathLike; wrapping
        # here guarantees we have a Path regardless of what the caller passed.
        self.queue_path = Path(queue_path)
        self.batch_size = batch_size
        self.flush_interval_sec = flush_interval_sec
        self.edge_base_url = edge_base_url or os.getenv("ROAD_EDGE_PUBLIC_URL", "")
        self.source_name = source_name or os.getenv("ROAD_EDGE_NODE_ID", "")
        # An asyncio.Lock serializes access to the queue file among coroutines
        # running on the same event loop. Without it, two concurrent
        # ``flush_once`` calls could race and corrupt the file.
        self._lock = asyncio.Lock()
        self._backoff = _BackoffState()
        # Make sure the parent directory exists so ``open("a")`` below works.
        # ``parents=True`` creates intermediate dirs; ``exist_ok=True`` stops
        # it from raising if the directory already exists.
        self.queue_path.parent.mkdir(parents=True, exist_ok=True)
        # Touch (create empty) the queue file so any concurrent reader that
        # lands before the first enqueue doesn't hit FileNotFoundError.
        if not self.queue_path.exists():
            self.queue_path.touch()

    # ------------------------------------------------------------------
    # Gating: both env vars must be present, or the publisher is a no-op.
    # ------------------------------------------------------------------

    def enabled(self) -> bool:
        """Return True iff both the endpoint URL and HMAC secret are set.

        We require BOTH because signing without an endpoint is pointless,
        and POSTing without signing would be a security regression. A
        missing config is a legitimate mode of operation (e.g. running the
        edge node standalone), so we silently disable rather than raising.
        """
        # ``bool(str)`` is False for the empty string, True for any
        # non-empty string — exactly the gate we want.
        return bool(self.endpoint_url) and bool(self.shared_secret)

    # ------------------------------------------------------------------
    # Ingress: append to the on-disk queue.
    # ------------------------------------------------------------------

    async def enqueue(
        self, event: dict, public_thumbnail_path: Path | None = None
    ) -> None:
        """Non-blocking append to local JSONL queue. Never raises.

        Called from the hot path of ``server.py::_emit_event``. If this
        method ever raised, it could crash the detection loop — so we
        wrap the whole body in a best-effort ``try/except`` and log on
        failure. The queue is strictly append-only; reconciliation with
        the cloud happens in ``flush_once``.

        Args:
            event: the safety-event dict as produced by the perception
                pipeline. Must already be redacted (hashed plate, etc.) —
                this method does NOT re-redact.
            public_thumbnail_path: filesystem path to the ``_public.jpg``
                thumbnail. Stored on the queue line under the reserved
                ``_thumbnail_path`` key (leading underscore = edge-only
                metadata; it is stripped before send in
                ``_prepare_outbound``). If None, no thumbnail URL is
                attached to the outbound record.

        Returns:
            None. Any failure is logged at WARNING and swallowed so the
            detection loop keeps running.
        """
        try:
            # ``dict(event)`` shallow-copies so our ``_thumbnail_path``
            # annotation does not mutate the caller's event dict (which is
            # also stored in in-memory buffers).
            record: dict[str, Any] = dict(event)
            if public_thumbnail_path is not None:
                # Stored only for local use; stripped before send by
                # ``_prepare_outbound``. The leading-underscore convention
                # marks "edge-internal, do not transmit".
                record["_thumbnail_path"] = str(public_thumbnail_path)
            # ``json.dumps`` converts Python data to a JSON string.
            # ``separators=(",", ":")`` removes whitespace for compact lines.
            # ``default=str`` turns any non-serializable objects (e.g.
            # datetimes, Paths) into their string form so we never crash on
            # exotic event fields.
            line = json.dumps(record, separators=(",", ":"), default=str) + "\n"
            # ``async with`` is the async flavour of ``with``; it awaits the
            # lock's __aenter__ before entering the block and releases in
            # __aexit__ on the way out, even if an exception is raised.
            async with self._lock:
                # Small writes; sync fs call under the lock is fine here —
                # the cost of a single append is measured in microseconds.
                # Opening in ``"a"`` mode means "append"; existing content is
                # preserved.
                with self.queue_path.open("a", encoding="utf-8") as f:
                    f.write(line)
        except Exception as exc:  # noqa: BLE001
            # Broad catch is intentional here: the enqueue path must never
            # take down the detection loop. The watchdog surfaces repeated
            # failures via its own fingerprinted-incident flow.
            logger.warning("edge_publisher.enqueue failed: %s", exc)

    # ------------------------------------------------------------------
    # Egress helpers: scrub internal fields, attach signed thumbnail URL,
    # sign the batch.
    # ------------------------------------------------------------------

    def _prepare_outbound(self, record: dict) -> dict:
        """Strip edge-only metadata and attach ``thumbnail_url`` if applicable.

        PRIVACY CALLOUT: this is the function that enforces "only the
        public thumbnail URL ever leaves the device". It works in three
        steps:
          1. Drop every field whose name starts with ``_`` — the
             leading-underscore prefix is the project-wide convention for
             "edge-internal, do not transmit". Includes ``_thumbnail_path``
             (local filesystem path, MUST NOT leak).
          2. If a public thumbnail path AND an edge public URL are both
             configured, build a short-lived signed URL and attach it as
             ``thumbnail_url`` along with a SHA-256 content digest.
          3. If a thumbnail exists on disk but ``ROAD_EDGE_PUBLIC_URL`` is
             not set, we deliberately emit no URL. The cloud gets a
             text-only event. This is the privacy-safe default.

        Raw frames never enter this function at all — the edge pipeline
        only persists already-redacted JPEGs — so there is no path by
        which raw imagery could escape here.
        """
        # Dict comprehension: ``{k: v for k, v in D.items() if pred}`` builds
        # a new dict including only items that pass ``pred``. This is the
        # idiomatic Python way to filter a mapping.
        out = {k: v for k, v in record.items() if not k.startswith("_")}
        thumb = record.get("_thumbnail_path")
        if thumb and self.shared_secret and self.edge_base_url:
            p = Path(thumb)
            try:
                url, sha = build_thumbnail_url(self.edge_base_url, self.shared_secret, p)
                if url:
                    out["thumbnail_url"] = url
                if sha:
                    # Hash lets the cloud verify the image bytes match what
                    # was signed — tamper-evidence for the thumbnail itself.
                    out["thumbnail_sha256"] = sha
            except Exception as exc:  # noqa: BLE001
                # Never fail the batch on a thumbnail hiccup — the event
                # metadata is the primary payload; the image is a nice-to-have.
                logger.warning("thumb url build failed for %s: %s", thumb, exc)
        elif thumb and not self.edge_base_url:
            # Explicit privacy-safe path: we HAVE a local thumbnail but
            # have not been told how to expose it publicly, so we say
            # nothing about it. The alternative (embedding bytes in the
            # JSON) would bust our bandwidth budget and also be harder to
            # audit.
            logger.debug("thumb url omitted: ROAD_EDGE_PUBLIC_URL not configured")
        return out

    def _sign(self, body: bytes, ts: int) -> str:
        """HMAC-SHA256 sign ``f"{ts}.{body}"`` with the shared secret.

        The cloud receiver verifies this signature before accepting the
        batch. Including ``ts`` in the signed payload means the cloud can
        reject stale messages (classic replay-attack defence). The
        ``sha256=`` prefix on the returned string is a small convention
        that makes it easy to extend to other algorithms later without
        breaking wire format.

        Why SHA-256? Industry-standard, well-vetted, hardware-accelerated
        on every modern CPU. Shorter digests (SHA-1, MD5) are broken for
        collision-resistance and should not be used for new HMACs.
        """
        # Defensive: if someone bypassed ``enabled()`` we want a loud error,
        # not a silent no-op sig. ``assert`` raises AssertionError when
        # falsy — sufficient for "programmer error" guard rails.
        assert self.shared_secret is not None
        # Concatenate the timestamp prefix with the raw JSON body. We build
        # the message from bytes rather than strings so the signature is
        # over the exact bytes that go on the wire (avoids any subtle
        # whitespace/encoding drift between signer and verifier).
        msg = f"{ts}.".encode() + body
        mac = hmac.new(self.shared_secret.encode(), msg, hashlib.sha256)
        return "sha256=" + mac.hexdigest()

    # ------------------------------------------------------------------
    # Egress driver: the one place that talks to the cloud over HTTPS.
    # ------------------------------------------------------------------

    async def flush_once(self) -> tuple[int, int]:
        """Drain up to ``batch_size`` events and POST as one request.

        Flow:
          1. Short-circuit if disabled (returns ``(0, 0)``).
          2. Read queue file under the lock; take the first N lines as the
             batch; keep the rest as ``remaining_raw``.
          3. Parse each line, ``_prepare_outbound`` each event (strips
             edge-only fields, attaches signed thumb URL if configured).
          4. Assemble the batch payload with a fresh ``nonce`` (a random
             hex token; guards the cloud receiver's deduplication from
             confusing two batches that happen to have identical bodies).
          5. HMAC-sign and POST.
          6. Classify the response:
               * 2xx   → success: rewrite the queue file to just the
                         remainder, reset backoff.
               * 5xx / 408 / 429 / transport error → keep the full queue
                         and sleep ``_backoff.next()``. This is the
                         "transient fault" path; the next scheduled flush
                         will retry the same batch.
               * other 4xx → "poison pill" (e.g. our signing is wrong, or
                         the cloud rejects our schema). Dropping the batch
                         avoids looping forever on the same bad lines, but
                         we log at ERROR so the operator sees it.

        Returns:
            Tuple ``(sent, queued_remaining)``:
              * ``sent``           = number of events delivered (0 on
                                     failure).
              * ``queued_remaining`` = number of lines still on disk after
                                     this call. Useful as a backlog metric.

        Edge cases intentionally handled:
          * Queue file vanished under us → treated as empty.
          * All lines in the batch are malformed JSON → truncate to avoid
            an infinite poison loop, log WARNING.
          * Batch body contains unusual Python types → ``default=str`` on
            ``json.dumps`` coerces them to strings rather than raising.
        """
        if not self.enabled():
            return (0, 0)

        async with self._lock:
            try:
                # ``read_text`` returns the whole file as one string.
                # ``splitlines()`` gives us a list of lines with the
                # trailing ``\n`` stripped — exactly what we want.
                lines = self.queue_path.read_text(encoding="utf-8").splitlines()
            except FileNotFoundError:
                # Queue could have been rotated/deleted from under us.
                # Treat as empty rather than crashing the loop.
                return (0, 0)

        if not lines:
            return (0, 0)

        # Python slicing: ``lines[:N]`` = first N elements, ``lines[N:]`` =
        # everything after index N. These slices never raise even if N >
        # len(lines).
        batch_raw = lines[: self.batch_size]
        remaining_raw = lines[self.batch_size :]

        events: list[dict] = []
        for raw in batch_raw:
            raw = raw.strip()
            if not raw:
                continue
            try:
                # ``json.loads`` parses a JSON string back to a Python dict.
                events.append(self._prepare_outbound(json.loads(raw)))
            except json.JSONDecodeError:
                # One bad line shouldn't sink the whole batch. Drop it with
                # a log entry; the truncation logic below will remove it
                # from disk so we don't re-see it on the next flush.
                logger.warning("dropping malformed queue line")

        if not events:
            # All lines were junk; still truncate so we don't loop on them.
            # Without this, a single corrupt leading line would block all
            # subsequent flushes forever.
            async with self._lock:
                self.queue_path.write_text(
                    "\n".join(remaining_raw) + ("\n" if remaining_raw else ""),
                    encoding="utf-8",
                )
            return (0, len(remaining_raw))

        payload = {
            "events": events,
            "source": self.source_name,
            # ``secrets.token_hex(8)`` gives 16 hex chars / 64 random bits.
            # Purpose of the nonce: two identical batches generated in the
            # same second would otherwise produce the same signed bytes;
            # the nonce breaks that accidental collision without requiring
            # the signer to track state.
            "nonce": secrets.token_hex(8),
        }
        # Compact JSON (no spaces) keeps the signed body short and matches
        # what the cloud receiver expects. ``.encode()`` turns it to bytes.
        body = json.dumps(payload, separators=(",", ":"), default=str).encode()
        ts = int(time.time())
        headers = {
            "Content-Type": "application/json",
            # The timestamp is sent as a header AND included in the signed
            # message — the cloud uses it to enforce a ~300s replay window.
            "X-Road-Timestamp": str(ts),
            "X-Road-Source": self.source_name,
            # ``Signature: sha256=...`` is a de facto convention for HMAC
            # webhooks (GitHub, Stripe, etc. use the same shape).
            "Signature": self._sign(body, ts),
        }

        try:
            # ``async with httpx.AsyncClient(...)`` creates a pooled HTTP
            # client and guarantees its connections are closed on exit.
            # ``timeout=15.0`` covers connect + read + write (wall-clock).
            async with httpx.AsyncClient(timeout=15.0) as client:
                # ``await`` suspends this coroutine until the POST returns.
                # We pass ``content=body`` (raw bytes) because we've already
                # built the exact bytes we signed — using ``json=`` here
                # would re-serialize and change the signature.
                resp = await client.post(self.endpoint_url, content=body, headers=headers)
            # Retryable failures — server-side hiccups, overload, or an
            # explicit "slow down" signal. Keep the batch queued.
            #   408 = Request Timeout, 429 = Too Many Requests (rate limit),
            #   5xx = server errors.
            if resp.status_code >= 500 or resp.status_code in (408, 429):
                delay = self._backoff.next()
                logger.warning(
                    "cloud ingest %s; backing off %.1fs, keeping %d queued",
                    resp.status_code,
                    delay,
                    len(batch_raw),
                )
                await asyncio.sleep(delay)
                return (0, len(lines))
            if resp.status_code >= 400:
                # 4xx other than the transient codes above: drop the batch
                # to avoid poison-pill loops, but log loudly. In practice
                # this is signature/secret misconfig and should page an
                # operator. Shipping the first 200 chars of the body
                # balances diagnostic value with log-volume sanity.
                logger.error(
                    "cloud ingest refused %s: %s; dropping batch of %d",
                    resp.status_code,
                    resp.text[:200],
                    len(events),
                )
                async with self._lock:
                    self.queue_path.write_text(
                        "\n".join(remaining_raw) + ("\n" if remaining_raw else ""),
                        encoding="utf-8",
                    )
                return (0, len(remaining_raw))
        except (httpx.HTTPError, OSError) as exc:
            # Narrow catches: httpx wraps network errors in HTTPError
            # subclasses; OSError covers DNS / socket / FS errors. We
            # deliberately do NOT catch plain Exception here — a logic bug
            # should bubble out and be surfaced by the watchdog.
            delay = self._backoff.next()
            logger.warning(
                "cloud ingest transport error %s; backing off %.1fs", exc, delay
            )
            await asyncio.sleep(delay)
            return (0, len(lines))

        # 2xx: truncate queue to the remainder. Write-truncate is safe
        # because we're holding the lock for the whole read-modify-write.
        self._backoff.reset()
        async with self._lock:
            self.queue_path.write_text(
                "\n".join(remaining_raw) + ("\n" if remaining_raw else ""),
                encoding="utf-8",
            )
        return (len(events), len(remaining_raw))

    # ------------------------------------------------------------------
    # Long-running driver: call this once from ``server.py`` at startup.
    # ------------------------------------------------------------------

    async def run_forever(self) -> None:
        """Background task. Flushes every ``flush_interval_sec`` until cancelled.

        Lifecycle:
          - Started by ``server.py`` via ``asyncio.create_task(...)``.
          - Runs until the task is cancelled during app shutdown.
          - Survives per-iteration errors by logging and continuing — the
            only way out is ``CancelledError``, which we re-raise so
            ``asyncio`` can unwind cleanly.

        Raises:
            asyncio.CancelledError: re-raised unchanged so ``asyncio``
                shutdown works correctly. Swallowing CancelledError here
                would leak a dangling task on shutdown.
        """
        logger.info(
            "edge_publisher.run_forever start (enabled=%s, endpoint=%s, queue=%s)",
            self.enabled(),
            self.endpoint_url,
            self.queue_path,
        )
        while True:
            try:
                # Sleep first so that at app startup we don't race other
                # init code. ``asyncio.sleep`` yields back to the event
                # loop — unlike ``time.sleep`` it doesn't block other
                # coroutines.
                await asyncio.sleep(self.flush_interval_sec)
                if not self.enabled():
                    continue
                sent, remaining = await self.flush_once()
                if sent or remaining:
                    logger.info(
                        "edge_publisher flushed sent=%d remaining=%d", sent, remaining
                    )
            except asyncio.CancelledError:
                # Re-raise so the event loop can finalize the task.
                raise
            except Exception as exc:  # noqa: BLE001
                # Last-line-of-defence catch so a bug in one iteration
                # doesn't permanently stop outbound delivery. The logger
                # emits a full traceback at EXCEPTION level.
                logger.exception("edge_publisher loop error: %s", exc)
                await asyncio.sleep(self._backoff.next())
