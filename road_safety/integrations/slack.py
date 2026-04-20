"""Slack notifier — Incoming Webhook with optional image relay.

Responsibility
--------------
Push human-readable road-safety notifications to a Slack channel via
Slack's "Incoming Webhook" feature. Slack is treated as a *shared,
third-party* channel: anything we post is visible to every human in that
channel and is stored by Slack indefinitely.

PRIVACY INVARIANTS (load-bearing — do not weaken)
-------------------------------------------------
- **Only the ``_public.jpg`` (redacted) thumbnail is ever referenced.**
  The caller passes the public thumb path into ``notify_event`` /
  ``notify_high``. Never pass the un-redacted path from this codebase.
- **No raw plate text.** The only plate-related field we ever render is
  ``enrichment.plate_hash`` — a salted SHA-256 digest — and we label it
  explicitly in the Slack card as "(salted — correlation only)".
- **Image upload is opt-in.** By default we do NOT ship the image bytes
  to any third-party host. With ``SLACK_ENABLE_IMAGE_RELAY=1`` the
  ``_upload_public_image`` helper uploads to a public image host so
  Slack can render the image inline. Turn this on only for deployments
  that have reviewed the privacy implications. A preferred production
  path is to replace that helper with an S3 / Azure Blob upload + signed
  URL so the image lives inside the fleet's own cloud tenant.

Tiered alerting (the core design)
---------------------------------
Different risk levels get different delivery cadences. The goal is an
actionable channel, not a noisy one — alert fatigue is the single biggest
reason fleet-safety tooling gets silenced.

  high    -> ``notify_high()``     fires immediately, rich Block Kit card,
                                    subject to the high-risk quality gate
  medium  -> ``buffer_medium()``   batched, flushed hourly as a digest
  low     -> ``buffer_low()``      batched, flushed daily as a digest

``notify_event(event, thumb_path)`` is the single public entry point that
routes by tier.

Environment variables read by this module
-----------------------------------------
- ``SLACK_WEBHOOK_URL``              — full ``https://hooks.slack.com/...``
                                       URL. If unset, every function here
                                       silently no-ops.
- ``SLACK_MIN_RISK``                 — floor for which risk levels we even
                                       consider posting (``low`` |
                                       ``medium`` | ``high``). Default
                                       ``high``.
- ``SLACK_ENABLE_IMAGE_RELAY``       — ``1`` / ``true`` to upload the
                                       redacted thumbnail to the public
                                       image host and include it inline.
                                       Default OFF.
- ``SLACK_HIGH_MIN_DURATION_SEC``    — sustained-evidence gate: episode
                                       must last at least this long to
                                       fire an immediate high-risk alert.
                                       Default 1.5s.
- ``SLACK_HIGH_MIN_FRAMES``          — sustained-evidence gate: episode
                                       must contain at least this many
                                       high-risk frames. Default 2.
- ``SLACK_HIGH_MIN_CONFIDENCE``      — sustained-evidence gate: peak
                                       confidence floor. Default 0.55.

State (in-process only)
-----------------------
- ``_MEDIUM_BUFFER``  — list[dict], drained by ``flush_medium_digest``.
- ``_LOW_BUFFER``     — list[dict], drained by ``flush_low_daily``.

Both buffers are in-memory and unbounded. Deployments expecting long
Slack outages should persist them (SQLite or equivalent) with a memory
cap — noted as a follow-up item elsewhere.
"""

# ``from __future__ import annotations`` makes type hints evaluated lazily
# (treated as strings), enabling newer syntax like ``str | None`` on older
# Python versions without runtime cost.
from __future__ import annotations

# Standard library imports, then third-party — blank line separates groups.
import os                        # read env vars at import time
from collections import Counter  # tally event types for digest summaries
from pathlib import Path         # OO filesystem paths for thumbnail access

# ``httpx`` is an async-capable HTTP client. We use its ``AsyncClient``
# context manager so connections are pooled and always closed cleanly.
import httpx

