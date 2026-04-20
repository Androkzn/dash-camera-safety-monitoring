"""Live safety review API — the "brain" of the fleet-safety dashcam system.

Pulls a live stream, runs YOLO at 2 fps in a background thread, emits typed safety
events over Server-Sent Events with an LLM-generated one-line narration, and exposes
a RAG-backed copilot endpoint over a tiny statute/policy corpus.

=============================================================================
 MODULE OVERVIEW (for readers new to the codebase)
=============================================================================

This file is a FastAPI web service. It does THREE things in parallel:

  1. A background worker thread pulls camera frames and runs perception
     (YOLO object detection + a stack of safety "gates"). This is the hot
     path. See ``_on_frame`` and ``_run_loop``-style logic below.

  2. An asyncio event loop serves HTTP + SSE (Server-Sent Events) endpoints
     that stream live detections, expose admin controls, and offer agent /
     LLM features. See the ``@app.get(...)`` / ``@app.post(...)`` handlers.

  3. Several long-running background asyncio tasks (edge -> cloud publisher,
     retention sweep, score decay, watchdog) — all spawned in ``lifespan``.

KEY DOMAIN CONCEPTS
-------------------

  * GATES: a "real" conflict must pass several independent checks — depth,
    convergence, ego-relative motion, TTC (time-to-collision), and
    perception quality. Each gate kills a specific class of false positive.
    They are NOT removable; if you loosen one, noise spikes somewhere.
    See the gate order in ``_on_frame`` (numbered comments below).

  * EPISODE: repeated frames of the same risky situation for the same pair
    of tracked objects merge into a SINGLE event. The ``Episode`` class
    below accumulates peak severity across frames, then flushes ONE event
    when the pair hasn't been seen for a short idle window. This is how
    we de-duplicate across time.

  * PRIVACY INVARIANT (critical): license-plate text is hashed and STRIPPED
    at ingest inside ``services/llm.py::enrich_event``. ``server.py``
    additionally pops ``plate_text`` / ``plate_state`` at egress as
    defence-in-depth — see ``_emit_event`` below. Raw plate text must
    NEVER enter any in-memory buffer, SSE channel, Slack message, or
    cloud payload.

  * DUAL THUMBNAILS: every event writes two JPEGs:
        ``<id>.jpg``        — internal, unredacted (DSAR-gated access)
        ``<id>_public.jpg`` — redacted (faces + plates blurred)
    All shared channels (SSE, Slack, cloud) get ONLY the ``_public`` copy.
    The internal copy is served only to a caller presenting a valid
    ``X-DSAR-Token`` header.

  * AUTH TIERS (enforced per endpoint):
        public        — SSE, redacted thumbs, dashboard reads.
        X-DSAR-Token  — unredacted thumbnail retrieval (Data Subject
                        Access Request workflow).
        Admin bearer  — audit logs, LLM observability, road registry,
                        agents, retention, active-learning. The token is
                        read from the ``ROAD_ADMIN_TOKEN`` env var.
    Each HTTP endpoint's docstring below is labelled with its tier.

PYTHON IDIOMS USED IN THIS FILE (explained on first appearance)
---------------------------------------------------------------
  * ``from __future__ import annotations`` — defers evaluation of type
    annotations so forward references like ``list[int] | None`` work on
    older Python versions at runtime.
  * ``@asynccontextmanager`` (from ``contextlib``) — turns an async
    generator into a manager usable with ``async with``. FastAPI uses
    this for startup / shutdown ("lifespan") hooks.
  * ``async def`` / ``await`` — coroutines. They only run inside an
    event loop (here: the uvicorn asyncio loop). ``await X`` yields
    control back to the loop until ``X`` finishes.
  * ``asyncio.Queue`` — FIFO queue safe for producer/consumer across
    coroutines; used for SSE fan-out to subscribers.
  * ``asyncio.run_coroutine_threadsafe(coro, loop)`` — schedule an async
    coroutine FROM a non-async thread (we use this to hand results from
    the perception thread back to the main loop).
  * ``threading.Lock`` — mutual exclusion so the perception thread and
    HTTP handlers don't read half-written frame state at the same time.
  * ``@app.get("/path")`` / ``@app.post(...)`` — FastAPI decorators that
    register the function below as an HTTP route.
  * ``Request`` / ``StreamingResponse`` / ``FileResponse`` /
    ``HTTPException`` — FastAPI primitives: request object, chunked
    streaming response body, static file response, and the exception
    that converts into an HTTP error.
  * f-string (``f"text {var}"``) — inline string interpolation.
  * ``list[X] | None`` / ``dict[K, V]`` — PEP 604 union + PEP 585 generics.
  * Comprehensions (``[x for x in xs if ...]``) — compact list/dict/set
    construction.
  * ``try/except/finally`` — exception handling with an always-run
    ``finally`` block.
  * Module-level ``state = LiveState()`` — a single process-wide singleton
    holding the live perception state. Do NOT create a second one.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import cv2
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from road_safety.logging import setup as setup_logging, get_logger

setup_logging()
log = get_logger(__name__)

from road_safety.config import (
    ALPR_MODE,
    DATA_DIR,
    DEFAULT_STREAM_SOURCE as DEFAULT_SOURCE,
    STREAM_SOURCES,
    ADMIN_TOKEN,
    DSAR_TOKEN,
    DRIVER_ID,
    EPISODE_IDLE_FLUSH_SEC,
    LOCATION,
    ROAD_ID,
    MAX_RECENT_EVENTS,
    MODEL_PATH,
    PAIR_COOLDOWN_SEC,
    PUBLIC_THUMBS_REQUIRE_TOKEN,
    SCORE_DECAY_INTERVAL_SEC,
    SSE_REPLAY_COUNT,
    VALIDATOR_ENABLED,
    VALIDATOR_IOU_THRESHOLD,
    VALIDATOR_QUEUE_MAX,
    VALIDATOR_SAMPLE_SEC,
    camera_calibration_for,
    WATCHDOG_ENABLED,
    WATCHDOG_INTERVAL_SEC,
    STATIC_DIR,
    TARGET_FPS,
    THUMB_SIGNING_SECRET,
    THUMBS_DIR,
    VEHICLE_ID,
)
from road_safety.core.detection import (
    LOW_SPEED_FLOOR_MPS,
    VEHICLE_CLASSES,
    VEHICLE_INTER_DISTANCE_GATE_M,
    TrackHistory,
    bbox_edge_distance,
    build_event_summary,
    classify_risk,
    detect_frame,
    estimate_distance_m,
    estimate_distances_batch,
    estimate_inter_distance_m,
    estimate_pair_ttc,
    estimate_ttc_sec,
    find_interactions,
    load_model,
    tracks_converging,
)
from road_safety.core.stream import (
    StreamReader,
    classify_source,
    display_video_id,
)
from road_safety.core.validator import (
    DiscrepancyComparator,
    SecondaryDetector,
    ValidatorJob,
    ValidatorWorker,
)
from road_safety.core.quality import QualityMonitor
from road_safety.core.context import SceneContextClassifier
from road_safety.core.egomotion import EgoMotionEstimator
from road_safety.core.orientation_policy import classify_event as _orientation_classify
from road_safety.services.llm import chat as llm_chat, enrich_event, llm_configured, narrate_event
from road_safety.services.llm_obs import observer as llm_observer
from road_safety.services.redact import hash_plate, public_thumbnail_name, write_thumbnails
from road_safety.services.agents import AgentExecutor, run_coaching_agent, run_investigation_agent, run_report_agent
from road_safety.services.registry import road_registry
from road_safety.services.drift import ActiveLearningSampler, DriftMonitor, drift_warning_message
from road_safety.services.digest import start_schedulers as start_digest_schedulers
from road_safety.services import demo_track as demo_track_service
from road_safety.services import video_metadata as video_metadata_service
from road_safety.integrations.slack import notify_event as slack_notify, slack_configured
from road_safety.integrations.edge_publisher import EdgePublisher
from road_safety.api.feedback import mount as mount_feedback_routes
from road_safety.api.settings import mount as mount_settings_routes
from road_safety.services.impact import ImpactMonitor as SettingsImpactMonitor
from road_safety.services.ops_sampler import OpsSampler
from road_safety.settings_store import STORE as SETTINGS_STORE
from road_safety.compliance import audit
from road_safety.compliance.retention import retention_loop, run_sweep as retention_sweep
from road_safety.services.test_runner import run_state as test_run_state, start_test_run
from road_safety.services.watchdog import Watchdog, WatchdogFinding, _write_finding as _watchdog_write_finding, tail as watchdog_tail, stats as watchdog_stats, delete_findings as watchdog_delete, delete_findings_by_id as watchdog_delete_by_id
from road_safety.security import require_bearer_token


# ===== SECTION: IMPORTS DONE. LOGGING CONFIGURED. =====
# Everything above is pure wiring — bring the dependencies into scope and
# configure structured logging (``setup_logging``) so every module uses the
# same formatter. ``log`` is a module-scoped logger; never ``print``.

# ===== SECTION: FLEET IDENTITY RESOLUTION =====
# Events are meaningless to downstream fleet analytics if they can't be
# attributed to a specific vehicle / road / driver. We resolve identity ONCE
# at import time; the results are frozen into module-level constants below
# and stamped onto every emitted event in ``_flush_episode``.


# Resolved fleet identity — every emitted event MUST carry a non-empty
# vehicle_id / road_id / driver_id or downstream fleet aggregation is
# broken (events appear as "unidentified"). If the operator didn't set the
# env vars, fall back to a stable hostname-derived default and warn loudly
# at startup so the deployment is obviously misconfigured instead of
# silently producing unattributable events.
def _resolve_identity() -> tuple[str, str, str, list[str]]:
    """Return the effective fleet identity for this process, plus any gaps.

    Reads ``VEHICLE_ID`` / ``ROAD_ID`` / ``DRIVER_ID`` (sourced from env in
    ``road_safety/config.py``). If any is missing, substitutes a stable
    hostname-derived placeholder so events still emit — but also records
    the missing env-var names so ``lifespan`` can log a loud warning.

    Returns:
        A 4-tuple ``(vehicle_id, road_id, driver_id, missing_env_vars)``.
        ``missing_env_vars`` is the list of env var names that were empty
        (e.g. ``["ROAD_VEHICLE_ID"]``) — empty means fully configured.
    """
    import socket
    host = socket.gethostname().split(".")[0] or "unknown"
    missing: list[str] = []
    vid = VEHICLE_ID
    rid = ROAD_ID
    did = DRIVER_ID
    if not vid:
        vid = f"unidentified_vehicle_{host}"
        missing.append("ROAD_VEHICLE_ID")
    if not rid:
        rid = f"unidentified_road_{host}"
        missing.append("ROAD_ID")
    if not did:
        did = f"unidentified_driver_{host}"
        missing.append("ROAD_DRIVER_ID")
    return vid, rid, did, missing


RESOLVED_VEHICLE_ID, RESOLVED_ROAD_ID, RESOLVED_DRIVER_ID, _MISSING_IDENTITY = (
    _resolve_identity()
)
# Note: tuple-unpacking a function result into multiple module-level
# constants is a common Python idiom. These values are frozen for the
# lifetime of the process; swapping identity mid-run would desync
# downstream cloud aggregation.


# ===== SECTION: TUNABLE CONSTANTS (WHY each matters) =====

# Sustained-risk requirements for episode emission. A single high-risk frame
# in an otherwise calm episode is almost always a transient detection artefact;
# real conflicts produce ≥ 2 high-risk frames over ≥ 1 s of episode duration.
# WHY these numbers: lowering ``MIN_HIGH_RISK_FRAMES`` below 2 lets bbox-
# jitter spikes through as "high"; raising ``MIN_HIGH_RISK_EPISODE_SEC``
# above ~1s starts missing real short-lived collisions (motorbike cut-ins).
MIN_HIGH_RISK_FRAMES = 2
MIN_MEDIUM_RISK_FRAMES = 2
MIN_HIGH_RISK_EPISODE_SEC = 1.0


# ===== SECTION: EPISODE — TEMPORAL DE-DUPLICATION =====
# An "episode" aggregates many consecutive frames observing the SAME pair of
# tracked objects into ONE emitted event. Without this layer the SSE feed
# would spew a hundred high-risk alerts for a single near-miss.


class Episode:
    """An ongoing interaction between a specific *pair* of tracked objects.

    The episode is held open while the pair stays in view, accumulating the
    worst risk and tightest distance across its lifetime, plus per-risk-level
    frame counts. On flush, the peak risk is **downgraded** if it lacks
    sustained support — a single high-risk frame is treated as a transient and
    reported as medium; a single medium frame becomes low.

    The episode model suppresses per-frame detection-artefact spam by
    requiring sustained evidence before promoting a peak risk into the
    emitted event.
    """

    def __init__(self, event_type: str, pair: tuple[int, int], started_at: float):
        """Initialise an empty episode for a specific (event_type, track-pair).

        Args:
            event_type: One of ``"pedestrian_proximity"`` /
                ``"vehicle_close_interaction"`` / etc. (see
                ``core/detection.py::find_interactions``).
            pair: Canonical ``(lo, hi)`` track-id pair as produced by
                ``_pair_key`` below.
            started_at: Wall-clock seconds (``time.time()``) when the pair
                was first observed. Doubles as the reference for the
                ``timestamp_sec`` field stamped onto the emitted event.

        State held:
            * ``peak_*``: snapshot of the worst frame seen so far (frame
              pixels, detections list, primary + secondary detection,
              distance_px, TTC, distance_m, risk label).
            * ``frame_count`` / ``risk_frame_counts``: per-risk tallies
              used by ``final_risk`` for the sustained-risk downgrade.
            * ``emitted``: one-shot guard — each episode emits at most
              one event regardless of how many flush attempts happen.
        """
        self.event_type = event_type
        self.pair = pair
        self.started_at = started_at
        self.last_seen_at = started_at
        # Orientation-policy decision cached on the episode so `_flush_episode`
        # can stamp SAE J3063 family + display-type overrides onto the emitted
        # event payload without re-running the gate at flush time. Populated
        # by the first frame that opens the episode (see `_run_loop`); later
        # frames never overwrite it because a pair that started as BSW cannot
        # mid-episode become FCW without a new pair key.
        self.camera_orientation: str | None = None
        self.event_taxonomy: str = "FCW"
        self.display_event_type: str | None = None
        self.policy_reason: str | None = None
        self.peak_frame = None
        self.peak_detections: list = []
        self.peak_primary = None
        self.peak_secondary = None
        # ``float("inf")`` is a valid float that compares greater than any
        # finite number — used as an initial sentinel so the first real
        # measurement always wins the "tightest distance" check below.
        self.peak_distance_px: float = float("inf")
        self.peak_ttc: float | None = None
        self.peak_distance_m: float | None = None
        self.peak_risk: str = "low"
        self.frame_count: int = 0
        self.risk_frame_counts: dict[str, int] = {"low": 0, "medium": 0, "high": 0}
        self.emitted: bool = False

    def update(
        self,
        frame,
        detections,
        a,
        b,
        distance_px: float,
        ttc: float | None,
        dist_m: float | None,
        risk: str,
        now: float,
    ) -> None:
        """Fold one fresh frame observation into the rolling episode.

        Replaces the stored "peak" snapshot when the new frame is strictly
        worse than anything seen before — either a higher risk tier, or
        the same tier at a tighter pixel distance (tighter = more
        visually compelling thumbnail for review).

        Args:
            frame: Raw BGR numpy image from OpenCV. ``frame.copy()`` is
                held when it becomes the peak — we own an independent
                copy, the background reader is free to reuse its buffer.
            detections: List of ``Detection`` dataclasses for the whole
                frame (not just the interacting pair). The full list is
                stored so the redactor can draw bounding boxes around
                every visible object, not just the conflict participants.
            a, b: The two ``Detection`` objects that form this interaction.
            distance_px: 2D pixel distance between bbox centres — used as
                a last-resort distance proxy and as the peak tiebreaker.
            ttc: Time-to-collision in seconds, or ``None`` when unknown.
            dist_m: Estimated 3D separation in metres, or ``None``.
            risk: ``"low"`` / ``"medium"`` / ``"high"`` — already scene-
                adapted and low-speed-floored by the caller.
            now: Wall-clock timestamp for this observation.
        """
        self.last_seen_at = now
        self.frame_count += 1
        if risk in self.risk_frame_counts:
            self.risk_frame_counts[risk] += 1
        # ``risk_rank`` gives us an ordinal comparison on the string enum.
        # Keeping the mapping local to this method means we can't
        # accidentally mutate it from outside.
        risk_rank = {"low": 0, "medium": 1, "high": 2}
        is_new_peak = (
            risk_rank[risk] > risk_rank[self.peak_risk]
            or (risk == self.peak_risk and distance_px < self.peak_distance_px)
        )
        if is_new_peak or self.peak_frame is None:
            self.peak_frame = frame.copy()
            self.peak_detections = list(detections)
            self.peak_primary = a
            self.peak_secondary = b
            self.peak_distance_px = distance_px
            self.peak_ttc = ttc
            self.peak_distance_m = dist_m
            self.peak_risk = risk

    def final_risk(self) -> str:
        """Sustained-risk-aware downgrade.

        A peak risk only stands if supported by enough frames AND enough
        episode duration. Otherwise it is downgraded one level.

        Returns:
            ``"low"`` / ``"medium"`` / ``"high"``. The returned value may
            differ from ``self.peak_risk`` — ``_flush_episode`` records
            ``risk_demoted=True`` when this happens so reviewers can tell
            at a glance that the peak wasn't sustained.
        """
        # ``max(..., 0.0)`` guards against clock skew / reorderings that
        # could produce a negative duration and misleadingly pass the
        # threshold in either direction.
        duration = max(self.last_seen_at - self.started_at, 0.0)
        high = self.risk_frame_counts.get("high", 0)
        med = self.risk_frame_counts.get("medium", 0)

        if self.peak_risk == "high":
            if high >= MIN_HIGH_RISK_FRAMES and duration >= MIN_HIGH_RISK_EPISODE_SEC:
                return "high"
            # Demote to medium if the medium support is there, else low.
            # Rationale: a momentary TTC spike with no follow-through is
            # likely bbox jitter, not an actual near-miss.
            if (high + med) >= MIN_MEDIUM_RISK_FRAMES:
                return "medium"
            return "low"
        if self.peak_risk == "medium":
            if (high + med) >= MIN_MEDIUM_RISK_FRAMES:
                return "medium"
            return "low"
        return "low"


# ===== SECTION: LIVESTATE — SINGLETON HOLDING ALL IN-MEMORY STATE =====
# One instance of ``LiveState`` exists per process (``state = LiveState()``
# below). It glues together the YOLO model, stream reader, episode registry,
# SSE subscribers, perception / scene / drift monitors, and the latest
# annotated frame for the admin video feed. Threads coordinate through it,
# so read/write guards (``threading.Lock``) protect any non-atomic fields.


class StreamSlot:
    """Per-source perception state.

    Each monitored stream gets its own slot bundling: the StreamReader,
    the per-frame annotated JPEG buffer, and every per-source perception
    object (quality, scene, ego, track history, episodes, pair cooldown).
    Detected events from this slot land in the SHARED ``state.recent_events``
    buffer with ``source_id`` / ``source_name`` tags so downstream
    consumers (UI, Slack, cloud) can disambiguate.

    Why per-slot for quality/scene/ego/episodes/pair_cooldown:
        These are stateful estimators whose output depends on a rolling
        window of frames from ONE camera. Sharing them across cameras
        would corrupt every estimate.

    Why shared for recent_events / subscribers / drift / publisher:
        These are pure aggregators / fan-outs — no per-source state to
        maintain. SSE clients want one merged stream, the cloud wants
        one HMAC-signed batch, drift is fleet-wide.
    """

    def __init__(self, source_id: str, name: str, original_source: str):
        self.source_id = source_id
        self.name = name
        # Per-camera calibration: focal length (px), mount height (m),
        # horizon fraction, orientation (forward/rear/side), and the
        # camera-to-body-edge offset along the optical axis. Resolved once
        # at slot construction from per-slot defaults + per-slot env
        # overrides (``ROAD_CAMERA_<FIELD>__<SLOT_ID>``). Frozen for the
        # lifetime of the slot so threads can read it without locks.
        # Threading the same calibration through every distance/TTC call
        # in this slot's perception loop is what makes a multi-camera
        # install (front 1× + rear 0.5× + left 0.5×) report accurate
        # distances per camera instead of pretending all three share the
        # front cam's intrinsics.
        self.calibration = camera_calibration_for(source_id)
        # Operator-supplied source URL. This is what the reader opens.
        self.original_source = original_source
        # UI-facing mode tag: "dashcam_file" (looping demo MP4),
        # "live_hls", "webcam", "unknown". Drives the badge on the admin
        # grid tile and whether the reader loops on EOF.
        self.stream_type = classify_source(original_source)
        # ``None`` until the slot has been started at least once.
        self.reader: StreamReader | None = None
        # Most recent reason a start attempt failed (e.g. network failure,
        # bad URL). Cleared on successful start. Surfaced over the API so
        # operators see *why* a stream is offline.
        self.last_error: str | None = None
        # Operator-controlled detection toggle. When False, ``_on_frame``
        # still renders the raw frame to the slot's MJPEG buffer (so the
        # operator can keep watching the camera) but skips YOLO,
        # quality / scene / ego updates, and event emission entirely. The
        # CPU saving is large; the trade-off is no boxes / no alerts from
        # this slot until re-enabled.
        self.detection_enabled: bool = True
        # Per-source perception estimators — fresh state machine per camera.
        self.track_history = TrackHistory()
        self.episodes: dict[tuple[int, int], Episode] = {}
        self.pair_cooldown: dict[tuple[int, int], float] = {}
        self.quality = QualityMonitor()
        self.last_perception_state: str | None = None
        # Thread the slot's camera calibration through the ego-motion estimator
        # so the pinhole speed-proxy uses the *correct* focal length + mount
        # height per slot (front 600px / 1.25m, rear 260px / 1.10m, side 260px /
        # 1.00m). Also lets the estimator emit a signed ``direction`` label
        # which the orientation policy consumes to gate rear-cam events on
        # "ego reversing".
        self.ego = EgoMotionEstimator(calibration=self.calibration)
        self.scene = SceneContextClassifier()
        self.last_ego_flow = None
        self.last_scene_ctx = None
        # Per-source MJPEG buffer. The capture thread writes; HTTP handlers
        # read. A dedicated lock per slot avoids cross-source contention.
        self._frame_lock = threading.Lock()
        self._annotated_jpeg: bytes | None = None
        self._frame_detections: list[dict] = []
        self._frame_ts: float = 0.0
        # Most-recent raw frame (BGR ndarray) captured for this slot. Used by
        # the polling endpoint as a fallback when ``_annotated_jpeg`` hasn't
        # been populated yet (e.g. first poll after stream start, or first
        # frame after a viewer-cycle). Storing the reference is O(1); we copy
        # only when we actually need to encode.
        self._latest_raw_frame = None
        # Active MJPEG viewer count. Incremented by ``_mjpeg_response`` on
        # connect and decremented in its ``finally`` block on disconnect.
        # When zero, ``_on_frame`` skips ``_render_annotated_frame`` /
        # ``cv2.imencode`` — the biggest per-frame cost after YOLO itself.
        self._mjpeg_subscribers: int = 0
        # Monotonic timestamp of the most recent poll to ``/admin/frame/{id}``.
        # The admin grid uses short-lived polls instead of a persistent MJPEG
        # connection (to dodge the browser's 6-conn-per-host cap), so viewer
        # presence has to be inferred from recent polls with a TTL.
        self._last_poll_monotonic: float = 0.0

    def has_viewers(self) -> bool:
        # Int read is atomic in CPython; a one-frame stale value is harmless
        # (at worst we skip one encode the frame a viewer connects on).
        if self._mjpeg_subscribers > 0:
            return True
        # Poll-based viewer: any /admin/frame hit in the last 2s counts. The
        # grid polls at ~400ms, so 2s = 5 polls of slack before we let the
        # encode path go idle (which matters on multi-stream hosts).
        return (time.monotonic() - self._last_poll_monotonic) < 2.0

    def mark_polled(self) -> None:
        self._last_poll_monotonic = time.monotonic()

    def _acquire_viewer(self) -> None:
        with self._frame_lock:
            self._mjpeg_subscribers += 1

    def _release_viewer(self) -> None:
        with self._frame_lock:
            self._mjpeg_subscribers = max(0, self._mjpeg_subscribers - 1)
            # Intentionally do NOT reset ``_annotated_jpeg`` here. Two reasons:
            #   1. The polling endpoint (``/admin/frame/{id}``) is a separate
            #      viewer path that doesn't increment ``_mjpeg_subscribers``;
            #      dropping the cached frame here strands every poll-based
            #      tile on the "WARMING UP" placeholder until the next
            #      perception tick (~0.5s) produces a fresh encode. With 6
            #      streams contending for the shared YOLO model the actual
            #      encode rate is closer to 0.5fps per slot, so the gap is
            #      visibly long.
            #   2. A stale-but-recent frame is strictly better UX than the
            #      placeholder JPEG. The next encode overwrites it anyway.

    def is_running(self) -> bool:
        # A paused reader is still "alive" (its capture thread is looping on
        # the pause gate) but from the UI's perspective it is not running —
        # frames are frozen, detection is off. Returning False while paused
        # keeps the Start/Pause toggle in sync with what the operator sees.
        return (
            self.reader is not None
            and self.reader._thread is not None
            and self.reader._thread.is_alive()
            and not self.reader.is_paused()
        )

    def status_dict(self) -> dict:
        """Public snapshot for ``/api/live/sources``."""
        q = self.quality.state()
        r = self.reader
        # Playback position: populated for looped local-file sources so the
        # frontend map overlay can sync its GPS marker to the MP4 loop. For
        # live feeds both numbers are 0.0 and the frontend falls back to a
        # wallclock loop.
        pos_sec, duration_sec = (
            r.playback_position() if r is not None else (0.0, 0.0)
        )
        return {
            "id": self.source_id,
            "name": self.name,
            "url": self.original_source,
            "stream_type": self.stream_type,
            "running": self.is_running(),
            "detection_enabled": self.detection_enabled,
            "last_error": self.last_error,
            "frames_read": r.frames_read if r else 0,
            "frames_processed": r.frames_processed if r else 0,
            "uptime_sec": round(r.uptime_sec(), 1) if r else 0.0,
            "playback_pos_sec": round(pos_sec, 2),
            "playback_duration_sec": round(duration_sec, 2),
            "started_at": r.started_at if r else None,
            "active_episodes": len(self.episodes),
            "perception_state": q.get("state"),
            "perception_reason": q.get("reason"),
        }


class LiveState:
    """Process-wide in-memory state for the live safety pipeline.

    Holds the loaded YOLO model, the asyncio loop, every shared aggregator
    (recent events, SSE subscribers, drift monitor, edge publisher), and a
    registry of per-source ``StreamSlot``s.

    Backwards-compatibility: legacy code paths read fields like
    ``state.reader``, ``state.quality``, ``state.scene``, ``state.episodes``
    that used to be single-source. Those are now ``@property``s that
    delegate to the *primary* slot. Any new code should use
    ``state.slots[source_id]`` explicitly.

    Lifecycle:
        * Constructed at module import time with an empty primary slot
          (no reader yet) so attribute access during import doesn't NPE.
        * ``lifespan`` populates ``loop``, ``model``, builds the
          configured slots, starts each reader.
        * On shutdown, ``lifespan`` cancels background tasks and stops
          every slot's reader.
    """

    PRIMARY_ID = "primary"

    def __init__(self):
        self.model = None
        self.source_label: str = DEFAULT_SOURCE
        self.loop: asyncio.AbstractEventLoop | None = None
        self.recent_events: list[dict] = []
        self.subscribers: set[asyncio.Queue] = set()
        self.event_counter = 0
        self.drift = DriftMonitor()
        self.active_learner = ActiveLearningSampler()
        self.edge_publisher = EdgePublisher()
        self.agent_executor: AgentExecutor | None = None
        self.admin_detection_subscribers: set[asyncio.Queue] = set()
        # Background validator (dual-model shadow detector). Populated in
        # ``lifespan`` when ``VALIDATOR_ENABLED`` is true; ``None`` in dev
        # and single-model deployments.
        self.validator: "ValidatorWorker | None" = None
        # Source registry. Always contains at least the primary slot
        # (created here so legacy ``state.X`` proxies have a target to
        # delegate to). ``lifespan`` may rename / replace it once it
        # reads the configured sources.
        self.slots: dict[str, StreamSlot] = {
            self.PRIMARY_ID: StreamSlot(self.PRIMARY_ID, "Primary", DEFAULT_SOURCE),
        }

    # ----- Per-slot proxies (legacy single-source accessors) -----
    # Every read here delegates to the primary slot. Writes that mutate
    # objects in place (``state.episodes[key] = ...``) work because the
    # property returns the live dict reference.
    @property
    def primary_slot(self) -> StreamSlot:
        slot = self.slots.get(self.PRIMARY_ID)
        if slot is not None:
            return slot
        if self.slots:
            # ``primary`` was removed but other slots remain — pick any so
            # legacy ``state.X`` access has a target.
            return next(iter(self.slots.values()))
        # Registry is empty (operator removed every slot). Re-create an
        # empty placeholder so legacy property accesses keep working.
        placeholder = StreamSlot(self.PRIMARY_ID, "Primary", DEFAULT_SOURCE)
        self.slots[self.PRIMARY_ID] = placeholder
        return placeholder

    @property
    def reader(self) -> StreamReader | None:
        return self.primary_slot.reader

    @reader.setter
    def reader(self, v):
        self.primary_slot.reader = v

    @property
    def track_history(self):
        return self.primary_slot.track_history

    @property
    def episodes(self):
        return self.primary_slot.episodes

    @property
    def pair_cooldown(self):
        return self.primary_slot.pair_cooldown

    @property
    def quality(self):
        return self.primary_slot.quality

    @property
    def last_perception_state(self):
        return self.primary_slot.last_perception_state

    @last_perception_state.setter
    def last_perception_state(self, v):
        self.primary_slot.last_perception_state = v

    @property
    def ego(self):
        return self.primary_slot.ego

    @property
    def scene(self):
        return self.primary_slot.scene

    @property
    def last_ego_flow(self):
        return self.primary_slot.last_ego_flow

    @last_ego_flow.setter
    def last_ego_flow(self, v):
        self.primary_slot.last_ego_flow = v

    @property
    def last_scene_ctx(self):
        return self.primary_slot.last_scene_ctx

    @last_scene_ctx.setter
    def last_scene_ctx(self, v):
        self.primary_slot.last_scene_ctx = v

    @property
    def _frame_lock(self):
        return self.primary_slot._frame_lock

    @property
    def _annotated_jpeg(self):
        return self.primary_slot._annotated_jpeg

    @_annotated_jpeg.setter
    def _annotated_jpeg(self, v):
        self.primary_slot._annotated_jpeg = v

    @property
    def _frame_detections(self):
        return self.primary_slot._frame_detections

    @_frame_detections.setter
    def _frame_detections(self, v):
        self.primary_slot._frame_detections = v

    @property
    def _frame_ts(self):
        return self.primary_slot._frame_ts

    @_frame_ts.setter
    def _frame_ts(self, v):
        self.primary_slot._frame_ts = v


# Module-level singleton. Import-time construction is safe because
# ``LiveState.__init__`` only builds default-constructed helpers.
state = LiveState()


# ===== SECTION: HELPERS (auth, thumb signing, utility coroutines) =====


def _require_admin(request: Request, realm: str = "admin") -> None:
    """Enforce the admin-bearer auth tier or raise 401.

    Wraps ``security.require_bearer_token`` with this module's constant
    admin token. Called at the top of every admin-tier endpoint.

    Args:
        request: The FastAPI request object (carries the Authorization header).
        realm: Human-readable label included in the 401 response so the
            UI can explain which scope was denied.

    Raises:
        HTTPException: 401 Unauthorized if the bearer token is missing
            or incorrect.
    """
    require_bearer_token(
        request,
        ADMIN_TOKEN,
        realm=realm,
        env_var="ROAD_ADMIN_TOKEN",
    )


async def _none_coro():
    """Trivial coroutine that yields ``None``.

    Used as a sentinel "no enrichment to run" task so the ``asyncio.gather``
    in ``_emit_event`` always has two awaitables regardless of whether we
    actually called the LLM enrichment path.
    """
    return None


def _thumb_token(name: str, expiry: int) -> str:
    """Produce a 32-hex-char HMAC tag binding ``name`` to an expiry time.

    Args:
        name: Thumbnail filename (e.g. ``evt_1234_0001_public.jpg``).
        expiry: Unix-epoch second at which this token stops being valid.

    Returns:
        The first 32 hex characters of the SHA-256 HMAC. 128 bits is more
        than enough entropy for a short-lived signed URL.
    """
    mac = hmac.new(
        THUMB_SIGNING_SECRET.encode("utf-8"),
        f"{name}.{expiry}".encode("utf-8"),
        hashlib.sha256,
    )
    return mac.hexdigest()[:32]


def _valid_thumb_request(name: str, request: Request) -> bool:
    """Check whether the signed-URL query params on a public-thumb fetch are valid.

    Args:
        name: Thumbnail filename from the URL path.
        request: The FastAPI request (to read ``?exp=`` + ``?token=``).

    Returns:
        True iff:
          * Token-gating is disabled entirely via config, OR
          * A signing secret is configured, AND the request carries
            ``exp`` + ``token`` query params, AND ``exp`` is in the
            future (but not more than 24h ahead), AND the HMAC matches.
    """
    if not PUBLIC_THUMBS_REQUIRE_TOKEN:
        return True
    if not THUMB_SIGNING_SECRET:
        return False
    exp_raw = request.query_params.get("exp")
    token = (request.query_params.get("token") or "").strip()
    if not exp_raw or not token:
        return False
    try:
        exp = int(exp_raw)
    except ValueError:
        return False
    now = int(time.time())
    if exp < now:
        return False
    # Reject far-future signatures in case of leaked URLs.
    # WHY 24h: a leaked URL with a 30-day expiry is a de facto permanent
    # bypass; capping exposure at ~1 day limits blast radius.
    if exp > now + (24 * 60 * 60):
        return False
    expected = _thumb_token(name, exp)
    # ``hmac.compare_digest`` is constant-time — prevents timing-oracle
    # attacks that would otherwise leak the correct token byte-by-byte.
    return hmac.compare_digest(expected, token)


async def _score_decay_loop(interval_sec: int) -> None:
    """Long-running background task: periodically decay driver safety scores.

    The driver-score model in ``services/registry.py`` decays over time so
    yesterday's bad trip doesn't permanently dominate today's score. This
    loop triggers that decay on a fixed cadence.

    Args:
        interval_sec: Seconds between decay passes. Sourced from
            ``ROAD_SCORE_DECAY_INTERVAL_SEC``; 0 disables the loop
            entirely (handled by the caller in ``lifespan``).

    Raises:
        asyncio.CancelledError: Re-raised on shutdown so the task cleanly
            terminates when ``lifespan`` cancels it.
    """
    while True:
        try:
            await asyncio.sleep(interval_sec)
            road_registry.decay_scores()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Narrow log-and-continue: one failed decay pass shouldn't
            # take the whole loop down — the next cycle retries cleanly.
            log.warning("score decay loop failed: %s", exc)


def _pair_key(event_type: str, a, b) -> tuple | None:
    """Canonical pair key for an interaction. Returns None if either side has
    no track_id — in which case we fall back to type-level dedup.

    Args:
        event_type: The interaction category (e.g. ``"pedestrian_proximity"``).
        a, b: The two ``Detection`` objects in the interaction.

    Returns:
        ``(event_type, lo_track_id, hi_track_id)`` with the track-ids
        sorted so (A, B) and (B, A) map to the same episode. Returns
        ``None`` when either detection lacks a track id (object only
        appeared in a single frame) — caller falls back to a time-bucket
        key so we still dedup across a short window.
    """
    if a.track_id is None or b.track_id is None:
        return None
    lo, hi = sorted((a.track_id, b.track_id))
    return (event_type, lo, hi)


def _classify_with_scene(
    ttc_sec,
    distance_m,
    fallback_px,
    thr,
    ego_speed_mps: float | None = None,
    any_track_approaching: bool = False,
) -> str:
    """Scene-adaptive risk classification with low-speed floor.

    Priority: TTC > distance > pixels. Highway widens TTC (more reaction
    time at speed); parking tightens it (close-quarters, slow).

    Low-speed floor: when ego is essentially stationary AND no track is
    actively approaching, risk is capped at 'medium'. Close-quarters
    proximity in stopped traffic is normal, not a conflict. A genuine
    approach by another moving object still upgrades the risk via
    `any_track_approaching`.

    Args:
        ttc_sec: Time-to-collision in seconds, or ``None`` if unknown.
        distance_m: 3D distance in metres, or ``None``.
        fallback_px: 2D pixel distance — used only when both of the
            above are unknown, as a last-resort proxy.
        thr: ``AdaptiveThresholds`` dataclass (scene-adapted: different
            numbers for urban vs highway vs parking).
        ego_speed_mps: Optical-flow-derived ego speed proxy, or ``None``
            when confidence is too low to trust.
        any_track_approaching: True if at least one track shows a
            positive ego-relative approach residual. Required to lift
            the low-speed floor.

    Returns:
        ``"low"`` / ``"medium"`` / ``"high"``. Never returns "unknown" —
        when all inputs are missing, defaults to "low".
    """
    levels = []
    if ttc_sec is not None:
        if ttc_sec <= thr.ttc_high_sec:
            levels.append("high")
        elif ttc_sec <= thr.ttc_med_sec:
            levels.append("medium")
    if distance_m is not None:
        if distance_m <= thr.dist_high_m:
            levels.append("high")
        elif distance_m <= thr.dist_med_m:
            levels.append("medium")
    if ttc_sec is None and distance_m is None:
        # Pixel fallback thresholds (60 / 180 px) are deliberately
        # conservative — only used when every other signal is missing.
        # They exist so a naive integration with no depth estimate at
        # all still produces something rather than silently swallowing
        # everything.
        if fallback_px <= 60:
            levels.append("high")
        elif fallback_px <= 180:
            levels.append("medium")

    # Highest tier wins across all priority levels.
    risk = "low"
    if "high" in levels:
        risk = "high"
    elif "medium" in levels:
        risk = "medium"

    # Speed-aware floor: in low-speed regimes (red light, traffic jam,
    # parking), close-quarters proximity is normal. Cap at medium unless
    # there is independent evidence of approach (ego-motion residual).
    # WHY this gate matters: without it, any stopped-at-a-light event with
    # a car within 2m was firing "high" — the single biggest source of
    # alert fatigue in early field tests.
    if (
        risk == "high"
        and ego_speed_mps is not None
        and ego_speed_mps < LOW_SPEED_FLOOR_MPS
        and not any_track_approaching
    ):
        return "medium"
    return risk


def _render_annotated_frame(frame, detections, interactions, distances_m=None):
    """Draw bounding boxes and labels on a copy of the frame for the admin feed.

    This is a purely visual helper — the output is fed only to the MJPEG
    admin video feed, not to any compliance-sensitive channel. It operates
    on a COPY so the perception pipeline's shared frame is not mutated.

    Args:
        frame: Raw BGR numpy image.
        detections: Iterable of ``Detection`` dataclasses (with ``.x1/.y1
            /.x2/.y2``, ``.cls``, ``.conf``, ``.track_id``, ``.center``).
        interactions: Iterable of ``(event_type, det_a, det_b, dist_px)``
            tuples — render as coloured connecting lines between pairs.
        distances_m: Optional list of ego→object distance estimates (metres)
            aligned 1:1 with ``detections``. ``None`` entries are skipped;
            non-None entries are appended to each bbox label as ``"12.3 m"``
            so the operator sees how far every obstacle is from the ego car.

    Returns:
        JPEG-encoded bytes (quality 70 — small enough for MJPEG but
        readable for operators).
    """
    vis = frame.copy()
    # BGR colour map (OpenCV uses BGR, not RGB). Anything unrecognised
    # falls back to neutral grey.
    color_map = {
        "person": (0, 220, 100),
        "car": (255, 160, 0),
        "truck": (255, 100, 0),
        "bus": (200, 80, 200),
        "motorcycle": (0, 180, 255),
    }
    for idx, det in enumerate(detections):
        color = color_map.get(det.cls, (200, 200, 200))
        cv2.rectangle(vis, (det.x1, det.y1), (det.x2, det.y2), color, 2)
        label = f"{det.cls} {det.conf:.0%}"
        if det.track_id is not None:
            label = f"#{det.track_id} {label}"
        if distances_m is not None and idx < len(distances_m):
            d = distances_m[idx]
            if d is not None:
                label = f"{label}  {d:.1f} m"
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
        cv2.rectangle(vis, (det.x1, det.y1 - th - 6), (det.x1 + tw + 4, det.y1), color, -1)
        cv2.putText(vis, label, (det.x1 + 2, det.y1 - 4),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1, cv2.LINE_AA)

    for event_type, a, b, dist_px in interactions:
        cx_a, cy_a = int(a.center[0]), int(a.center[1])
        cx_b, cy_b = int(b.center[0]), int(b.center[1])
        line_color = (0, 0, 255) if event_type == "pedestrian_proximity" else (0, 165, 255)
        cv2.line(vis, (cx_a, cy_a), (cx_b, cy_b), line_color, 2, cv2.LINE_AA)
        mid_x, mid_y = (cx_a + cx_b) // 2, (cy_a + cy_b) // 2
        cv2.putText(vis, f"{int(dist_px)}px", (mid_x + 4, mid_y - 4),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, line_color, 1, cv2.LINE_AA)

    _, jpeg = cv2.imencode(".jpg", vis, [cv2.IMWRITE_JPEG_QUALITY, 70])
    return jpeg.tobytes()


# ===== SECTION: ANNOTATED CLIP RENDERER =====
# Used by the ``/api/events/{id}/clip`` endpoint to build a recognition-
# overlay clip on demand: re-decodes the source MP4 in the event's
# ±N-second window, runs YOLO on each (sampled) frame, burns class-
# coloured boxes in cv2, and pipes the BGR frames into ffmpeg for h264
# encoding. Heavy on first render, then cached on disk.

# Lazy second YOLO instance dedicated to clip annotation. We can't
# reuse ``state.model`` because the live ``_on_frame`` thread calls
# ``model.track(frame, persist=True, ...)``: ByteTrack stores hidden
# state on the model and concurrent inference from this thread would
# corrupt the live tracker IDs (and worst case crash on shared tensor
# buffers). A separate instance is the simple, correct fix.
_clip_model_lock = threading.Lock()
_clip_model = None  # type: ignore[var-annotated]


def _get_clip_model():
    """Lazy-load a clip-only YOLO instance under a lock.

    Returns the model; raises whatever ``load_model`` raises on failure
    (caller catches and falls back to raw ffmpeg cut).
    """
    global _clip_model
    with _clip_model_lock:
        if _clip_model is None:
            from road_safety.core.detection import load_model
            _clip_model = load_model()
        return _clip_model


# Class palette for the annotated clip — same colours as the live admin
# tile (see ``_render_annotated_frame.color_map`` above) so reviewers
# get visual continuity between the two surfaces.
_CLIP_COLOR_MAP: dict[str, tuple[int, int, int]] = {
    "person": (0, 220, 100),
    "car": (255, 160, 0),
    "truck": (255, 100, 0),
    "bus": (200, 80, 200),
    "motorcycle": (0, 180, 255),
    "bicycle": (0, 180, 255),
}


def _draw_clip_overlay(frame, detections):
    """Draw class-coloured bboxes + labels on a copy of ``frame``.

    Args:
        frame: BGR ndarray.
        detections: iterable of ``Detection`` objects.

    Returns:
        BGR ndarray of the same shape with overlays burned in.
    """
    out = frame.copy()
    for det in detections:
        color = _CLIP_COLOR_MAP.get(det.cls, (200, 200, 200))
        cv2.rectangle(out, (det.x1, det.y1), (det.x2, det.y2), color, 2)
        label = f"{det.cls} {det.conf:.0%}"
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
        cv2.rectangle(
            out, (det.x1, det.y1 - th - 6), (det.x1 + tw + 4, det.y1), color, -1,
        )
        cv2.putText(
            out, label, (det.x1 + 2, det.y1 - 4),
            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1, cv2.LINE_AA,
        )
    return out


def _render_annotated_event_clip(
    source_path: Path,
    start_sec: float,
    duration_sec: float,
    out_path: Path,
) -> None:
    """Render an annotated MP4 from ``source_path`` over the given window.

    Decodes frames with cv2, runs YOLO at a throttled cadence (re-using
    the previous detection between detect ticks so playback stays smooth
    without paying full per-frame inference cost), burns class-coloured
    bboxes, and pipes raw BGR frames to ``ffmpeg`` for h264+faststart
    encoding. Output is written atomically: a sibling ``.tmp`` file is
    fully encoded then renamed to ``out_path``, so a partially-written
    clip never gets cached after a crash mid-render.

    Args:
        source_path: Local MP4 to read.
        start_sec: Seek offset in seconds.
        duration_sec: Clip length in seconds.
        out_path: Destination MP4. Parent must exist.

    Raises:
        FileNotFoundError: ``ffmpeg`` binary missing.
        RuntimeError: source could not be opened, or ffmpeg returned
            non-zero (caller falls back to raw cut).
        subprocess.TimeoutExpired: ffmpeg encode exceeded the 60s budget.
    """
    import subprocess

    from road_safety.core.detection import detect_frame

    cap = cv2.VideoCapture(str(source_path))
    if not cap.isOpened():
        raise RuntimeError(f"could not open source video: {source_path}")
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    proc: "subprocess.Popen | None" = None
    try:
        src_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        if width <= 0 or height <= 0:
            raise RuntimeError("source video reported zero frame size")
        cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, start_sec) * 1000.0)

        # Cap output fps so a long clip-render time stays bounded. 12 fps
        # is smooth enough for review video and ~2-3x cheaper than the
        # typical 24-30 fps source.
        out_fps = min(src_fps, 12.0)
        # Run YOLO every Nth source frame and carry boxes forward in
        # between. ~4 fps detection cadence matches the live admin tile
        # (TARGET_FPS) so the on-screen overlay updates at the same
        # rhythm reviewers are used to from the live feed.
        detect_every = max(1, int(round(src_fps / 4.0)))
        # Frame-emit step — we sample every ``out_step``-th source frame
        # to map ``src_fps`` down to ``out_fps`` without re-encoding the
        # decoded stream twice.
        out_step = src_fps / out_fps if out_fps > 0 else 1.0
        end_sec = max(0.0, start_sec) + max(0.0, duration_sec)

        proc = subprocess.Popen(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "rawvideo", "-pix_fmt", "bgr24",
                "-s", f"{width}x{height}",
                "-r", f"{out_fps:.3f}",
                "-i", "pipe:",
                "-c:v", "libx264", "-preset", "veryfast",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                "-an",
                # Force MP4 muxer explicitly — ffmpeg can't infer format
                # from the ``.mp4.tmp`` suffix (it picks muxer by extension
                # and ``.tmp`` is unknown), so without ``-f mp4`` the encode
                # fails with "Invalid argument" before a single frame lands.
                "-f", "mp4",
                str(tmp_path),
            ],
            stdin=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert proc.stdin is not None  # mypy/runtime guard

        model = _get_clip_model()
        last_dets: list = []
        frame_idx = 0
        next_emit = 0.0
        emitted = 0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            pos_sec = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
            if pos_sec >= end_sec:
                break
            if frame_idx % detect_every == 0:
                try:
                    last_dets = detect_frame(model, frame, persistent=False)
                except Exception as exc:  # noqa: BLE001
                    # Detection failure on one frame must not abort the
                    # clip — keep the previous boxes (or none) so the
                    # reviewer at least gets the source pixels.
                    log.debug("clip annotator: detect_frame failed: %s", exc)
            if frame_idx >= next_emit:
                annotated = _draw_clip_overlay(frame, last_dets)
                try:
                    proc.stdin.write(annotated.tobytes())
                except BrokenPipeError:
                    # ffmpeg died mid-encode — surface as RuntimeError so
                    # the route handler falls back to the raw ffmpeg cut.
                    break
                emitted += 1
                next_emit += out_step
            frame_idx += 1

        proc.stdin.close()
        proc.wait(timeout=60)
        if proc.returncode != 0:
            err = b""
            if proc.stderr is not None:
                err = proc.stderr.read() or b""
            raise RuntimeError(
                f"ffmpeg returned {proc.returncode}: "
                f"{err.decode('utf-8', errors='replace')[-400:]}"
            )
        if emitted == 0:
            raise RuntimeError(
                "no frames emitted (event timestamp outside source duration?)"
            )
        # Atomic publish — readers either see the fully-encoded clip or
        # nothing at all. ``Path.replace`` is atomic on POSIX.
        tmp_path.replace(out_path)
    finally:
        cap.release()
        if proc is not None and proc.stdin is not None and not proc.stdin.closed:
            try:
                proc.stdin.close()
            except Exception:  # noqa: BLE001
                pass
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        # Clean up partial tmp on any error path so the next request
        # re-renders fresh instead of hitting a corrupt cache.
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


# ===== SECTION: PERCEPTION HOT PATH =====
# ``_on_frame`` is called by ``StreamReader`` in a background thread at
# approximately ``TARGET_FPS`` Hz (default 2). It must do ALL the CPU-bound
# perception work AND ensure results reach the asyncio loop without blocking
# it. Every safety-critical "gate" lives here — see the numbered list in
# the docstring below. If you touch this function, run ``tests/test_core.py``.


def _start_slot(slot: StreamSlot) -> None:
    """Resolve the slot's source URL and spawn its capture thread.

    Idempotent in spirit but assumes the caller has checked
    ``slot.is_running()`` and stopped any prior reader. On success,
    ``slot.reader`` is replaced with a freshly-started ``StreamReader``
    and ``slot.last_error`` is cleared.
    """
    if not slot.original_source:
        raise RuntimeError(f"slot {slot.source_id} has no source URL")
    hls = slot.original_source
    live_fps = float(SETTINGS_STORE.snapshot().get("TARGET_FPS", TARGET_FPS))
    # Local-file sources loop forever by default so the demo "fake dashcam"
    # MP4 replays end-to-end. Live URLs (HLS/RTSP) keep the legacy
    # "exit on EOF" behaviour — there is no EOF on a live feed anyway.
    should_loop = slot.stream_type == "dashcam_file"
    reader = StreamReader(
        hls,
        target_fps=live_fps,
        original_source=slot.original_source,
        loop=should_loop,
    )
    reader.start(_make_on_frame(slot))
    slot.reader = reader
    slot.last_error = None
    log.info(
        "slot %s started (source=%s, target_fps=%.1f)",
        slot.source_id,
        slot.original_source[:80],
        live_fps,
    )


def _stop_slot(slot: StreamSlot) -> None:
    """Stop the slot's capture thread without dropping the slot.

    The slot stays in ``state.slots`` so the operator can restart it
    later. Per-source perception state (quality / scene / episodes)
    is intentionally NOT reset — restarting the same camera shouldn't
    re-learn its scene from scratch.
    """
    r = slot.reader
    if r is not None:
        try:
            r.stop()
        except Exception as exc:
            log.warning("slot %s stop failed: %s", slot.source_id, exc)
    slot.reader = None


def _pause_slot(slot: StreamSlot) -> bool:
    """Freeze the slot's capture loop without tearing down the reader.

    Unlike :func:`_stop_slot` this keeps ``slot.reader`` attached and its
    capture thread alive — for a dashcam MP4 that means playback position
    survives across a Pause → Start cycle so the operator resumes exactly
    where they paused instead of replaying from frame 0.

    Returns True when a reader was actually paused, False if the slot had
    nothing alive to pause (caller can treat that as a no-op).
    """
    r = slot.reader
    if r is None or r._thread is None or not r._thread.is_alive():
        return False
    r.pause()
    return True


def _resume_slot(slot: StreamSlot) -> bool:
    """Reverse :func:`_pause_slot`. Returns True when a paused reader was resumed."""
    r = slot.reader
    if r is None or not r.is_paused():
        return False
    r.resume()
    return True


def _make_on_frame(slot: StreamSlot):
    """Return a thread-safe ``on_frame(wall_ts, frame)`` closure for ``slot``.

    Each StreamReader needs its own callback that knows which slot it
    belongs to. The closure binds ``slot`` so the per-source perception
    state (quality, scene, ego, episodes…) updates correctly without
    crossing wires between cameras.
    """

    def _cb(wall_ts: float, frame) -> None:
        _on_frame(slot, wall_ts, frame)

    return _cb


def _on_frame(slot: StreamSlot, wall_ts: float, frame) -> None:
    """Perception hot path — runs in the StreamReader background thread.

    CPU-bound YOLO + gate evaluation happens here. Results are handed to
    the asyncio loop via ``asyncio.run_coroutine_threadsafe``; we never
    block that loop with the heavy work.

    Args:
        slot: The ``StreamSlot`` whose reader produced this frame. All
            per-source perception state (quality, scene, ego, episodes,
            track history, frame buffer) is read/written via ``slot.X``.
        wall_ts: Wall-clock timestamp (``time.time()``) when the frame
            was captured by the stream reader.
        frame: Raw BGR numpy image. This buffer is shared with the reader —
            we must not hold references to it beyond the call; ``frame.copy()``
            when we need to stash a peak.

    GATE ORDER (DO NOT SHORT-CIRCUIT — each kills a specific FP class):

        1. ``detect_frame``            — YOLOv8 + ByteTrack tracked detection.
        2. ``QualityMonitor.observe``  — night/rain/glare/dirty-lens detector;
                                         yields multipliers that tighten
                                         thresholds when perception is poor.
        3. ``EgoMotionEstimator``      — optical-flow ego speed proxy.
        4. ``SceneContextClassifier``  — urban / highway / parking tag;
                                         produces ``AdaptiveThresholds`` so
                                         65mph highway doesn't reuse city
                                         numbers.
        5. ``TrackHistory.update``     — per-track trailing window for
                                         multi-frame TTC.
        6. ``find_interactions``       — candidate pairs (person-vehicle,
                                         vehicle-vehicle close, etc.).
        7. Depth gate (vehicle-vehicle) — reject pairs > VEHICLE_INTER_DISTANCE_GATE_M
                                         apart in 3D even if bboxes overlap.
        8. Convergence gate             — reject parallel / same-direction
                                         traffic (``tracks_converging``).
        9. Ego-relative motion gate     — require at least one track to be
                                         approaching in ego-relative space;
                                         otherwise TTC is bbox noise.
       10. Pair TTC (fallback: per-object) — SSAM-style closing-rate TTC.
       11. Approach-required TTC scrub — if no track is approaching, discard
                                         the TTC value (keep distance gates).
       12. Quality-adjusted classification — divide by QualityMonitor
                                         multipliers, classify with scene.
       13. Per-type floors             — pedestrian_proximity must clear "low".
       14. Cooldown check              — ``pair_cooldown`` mutes recently-
                                         emitted pairs until they clear.
       15. Episode open / update       — aggregate across frames.
       16. Idle-flush                  — emit ONE event when a pair hasn't
                                         been seen for ``EPISODE_IDLE_FLUSH_SEC``.
    """
    # Guard against callbacks firing before ``lifespan`` finished wiring
    # the model / loop. Skipping early is harmless — next frame retries.
    if state.model is None or state.loop is None:
        return

    # Stash the raw frame reference so the polling endpoint can encode it
    # on demand if no annotated JPEG is cached yet (e.g. very first poll
    # after stream start, or any time the encode path was skipped because
    # ``has_viewers()`` returned False). Cheap O(1) reference assignment;
    # we never copy on the hot path. The polling endpoint copies only when
    # it actually needs to encode.
    slot._latest_raw_frame = frame

    # ----- Detection-disabled bypass -----
    # Operator unchecked the "detection" toggle for this slot. We still
    # write the raw (un-annotated) JPEG to the MJPEG buffer so the
    # operator can keep watching the camera live, then return without
    # running YOLO, quality / scene / ego, or any event emission.
    # Skip the encode entirely when nobody is watching — with detection
    # off there is no SSE/event side-effect to preserve.
    if not slot.detection_enabled:
        if not slot.has_viewers():
            return
        try:
            ok, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
            if ok:
                with slot._frame_lock:
                    slot._annotated_jpeg = jpeg.tobytes()
                    slot._frame_detections = []
                    slot._frame_ts = wall_ts
        except Exception as exc:
            log.warning("raw frame encode failed (%s): %s", slot.source_id, exc)
        return

    # ----- Gate 1: tracked detection (YOLO + ByteTrack) -----
    detections = detect_frame(state.model, frame)
    frame_h = frame.shape[0]  # image height — needed by pinhole distance estimates.
    frame_w = frame.shape[1]  # image width — needed by orientation_policy BSW ROI.

    # ----- Shadow-mode validator tee (sampled) -----
    # Non-blocking fan-out to the background dual-model validator. Runs
    # at most once per ``VALIDATOR_SAMPLE_SEC`` per source. Heavy work
    # happens on the asyncio loop / worker thread; this call is O(1).
    if state.validator is not None and state.loop is not None:
        if state.validator.should_sample(slot.source_id, wall_ts):
            state.loop.call_soon_threadsafe(
                state.validator.enqueue,
                ValidatorJob(
                    kind="sampled",
                    slot_id=slot.source_id,
                    wall_ts=wall_ts,
                    frame=frame.copy(),
                    primary_detections=list(detections),
                    calibration=slot.calibration,
                ),
            )

    # ----- Gate 2: perception-quality observer -----
    # Feed every frame so degradations (night, rain, glare, dirty lens)
    # flip the pipeline into conservative mode. ``risk_adjustment`` returns
    # multipliers applied below; ``state()`` is a human-readable summary we
    # broadcast over SSE when it transitions.
    slot.quality.observe_frame(frame, detections, wall_ts)
    adj = slot.quality.risk_adjustment()
    qstate = slot.quality.state()
    if qstate["state"] != slot.last_perception_state and state.loop is not None:
        slot.last_perception_state = qstate["state"]
        # ``run_coroutine_threadsafe`` is the official bridge from a
        # worker thread to the asyncio loop. We schedule and forget —
        # the returned Future is not awaited here.
        asyncio.run_coroutine_threadsafe(_broadcast_perception(qstate, slot), state.loop)

    # ----- Gate 3: ego-motion estimation -----
    # Farneback dense optical flow on the masked background → ego flow vector
    # + speed proxy. Ego-motion lets downstream code tell "object approaching
    # me" apart from "I'm approaching a parked object." Also feeds scene
    # context. Wrapped in try/except because optical flow can fail on tiny /
    # degenerate frames and we don't want one bad frame to crash the thread.
    try:
        ego_flow = slot.ego.update(frame, detections, wall_ts)
    except Exception as exc:
        log.warning("ego-motion update failed (%s): %s", slot.source_id, exc)
        ego_flow = None
    slot.last_ego_flow = ego_flow

    # ----- Gate 4: scene context + adaptive thresholds -----
    # Classify urban / highway / parking from rolling detection density +
    # ego speed. Thresholds adapt per scene so 65mph highway doesn't reuse
    # city-street numbers.
    # WHY the 0.4 confidence floor: only feed the speed proxy in when ego-
    # flow confidence is high enough that the median flow is reliable.
    # Below this band (rain, wipers, low texture, pure rotation) we let the
    # classifier fall back to detection-density rules rather than driving
    # adaptive thresholds off a noisy speed estimate — that's how "highway"
    # mistakenly fires in parking lots with reflective floors.
    if ego_flow is not None and ego_flow.confidence >= 0.4:
        speed_proxy = ego_flow.speed_proxy_mps
    else:
        speed_proxy = None
    slot.scene.observe(detections, wall_ts, speed_proxy_mps=speed_proxy)
    scene_ctx = slot.scene.classify()
    slot.last_scene_ctx = scene_ctx
    thr = slot.scene.adaptive_thresholds(scene_ctx)

    # ----- Gate 5: update per-track history -----
    # ``live_ids`` is the set of tracks still present THIS frame; ``prune``
    # evicts older ones from the rolling history so memory doesn't grow
    # unbounded as tracks come and go.
    live_ids: set[int] = set()
    for det in detections:
        if det.track_id is not None:
            live_ids.add(det.track_id)
            slot.track_history.update(det, wall_ts)
    slot.track_history.prune(live_ids, wall_ts)

    # ----- Gate 6: candidate interaction generation -----
    interactions = find_interactions(detections)
    # Track which pair keys we've seen this frame so the idle-flush below
    # can correctly identify "absent for long enough to close" pairs.
    seen_pairs_this_frame: set[tuple] = set()

    for event_type, a, b, distance_px in interactions:
        # Pull the trailing-window samples for both tracks — needed for
        # TTC and convergence checks. May be empty for brand-new tracks.
        hist_a = slot.track_history.samples(a.track_id)
        hist_b = slot.track_history.samples(b.track_id)

        # Inter-object distance (depth difference + lateral offset), not
        # single-object range-to-camera. Fall back to per-object range if
        # the pair-wise estimator can't produce a value. Both calls thread
        # ``slot.calibration`` so multi-camera installs use the right
        # focal/height/horizon/offset per camera (front 1× vs rear/side
        # 0.5× ultra-wide etc.).
        dist_m = estimate_inter_distance_m(a, b, frame_h, calibration=slot.calibration)
        if dist_m is None:
            cal = slot.calibration
            skip_ground = cal.orientation == "side"
            for sub in (a, b):
                cand = estimate_distance_m(
                    sub, frame_h,
                    focal_px=cal.focal_px,
                    height_m=cal.height_m,
                    horizon_frac=cal.horizon_frac,
                    offset_m=cal.bumper_offset_m,
                    skip_ground_plane=skip_ground,
                )
                if cand is not None and (dist_m is None or cand < dist_m):
                    dist_m = cand

        # ----- Gate 7 + 8: depth + convergence (vehicle-vehicle only) -----
        # Depth-aware proximity for vehicle-vehicle pairs. Two cars more than
        # VEHICLE_INTER_DISTANCE_GATE_M apart in 3D are not a close interaction
        # even when their bboxes overlap in the image plane — perspective
        # overlap of distant objects is not collision risk.
        if event_type == "vehicle_close_interaction":
            if dist_m is not None and dist_m > VEHICLE_INTER_DISTANCE_GATE_M:
                continue
            # Convergence filter: reject parallel / same-direction traffic.
            # Two cars in adjacent lanes moving the same direction at the
            # same speed are not a conflict — only *converging* tracks are.
            if not tracks_converging(hist_a, hist_b):
                continue

        # ----- Gate 9: ego-relative motion -----
        # If neither track shows a positive approach residual against the
        # optical-flow ego-motion estimate, TTC from bbox noise alone is
        # not a conflict — discard it.
        approaching_a = approaching_b = False
        if ego_flow is not None:
            try:
                rm_a = slot.ego.relative_motion(a.track_id, a, ego_flow, slot.track_history)
                rm_b = slot.ego.relative_motion(b.track_id, b, ego_flow, slot.track_history)
                # ``bool(rm and rm.approaching)`` handles both None and False
                # uniformly — we need a strict bool for the OR below.
                approaching_a = bool(rm_a and rm_a.approaching)
                approaching_b = bool(rm_b and rm_b.approaching)
            except Exception as exc:
                log.debug("relative_motion failed: %s", exc)
        any_approaching = approaching_a or approaching_b

        # ----- Gate 10: pair TTC (fallback: per-object) -----
        # Pair-wise TTC from closing rate between the two tracks (SSAM method).
        # Falls back to per-object scale-expansion TTC if pair TTC unavailable.
        # We keep the MINIMUM (most urgent) across the two fallbacks.
        ttc = estimate_pair_ttc(hist_a, hist_b)
        if ttc is None:
            for sub in (a, b):
                hist = slot.track_history.samples(sub.track_id)
                cand = estimate_ttc_sec(hist)
                if cand is not None and (ttc is None or cand < ttc):
                    ttc = cand

        # ----- Gate 11: approach-required TTC scrub -----
        # If a TTC value passed the upstream gates but neither track shows
        # an approach residual, treat it as unreliable and discard. Distance
        # and edge-pixel gates still apply.
        if ttc is not None and ego_flow is not None and not any_approaching:
            ttc = None

        # ----- Gate 12: quality-adjusted classification -----
        # Divide effective TTC / px to tighten thresholds when perception is
        # degraded (earlier triggers, more cautious). ``adj`` multipliers
        # come from ``QualityMonitor``; they are ``1.0`` in nominal state.
        eff_ttc = ttc / adj["ttc_multiplier"] if ttc is not None else None
        eff_px = distance_px / adj["pixel_dist_multiplier"]
        ego_speed_mps = ego_flow.speed_proxy_mps if ego_flow is not None else None
        risk = _classify_with_scene(
            eff_ttc, dist_m, eff_px, thr,
            ego_speed_mps=ego_speed_mps,
            any_track_approaching=any_approaching,
        )
        # ----- Gate 13: per-type floor -----
        # Pedestrians deserve tighter attention, but a low-risk person on
        # the other side of the street is not worth an event. Require at
        # least medium for this type before we even consider emitting.
        if event_type == "pedestrian_proximity" and risk == "low":
            continue

        # ----- Gate 13.5: orientation policy (SAE J3063) -----
        # Dispatch the candidate through the per-camera-orientation gate.
        # Forward cams pass through (standard FCW). Rear cams require ego
        # to be reversing (ISO 22840) and pick RCW vs RCTA based on the
        # secondary track's lateral/longitudinal motion. Side cams require
        # blind-zone presence + dwell >= BSW_DWELL_SEC (ISO 17387).
        # Suppressed candidates never open an episode nor consume a
        # cooldown slot — they're simply invisible to the rest of the
        # pipeline, which eliminates the rear/side false-positive class.
        policy_decision = _orientation_classify(
            calibration=slot.calibration,
            event_type=event_type,
            primary=a,
            secondary=b,
            frame_w=frame_w,
            frame_h=frame_h,
            ego=ego_flow,
            track_history=slot.track_history,
        )
        if not policy_decision.emit:
            log.debug(
                "event suppressed by orientation policy: slot=%s type=%s reason=%s",
                slot.source_id, event_type, policy_decision.reason,
            )
            continue

        # ----- Gate 14: cooldown check -----
        key = _pair_key(event_type, a, b)
        if key is None:
            # Untracked fallback — synthesise a per-frame key so we still
            # dedup within a short window without a stable pair identity.
            # ``int(wall_ts)`` buckets to a 1s window.
            key = (event_type, "no_track", int(wall_ts))

        cooldown_until = slot.pair_cooldown.get(key, 0.0)
        if wall_ts < cooldown_until:
            continue

        # ----- Gate 15: episode open / update -----
        seen_pairs_this_frame.add(key)
        ep = slot.episodes.get(key)
        if ep is None:
            ep = Episode(event_type, key, wall_ts)
            # Stamp the orientation-policy decision onto the episode at open
            # time; downstream `_flush_episode` reads these to stamp the
            # SAE taxonomy + display type onto the final event payload.
            ep.camera_orientation = slot.calibration.orientation
            ep.event_taxonomy = policy_decision.taxonomy
            ep.display_event_type = policy_decision.display_event_type
            ep.policy_reason = policy_decision.reason
            slot.episodes[key] = ep
        ep.update(frame, detections, a, b, distance_px, ttc, dist_m, risk, wall_ts)

    # ----- Gate 16: idle-flush episodes -----
    # Iterate over a *snapshot* of keys because we mutate the dict below.
    # When a pair has gone EPISODE_IDLE_FLUSH_SEC without a fresh frame,
    # we emit the one peak event and start a cooldown window to prevent
    # re-alerting on the same objects if they re-appear shortly after.
    for key in list(slot.episodes.keys()):
        ep = slot.episodes[key]
        if key in seen_pairs_this_frame:
            continue
        if wall_ts - ep.last_seen_at >= EPISODE_IDLE_FLUSH_SEC:
            _flush_episode(slot, ep, wall_ts)
            slot.episodes.pop(key, None)
            slot.pair_cooldown[key] = wall_ts + PAIR_COOLDOWN_SEC

    # ----- Admin video feed + detections broadcast -----
    # Non-safety-critical visualization. Wrapped in try/except so a JPEG
    # encoding failure does not break the perception loop for the next frame.
    #
    # JPEG encode is the single most expensive step here after YOLO itself
    # (_render_annotated_frame does frame.copy + N overlays + cv2.imencode).
    # Skip it when no MJPEG client is attached to this slot — the SSE
    # detection-metadata broadcast below still runs so dashboard counters
    # keep updating even with no video viewers.
    try:
        has_viewers = slot.has_viewers()
        # Per-slot camera calibration (focal, height, horizon, offset_m,
        # axis). Accurate distance on a multi-camera install needs the
        # right focal for each lens (iPhone 1× ≈ 600 px, 0.5× ≈ 260 px)
        # and the right ``offset_m`` so the number we report is "metres
        # to my bumper", not "metres to the camera glass". ``axis`` is
        # semantic metadata: forward/rear → TTC is meaningful; lateral
        # → distance is a sideways proximity reading only.
        cam_cal = slot.calibration
        # Wire-format axis label for the SSE detection snapshot:
        #   - "range":   forward / rear cams — distance is longitudinal
        #                (down the direction of travel). TTC is meaningful.
        #   - "lateral": side-window cams — distance is sideways
        #                (adjacent-lane proximity). TTC is largely
        #                meaningless because lateral closing rate isn't
        #                "time to collision" in the dashcam sense.
        # The frontend uses this tag to label the chip ("range" vs
        # "lateral") so operators read the right semantic.
        cam_axis = "lateral" if cam_cal.orientation == "side" else "range"
        per_det_distances: list[float | None] = estimate_distances_batch(
            detections, frame_h, frame, calibration=cam_cal,
        )
        jpeg_bytes = (
            _render_annotated_frame(
                frame, detections, interactions, distances_m=per_det_distances
            )
            if has_viewers else None
        )
        det_snapshot = [
            {
                "cls": d.cls, "conf": round(d.conf, 3),
                "track_id": d.track_id,
                "bbox": [d.x1, d.y1, d.x2, d.y2],
                "distance_m": (
                    round(per_det_distances[i], 1)
                    if per_det_distances[i] is not None else None
                ),
                "distance_axis": cam_axis,
            }
            for i, d in enumerate(detections)
        ]
        with slot._frame_lock:
            if jpeg_bytes is not None:
                slot._annotated_jpeg = jpeg_bytes
            slot._frame_detections = det_snapshot
            slot._frame_ts = wall_ts

        if state.loop is not None:
            # Per-frame MP4 playhead in seconds. Sourced from
            # ``cv2.CAP_PROP_POS_MSEC`` for looped local-file sources; 0.0
            # for live feeds. Threading it through the per-frame SSE
            # message gives the frontend a tight, authoritative clock to
            # drive the map marker — when the video pauses, the value
            # stops advancing → the marker freezes; when the MP4 loops,
            # the value resets → the marker snaps back to the start of
            # the GPS track. This is what keeps map and video in lock-step
            # without a 5 s polling-interval drift.
            reader = slot.reader
            playback_pos_sec, playback_duration_sec = (
                reader.playback_position() if reader is not None else (0.0, 0.0)
            )
            msg = {
                "ts": round(wall_ts, 3),
                "source_id": slot.source_id,
                "source_name": slot.name,
                "detections": len(detections),
                "persons": sum(1 for d in detections if d.cls == "person"),
                "vehicles": sum(1 for d in detections if d.cls in VEHICLE_CLASSES),
                "interactions": len(interactions),
                "objects": det_snapshot,
                "playback_pos_sec": round(playback_pos_sec, 3),
                "playback_duration_sec": round(playback_duration_sec, 3),
            }
            asyncio.run_coroutine_threadsafe(
                _broadcast_admin_detections(msg), state.loop
            )
    except Exception as exc:
        log.warning("annotated frame failed (%s): %s", slot.source_id, exc)


# ===== SECTION: EVENT MATERIALIZATION (EPISODE -> TYPED EVENT DICT) =====


def _flush_episode(slot: StreamSlot, ep: Episode, wall_ts: float) -> None:
    """Materialise an episode's peak frame into an Event and hand off to the
    asyncio side for LLM enrichment + egress.

    The peak risk is downgraded by ``Episode.final_risk()`` if it lacks
    sustained support — a single high-risk frame in an otherwise calm
    episode is treated as a transient and emitted at the lower tier. This
    rejects single-frame TTC spikes from bbox jitter without losing the
    peak-frame thumbnail for review.

    Side effects:
        * Writes two JPEGs under ``THUMBS_DIR``:
            - ``<event_id>.jpg``        (internal, unredacted — DSAR-gated)
            - ``<event_id>_public.jpg`` (redacted: faces + plates blurred)
          Shared channels (SSE, Slack, cloud) reference ONLY the public copy.
        * Schedules ``_emit_event`` on the asyncio loop.
        * Sets ``ep.emitted = True`` so repeated flush calls are no-ops.

    Args:
        ep: The ``Episode`` being flushed.
        wall_ts: Current wall-clock timestamp (unused inside the body but
            kept for call-site symmetry with the idle-flush check).
    """
    if ep.emitted or ep.peak_frame is None:
        return
    ep.emitted = True

    # Sustained-risk downgrade — see Episode.final_risk().
    final_risk = ep.final_risk()
    risk_demoted = final_risk != ep.peak_risk

    # Event id format: ``evt_<ms-since-epoch>_<4-digit-counter>`` — roughly
    # sortable and globally unique per process. ``:04d`` is a format-spec
    # that zero-pads the integer to 4 digits.
    state.event_counter += 1
    event_id = f"evt_{int(ep.started_at * 1000)}_{state.event_counter:04d}"
    internal_name = f"{event_id}.jpg"
    public_name = public_thumbnail_name(internal_name)

    # PRIVACY INVARIANT: ``write_thumbnails`` is the ONLY place both JPEGs
    # are produced. The internal copy stays on disk behind DSAR-gating;
    # the public copy has faces + plates blurred. Shared channels MUST
    # only reference the public copy.
    THUMBS_DIR.mkdir(parents=True, exist_ok=True)
    write_thumbnails(
        ep.peak_frame,
        ep.peak_detections,
        ep.peak_primary,
        ep.peak_secondary,
        THUMBS_DIR / internal_name,
        THUMBS_DIR / public_name,
    )

    a, b = ep.peak_primary, ep.peak_secondary
    # ``stream_t`` is seconds-since-stream-start; handy for aligning an
    # event back to a recorded video file. Falls back to 0 when there is
    # no active reader (single-shot test mode).
    stream_t = ep.started_at - (slot.reader.started_at if slot.reader else ep.started_at)
    # Filter out None track ids (untracked fallback case).
    pair_ids = [tid for tid in (a.track_id, b.track_id) if tid is not None]
    duration_sec = round(ep.last_seen_at - ep.started_at, 2)

    scene_ctx = slot.last_scene_ctx
    ego = slot.last_ego_flow
    # ===== Typed event dict — the canonical wire format =====
    # Every field here is part of the public contract with downstream
    # consumers (dashboard, Slack, cloud). If you rename a field, grep
    # for it in frontend/ and cloud/ first.
    event = {
        "event_id": event_id,
        "source_id": slot.source_id,
        "source_name": slot.name,
        "vehicle_id": RESOLVED_VEHICLE_ID,
        "road_id": RESOLVED_ROAD_ID,
        "driver_id": RESOLVED_DRIVER_ID,
        # Egress-safe identifier: raw source is often an absolute file path
        # (``/Users/alice/…/Left Cam.mp4``) which would leak the operator's
        # home directory into the UI, cloud receiver, Slack, and audit logs.
        "video_id": display_video_id(slot.original_source or DEFAULT_SOURCE),
        "timestamp_sec": round(stream_t, 2),
        "wall_time": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        # Prefer the orientation-aware display type the policy provided
        # (e.g. "blind_spot_pedestrian" for a side-cam BSW), falling back
        # to the raw internal type from `find_interactions` for forward
        # cams where the policy opts out of relabelling.
        "event_type": ep.display_event_type or ep.event_type,
        "internal_event_type": ep.event_type,
        "risk_level": final_risk,
        "peak_risk_level": ep.peak_risk,
        "risk_demoted": risk_demoted,
        "risk_frame_counts": dict(ep.risk_frame_counts),
        "frame_count": ep.frame_count,
        "confidence": round(min(a.conf, b.conf), 3),
        "objects": sorted({a.cls, b.cls}),
        "track_ids": pair_ids,
        "episode_duration_sec": duration_sec,
        # SAE J3063 taxonomy + camera orientation. These let downstream
        # consumers (UI badges, LLM narration, Slack, cloud) render the
        # right label per orientation ("FCW" for forward, "BSW" for side,
        # "RCW"/"RCTA" for rear) instead of every event looking identical.
        "camera_orientation": ep.camera_orientation,
        "event_taxonomy": ep.event_taxonomy,
        "policy_reason": ep.policy_reason,
        "ttc_sec": ep.peak_ttc,
        "distance_m": ep.peak_distance_m,
        "distance_px": round(ep.peak_distance_px, 1),
        "scene_context": (
            {
                "label": scene_ctx.label,
                "confidence": round(scene_ctx.confidence, 2),
                "speed_proxy_mps": (
                    round(scene_ctx.speed_proxy_mps, 2)
                    if scene_ctx.speed_proxy_mps is not None else None
                ),
                "reason": scene_ctx.reason,
            }
            if scene_ctx is not None else None
        ),
        "ego_flow": (
            {
                "speed_proxy_mps": round(ego.speed_proxy_mps, 2),
                "confidence": round(ego.confidence, 2),
            }
            if ego is not None else None
        ),
        "summary": build_event_summary(
            ep.event_type, a, b, ep.peak_distance_px, final_risk,
            ttc_sec=ep.peak_ttc, distance_m=ep.peak_distance_m,
            camera_orientation=ep.camera_orientation,
            event_taxonomy=ep.event_taxonomy,
        ),
        "narration": None,
        # Egress-safe URL — internal unredacted copy is not served publicly.
        # Anyone rendering this URL gets the redacted thumbnail. DSAR
        # workflows must request the internal ``evt_*.jpg`` by name and
        # authenticate with ``X-DSAR-Token``.
        "thumbnail": f"thumbnails/{public_name}",
    }

    # Hand off to the asyncio loop — LLM enrichment + SSE broadcast + Slack
    # dispatch all happen there, not in this background thread.
    asyncio.run_coroutine_threadsafe(_emit_event(event, internal_name), state.loop)

    # ----- Validator deep re-check of the peak frame -----
    # Non-blocking hand-off to the shadow validator. It will re-run a
    # heavier detector on ``ep.peak_frame`` and emit watchdog findings
    # if the secondary disagrees (false positive, classification mismatch).
    if (
        state.validator is not None
        and state.loop is not None
        and ep.peak_frame is not None
    ):
        state.validator.mark_primary_event(slot.source_id, wall_ts)
        state.loop.call_soon_threadsafe(
            state.validator.enqueue,
            ValidatorJob(
                kind="episode",
                slot_id=slot.source_id,
                wall_ts=wall_ts,
                frame=ep.peak_frame,
                primary_detections=list(ep.peak_detections),
                primary_event=event,
                calibration=slot.calibration,
            ),
        )


# ===== SECTION: SSE BROADCAST HELPERS =====
# Each connected client has its own ``asyncio.Queue``. Broadcast = iterate
# the subscriber set and ``put_nowait`` on each queue. ``QueueFull`` is
# swallowed so a single slow consumer can't back-pressure the whole fan-out.


async def _broadcast_perception(qstate: dict, slot: "StreamSlot | None" = None) -> None:
    """Broadcast a perception-state change as a control-plane SSE message.

    Uses a sentinel ``_meta: "perception_state"`` so the UI can render a
    banner without confusing these with safety events.

    Args:
        qstate: A dict from ``QualityMonitor.state()`` describing the new
            perception state (nominal / degraded / blind, plus reason text).
        slot: Source the change came from. Tagged onto the message so the
            UI can attribute the banner to the right stream.
    """
    # ``**qstate`` unpacks the dict into kwargs at literal-construction
    # time — merges the ``_meta`` tag with the fields. Python's dict
    # unpacking syntax ``{**a, **b}`` is equivalent to ``a | b`` on 3.9+.
    msg = {"_meta": "perception_state", **qstate}
    if slot is not None:
        msg["source_id"] = slot.source_id
        msg["source_name"] = slot.name
    # ``list(state.subscribers)`` snapshots the set so a concurrent
    # disconnect can't mutate what we're iterating over.
    for q in list(state.subscribers):
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            # Dropping a message is preferable to blocking the broadcast
            # on one stuck subscriber. Client will resync on next event.
            pass


async def _broadcast_admin_detections(msg: dict) -> None:
    """Fan out a per-frame detection snapshot to admin-dashboard SSE clients.

    Args:
        msg: Pre-serialised snapshot built in ``_on_frame`` — counts plus
            the list of object boxes for the current frame.
    """
    for q in list(state.admin_detection_subscribers):
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            pass


# ===== SECTION: ASYNC EVENT EMISSION (ENRICH + BROADCAST + EGRESS) =====


async def _emit_event(event: dict, internal_thumb_name: str) -> None:
    """Runs on the main asyncio loop. Narrates + enriches (parallel), then broadcasts.

    ALPR runs against the *internal* (unredacted) thumbnail because we need
    the plate text to hash it. The raw plate string is then discarded —
    only the salted hash survives into the egress payload. This is the
    key compliance boundary: plate text never reaches Slack, the SSE feed,
    or the recent-events buffer.

    Args:
        event: The typed event dict built by ``_flush_episode``. Mutated
            in-place to add ``narration``, optional ``enrichment``, and
            ``perception_state`` / ``enrichment_skipped`` flags.
        internal_thumb_name: Basename of the unredacted JPEG on disk —
            passed to ``enrich_event`` so it can feed pixels to the
            optional external ALPR provider.

    Side effects:
        * Appends to ``state.recent_events`` (capped rolling buffer).
        * Updates ``road_registry`` (per-vehicle event counts + score).
        * Fans out to all SSE subscribers.
        * Fires Slack notification against the redacted thumbnail.
        * Optionally samples for active learning.
        * Optionally enqueues to the edge -> cloud publisher.
    """
    internal_path = THUMBS_DIR / internal_thumb_name
    public_path = THUMBS_DIR / Path(event["thumbnail"]).name

    # Three skip paths for the vision call:
    #   (a) policy: ALPR disabled unless ROAD_ALPR_MODE=third_party.
    #       Default is ``off`` — no external ALPR call, ever. This is a
    #       deployment-wide posture (visible via /api/settings) and is
    #       NOT stamped onto each event: doing so would attach a constant
    #       banner to every card in the default posture, drowning the
    #       per-event signals below.
    #   (b) perception is degraded — low-SNR image, money wasted.
    #   (c) low-risk events — weekly-batch review SLA, ALPR adds little value.
    # This prevents unnecessary external calls before rate limiting even
    # starts. The per-event reason string is surfaced on the event so
    # dashboards can explain *why* enrichment was skipped for that frame.
    policy_skip = ALPR_MODE != "third_party"
    perception_skip = state.quality.risk_adjustment().get("skip_vision_enrichment", False)
    low_risk_skip = event.get("risk_level") == "low"
    skip_enrich = policy_skip or perception_skip or low_risk_skip
    event["perception_state"] = state.quality.state().get("state", "nominal")
    # Only emit per-event skip reasons. Policy-level skipping is a
    # deployment property, not a per-event signal — operators read it
    # from settings, not from each event.
    if perception_skip:
        event["enrichment_skipped"] = "perception_degraded"
    elif low_risk_skip:
        event["enrichment_skipped"] = "low_risk_event"

    # ``asyncio.gather`` runs both coroutines concurrently; the call
    # completes when BOTH finish. Narration ~200ms; enrichment ~500ms.
    # Running them in parallel cuts the critical path in half.
    narrate_task = narrate_event(event)
    enrich_task = (
        enrich_event(event, internal_path) if not skip_enrich else _none_coro()
    )
    narration, enrichment = await asyncio.gather(narrate_task, enrich_task)
    if narration:
        event["narration"] = narration
    if enrichment:
        # PRIVACY INVARIANT (defence in depth): enrich_event() already
        # hashes plate at the LLM boundary so raw plate_text never reaches
        # this process. Re-pop here in case a caller ever wires up a
        # different enrichment source — the invariant "no raw plate in
        # the event buffer" is enforced twice. Any future enrichment
        # provider MUST respect this.
        enrichment.pop("plate_text", None)
        enrichment.pop("plate_state", None)
        event["enrichment"] = enrichment

    # Append to rolling buffer, evicting oldest when we exceed the cap.
    # A plain list is fine at ``MAX_RECENT_EVENTS`` (usually ~500) — at
    # that size the O(n) pop(0) cost is negligible compared to the rest
    # of the pipeline, and list has better random-access semantics than
    # deque for the slicing read paths below.
    state.recent_events.append(event)
    if len(state.recent_events) > MAX_RECENT_EVENTS:
        state.recent_events.pop(0)

    # Road registry — track per-vehicle event counts + safety score.
    road_registry.record_event(event)

    # Fan out the event to every SSE subscriber. Same pattern as
    # ``_broadcast_perception`` — drop on QueueFull, never block.
    for q in list(state.subscribers):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass

    # Slack gets the redacted (public) thumbnail — never the internal one.
    # ``asyncio.create_task`` schedules the coroutine without awaiting it,
    # so a slow Slack webhook does not delay the next perception frame's
    # event emission.
    asyncio.create_task(slack_notify(event, public_path))

    # Active-learning sampler: if confidence is near the decision boundary,
    # tag this event for later human labelling. No-op otherwise.
    try:
        state.active_learner.maybe_sample(event)
    except Exception as exc:
        log.warning("active-learning sample failed: %s", exc)

    # Edge -> Cloud publisher: enqueues to a local JSONL, drained by a
    # background task. No-op if ROAD_CLOUD_ENDPOINT / ROAD_CLOUD_HMAC_SECRET
    # aren't set. Only redacted thumbs + hashed plate cross the wire.
    if state.edge_publisher.enabled():
        try:
            await state.edge_publisher.enqueue(event, public_path)
        except Exception as exc:
            log.warning("edge enqueue failed: %s", exc)


# ===== SECTION: APP LIFESPAN (startup + shutdown orchestration) =====
# FastAPI's ``lifespan`` is an async context manager invoked once per process:
# everything BEFORE the ``yield`` runs at startup; everything AFTER runs at
# shutdown. Startup here does a lot of work — model loading, stream
# resolution, launching background tasks. Shutdown cancels those tasks.
#
# Python note: ``@asynccontextmanager`` turns a single async generator
# (``async def ... yield ...``) into a usable ``async with`` manager.
# The function must yield EXACTLY ONCE; code before the yield runs on
# ``__aenter__``, code after on ``__aexit__``.


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI startup + shutdown hook.

    Startup (pre-yield):
        * Captures the running event loop for the perception thread.
        * Warns if fleet identity is missing.
        * Loads the YOLO model.
        * Resolves the HLS URL + starts the StreamReader thread.
        * Starts digest schedulers, edge publisher, retention sweep loop,
          score decay loop, test runner, and the AI watchdog.
        * Constructs the agent executor (needs a live event-lookup).

    Shutdown (post-yield):
        * Stops the stream reader.
        * Cancels every long-running asyncio task.

    Args:
        app: The FastAPI application instance (unused here; required by
            the lifespan signature).
    """
    state.loop = asyncio.get_running_loop()
    if _MISSING_IDENTITY:
        log.warning(
            "fleet identity unset for %s — using hostname fallbacks "
            "(vehicle_id=%s, road_id=%s, driver_id=%s); events will not "
            "attribute to a real fleet until these env vars are set",
            ", ".join(_MISSING_IDENTITY),
            RESOLVED_VEHICLE_ID,
            RESOLVED_ROAD_ID,
            RESOLVED_DRIVER_ID,
        )
    log.info("loading YOLO model")
    state.model = load_model()

    # Point drift monitor at the live in-memory buffer so it reads fresh
    # events rather than the on-disk snapshot.
    state.drift.set_event_source(lambda: list(state.recent_events))

    # ----- Build per-source slots and start each reader -----
    # ``STREAM_SOURCES`` is parsed in ``config.py`` from
    # ``ROAD_STREAM_SOURCES`` (multi-source, primary fallback to legacy
    # ``ROAD_STREAM_SOURCE``). When the env is empty we keep the
    # default-constructed empty primary slot — the API still works,
    # operators can start a stream later via ``/api/live/sources/.../start``.
    if STREAM_SOURCES:
        # Replace the placeholder primary slot with the configured one.
        state.slots.clear()
        for entry in STREAM_SOURCES:
            sid = entry["id"]
            state.slots[sid] = StreamSlot(sid, entry.get("name") or sid, entry["url"])
        # Track human-readable label for the legacy ``/api/live/status``
        # response (single-source clients still see the primary URL).
        primary = state.primary_slot
        state.source_label = primary.original_source

    started = []
    for slot in state.slots.values():
        if not slot.original_source:
            log.warning("slot %s has no source URL; skipping start", slot.source_id)
            continue
        try:
            _start_slot(slot)
            started.append(slot.source_id)
        except Exception as exc:
            log.error("failed to start slot %s: %s", slot.source_id, exc)
            slot.last_error = str(exc)
    if started:
        log.info("started %d stream reader(s): %s", len(started), ", ".join(started))
    else:
        log.warning("no live streams started (no sources or all failed)")

    # Settings Console: warm-reload for TARGET_FPS. The StreamReader captures
    # the fps value at construction time (and bakes it into an ffmpeg command),
    # so a live change only takes effect after the reader
    # is recycled. We restart each active slot on a background thread so the
    # settings-apply HTTP response is not delayed by the stop/join.
    def _on_target_fps_change(before, after) -> None:
        old = float(before.get("TARGET_FPS") or 0.0)
        new = float(after.get("TARGET_FPS") or 0.0)
        if old == new:
            return
        log.info("TARGET_FPS change %.1f -> %.1f — restarting active slots", old, new)

        def _restart_slots() -> None:
            for slot in list(state.slots.values()):
                if slot.reader is None:
                    continue
                try:
                    _stop_slot(slot)
                    _start_slot(slot)
                except Exception as exc:
                    log.warning(
                        "slot %s restart for TARGET_FPS failed: %s", slot.source_id, exc
                    )

        threading.Thread(
            target=_restart_slots, daemon=True, name="target_fps_reload"
        ).start()

    SETTINGS_STORE.register_subscriber_for(
        ["TARGET_FPS"], _on_target_fps_change, name="restart_slots_for_fps"
    )

    # Ops sampler: one periodic thread that records fps / CPU / LLM
    # spend. Safe to start even when no slots are active yet — the
    # sampler will just record zero-fps samples until a reader appears.
    state.ops_sampler.start()

    # Digest schedulers (medium hourly, low daily). Idempotent.
    start_digest_schedulers(state.loop)
    log.info("digest schedulers started")

    # Edge -> Cloud publisher loop (no-op if not configured).
    edge_task = None
    if state.edge_publisher.enabled():
        edge_task = asyncio.create_task(state.edge_publisher.run_forever())
        log.info("edge publisher started")
    else:
        log.info("edge publisher disabled (ROAD_CLOUD_ENDPOINT / _HMAC_SECRET unset)")

    # Data retention background sweep.
    retention_task = asyncio.create_task(retention_loop())
    log.info("retention policy loop started")
    score_decay_task = None
    if SCORE_DECAY_INTERVAL_SEC > 0:
        score_decay_task = asyncio.create_task(
            _score_decay_loop(SCORE_DECAY_INTERVAL_SEC)
        )
        log.info("score decay loop started (interval=%ds)", SCORE_DECAY_INTERVAL_SEC)

    # Agent executor — wired after event_lookup is available.
    state.agent_executor = AgentExecutor(
        event_lookup=_find_event,
        events_source=lambda: list(state.recent_events),
        drift_monitor=state.drift,
    )
    log.info("agent executor ready (coaching, investigation, report)")

    # Auto-run test suite in background on startup.
    start_test_run()
    log.info("test suite started in background")

    # Background validator — shadow-mode dual-model disagreement detector.
    # Runs a heavier detector on sampled frames + every emitted episode's
    # peak frame; publishes disagreements as watchdog findings. Never
    # blocks the primary perception path.
    validator_task = None
    if VALIDATOR_ENABLED:
        try:
            detector = SecondaryDetector()
            comparator = DiscrepancyComparator(iou_threshold=VALIDATOR_IOU_THRESHOLD)
            state.validator = ValidatorWorker(
                detector=detector,
                comparator=comparator,
                write_finding=_watchdog_write_finding,
                finding_ctor=WatchdogFinding,
                observer_record_skip=(
                    getattr(llm_observer, "record_skip", None)
                ),
                queue_max=VALIDATOR_QUEUE_MAX,
                sample_sec=VALIDATOR_SAMPLE_SEC,
            )
            validator_task = asyncio.create_task(state.validator.run_forever())
            log.info(
                "validator started (backend=%s, sample_sec=%.1f, queue_max=%d)",
                detector.backend,
                VALIDATOR_SAMPLE_SEC,
                VALIDATOR_QUEUE_MAX,
            )
        except Exception as exc:
            log.warning("validator failed to start, continuing without it: %s", exc)
            state.validator = None
    else:
        log.info("validator disabled (ROAD_VALIDATOR_ENABLED=0)")

    # AI Watchdog — background health monitor.
    # Collects a snapshot of the system's health periodically and fingerprints
    # repeated issues into an incident queue (not a log-tail wall of red).
    watchdog_task = None
    if WATCHDOG_ENABLED:
        def _collect_snapshot() -> dict:
            """Build the point-in-time health snapshot for the watchdog.

            Pulled from closures over ``state`` / ``llm_observer``. Returned
            dict structure is owned by the watchdog — adding a field is
            fine, renaming requires updating the watchdog rules.
            """
            q = state.quality.state()
            ctx = state.last_scene_ctx
            ego = state.last_ego_flow
            reader = state.reader
            drift_report = state.drift.compute().as_dict()
            llm_stats = llm_observer.stats(window_sec=300)
            recent_events = list(state.recent_events)[-25:]
            unknown_event_types = sum(
                1 for evt in recent_events
                if not evt.get("event_type") or evt.get("event_type") == "unknown"
            )
            unknown_risk_levels = sum(
                1 for evt in recent_events
                if not evt.get("risk_level") or evt.get("risk_level") == "unknown"
            )
            recent_confidences = [
                float(evt.get("confidence"))
                for evt in recent_events
                if isinstance(evt.get("confidence"), (int, float))
            ]
            return {
                "server": {
                    "running": reader is not None and reader._thread is not None and reader._thread.is_alive() if reader else False,
                    "uptime_sec": round(reader.uptime_sec(), 1) if reader else 0.0,
                    "source": state.source_label,
                    "target_fps": TARGET_FPS,
                },
                "pipeline": {
                    "frames_read": reader.frames_read if reader else 0,
                    "frames_processed": reader.frames_processed if reader else 0,
                    "event_count": len(state.recent_events),
                    "active_episodes": len(state.episodes),
                },
                "perception": {
                    "state": q["state"],
                    "reason": q["reason"],
                    "samples": q["samples"],
                    "avg_confidence": q.get("avg_confidence", 0),
                    "luminance": q.get("luminance", 0),
                    "sharpness": q.get("sharpness", 0),
                },
                "drift": drift_report,
                "llm": llm_stats,
                "scene": {
                    "label": ctx.label if ctx else "unknown",
                    "reason": ctx.reason if ctx else "not yet observed",
                },
                "ego": {
                    "speed_proxy_mps": round(ego.speed_proxy_mps, 2) if ego else None,
                },
                "taxonomy": {
                    "recent_events": len(recent_events),
                    "unknown_event_types": unknown_event_types,
                    "unknown_risk_levels": unknown_risk_levels,
                    "unknown_event_ratio": round(unknown_event_types / len(recent_events), 4) if recent_events else 0.0,
                    "unknown_risk_ratio": round(unknown_risk_levels / len(recent_events), 4) if recent_events else 0.0,
                    "avg_event_confidence": round(sum(recent_confidences) / len(recent_confidences), 4) if recent_confidences else 0.0,
                },
            }

        state.watchdog = Watchdog(
            collect_fn=_collect_snapshot,
            interval_sec=WATCHDOG_INTERVAL_SEC,
        )
        watchdog_task = asyncio.create_task(state.watchdog.run_loop())
        log.info("watchdog started (interval=%ds)", WATCHDOG_INTERVAL_SEC)
    else:
        state.watchdog = None
        log.info("watchdog disabled (ROAD_WATCHDOG_ENABLED=0)")

    # ----- Yield control to the running app -----
    # While yielded, FastAPI serves requests. When the process receives a
    # shutdown signal, control returns here for cleanup.
    yield

    # ----- Shutdown: cancel every background task we started -----
    # ``cancel()`` on an asyncio.Task raises CancelledError inside the
    # coroutine at its next await point; the tasks above all handle this
    # by re-raising (clean exit).
    for slot in state.slots.values():
        if slot.reader:
            try:
                slot.reader.stop()
            except Exception as exc:
                log.warning("slot %s stop failed: %s", slot.source_id, exc)
    try:
        state.ops_sampler.stop()
    except Exception as exc:
        log.warning("ops_sampler stop failed: %s", exc)
    if edge_task is not None:
        edge_task.cancel()
    retention_task.cancel()
    if score_decay_task is not None:
        score_decay_task.cancel()
    if watchdog_task is not None:
        watchdog_task.cancel()
    if validator_task is not None:
        validator_task.cancel()