# ============================================================================
# Section: module-level configuration (read once at import time)
# ----------------------------------------------------------------------------
# Leading-underscore names are a Python convention for "module-private":
# they are NOT exported by ``from module import *`` and signal to human
# readers "do not import this from outside".
# ============================================================================

# ``os.getenv(NAME)`` returns the env var value or None if unset.
_WEBHOOK = os.getenv("SLACK_WEBHOOK_URL")
# ``os.getenv(NAME, DEFAULT)`` adds a fallback. ``.lower()`` normalizes so
# "HIGH" / "High" / "high" all behave the same.
_MIN_RISK = os.getenv("SLACK_MIN_RISK", "high").lower()

# Risk tier ordering — higher number = more severe. Used as a cheap
# numerical threshold in ``_should_notify``. Storing this as a dict (rather
# than an Enum) keeps the mapping easy to eyeball.
_RISK_ORDER = {"low": 1, "medium": 2, "high": 3}
# Slack renders these emoji shortcodes inline. :rotating_light: is widely
# understood as "active incident"; :warning: as "needs attention"; and
# :information_source: as "FYI". Consistent iconography is how humans
# triage a noisy channel at a glance.
_RISK_EMOJI = {"high": ":rotating_light:", "medium": ":warning:", "low": ":information_source:"}
# Human-readable review SLAs embedded in the Slack card. They mirror the
# values in ``docs/road_policy.md`` — if one side changes, update the other.
_SLA = {"high": "15 minutes", "medium": "24 hours", "low": "weekly batch"}

# Public image host used when ``SLACK_ENABLE_IMAGE_RELAY`` is truthy.
# catbox.moe is a simple anonymous file host; good for demos, NOT a
# recommended production target — swap this out for an S3 / Azure Blob
# + signed URL path before rolling out widely.
_IMAGE_HOST = "https://catbox.moe/user/api.php"
# Truthy parse: accept any of ``1 / true / yes / on`` (case-insensitive).
# The default is OFF because uploading images to a third-party host is a
# deliberate privacy decision, not something to enable by accident.
_IMAGE_RELAY_ENABLED = os.getenv("SLACK_ENABLE_IMAGE_RELAY", "0").lower() in ("1", "true", "yes", "on")

# High-risk Slack quality gate. The immediate Slack alert fires only when
# the underlying episode has sustained evidence: minimum duration, minimum
# number of high-risk frames, and minimum detection confidence. Events that
# fail the gate route to the hourly medium digest — never silently dropped.
# All thresholds tunable via environment variables.
#
# Why these specific defaults?
#   * 1.5s duration: below this, an event is usually a tracker flicker or
#     a single-frame false positive. Real near-misses persist ≥2 frames
#     at 2 fps, i.e. about 1 second; we add ~0.5s of buffer.
#   * 2 high-risk frames: the sustained-risk downgrade in the episode
#     accumulator demotes peaks unsupported over ≥2 frames.
#   * 0.55 confidence: tuned against our labelled FP set; below this the
#     operator-accept rate drops below the channel's tolerance.
SLACK_HIGH_MIN_DURATION_SEC = float(os.getenv("SLACK_HIGH_MIN_DURATION_SEC", "1.5"))
SLACK_HIGH_MIN_FRAMES = int(os.getenv("SLACK_HIGH_MIN_FRAMES", "2"))
SLACK_HIGH_MIN_CONFIDENCE = float(os.getenv("SLACK_HIGH_MIN_CONFIDENCE", "0.55"))

# Settings Console: confidence floor is operator-tunable at runtime.
from road_safety.settings_store import STORE as _SETTINGS_STORE  # noqa: E402

# ---------------------------------------------------------------------------
# Tiered buffers — drained by digest.py schedulers.
# NOTE: unbounded in-memory. See module docstring.
# The annotation ``list[dict]`` says "a list of dicts" and doubles as
# documentation for anyone reading the module.
# ---------------------------------------------------------------------------
_MEDIUM_BUFFER: list[dict] = []
_LOW_BUFFER: list[dict] = []