# ===== SECTION: FASTAPI APP CONSTRUCTION + STATIC MOUNTS =====

app = FastAPI(title="Live Safety Review", lifespan=lifespan)

THUMBS_DIR.mkdir(parents=True, exist_ok=True)

app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

def _find_event(event_id: str) -> dict | None:
    """Locate an event by id in the recent-events buffer.

    Searches newest-first (``reversed``) because the most recent events
    are the ones an operator is likely querying.

    Args:
        event_id: The ``evt_...`` id produced by ``_flush_episode``.

    Returns:
        The event dict if found, else ``None``.
    """
    for ev in reversed(state.recent_events):
        if ev.get("event_id") == event_id:
            return ev
    return None


async def _on_feedback(record: dict, matched: dict | None) -> None:
    """Runs after each /api/feedback POST.

    - If verdict=fp: pull the event into the active-learning pool
      (disputed events are the highest-value training data).
    - Recompute drift; if precision dropped past threshold, post a one-off
      Slack warning (rate-limited by DriftMonitor's internal state).

    Args:
        record: The feedback payload as posted to ``/api/feedback``
            (verdict, optional note, event_id).
        matched: The matched event dict from ``state.recent_events``, or
            ``None`` if feedback references an event that has already
            rolled off the buffer.
    """
    audit.log(
        "submit_feedback", record.get("event_id", "unknown"),
        detail={"verdict": record.get("verdict"), "note": record.get("note")},
    )
    vehicle_id = matched.get("vehicle_id") if isinstance(matched, dict) else None
    road_registry.record_feedback(
        record.get("event_id", ""), record.get("verdict", ""), vehicle_id
    )
    if record.get("verdict") == "fp" and matched is not None:
        try:
            state.active_learner.sample_disputed(matched, note=record.get("note"))
        except Exception as exc:
            log.warning("active-learning sample_disputed failed: %s", exc)

    try:
        report = state.drift.compute()
    except Exception as exc:
        log.warning("drift compute failed: %s", exc)
        return
    if not report.alert_triggered:
        return
    warning = drift_warning_message(report)
    if not warning or not slack_configured():
        return
    # Piggyback on slack_notify's webhook — use the digest post path so it
    # renders as a section, not a full block-kit high-risk card.
    try:
        from road_safety.integrations.slack import _post_digest
        await _post_digest(
            title="Drift warning",
            summary=f"Precision {report.precision:.2f} over {report.window_size} labels",
            body=warning,
        )
    except Exception as exc:
        log.warning("drift slack warn failed: %s", exc)