def slack_configured() -> bool:
    """True iff ``SLACK_WEBHOOK_URL`` is set.

    Every outbound function checks this first and returns silently when
    False, so callers can import and invoke this module without worrying
    about whether Slack is configured.
    """
    return bool(_WEBHOOK)


def _should_notify(risk_level: str) -> bool:
    """Compare ``risk_level`` against the configured floor ``_MIN_RISK``.

    The ``.get(key, DEFAULT)`` calls ensure unknown risk strings don't
    raise — an unknown incoming level is treated as rank 0 (always fails
    the gate), and an unknown configured floor is treated as rank 3
    ("high"), which is the strictest possible setting. Fail closed.
    """
    return _RISK_ORDER.get(risk_level, 0) >= _RISK_ORDER.get(_MIN_RISK, 3)


def get_medium_buffer() -> list[dict]:
    """Return a READ-ONLY snapshot of pending medium-risk events.

    ``list(_MEDIUM_BUFFER)`` shallow-copies so callers (e.g. the UI
    coaching queue) can iterate without racing the digest flush.
    """
    return list(_MEDIUM_BUFFER)


def get_low_buffer() -> list[dict]:
    """Return a read-only snapshot of pending low-risk events. See above."""
    return list(_LOW_BUFFER)


async def _upload_public_image(client: httpx.AsyncClient, thumb_path: Path) -> str | None:
    """Upload a REDACTED thumbnail to the public image host and return its URL.

    PRIVACY CALLOUT: the caller MUST pass the ``_public.jpg`` path — never
    the un-redacted original. This function does not re-check redaction;
    the caller owns that invariant (see ``notify_high``). The upload
    target is a third-party host: once a file lands there, assume it is
    public-forever.

    Args:
        client: a live ``httpx.AsyncClient`` so this function shares the
            connection pool of the parent request.
        thumb_path: filesystem path to the redacted JPEG.

    Returns:
        The publicly-reachable URL on success, or None on any failure.
        Failure is logged but never raised — image upload is optional
        decoration; the text alert is what matters.
    """
    try:
        # ``with ... as f`` is a context manager; ``f`` is closed
        # automatically when the block exits, even on exception. Opened in
        # binary mode (``"rb"``) because JPEGs are binary data.
        with thumb_path.open("rb") as f:
            r = await client.post(
                _IMAGE_HOST,
                data={"reqtype": "fileupload"},
                # multipart/form-data file upload. Tuple form is
                # (filename, file-object, content-type).
                files={"fileToUpload": (thumb_path.name, f, "image/jpeg")},
                # 20s timeout is deliberately generous because catbox-style
                # hosts can be slow but are usually eventually-successful.
                timeout=20,
            )
        body = r.text.strip()
        if r.status_code == 200 and body.startswith("http"):
            return body
        print(f"[slack] image relay rejected: {r.status_code} {body[:120]}")
    except Exception as exc:
        # Broad catch: image upload is best-effort decoration. Swallow
        # any network/SSL/FS issue and fall back to a text-only card.
        print(f"[slack] image relay failed: {exc}")
    return None