# Feedback (thumbs-up/down) + coaching queue routes, wired to drift + AL hooks.
# The routes themselves live in ``road_safety/api/feedback.py`` — this call
# bolts them onto this app and wires our ``_on_feedback`` hook for drift
# recomputation and active-learning sampling.
mount_feedback_routes(app, on_feedback=_on_feedback, event_lookup=_find_event)


# ===== SECTION: SETTINGS CONSOLE ROUTES =====
# The Settings Console (see docs/improvements/settings-console-plan.md) lets
# operators tune backend parameters at runtime, save templates, and inspect
# baseline-vs-after impact with deterministic comparability gates and an
# optional advisory LLM narrative. The router is admin-bearer only; the
# impact SSE stream uses a single-use ticket exchange to avoid leaking the
# long-lived bearer through query strings / access logs.
# Ops sampler: periodic snapshot of actual fps, CPU, memory, and LLM
# cost/latency/tokens. The Settings Console uses its window_stats() to
# populate the operational-metric deltas in the Impact report so an
# operator can see whether a change made the pipeline cheaper / faster /
# heavier, not just whether it shifted the event-rate distribution.
def _aggregate_frames() -> tuple[int, int]:
    """Sum ``frames_read`` / ``frames_processed`` across all active slots.

    Returning a single pair keeps the sampler agnostic to the
    multi-source slot model — it just sees "how many frames did the
    pipeline ingest + process since start".
    """
    total_read = 0
    total_proc = 0
    for slot in state.slots.values():
        r = slot.reader
        if r is not None:
            total_read += int(getattr(r, "frames_read", 0) or 0)
            total_proc += int(getattr(r, "frames_processed", 0) or 0)
    return total_read, total_proc


state.ops_sampler = OpsSampler(
    frames_source=_aggregate_frames,
    llm_stats_fn=llm_observer.stats,
)

state.settings_impact = SettingsImpactMonitor(
    events_source=lambda: list(state.recent_events),
    ops_stats_fn=state.ops_sampler.window_stats,
)
state.settings_impact_subscribers: list[asyncio.Queue] = []
mount_settings_routes(
    app,
    impact_monitor=state.settings_impact,
    impact_subscribers=state.settings_impact_subscribers,
)


# ===== SECTION: ROUTE HANDLERS — STATIC + ROOT =====


@app.get("/")
def index():
    """Serve the SPA index.html.

    HTTP: GET /
    AUTH: public
    Returns:
        The built ``index.html`` (React app entrypoint).
    """
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/favicon.ico")
def favicon():
    """Serve the favicon if present, else 404.

    HTTP: GET /favicon.ico
    AUTH: public
    """
    path = STATIC_DIR / "favicon.ico"
    if path.exists():
        return FileResponse(path)
    raise HTTPException(404)


@app.get("/thumbnails/{name}")
def thumbnail(name: str, request: Request):
    """Serve redacted (public) thumbnails freely; gate unredacted on DSAR token.

    Public UI + Slack relay + SSE all reference ``*_public.jpg``. Requesting
    the internal unredacted ``evt_xxxx.jpg`` requires a preconfigured
    X-DSAR-Token header — the minimum viable DSAR (Data Subject Access
    Request) access workflow. With no token set in env, unredacted
    retrieval is closed entirely.

    HTTP: GET /thumbnails/{name}
    AUTH:
        * ``*_public.jpg`` — public, but requires a signed ``exp`` + ``token``
          pair when ``PUBLIC_THUMBS_REQUIRE_TOKEN`` is on.
        * Internal ``evt_*.jpg`` — requires ``X-DSAR-Token`` header.
    Args:
        name: Thumbnail filename from the URL path.
        request: FastAPI request, inspected for signing query params and
            the DSAR header.
    Returns:
        FileResponse streaming the JPEG, or raises HTTPException (400 for
        a traversal-ish name, 403 on auth fail, 404 when missing).
    Side effects:
        Every access — success or denial — is recorded to the audit log
        so a compliance reviewer can reconstruct who saw what.
    """
    # Basic path-traversal / hidden-file protection. ``THUMBS_DIR / name``
    # would otherwise happily resolve ``../../etc/passwd``.
    if "/" in name or "\\" in name or name.startswith("."):
        raise HTTPException(400, "invalid name")
    path = THUMBS_DIR / name
    if not path.exists():
        raise HTTPException(404, "thumbnail not found")
    ip = request.client.host if request.client else None
    if "_public." in name:
        if not _valid_thumb_request(name, request):
            audit.log("access_public_thumbnail", name, outcome="denied", ip=ip)
            raise HTTPException(
                403,
                "public thumbnail requires valid exp/token query params",
            )
        audit.log("access_public_thumbnail", name, outcome="success", ip=ip)
        return FileResponse(path)
    # Internal unredacted path — DSAR-token required. Absence of env var
    # closes the gate entirely (``not DSAR_TOKEN`` short-circuits).
    token = request.headers.get("X-DSAR-Token")
    if not DSAR_TOKEN or token != DSAR_TOKEN:
        audit.log("access_unredacted_thumbnail", name, outcome="denied", ip=ip)
        raise HTTPException(
            403,
            "unredacted thumbnail — present X-DSAR-Token header "
            "(set ROAD_DSAR_TOKEN env var on the server)",
        )
    audit.log("access_unredacted_thumbnail", name, outcome="success", ip=ip)
    return FileResponse(path)