def _build_blocks(event: dict, image_url: str | None) -> list:
    """Assemble the Slack "Block Kit" payload for a single event card.

    Slack's Block Kit is a JSON schema of structured message components
    (headers, sections, images, contexts). We build a list of block dicts
    and return it to be attached as the ``blocks`` field on the webhook
    payload. Slack renders this as a rich card in the channel.

    Args:
        event: the internal event dict. We read only fields that have
            already been redacted (hashed plate, etc.).
        image_url: public URL of the redacted thumbnail (may be None). If
            None we render a "screenshot omitted" context block so
            humans know the image was intentionally withheld.

    Returns:
        A list of Block Kit dicts, safe to JSON-serialize into the
        webhook request body.
    """
    risk = event["risk_level"]
    emoji = _RISK_EMOJI.get(risk, ":warning:")
    # ``"snake_case".replace("_", " ").title()`` yields "Snake Case".
    # Makes event types human-readable in the header.
    etype_pretty = event["event_type"].replace("_", " ").title()
    # ``d.get(k) or default`` — because ``d.get(k)`` returns None when
    # missing, and ``None or x`` is ``x``. This gives us a safe fallback
    # without nested ifs.
    narration = event.get("narration") or event.get("summary", "")
    # ``", ".join(iterable)`` = concatenate strings with a separator.
    objs = ", ".join(event.get("objects", []))
    confidence_pct = int(round(event.get("confidence", 0) * 100))

    enrich = event.get("enrichment") or {}
    # Plate text is never on the egress payload — we only ever see a salted
    # hash. If this ever receives a raw plate, something upstream has
    # broken the privacy invariant and the fix is there, not here.
    plate_hash = enrich.get("plate_hash")
    color = enrich.get("vehicle_color")
    vtype = enrich.get("vehicle_type")
    readability = enrich.get("readability")

    plate_line = ""
    if plate_hash:
        plate_line = f"*Plate ref:* `{plate_hash}` _(salted — correlation only)_"
    elif readability:
        plate_line = f"*Plate:* _unreadable ({readability})_"

    vehicle_bits = [v for v in (color, vtype) if v]
    vehicle_line = f"*Vehicle:* {' '.join(vehicle_bits)}" if vehicle_bits else ""

    enrich_fields = "  ·  ".join(s for s in (plate_line, vehicle_line) if s)

    kinematics_parts = []
    if event.get("ttc_sec") is not None:
        kinematics_parts.append(f"*TTC:* `{event['ttc_sec']}s`")
    if event.get("distance_m") is not None:
        kinematics_parts.append(f"*Distance:* `{event['distance_m']}m`")
    kinematics_parts.append(f"*Edge px:* `{event['distance_px']}`")
    kinematics_line = "   ".join(kinematics_parts)

    track_ids = event.get("track_ids") or []
    track_line = (
        f"*Track pair:* `{'/'.join(str(t) for t in track_ids)}`"
        if track_ids
        else "*Track pair:* _untracked_"
    )
    duration = event.get("episode_duration_sec")
    duration_line = f"*Episode:* `{duration}s`" if duration else ""

    fields_md = (
        f"*Event ID:* `{event['event_id']}`   {track_line}\n"
        f"*Time:* `{event['wall_time']}`   *Stream t+:* `{event['timestamp_sec']}s`"
        + (f"   {duration_line}" if duration_line else "") + "\n"
        f"*Objects:* {objs}   {kinematics_line}   *Confidence:* `{confidence_pct}%`\n"
        f"*Review SLA:* {_SLA.get(risk, '—')} _(per road_policy.md)_"
    )
    if enrich_fields:
        fields_md += f"\n{enrich_fields}"

    blocks: list = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{risk.upper()}-risk road event — {etype_pretty}",
            },
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"{emoji} {narration}"},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": fields_md},
        },
    ]

    if image_url:
        # Slack renders the image inline. The URL must be publicly
        # reachable from Slack's servers — hence the image-relay upload
        # path. Alt text is required by Block Kit and aids accessibility.
        blocks.append(
            {
                "type": "image",
                "image_url": image_url,
                "alt_text": f"{event['event_id']} screenshot",
            }
        )
    else:
        # No image: emit an explicit context line so operators understand
        # the image was withheld by policy (or the relay failed) — vs.
        # silently having no image, which is confusing.
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            "_screenshot omitted (image relay disabled)_"
                            if not _IMAGE_RELAY_ENABLED
                            else "_screenshot unavailable (image relay failed)_"
                        ),
                    }
                ],
            }
        )

    return blocks


# ============================================================================
# Section: high-risk immediate delivery
# ----------------------------------------------------------------------------
# High-risk events are the only ones that fire immediately. The caller
# (``notify_event``) applies the sustained-evidence quality gate before
# reaching this function; bypass it only if you understand that.
# ============================================================================


async def notify_high(event: dict, thumb_path: Path) -> None:
    """Post a high-risk event to Slack immediately (rich Block Kit card).

    Tier gating is assumed to have been done by the caller — this function
    trusts that ``event`` is already high-risk, has passed the quality
    gate, and that ``thumb_path`` points at the REDACTED thumbnail.

    Args:
        event: the internal event dict (fields: ``risk_level``,
            ``event_type``, ``narration`` / ``summary``, etc.).
        thumb_path: path to the ``_public.jpg`` redacted thumbnail. Only
            used when ``SLACK_ENABLE_IMAGE_RELAY`` is truthy AND the file
            exists. If missing or the relay is disabled, the message is
            sent text-only.

    Returns:
        None. Failures are logged but never raised: the perception loop
        must not crash because Slack is down.
    """
    # Configuration gate — if no webhook URL, silently do nothing. This
    # lets development / CI environments run without faking out Slack.
    if not slack_configured():
        return

    # Explicit ``: str | None`` annotation documents that the URL may be
    # absent — the ``_build_blocks`` call handles both cases.
    image_url: str | None = None
    try:
        async with httpx.AsyncClient() as client:
            if _IMAGE_RELAY_ENABLED and thumb_path and thumb_path.exists():
                image_url = await _upload_public_image(client, thumb_path)

            payload = {
                "blocks": _build_blocks(event, image_url),
                # ``text`` is Slack's fallback for notifications and
                # older clients / mobile push previews. We keep it terse.
                "text": (
                    f"{event['risk_level'].upper()} road event: {event['event_type']} — "
                    f"{event.get('narration') or event.get('summary', '')}"
                ),
            }
            # 10s POST timeout — Slack webhooks are normally sub-second;
            # anything over 10s is effectively a failure.
            r = await client.post(_WEBHOOK, json=payload, timeout=10)
            # Slack's incoming-webhook spec: success = HTTP 200 with body
            # literally "ok". Anything else is an error.
            if r.status_code != 200 or r.text.strip() != "ok":
                print(f"[slack] webhook rejected: {r.status_code} {r.text[:200]}")
                return
        print(
            f"[slack] notified {event['event_id']} ({event['risk_level']}) "
            f"image={'yes' if image_url else 'no'}"
        )
    except Exception as exc:
        # Broad catch: Slack is non-critical infrastructure. Never let
        # a notify hiccup propagate into the detection pipeline.
        print(f"[slack] notify failed for {event['event_id']}: {exc}")


# ============================================================================
# Section: medium / low buffering
# ----------------------------------------------------------------------------
# Cheap in-memory appends, no network I/O. Drained by digest schedulers
# elsewhere (``services/digest.py``). Rate-limiting Slack to a single
# grouped message per hour/day is what keeps the channel actionable.
# ============================================================================


def buffer_medium(event: dict) -> None:
    """Append a medium-risk event to the hourly digest buffer (no I/O)."""
    _MEDIUM_BUFFER.append(event)


def buffer_low(event: dict) -> None:
    """Append a low-risk event to the daily digest buffer (no I/O)."""
    _LOW_BUFFER.append(event)


def _summarise_counts(events: list[dict]) -> str:
    """Human-readable tally for the digest header.

    Example output: ``"3 vehicle-proximity events, 2 pedestrian-proximity events"``.

    ``collections.Counter`` is a dict subclass that counts occurrences in
    an iterable. ``.most_common()`` yields ``(item, count)`` pairs in
    descending frequency. Using it here means we never hand-roll a
    frequency loop.
    """
    # Generator expression — ``(expr for item in iterable)`` — is passed
    # directly to Counter without building an intermediate list.
    counter = Counter(e.get("event_type", "unknown") for e in events)
    parts = []
    for etype, n in counter.most_common():
        pretty = etype.replace("_", "-")
        # English pluralization via a ternary expression:
        #   ``X if cond else Y``
        noun = "event" if n == 1 else "events"
        parts.append(f"{n} {pretty} {noun}")
    return ", ".join(parts) if parts else "no events"