# ===== SECTION: ROUTE HANDLERS — LIVE SSE + COPILOT =====


@app.get("/stream/events")
async def stream_events(request: Request):
    """Server-Sent Events feed of live safety events.

    HTTP: GET /stream/events
    AUTH: public
    Response shape: ``text/event-stream``. Each message is a JSON-
        serialised event dict on a ``data:`` line; keepalives are sent as
        ``: keepalive`` comment frames so proxies don't drop the
        connection during quiet periods.
    Lifecycle:
        * Client connects: we create a per-client ``asyncio.Queue``, add
          it to ``state.subscribers``, and immediately replay the last
          ``SSE_REPLAY_COUNT`` events from the rolling buffer.
        * While connected: ``await queue.get()`` blocks for new events.
          A 15-second timeout sends a keepalive comment to keep the
          connection open even when the pipeline is quiet.
        * On disconnect: ``finally`` removes the queue from subscribers
          so ``_emit_event`` stops fanning to it.
    """
    # ``maxsize=200`` caps a single slow consumer at 200 buffered events
    # before broadcasts start dropping to their queue. The client can
    # tell from gaps in event_id sequence that a reconnect is needed.
    queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    state.subscribers.add(queue)

    async def gen():
        """Async generator producing SSE frames.

        Python note: ``yield`` inside an async generator makes each
        ``yield``-ed string a chunk of the HTTP response body. FastAPI's
        ``StreamingResponse`` pipes these chunks to the client as they
        are produced.
        """
        try:
            # Replay recent buffer so a fresh client sees context, not
            # just the next new event.
            for ev in state.recent_events[-SSE_REPLAY_COUNT:]:
                yield f"data: {json.dumps(ev)}\n\n"
            while True:
                # ``Request.is_disconnected`` is how FastAPI exposes
                # "client hung up" to our coroutine.
                if await request.is_disconnected():
                    break
                try:
                    # ``wait_for`` caps the blocking get at 15s so we can
                    # emit a keepalive if the pipeline is silent.
                    ev = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {json.dumps(ev)}\n\n"
                except asyncio.TimeoutError:
                    # SSE comment line (starts with ``:``) — ignored by
                    # the EventSource client, keeps proxies happy.
                    yield ": keepalive\n\n"
        finally:
            # ``finally`` always runs — guarantees subscriber cleanup
            # even if the coroutine is cancelled mid-iteration.
            state.subscribers.discard(queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/chat")
async def chat(body: dict):
    """Copilot endpoint — RAG-style Q&A over recent events + statute corpus.

    HTTP: POST /chat
    AUTH: public
    Request body: ``{"query": "<free-text question>"}``
    Response: ``{"answer": "<LLM-generated answer>"}``
    Side effects: audit-logs the first 200 chars of the query.
    """
    query = (body.get("query") or "").strip()
    if not query:
        raise HTTPException(400, "missing 'query'")
    # Truncate to 200 chars in the audit log — long queries may contain
    # PII from the user; we only need enough to identify the pattern.
    audit.log("chat_query", query[:200])
    answer = await llm_chat(query, state.recent_events)
    return {"answer": answer}


# ===== SECTION: ROUTE HANDLERS — LIVE STATUS + PERCEPTION + SCENE =====


@app.get("/api/live/status")
def live_status():
    """Public health + configuration snapshot for the operator UI.

    HTTP: GET /api/live/status
    AUTH: public
    Response: a large dict with source label, running flag, frame counts,
        uptime, tracker/risk-model names, and PII-redaction config.
    """
    q = state.quality.state()
    return {
        "source": state.source_label,
        "location": LOCATION,
        "running": state.reader is not None and state.reader._thread is not None and state.reader._thread.is_alive(),
        "event_count": len(state.recent_events),
        "frames_read": state.reader.frames_read if state.reader else 0,
        "frames_processed": state.reader.frames_processed if state.reader else 0,
        "uptime_sec": round(state.reader.uptime_sec(), 1) if state.reader else 0.0,
        "started_at": state.reader.started_at if state.reader else None,
        "llm_configured": llm_configured(),
        "slack_configured": slack_configured(),
        "target_fps": TARGET_FPS,
        "active_episodes": len(state.episodes),
        "tracker": "bytetrack",
        "risk_model": "ttc+ground_plane",
        "pii_redaction": "face+plate",
        "dsar_endpoint_enabled": bool(DSAR_TOKEN),
        "public_thumb_token_required": PUBLIC_THUMBS_REQUIRE_TOKEN,
        "alpr_mode": ALPR_MODE,
        "perception": {
            "state": q["state"],
            "reason": q["reason"],
            "samples": q["samples"],
            "since_sec": q["since_sec"],
            "avg_confidence": q["avg_confidence"],
            "luminance": q["luminance"],
            "sharpness": q["sharpness"],
        },
    }


@app.get("/api/demo/track")
def demo_track():
    """Return the bundled demo GPS track + vehicle identity for the map overlay.

    HTTP: GET /api/demo/track
    AUTH: public (no PII — just a synthetic route and demo plate "XX 001 X").
    Response shape::

        {
          "ok": true,
          "vehicle": {
            "plate": "XX 001 X",
            "model": "Nissan Rogue",
            "company": "Fox Factory",
            "vehicle_id": "<resolved ROAD_VEHICLE_ID>"
          },
          "points": [{"lat": float, "lng": float, "t_sec": float}, ...],
          "total_duration_sec": float,
          "bounds": {"south": float, "west": float, "north": float, "east": float}
        }

    The frontend loops through ``points`` at a display-friendly cadence
    (faster than the original wall-clock spacing) and interpolates between
    consecutive points to animate the map marker smoothly. Cached in-process
    after first read — the source JSON is part of the repo and doesn't
    change between requests.
    """
    payload = demo_track_service.load_track()
    # Layer the demo vehicle identity on top of the cached geo payload. The
    # vehicle dict is tiny + cheap to build; returning it here keeps the
    # frontend map self-contained (one fetch, full picture).
    return {
        **payload,
        "vehicle": {
            "plate": "XX 001 X",
            "model": "Nissan Rogue",
            "company": "Fox Factory",
            "vehicle_id": RESOLVED_VEHICLE_ID,
        },
    }


# Videos that this endpoint knows how to probe + sync. Keyed on the short
# ``?video=`` parameter so the frontend doesn't need to know absolute paths.
from road_safety.config import (  # noqa: E402
    _DEMO_FRONT_CAM_FILE,
    _DEMO_LEFT_CAM_FILE,
    _DEMO_REAR_CAM_FILE,
)

_DEMO_VIDEO_SOURCES: dict[str, Path] = {
    "front": _DEMO_FRONT_CAM_FILE,
    "rear": _DEMO_REAR_CAM_FILE,
    "left": _DEMO_LEFT_CAM_FILE,
}

# The bundled dashcam MP4s carry ``creation_time`` from the original recording
# session, which falls outside the bundled GPX window (01:26:42Z–01:51:15Z).
# Override to the GPX waypoint where the vehicle starts moving after the idle
# stretch — 18:28:25 Vancouver — so the map marker departs in lockstep with
# the video's first frame instead of falling back to the ``nearest`` segment.
_DEMO_VIDEO_START_ISO_UTC = "2026-04-20T01:28:25.002Z"


@app.get("/api/demo/video-track")
def demo_video_track(video: str = "front"):
    """Return a GPS track aligned to the requested video's recording window.

    HTTP: GET /api/demo/video-track?video=front
    AUTH: public — same as /api/demo/track.

    Unlike /api/demo/track (which flattens the whole Timeline into a loop),
    this endpoint uses the MP4's ``creation_time`` + ``duration`` as a
    wallclock window, slices the Timeline to that window, and re-bases
    ``t_sec`` so ``0`` == first frame of the video. The frontend can then
    drive the map marker directly from video playback time — no wallclock
    compression needed.

    Response shape (same as /api/demo/track plus a ``video`` block)::

        {
          "ok": true,
          "video": {
            "key": "front",
            "path": "...",
            "creation_time": "2026-04-19T22:41:03Z",
            "duration_sec": 653.85,
            "width": 3840, "height": 2160,
            "fps": 59.94, "codec": "h264"
          },
          "vehicle": {...},
          "points": [...],           # t_sec is video-relative
          "total_duration_sec": 653.85,
          "bounds": {...}
        }

    Error cases return ``ok: false`` with an ``error`` string; HTTP 200
    is preserved so the frontend can render a graceful fallback instead of
    handling a 4xx path.
    """
    path = _DEMO_VIDEO_SOURCES.get(video)
    if path is None:
        return {
            "ok": False,
            "error": f"unknown video key {video!r}; known: {sorted(_DEMO_VIDEO_SOURCES)}",
            "video": None,
            "points": [],
            "total_duration_sec": 0.0,
            "bounds": None,
        }

    meta = video_metadata_service.probe(path)
    if meta is None or not meta.creation_time or meta.duration_sec <= 0:
        return {
            "ok": False,
            "error": (
                f"could not extract usable metadata from {path.name!r} "
                "(file missing, ffprobe unavailable, or no creation_time)"
            ),
            "video": meta.to_dict() if meta is not None else None,
            "points": [],
            "total_duration_sec": 0.0,
            "bounds": None,
        }

    track = demo_track_service.load_track_for_window(
        start_iso_utc=_DEMO_VIDEO_START_ISO_UTC,
        duration_sec=meta.duration_sec,
    )
    return {
        **track,
        "video": {
            "key": video,
            **meta.to_dict(),
            "creation_time": _DEMO_VIDEO_START_ISO_UTC,
        },
        "vehicle": {
            "plate": "XX 001 X",
            "model": "Nissan Rogue",
            "company": "Fox Factory",
            "vehicle_id": RESOLVED_VEHICLE_ID,
        },
    }


@app.get("/api/live/perception")
def live_perception():
    """Return the perception-quality monitor's current state.

    HTTP: GET /api/live/perception
    AUTH: public
    Returns: dict with ``state``, ``reason``, ``samples``, ``avg_confidence``,
        ``luminance``, ``sharpness`` — suitable for rendering the
        perception banner in the UI.
    """
    return state.quality.state()


@app.get("/api/live/scene")
def live_scene():
    """Current scene context (urban/highway/parking/unknown) + adaptive
    thresholds in effect right now. Useful for the UI to explain why a given
    TTC threshold is being applied.

    HTTP: GET /api/live/scene
    AUTH: public
    """
    ctx = state.last_scene_ctx
    if ctx is None:
        return {"label": "unknown", "reason": "not yet observed"}
    thr = state.scene.adaptive_thresholds(ctx)
    ego = state.last_ego_flow
    return {
        "label": ctx.label,
        "confidence": round(ctx.confidence, 2),
        "speed_proxy_mps": (
            round(ctx.speed_proxy_mps, 2) if ctx.speed_proxy_mps is not None else None
        ),
        "pedestrian_rate_per_min": round(ctx.pedestrian_rate_per_min, 2),
        "vehicle_rate_per_min": round(ctx.vehicle_rate_per_min, 2),
        "reason": ctx.reason,
        "thresholds": {
            "ttc_high_sec": thr.ttc_high_sec,
            "ttc_med_sec": thr.ttc_med_sec,
            "dist_high_m": thr.dist_high_m,
            "dist_med_m": thr.dist_med_m,
        },
        "ego_flow": (
            {
                "speed_proxy_mps": round(ego.speed_proxy_mps, 2),
                "confidence": round(ego.confidence, 2),
            }
            if ego is not None else None
        ),
    }


@app.get("/api/drift")
def api_drift():
    """Rolling precision over the most recent labelled events. Emits a
    structured report broken down by risk level and event_type.

    HTTP: GET /api/drift
    AUTH: public
    """
    return state.drift.compute().as_dict()


@app.post("/api/active_learning/export")
def api_active_learning_export(request: Request):
    """Bundle pending active-learning samples into a zip for Label Studio /
    CVAT import. Returns the zip path (on-disk; operator downloads it
    out-of-band) or 204 when the pool is empty.

    HTTP: POST /api/active_learning/export
    AUTH: admin bearer
    Returns: ``{"path": "<absolute-path-to-zip>"}`` or 204 no-content.
    """
    _require_admin(request, "active-learning export")
    audit.log("export_active_learning", "batch_export")
    try:
        path = state.active_learner.export_batch()
    except Exception as exc:
        raise HTTPException(500, f"export failed: {exc}")
    if path is None:
        raise HTTPException(204, "no pending samples")
    return {"path": str(path)}


@app.get("/api/live/events")
def live_events(risk_level: str | None = None, event_type: str | None = None, limit: int = 100):
    """Paginated read of live events with optional filters.

    HTTP: GET /api/live/events
    AUTH: public
    Query params:
        risk_level: Filter to one of "low" / "medium" / "high".
        event_type: Filter to a specific event_type string.
        limit: Return at most this many most-recent events (default 100).
    """
    items = list(state.recent_events)
    if risk_level:
        items = [e for e in items if e["risk_level"] == risk_level]
    if event_type:
        items = [e for e in items if e["event_type"] == event_type]
    # ``items[-limit:]`` grabs the tail (newest); Python slices are safe
    # even when ``limit`` exceeds len.
    return items[-limit:]


def _load_batch(name: str):
    """Read a batch-pipeline JSON artefact from DATA_DIR or raise 404.

    Used by the legacy ``/api/summary`` endpoint to serve files written
    by the offline ``analyze.py`` script. Live endpoints should read
    ``state.recent_events`` directly instead.
    """
    path = DATA_DIR / name
    if not path.exists():
        raise HTTPException(404, f"{name} not found — run analyze.py first")
    return json.loads(path.read_text())


@app.get("/api/events")
def events(
    risk_level: str | None = None,
    event_type: str | None = None,
    limit: int = 500,
):
    """Live events from the in-memory recent-events buffer.

    Previously this endpoint read a stale on-disk `events.json` written only
    by the batch `analyze.py` pipeline, so dashboards saw 0 events even while
    the live stream emitted them. Now it serves the same buffer as
    `/api/live/events` and `/api/summary`.

    HTTP: GET /api/events
    AUTH: public
    """
    items = list(state.recent_events)
    if risk_level:
        items = [e for e in items if e.get("risk_level") == risk_level]
    if event_type:
        items = [e for e in items if e.get("event_type") == event_type]
    return items[-limit:]


@app.get("/api/events/{event_id}")
def event(event_id: str):
    """Look up a single event by id.

    HTTP: GET /api/events/{event_id}
    AUTH: public
    Raises: 404 if no matching event is in the current buffer.
    """
    for ev in state.recent_events:
        if ev.get("event_id") == event_id:
            return ev
    raise HTTPException(404, "event not found")


@app.get("/api/events/{event_id}/clip")
def event_clip(
    event_id: str,
    before: float = 3.0,
    after: float = 3.0,
    annotated: bool = True,
):
    """Serve a ±N-second MP4 clip centred on the event's timestamp.

    HTTP: GET /api/events/{event_id}/clip?before=3&after=3&annotated=1
    AUTH: public
    Returns: ``FileResponse`` with the cached clip; 404 if the event is
        unknown, the source is not a seekable local file, or the clip
        can't be produced.

    Two flavours, both cached under ``data/clips/``:

    * ``annotated=1`` (default) — every frame in the window is run through
      YOLO and class-coloured bboxes are burned in via cv2 → ffmpeg pipe.
      Cache key: ``{event_id}_{before}_{after}_annotated.mp4``. Matches
      the recognition overlay on the live admin tile so reviewers see
      "what the camera was thinking" while they review the clip.
    * ``annotated=0`` — raw ffmpeg cut, kept as an escape hatch for
      reviewers who want unannotated source pixels (e.g. legal evidence
      capture). Cache key: ``{event_id}_{before}_{after}.mp4``.

    Annotated clips are MUCH heavier on first render (full YOLO pass over
    the window) but identical to raw clips on cache hit — both stream from
    disk with native Range-request support.
    """
    import shlex
    import subprocess

    event = None
    for ev in state.recent_events:
        if ev.get("event_id") == event_id:
            event = ev
            break
    if event is None:
        raise HTTPException(404, "event not found")

    ts_sec = event.get("timestamp_sec")
    source_id = event.get("source_id")
    if ts_sec is None or not source_id:
        raise HTTPException(404, "event has no seekable source timestamp")

    slot = state.slots.get(source_id)
    if slot is None or not slot.original_source:
        raise HTTPException(404, "source slot not found")

    source_path = Path(slot.original_source)
    if not source_path.exists() or not source_path.is_file():
        raise HTTPException(
            404, "source is not a local file (live streams can't be clipped)",
        )

    before = max(0.0, min(30.0, float(before)))
    after = max(0.0, min(30.0, float(after)))
    start = max(0.0, float(ts_sec) - before)
    duration = before + after

    clips_dir = DATA_DIR / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    suffix = "_annotated" if annotated else ""
    cache_path = clips_dir / f"{event_id}_{before:g}_{after:g}{suffix}.mp4"

    if not cache_path.exists():
        if annotated:
            try:
                _render_annotated_event_clip(source_path, start, duration, cache_path)
            except FileNotFoundError:
                raise HTTPException(500, "ffmpeg not installed on server")
            except subprocess.TimeoutExpired:
                raise HTTPException(504, "annotated clip extraction timed out")
            except Exception as exc:  # noqa: BLE001
                log.warning("annotated clip extraction failed: %s", exc)
                # Best-effort fallback to a raw ffmpeg cut so the dialog
                # still has something to play if the YOLO/encode pipeline
                # broke (e.g. ultralytics import error in a slim image).
                annotated = False
                cache_path = clips_dir / f"{event_id}_{before:g}_{after:g}.mp4"
        if not annotated and not cache_path.exists():
            # -ss before -i is fast (keyframe seek); -c copy would be fastest but
            # breaks on non-keyframe starts, so we re-encode the short clip.
            cmd = [
                "ffmpeg",
                "-y",
                "-ss", f"{start:.3f}",
                "-i", str(source_path),
                "-t", f"{duration:.3f}",
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-pix_fmt", "yuv420p",
                "-movflags", "+faststart",
                "-an",
                str(cache_path),
            ]
            try:
                subprocess.run(cmd, check=True, capture_output=True, timeout=30)
            except FileNotFoundError:
                raise HTTPException(500, "ffmpeg not installed on server")
            except subprocess.TimeoutExpired:
                raise HTTPException(504, "clip extraction timed out")
            except subprocess.CalledProcessError as exc:
                log.warning("clip extraction failed: %s", exc.stderr[-400:] if exc.stderr else exc)
                raise HTTPException(500, f"clip extraction failed: {shlex.quote(str(exc))[-200:]}")

    return FileResponse(cache_path, media_type="video/mp4")


@app.delete("/api/events")
def clear_events(request: Request):
    """Wipe the in-memory event buffer.

    HTTP: DELETE /api/events
    AUTH: admin bearer
    Returns: ``{"cleared": <n>}`` — count of events removed.

    Events are not persisted server-side (they live in a ring buffer),
    so this is a soft wipe: the next emitted event still appears, but
    everything currently served by ``/api/events`` / ``/api/live/events``
    and replayed on SSE reconnect is gone.
    """
    _require_admin(request, "clear events")
    cleared = len(state.recent_events)
    state.recent_events.clear()
    audit.log("clear_events", "recent_events", outcome="success")
    return {"cleared": cleared}


# ===== SECTION: ROUTE HANDLERS — LLM OBSERVABILITY (ADMIN) =====

@app.get("/api/llm/stats")
def llm_stats(request: Request, window_sec: float | None = None):
    """Aggregated LLM usage: cost, latency percentiles, error/skip rates.

    HTTP: GET /api/llm/stats
    AUTH: admin bearer
    Query params:
        window_sec: Optional rolling window. Defaults to observer's config.
    """
    _require_admin(request, "LLM observability")
    return llm_observer.stats(window_sec)


@app.get("/api/llm/recent")
def llm_recent(request: Request, limit: int = 50):
    """Raw recent LLM call records for debugging.

    HTTP: GET /api/llm/recent
    AUTH: admin bearer
    Query params:
        limit: Max records (capped at 200 server-side).
    """
    _require_admin(request, "LLM observability")
    return {"items": llm_observer.recent(min(limit, 200))}


# ===== SECTION: ROUTE HANDLERS — AUDIT LOG (ADMIN) =====

@app.get("/api/audit")
def api_audit(request: Request, limit: int = 100):
    """Tail of the audit log for compliance review.

    HTTP: GET /api/audit
    AUTH: admin bearer
    Query params:
        limit: Max records (capped at 500 server-side).
    """
    _require_admin(request, "audit log")
    return {"items": audit.tail(min(limit, 500))}


@app.get("/api/audit/stats")
def api_audit_stats(request: Request):
    """Aggregate audit counters (actions, outcomes).

    HTTP: GET /api/audit/stats
    AUTH: admin bearer
    """
    _require_admin(request, "audit log")
    return audit.stats()


# ===== SECTION: ROUTE HANDLERS — DATA RETENTION (ADMIN) =====

@app.post("/api/retention/sweep")
def api_retention_sweep(request: Request):
    """Trigger an immediate retention sweep (normally runs hourly).

    HTTP: POST /api/retention/sweep
    AUTH: admin bearer
    Returns: dict summarising files deleted by the sweep.
    """
    _require_admin(request, "retention control")
    audit.log("retention_sweep", "manual_trigger")
    return retention_sweep()


# ===== SECTION: ROUTE HANDLERS — ROAD / MULTI-VEHICLE (ADMIN) =====

@app.get("/api/road/summary")
def api_road_summary(request: Request):
    """System-wide aggregation: all vehicles, scores, event counts.

    HTTP: GET /api/road/summary
    AUTH: admin bearer
    """
    _require_admin(request, "road summary")
    return road_registry.road_summary()


@app.get("/api/road/vehicle/{vehicle_id}")
def api_road_vehicle(request: Request, vehicle_id: str):
    """Fetch details for a single vehicle.

    HTTP: GET /api/road/vehicle/{vehicle_id}
    AUTH: admin bearer
    Raises: 404 if the vehicle is not known to the registry.
    """
    _require_admin(request, "road vehicle detail")
    v = road_registry.get_vehicle(vehicle_id)
    if v is None:
        raise HTTPException(404, "vehicle not found")
    return v


@app.get("/api/road/drivers")
def api_road_drivers(request: Request, limit: int = 20):
    """Driver safety leaderboard (worst-first).

    HTTP: GET /api/road/drivers
    AUTH: admin bearer
    Query params:
        limit: Max drivers returned (capped at 100 server-side).
    """
    _require_admin(request, "driver leaderboard")
    return {"drivers": road_registry.driver_leaderboard(min(limit, 100))}


# ===== SECTION: ROUTE HANDLERS — AI AGENTS (ADMIN) =====
# Each agent is a bounded-tool LLM loop (see services/agents.py). Tool sets
# are capped at 5 to avoid tool-overload hallucination — do not widen past
# that cap without a specific reason.

@app.post("/api/agents/coaching")
async def api_agent_coaching(request: Request, body: dict):
    """Generate an AI coaching note for a specific event.

    HTTP: POST /api/agents/coaching
    AUTH: admin bearer
    Request body: ``{"event_id": "<id>"}``
    Returns: agent result dict (narrative + metadata).
    """
    _require_admin(request, "agent coaching")
    event_id = (body.get("event_id") or "").strip()
    if not event_id:
        raise HTTPException(400, "missing 'event_id'")
    if state.agent_executor is None:
        raise HTTPException(503, "agent executor not ready")
    audit.log("agent_coaching", event_id)
    result = await run_coaching_agent(state.agent_executor, event_id)
    return result.as_dict()


@app.post("/api/agents/investigation")
async def api_agent_investigation(request: Request, body: dict):
    """Run an AI investigation on a specific event.

    HTTP: POST /api/agents/investigation
    AUTH: admin bearer
    Request body: ``{"event_id": "<id>"}``
    """
    _require_admin(request, "agent investigation")
    event_id = (body.get("event_id") or "").strip()
    if not event_id:
        raise HTTPException(400, "missing 'event_id'")
    if state.agent_executor is None:
        raise HTTPException(503, "agent executor not ready")
    audit.log("agent_investigation", event_id)
    result = await run_investigation_agent(state.agent_executor, event_id)
    return result.as_dict()


@app.post("/api/agents/report")
async def api_agent_report(request: Request):
    """Generate an AI safety summary report for the current session.

    HTTP: POST /api/agents/report
    AUTH: admin bearer
    """
    _require_admin(request, "agent report")
    if state.agent_executor is None:
        raise HTTPException(503, "agent executor not ready")
    audit.log("agent_report", "session_report")
    result = await run_report_agent(state.agent_executor)
    return result.as_dict()


# ===== SECTION: ROUTE HANDLERS — ADMIN DASHBOARD (video, health, SSE) =====
# These endpoints feed the React admin dashboard. Some are intentionally
# unauthenticated (``admin_health``, ``admin_video_feed``) because the
# dashboard itself is served behind an operator network; others are
# admin-gated where they would leak sensitive state.

@app.get("/api/admin/health")
def admin_health():
    """Comprehensive health snapshot for the admin dashboard.

    HTTP: GET /api/admin/health
    AUTH: public (dashboard metadata only)
    Returns: nested dict with server / pipeline / integrations /
        perception / scene / ego sub-objects — designed for direct
        rendering in the admin health panel.
    """
    q = state.quality.state()
    ctx = state.last_scene_ctx
    ego = state.last_ego_flow
    reader = state.reader
    return {
        "server": {
            "running": reader is not None and reader._thread is not None and reader._thread.is_alive(),
            "uptime_sec": round(reader.uptime_sec(), 1) if reader else 0.0,
            "started_at": reader.started_at if reader else None,
            "source": state.source_label,
            "location": LOCATION,
            "target_fps": TARGET_FPS,
        },
        "pipeline": {
            "frames_read": reader.frames_read if reader else 0,
            "frames_processed": reader.frames_processed if reader else 0,
            "event_count": len(state.recent_events),
            "active_episodes": len(state.episodes),
            "tracker": "bytetrack",
            "risk_model": "ttc+ground_plane",
            "model": Path(MODEL_PATH).name,
        },
        "integrations": {
            "llm_configured": llm_configured(),
            "slack_configured": slack_configured(),
            "edge_publisher": state.edge_publisher.enabled(),
            "pii_redaction": "face+plate",
            "dsar_endpoint": bool(DSAR_TOKEN),
            "public_thumb_token_required": PUBLIC_THUMBS_REQUIRE_TOKEN,
            "alpr_mode": ALPR_MODE,
        },
        "perception": {
            "state": q["state"],
            "reason": q["reason"],
            "samples": q["samples"],
            "avg_confidence": q["avg_confidence"],
            "luminance": q["luminance"],
            "sharpness": q["sharpness"],
        },
        "scene": {
            "label": ctx.label if ctx else "unknown",
            "confidence": round(ctx.confidence, 2) if ctx else None,
            "speed_proxy_mps": round(ctx.speed_proxy_mps, 2) if ctx and ctx.speed_proxy_mps is not None else None,
            "reason": ctx.reason if ctx else "not yet observed",
        },
        "ego": {
            "speed_proxy_mps": round(ego.speed_proxy_mps, 2) if ego else None,
            "confidence": round(ego.confidence, 2) if ego else None,
        },
    }


# ===== SECTION: ROUTE HANDLERS — WATCHDOG (INCIDENT QUEUE) =====


@app.get("/api/watchdog")
def watchdog_summary():
    """Watchdog status and finding counts.

    HTTP: GET /api/watchdog
    AUTH: public
    Returns: ``{"enabled": False}`` when the watchdog was disabled at
        startup, otherwise a status dict from ``Watchdog.status()``.
    """
    if state.watchdog is None:
        return {"enabled": False}
    return state.watchdog.status()


@app.get("/api/validator/status")
def validator_status():
    """Background shadow-validator worker status.

    HTTP: GET /api/validator/status
    AUTH: public
    Returns: ``{"enabled": False}`` when the validator was disabled at
        startup, otherwise queue depth + processed/dropped counters from
        ``ValidatorWorker.status()``.
    """
    if state.validator is None:
        return {"enabled": False}
    return {"enabled": True, **state.validator.status()}


@app.post("/api/validator/toggle")
async def validator_toggle(request: Request):
    """Enable or disable the shadow validator at runtime.

    HTTP: POST /api/validator/toggle
    AUTH: public (read-only toggle of a background observability job;
        does not affect live alerts).
    Body: ``{"enabled": true|false}`` — ``true`` resumes accepting shadow
        jobs, ``false`` pauses enqueue without tearing down the worker
        (so model weights stay loaded for fast resume).
    Returns: the updated ``/api/validator/status`` payload.
    Raises:
        409 when the validator was disabled at startup (``state.validator``
            is ``None``) — nothing to toggle.
    """
    if state.validator is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "validator was disabled at startup; set ROAD_VALIDATOR_ENABLED=1 "
                "and restart to enable runtime toggling"
            ),
        )
    body = await request.json()
    enabled = bool(body.get("enabled", True))
    state.validator.set_paused(not enabled)
    log.info("validator %s by operator", "resumed" if enabled else "paused")
    return {"enabled": True, **state.validator.status()}