def _format_digest_lines(events: list[dict], limit: int = 25) -> str:
    """Plain-text per-event lines for the digest body (capped at ``limit``).

    ``limit`` defaults to 25 because Slack message length is bounded and
    operators eyes glaze over beyond ~25 bullets. Overflow is collapsed
    into a single "… and N more" line so the exact total is still visible.
    """
    lines = []
    for e in events[:limit]:
        eid = e.get("event_id", "?")
        etype = e.get("event_type", "event").replace("_", "-")
        # Prefer wall-clock time; fall back to the stream-relative offset
        # when wall time is not present (e.g. replay of a file stream).
        when = e.get("wall_time") or f"t+{e.get('timestamp_sec', '?')}s"
        lines.append(f"• `{eid}` — {etype} @ {when}")
    if len(events) > limit:
        lines.append(f"… and {len(events) - limit} more")
    return "\n".join(lines)


async def _post_digest(title: str, summary: str, body: str) -> None:
    """Post a digest message (simple three-section Block Kit layout).

    Digests share a consistent visual shape: a title header, a one-line
    summary with a warning emoji, and a body of bullet lines. Anything
    more elaborate (metric charts, per-vehicle breakdowns) belongs in the
    dashboard UI, not here.
    """
    if not slack_configured():
        return
    payload = {
        "text": f"{title} — {summary}",
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": title},
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f":warning: {summary}"},
            },
            {
                "type": "section",
                # Italicized placeholder when there is literally nothing
                # to report — Slack rejects empty text fields.
                "text": {"type": "mrkdwn", "text": body or "_(no details)_"},
            },
        ],
    }
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(_WEBHOOK, json=payload, timeout=10)
            if r.status_code != 200 or r.text.strip() != "ok":
                print(f"[slack] digest webhook rejected: {r.status_code} {r.text[:200]}")
    except Exception as exc:
        print(f"[slack] digest post failed: {exc}")


async def flush_medium_digest() -> None:
    """Drain the medium buffer and post a single grouped Slack digest.

    Called by the hourly scheduler. Rate-limiting Slack to ONE message
    per hour for medium-risk events is how we prevent alert fatigue:
    operators see one aggregate notification, can click into the UI for
    the full list, and are not interrupted 30 times for the same
    low-priority class of event.

    No-op if the buffer is empty.
    """
    if not _MEDIUM_BUFFER:
        return
    events = list(_MEDIUM_BUFFER)
    # ``.clear()`` empties the list in place. Together with the snapshot
    # above, this pattern ("snapshot then clear") is the closest we have
    # to an atomic drain without a real queue.
    _MEDIUM_BUFFER.clear()
    summary = _summarise_counts(events) + " in the last hour"
    body = _format_digest_lines(events)
    print(f"[slack] flushing medium digest: {len(events)} events — {summary}")
    await _post_digest(
        title="Medium-risk road digest (hourly)",
        summary=summary,
        body=body,
    )


async def flush_low_daily() -> None:
    """Drain the low buffer and post a daily summary. No-op if empty.

    Daily cadence for low-risk events: they are almost always driver-
    coaching material, not operational alerts. Shipping them as a daily
    digest keeps the stream of "FYI" noise out of the main channel.
    """
    if not _LOW_BUFFER:
        return
    events = list(_LOW_BUFFER)
    _LOW_BUFFER.clear()
    summary = _summarise_counts(events) + " in the last 24h"
    body = _format_digest_lines(events)
    print(f"[slack] flushing low daily: {len(events)} events — {summary}")
    await _post_digest(
        title="Low-risk road summary (daily)",
        summary=summary,
        body=body,
    )


# ============================================================================
# Section: public tier dispatcher
# ----------------------------------------------------------------------------
# This is what ``server.py`` calls. It owns the "which tier?" decision and
# the high-risk sustained-evidence gate.
# ============================================================================


def _passes_high_quality_gate(event: dict) -> tuple[bool, str | None]:
    """Decide whether a high-risk event has enough sustained evidence to
    warrant an immediate Slack interruption.

    Gates (all must pass):
      - ``episode_duration_sec >= SLACK_HIGH_MIN_DURATION_SEC``
      - ``risk_frame_counts['high'] >= SLACK_HIGH_MIN_FRAMES``
      - ``confidence >= SLACK_HIGH_MIN_CONFIDENCE``

    A failing event is NOT silently dropped — the caller routes it into
    the hourly medium digest instead, so nothing is lost but the channel
    stays quiet.

    Returns:
        Tuple ``(passes, reason_if_not)``. When ``passes`` is False the
        second element is a short human-readable explanation suitable for
        logging; when True the second element is None.
    """
    # ``event.get(k) or 0.0`` defends against missing AND None AND 0.
    # ``float(...)`` coerces anything numeric to a float; values arrive
    # here from JSON where integer / float / string cannot be assumed.
    duration = float(event.get("episode_duration_sec") or 0.0)
    if duration < SLACK_HIGH_MIN_DURATION_SEC:
        return False, f"episode {duration:.2f}s < min {SLACK_HIGH_MIN_DURATION_SEC}s"

    risk_counts = event.get("risk_frame_counts") or {}
    high_frames = int(risk_counts.get("high", 0))
    if high_frames < SLACK_HIGH_MIN_FRAMES:
        return False, f"only {high_frames} high-risk frame(s) < min {SLACK_HIGH_MIN_FRAMES}"

    confidence = float(event.get("confidence") or 0.0)
    _conf_floor = float(
        _SETTINGS_STORE.snapshot().get("SLACK_HIGH_MIN_CONFIDENCE", SLACK_HIGH_MIN_CONFIDENCE)
    )
    if confidence < _conf_floor:
        return False, f"confidence {confidence:.2f} < min {_conf_floor:.2f}"

    return True, None


async def notify_event(event: dict, thumb_path: Path) -> None:
    """Tier-aware dispatcher with high-risk quality gate.

    This is the single public entry point used by ``server.py`` when it
    emits a safety event. Routing logic:
      - ``risk_level == "high"``: apply the sustained-evidence gate.
        Pass → fire immediately via ``notify_high``. Fail → buffer into
        the medium digest (downgrade rather than drop).
      - ``risk_level == "medium"``: buffer for the hourly digest.
      - Anything else (``"low"`` or unknown): buffer for the daily digest.

    Args:
        event: internal event dict — risk_level is the routing key.
        thumb_path: path to the ``_public.jpg`` redacted thumbnail. Only
            used by the immediate high-risk path (and only when image
            relay is enabled).
    """
    # ``(x or "").lower()`` — tolerant normalization: None, "", "HIGH",
    # "High" all end up as lowercase strings we can compare.
    risk = (event.get("risk_level") or "").lower()
    if risk == "high":
        passes, reason = _passes_high_quality_gate(event)
        if not passes:
            print(
                f"[slack] high-risk event {event.get('event_id')} downgraded "
                f"to medium digest: {reason}"
            )
            buffer_medium(event)
            return
        if _should_notify("high"):
            await notify_high(event, thumb_path)
    elif risk == "medium":
        buffer_medium(event)
    else:
        buffer_low(event)


# Diagnostic breadcrumb at import time so operators can confirm
# configuration from a single glance in the server log. This is the only
# side-effect we intentionally run at module import.
print(
    f"[slack] configured: {slack_configured()}  "
    f"min_risk: {_MIN_RISK}  "
    f"image_relay: {'enabled' if _IMAGE_RELAY_ENABLED else 'disabled'}"
)