@app.get("/api/watchdog/recent")
def watchdog_recent(n: int = 50):
    """Most recent watchdog findings for investigation.

    HTTP: GET /api/watchdog/recent
    AUTH: public
    Query params:
        n: Max findings (capped at 200).
    """
    return watchdog_tail(min(n, 200))


@app.delete("/api/watchdog/findings")
def watchdog_delete_findings(request: Request, clear_all: bool = False):
    """Delete specific findings by composite key or clear all.

    HTTP: DELETE /api/watchdog/findings
    AUTH: admin Bearer (Settings Console S0 prereq hardening)
    Query params:
        clear_all: If true, wipe every finding.
    Returns: ``{"deleted": <count>}``.
    """
    require_bearer_token(request, ADMIN_TOKEN, realm="watchdog", env_var="ROAD_ADMIN_TOKEN")
    if clear_all:
        removed = watchdog_delete(indices=None)
        return {"deleted": removed}
    return {"deleted": 0}


@app.post("/api/watchdog/findings/delete")
async def watchdog_delete_selected(request: Request):
    """Delete selected findings by snapshot_id + ts composite keys.

    HTTP: POST /api/watchdog/findings/delete
    AUTH: admin Bearer (Settings Console S0 prereq hardening)
    Request body: ``{"keys": ["<snapshot_id>:<ts>", ...]}``.
    Returns: ``{"deleted": <count>}``.
    """
    require_bearer_token(request, ADMIN_TOKEN, realm="watchdog", env_var="ROAD_ADMIN_TOKEN")
    body = await request.json()
    keys: list[str] = body.get("keys", [])
    if not keys:
        return {"deleted": 0}
    removed = watchdog_delete_by_id(keys)
    return {"deleted": removed}


# ===== SECTION: ROUTE HANDLERS — SPA PAGE PASSTHROUGHS =====
# All these paths serve the same React SPA entrypoint; the router inside
# the app handles the actual page switch.


@app.get("/admin")
def admin_page():
    """Serve the admin SPA page.

    HTTP: GET /admin
    AUTH: public (page shell only; data endpoints enforce their own auth)
    """
    return FileResponse(STATIC_DIR / "index.html")

@app.get("/dashboard")
def dashboard_page():
    """Serve the dashboard SPA page.

    HTTP: GET /dashboard
    AUTH: public
    """
    return FileResponse(STATIC_DIR / "index.html")

@app.get("/monitoring")
def monitoring_page():
    """Serve the monitoring (watchdog incident queue) SPA page.

    HTTP: GET /monitoring
    AUTH: public
    """
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/settings")
def settings_page():
    """Serve the Settings Console SPA shell.

    HTTP: GET /settings
    AUTH: public (page shell only; every ``/api/settings/*`` data endpoint
                  is admin-bearer gated and the SPA prompts for the token).
    """
    return FileResponse(STATIC_DIR / "index.html")


def _make_placeholder_jpeg() -> bytes:
    """Build a small dark-grey 'Warming up…' placeholder JPEG once at import.

    The MJPEG generator emits this placeholder to slots that have not yet
    published their first annotated frame. Without it, browsers loading the
    multi-source admin page during boot see a connection with no data,
    time out, and render a black tile that never recovers — even after the
    slot starts producing frames a few seconds later.
    """
    import numpy as np  # local import: avoid pulling numpy on bare module load

    # 320x180 dark-grey canvas with a centred "WARMING UP" caption.
    img = np.full((180, 320, 3), 28, dtype=np.uint8)
    cv2.putText(
        img,
        "WARMING UP…",
        (50, 100),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.9,
        (180, 180, 180),
        2,
        cv2.LINE_AA,
    )
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
    if not ok:
        # Defensive fallback: minimal valid JPEG if encoding fails for any reason.
        return (
            b"\xff\xd8\xff\xdb\x00\x43\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07"
            b"\x07\x09\x09\x08\x0a\x0c\x14\x0d\x0c\x0b\x0b\x0c\x19\x12\x13\x0f"
            b"\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c\x20\x24\x2e\x27\x20\x22\x2c"
            b"\x23\x1c\x1c\x28\x37\x29\x2c\x30\x31\x34\x34\x34\x1f\x27\x39\x3d"
            b"\x38\x32\x3c\x2e\x33\x34\x32\xff\xd9"
        )
    return buf.tobytes()


# Build once; reused by every slot's MJPEG generator while it warms up.
_WARMING_UP_JPEG: bytes = _make_placeholder_jpeg()


def _mjpeg_response(slot: StreamSlot) -> StreamingResponse:
    """Build an MJPEG ``StreamingResponse`` reading from ``slot``'s buffer.

    Shared by the legacy primary-only ``/admin/video_feed`` endpoint and
    the new per-source ``/admin/video_feed/{source_id}`` endpoint.

    Behaviour: while the slot has not yet published its first annotated
    frame, we send a "Warming up…" placeholder JPEG instead of nothing.
    That keeps the browser's MJPEG connection alive so the very next real
    frame swaps in cleanly — without it, slots that finish booting after
    the page loads strand the browser on a stalled stream that renders as
    a black tile for the rest of the session.
    """

    def generate():
        # Announce ourselves as an active viewer so the perception loop
        # actually produces annotated JPEGs for this slot. When the client
        # disconnects, StreamingResponse closes the generator which fires
        # the finally block below and releases the viewer slot — so the
        # encode cost stops the moment the tile is unmounted.
        slot._acquire_viewer()
        try:
            # Emit the placeholder ONCE up front so the <img> tag receives
            # data immediately — even browsers that time out on a 4 s
            # no-data stream stay connected.
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n" + _WARMING_UP_JPEG + b"\r\n"
            )
            sent_real = False
            while True:
                with slot._frame_lock:
                    jpeg = slot._annotated_jpeg
                if jpeg is not None:
                    yield (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"
                    )
                    sent_real = True
                elif not sent_real:
                    # Still warming up — keep the placeholder visible (and the
                    # connection alive) instead of yielding nothing.
                    yield (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n\r\n" + _WARMING_UP_JPEG + b"\r\n"
                    )
                # ~0.4s matches the 2fps perception tick; faster would resend
                # identical frames.
                time.sleep(0.4)
        finally:
            slot._release_viewer()

    return StreamingResponse(
        generate(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@app.get("/admin/video_feed")
def admin_video_feed():
    """MJPEG stream of annotated frames for the PRIMARY source.

    HTTP: GET /admin/video_feed
    AUTH: public (network-gated operator UI)
    Response: ``multipart/x-mixed-replace`` stream — each JPEG part is a
        freshly-annotated frame. Consumer renders in an ``<img>`` tag.

    For multi-source UIs prefer ``/admin/video_feed/{source_id}``.
    """
    return _mjpeg_response(state.primary_slot)


@app.get("/admin/video_feed/{source_id}")
def admin_video_feed_for(source_id: str):
    """Per-source MJPEG stream — one slot's annotated frames.

    HTTP: GET /admin/video_feed/{source_id}
    AUTH: public (network-gated operator UI)
    """
    slot = state.slots.get(source_id)
    if slot is None:
        raise HTTPException(404, f"unknown source: {source_id}")
    return _mjpeg_response(slot)


@app.get("/admin/frame/{source_id}")
def admin_frame_for(source_id: str):
    # Single-shot JPEG for the admin grid's polling renderer. MJPEG holds a
    # persistent multipart connection per tile, which bumps into the browser's
    # 6-concurrent-connections-per-host cap once you have >4 tiles + the SSE
    # channel open. Polling short-lived JPEGs dodges that cap.
    slot = state.slots.get(source_id)
    if slot is None:
        raise HTTPException(404, f"unknown source: {source_id}")
    # Signal to ``_on_frame`` that this slot has a viewer, so the encode path
    # actually runs. Without this the cached jpeg stays ``None`` forever.
    slot.mark_polled()
    with slot._frame_lock:
        jpeg = slot._annotated_jpeg
        # Capture a reference under the lock; only used if jpeg is missing.
        raw = slot._latest_raw_frame if jpeg is None else None
    # Encode-on-demand fallback. Covers two cases that previously made non-
    # primary tiles look broken:
    #   * First few polls after page-load arrive BEFORE the slot's first
    #     ``_on_frame`` tick has produced an annotated JPEG.
    #   * Under heavy contention (6 streams sharing one YOLO instance), the
    #     annotated encode lags behind the poll cadence; without this we'd
    #     return 503 and the <img> onError would mark the tile permanently
    #     errored, hiding the live feed even after fresh frames arrived.
    # Encoded without bbox overlays — operators still see the live video,
    # detections appear as soon as the next annotated tick lands.
    if jpeg is None and raw is not None:
        try:
            ok, buf = cv2.imencode(".jpg", raw, [cv2.IMWRITE_JPEG_QUALITY, 70])
            if ok:
                jpeg = buf.tobytes()
        except Exception as exc:
            log.warning("on-demand frame encode failed (%s): %s", source_id, exc)
    if jpeg is None:
        # Still nothing — stream truly hasn't produced its first frame. Send
        # the warming-up placeholder so the <img> onError doesn't fire and
        # poison the tile; the next poll will pick up a real frame.
        jpeg = _WARMING_UP_JPEG
    return Response(
        content=jpeg,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )


# ===== SECTION: ROUTE HANDLERS — MULTI-SOURCE LIFECYCLE =====


@app.get("/api/live/sources")
def live_sources():
    """List every configured source with running status + counters.

    HTTP: GET /api/live/sources
    AUTH: public (the same status info is on ``/api/live/status``)
    """
    return {
        "primary_id": state.PRIMARY_ID,
        "sources": [slot.status_dict() for slot in state.slots.values()],
    }


@app.post("/api/live/sources/{source_id}/start")
def live_source_start(source_id: str):
    """Resume capture for a paused source.

    HTTP: POST /api/live/sources/{source_id}/start
    AUTH: public (operator network)
    Returns: the slot's status dict, with ``running=true`` on success
        or ``last_error`` populated on failure.

    Semantics:
        - If the slot's reader is alive but paused → flip the pause gate
          off and continue from the preserved playback position.
        - Otherwise (never started, or fully stopped) → spawn a fresh
          reader from frame 0.
    """
    slot = state.slots.get(source_id)
    if slot is None:
        raise HTTPException(404, f"unknown source: {source_id}")
    if slot.is_running():
        return {"ok": True, "already_running": True, **slot.status_dict()}
    # Prefer resume-in-place over a fresh reader so the MP4 picks up where
    # it was paused instead of rewinding to frame 0.
    if _resume_slot(slot):
        audit.log("stream_resume", source_id)
        return {"ok": True, "resumed": True, **slot.status_dict()}
    try:
        _start_slot(slot)
    except Exception as exc:
        slot.last_error = str(exc)
        log.warning("start slot %s failed: %s", source_id, exc)
        # Surface the failure in the response so the UI can render it
        # next to the start button without polling for status.
        return {"ok": False, "error": str(exc), **slot.status_dict()}
    audit.log("stream_start", source_id)
    return {"ok": True, **slot.status_dict()}


@app.post("/api/live/sources/{source_id}/pause")
def live_source_pause(source_id: str):
    """Pause capture for a running source (slot is preserved for restart).

    HTTP: POST /api/live/sources/{source_id}/pause
    AUTH: public (operator network)

    Unlike the prior implementation this does NOT tear down the reader —
    it just flips the pause gate so the capture thread sits idle without
    releasing the VideoCapture handle. That preserves MP4 playback
    position so the next Start resumes in place.
    """
    slot = state.slots.get(source_id)
    if slot is None:
        raise HTTPException(404, f"unknown source: {source_id}")
    if not _pause_slot(slot):
        # Reader wasn't alive to begin with — nothing to pause. Treat as
        # idempotent: the UI's optimistic "paused" state is already correct.
        pass
    audit.log("stream_pause", source_id)
    return {"ok": True, **slot.status_dict()}


@app.post("/api/live/sources/restart_all")
def live_source_restart_all():
    """Restart every slot from the beginning (demo reset).

    HTTP: POST /api/live/sources/restart_all
    AUTH: public (operator network)

    For each slot: stop the running reader (if any) and start a fresh one.
    For MP4 demo sources this rewinds capture to frame 0; for live feeds
    it reconnects. ``slot.started_at`` is replaced so ``uptime_sec``
    re-zeroes, which the frontend uses as the map-playhead reset signal.
    Per-source perception state is intentionally preserved — the scene
    classifier and quality monitor don't need to re-learn an unchanged
    camera.
    """
    results: list[dict] = []
    for sid, slot in list(state.slots.items()):
        if not slot.original_source:
            # Skip placeholder / empty slots — nothing to restart.
            continue
        if slot.is_running():
            _stop_slot(slot)
        try:
            _start_slot(slot)
            results.append({"id": sid, "ok": True, **slot.status_dict()})
        except Exception as exc:
            slot.last_error = str(exc)
            log.warning("restart_all: slot %s failed: %s", sid, exc)
            results.append({
                "id": sid, "ok": False, "error": str(exc), **slot.status_dict(),
            })
    audit.log("stream_restart_all", "all", detail={"count": len(results)})
    return {"ok": True, "results": results}


def _slugify_id(seed: str) -> str:
    """Build a short, URL-safe id from a seed string (e.g. a stream URL or file path).

    Used when the operator adds a stream without supplying an explicit id.
    Output is alphanumeric only, max 24 chars, prefixed ``user_`` so it
    can't collide with the env-configured ``primary`` / ``srcN`` ids.
    """
    keep = "".join(c for c in seed if c.isalnum())
    if not keep:
        keep = "stream"
    return f"user_{keep[-12:].lower()}"


def _unique_slot_id(seed: str) -> str:
    """Return ``_slugify_id(seed)`` adjusted with a numeric suffix if needed."""
    base = _slugify_id(seed)
    if base not in state.slots:
        return base
    for n in range(2, 100):
        cand = f"{base}_{n}"
        if cand not in state.slots:
            return cand
    # Fallback — ms-since-epoch suffix; effectively guaranteed unique.
    return f"{base}_{int(time.time() * 1000)}"


@app.post("/api/live/sources")
async def live_source_add(request: Request):
    """Register a new perception source from a URL the operator pastes.

    HTTP: POST /api/live/sources
    Body (JSON): ``{"url": "<stream url>", "name"?: "<display name>",
                   "id"?: "<explicit slot id>", "autostart"?: bool}``
    AUTH: public (operator network)

    The slot is held in-memory only — it survives until the next process
    restart. To make it permanent, add the URL to ``ROAD_STREAM_SOURCES``
    in ``.env``.

    Returns the slot's status dict on success, or ``{ok: false, error}``
    when ``autostart`` is true and the resolver fails (slot is still
    created so the operator can retry from the Start button).
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "expected JSON body")
    url = (body.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "missing 'url'")
    # Permissive prefix check — OpenCV rejects the rest. Catches
    # accidental bare-host paste without a scheme.
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(400, "url must start with http:// or https://")

    requested_id = (body.get("id") or "").strip()
    if requested_id:
        if requested_id in state.slots:
            raise HTTPException(409, f"id already in use: {requested_id}")
        sid = requested_id
    else:
        sid = _unique_slot_id(url)

    name = (body.get("name") or "").strip() or f"Custom ({sid})"
    autostart = bool(body.get("autostart", True))

    slot = StreamSlot(sid, name, url)
    state.slots[sid] = slot
    audit.log("stream_add", sid, detail={"url": url[:200], "name": name})

    if autostart:
        try:
            _start_slot(slot)
        except Exception as exc:
            slot.last_error = str(exc)
            log.warning("autostart of %s failed: %s", sid, exc)
            return {"ok": False, "error": str(exc), **slot.status_dict()}

    return {"ok": True, **slot.status_dict()}


@app.delete("/api/live/sources/{source_id}")
def live_source_remove(source_id: str):
    """Stop the slot and drop it from the registry.

    HTTP: DELETE /api/live/sources/{source_id}
    AUTH: public (operator network)
    Removing the primary slot is allowed — the legacy ``state.X``
    properties will simply delegate to whichever slot remains. Removing
    the *last* slot leaves the registry empty; the proxy then returns
    None for ``state.reader`` etc., which existing code already tolerates.
    """
    slot = state.slots.get(source_id)
    if slot is None:
        raise HTTPException(404, f"unknown source: {source_id}")
    _stop_slot(slot)
    state.slots.pop(source_id, None)
    audit.log("stream_remove", source_id)
    return {"ok": True, "removed": source_id}


@app.post("/api/live/sources/{source_id}/detection")
def live_source_set_detection(source_id: str, enabled: bool = True):
    """Toggle whether YOLO + event emission runs for a source.

    HTTP: POST /api/live/sources/{source_id}/detection?enabled=true|false
    AUTH: public (operator network)
    Effect: when ``enabled=false`` the slot keeps reading frames (so the
        live preview stays up) but ``_on_frame`` short-circuits before
        running YOLO / quality / scene / episode logic. Toggling back to
        true picks up on the next frame; per-source perception state is
        preserved (not reset) across the toggle.
    """
    slot = state.slots.get(source_id)
    if slot is None:
        raise HTTPException(404, f"unknown source: {source_id}")
    slot.detection_enabled = bool(enabled)
    audit.log(
        "stream_detection_enabled" if slot.detection_enabled else "stream_detection_disabled",
        source_id,
    )
    return {"ok": True, **slot.status_dict()}


@app.get("/admin/detections")
async def admin_detections_sse(request: Request):
    """SSE stream of per-frame detection snapshots for the admin dashboard.

    HTTP: GET /admin/detections
    AUTH: public (network-gated operator UI)
    Response: ``text/event-stream`` of JSON snapshots — frame counters
        plus object bounding boxes. Much lighter than the MJPEG feed,
        suitable for charts / counters.
    """
    # Smaller queue cap than the safety-event SSE — these messages are
    # per-frame at 2Hz; a client that's 50 frames behind already lost
    # 25 seconds of data and should reconnect anyway.
    queue: asyncio.Queue = asyncio.Queue(maxsize=50)
    state.admin_detection_subscribers.add(queue)

    async def gen():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=10.0)
                    yield f"data: {json.dumps(msg)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            state.admin_detection_subscribers.discard(queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ===== SECTION: ROUTE HANDLERS — TEST RUNNER =====

@app.get("/api/tests/status")
def api_test_status():
    """Current test run status and results.

    HTTP: GET /api/tests/status
    AUTH: public (dashboard action)
    """
    return test_run_state.as_dict()


@app.post("/api/tests/run")
def api_test_run():
    """Trigger a new test run (if not already running).

    HTTP: POST /api/tests/run
    AUTH: public (dashboard action)
    Returns: ``{"ok": True}`` when started, or ``{"ok": False,
        "reason": "already running"}`` when a run is already in flight.
    """
    if test_run_state.status == "running":
        return {"ok": False, "reason": "already running"}
    start_test_run()
    return {"ok": True}


# ===== SECTION: ROUTE HANDLERS — BATCH SUMMARY (LEGACY) =====

@app.get("/api/summary")
def summary():
    """Serve the offline batch summary JSON produced by ``analyze.py``.

    HTTP: GET /api/summary
    AUTH: public
    Raises: 404 if ``summary.json`` has not been produced yet.
    """
    return _load_batch("summary.json")
