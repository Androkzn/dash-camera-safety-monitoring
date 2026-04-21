"""Background dual-model validator — shadow-mode second opinion on the primary YOLO.

Role
----
This module implements the secondary, heavier detector that shadows the
primary real-time perception pipeline. It never gates live alerts. Its
findings land in the existing watchdog incident queue under the
``validator`` category so operators can see where the primary disagrees
with a stronger model.

Shadow mode (standard ML validation pattern)
--------------------------------------------
The production model (YOLOv8n + gate chain) keeps serving real traffic
end-to-end. The challenger runs asynchronously off a bounded queue,
re-processes the peak frame of every emitted episode (deep re-check),
and also samples "quiet" frames at a slow cadence to look for events
the primary missed.

Three disagreement classes are reported:

* ``validator/false-positive``  — primary emitted an event; secondary
  could not corroborate the key objects (no IoU match ≥ threshold).
* ``validator/false-negative``  — on a sampled frame with no active
  primary event, the secondary finds a would-be-risky pair the primary
  never flagged.
* ``validator/classification-mismatch`` — both models see the objects
  but disagree on class or risk bucket.

Design constraints
------------------
* The perception hot path must not be blocked by this module.
  ``enqueue_*`` calls are O(1) non-blocking dict/queue operations on the
  primary thread; all heavy work runs in the worker via
  ``asyncio.to_thread``.
* The queue is bounded (``VALIDATOR_QUEUE_MAX``). Under pressure, new
  jobs are dropped silently (logged as skip records on the LLM observer
  so operators can see the drop rate).
* No new persistence store. Findings go through
  :func:`road_safety.services.watchdog._write_finding`.
"""

import asyncio
import collections
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from road_safety.config import (
    CameraCalibration,
    VALIDATOR_BACKEND,
    VALIDATOR_DEVICE,
    VALIDATOR_IOU_THRESHOLD,
    VALIDATOR_MODEL_PATH,
    VALIDATOR_QUEUE_MAX,
    VALIDATOR_SAMPLE_SEC,
)
from road_safety.core.detection import (
    PEDESTRIAN_CLASSES,
    VEHICLE_CLASSES,
    VEHICLE_INTER_DISTANCE_GATE_M,
    Detection,
    bbox_edge_distance,
    classify_risk,
    estimate_inter_distance_m,
    find_interactions,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Job records (producer → worker)
# ---------------------------------------------------------------------------


@dataclass
class ValidatorJob:
    """One unit of work for the background worker.

    Two kinds:

    * ``kind="episode"``: re-check the peak frame of a just-emitted
      primary event. ``primary_event`` is the full event dict. Used by
      rules A (false-positive) and C (classification mismatch).
    * ``kind="sampled"``: inspect a quiet frame at the sampling cadence.
      ``primary_detections`` is whatever the primary produced on that
      frame (may be empty). Used by rule B (false-negative).
    """

    kind: str
    slot_id: str
    wall_ts: float
    frame: Any  # np.ndarray BGR; not typed precisely to avoid a hard numpy runtime dep on import
    primary_detections: list[Detection] = field(default_factory=list)
    primary_event: Optional[dict] = None
    # Per-slot camera calibration carried into the worker so the secondary
    # depth math sees the same focal/height/horizon/offset the primary did.
    # Without it, validator distance estimates use the global default and
    # disagreements fire purely from a calibration mismatch instead of a
    # real model disagreement.
    calibration: Optional[CameraCalibration] = None


# ---------------------------------------------------------------------------
# Secondary detector backend
# ---------------------------------------------------------------------------


class SecondaryDetector:
    """Heavy background detector.

    Default backend is ultralytics RT-DETR (``rtdetr-l.pt``) — drop-in
    with the same ``.track()`` / ``__call__`` surface as YOLO. Other
    backends (Co-DETR, RF-DETR) would need extra deps and are not
    implemented in this drop.
    """

    def __init__(
        self,
        backend: str = VALIDATOR_BACKEND,
        model_path: str = VALIDATOR_MODEL_PATH,
        device: str = VALIDATOR_DEVICE,
    ) -> None:
        self.backend = backend
        self.model_path = model_path
        self.device = device or ""
        self._model = None  # lazy load

    def load(self) -> None:
        """Load the underlying weights. Safe to call once at worker start."""
        if self._model is not None:
            return
        if self.backend not in ("rtdetr", "yolo"):
            raise NotImplementedError(
                f"validator backend {self.backend!r} is not wired up; "
                "set ROAD_VALIDATOR_BACKEND=rtdetr (or yolo)"
            )
        # IMPORTANT: ``ultralytics.YOLO("rtdetr-l.pt")`` does NOT correctly
        # post-process RT-DETR outputs in ultralytics ≥ 8.3 — the boxes
        # carry decoder-query indices (e.g. 166, 256) instead of COCO-80
        # class IDs, so almost every detection is dropped by the
        # in-range/class-allowlist filter and the secondary effectively
        # goes blind. Use the dedicated ``RTDETR`` task class for those
        # weights; it owns the matching head + post-processing.
        from ultralytics import RTDETR, YOLO  # lazy — keep cold-start cheap

        log.info(
            "validator: loading secondary detector backend=%s path=%s device=%s",
            self.backend,
            self.model_path,
            self.device or "auto",
        )
        model_cls = RTDETR if self.backend == "rtdetr" else YOLO
        model = model_cls(self.model_path)
        if self.device:
            try:
                model.to(self.device)
            except Exception as exc:
                log.warning(
                    "validator: .to(%s) failed, using default device: %s",
                    self.device,
                    exc,
                )
        self._model = model

    def predict(self, frame) -> list[Detection]:
        """Run one inference pass and return our project-standard Detection list.

        Applies only class and min-confidence filters — no multi-gate
        chain. The comparator layer is what turns raw boxes into
        disagreements.
        """
        if self._model is None:
            self.load()
        if self._model is None:
            return []
        results = self._model(frame, verbose=False)[0]
        names = results.names
        out: list[Detection] = []
        boxes = results.boxes
        if boxes is None:
            return out
        for box in boxes:
            # Some ultralytics RT-DETR builds emit class indices beyond the
            # COCO-80 ``names`` dict (decoder-query ids / wrong-head check-
            # point). Skip instead of crashing so the comparator still runs.
            cls_id = int(box.cls)
            cls = (
                names.get(cls_id) if isinstance(names, dict)
                else (names[cls_id] if 0 <= cls_id < len(names) else None)
            )
            if cls is None:
                continue
            if cls not in VEHICLE_CLASSES and cls not in PEDESTRIAN_CLASSES:
                continue
            conf = float(box.conf)
            # Secondary keeps a generous confidence floor — its whole job
            # is to find what the primary missed, so over-filtering here
            # would defeat the purpose.
            if conf < 0.20:
                continue
            x1, y1, x2, y2 = (int(v) for v in box.xyxy[0].tolist())
            out.append(Detection(cls=cls, conf=conf, x1=x1, y1=y1, x2=x2, y2=y2))
        return out


# ---------------------------------------------------------------------------
# Discrepancy comparator
# ---------------------------------------------------------------------------


def _iou(a: Detection, b: Detection) -> float:
    """Intersection-over-union between two detection bboxes.

    Returns 0.0 when boxes do not overlap.
    """
    ix1 = max(a.x1, b.x1)
    iy1 = max(a.y1, b.y1)
    ix2 = min(a.x2, b.x2)
    iy2 = min(a.y2, b.y2)
    iw = max(ix2 - ix1, 0)
    ih = max(iy2 - iy1, 0)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = max(a.x2 - a.x1, 0) * max(a.y2 - a.y1, 0)
    area_b = max(b.x2 - b.x1, 0) * max(b.y2 - b.y1, 0)
    union = area_a + area_b - inter
    if union <= 0:
        return 0.0
    return inter / union


def _best_iou_match(
    target: Detection,
    pool: list[Detection],
) -> tuple[Optional[Detection], float]:
    """Return (best-matching-det, iou) for ``target`` against ``pool``."""
    best: Optional[Detection] = None
    best_iou = 0.0
    for cand in pool:
        score = _iou(target, cand)
        if score > best_iou:
            best_iou = score
            best = cand
    return best, best_iou


def _humanize(value: str) -> str:
    """snake_case → "Snake case". Used only for operator-facing prose in
    ``Discrepancy.detail``; evidence fields keep their raw machine values.
    """
    if not value:
        return "?"
    return value.replace("_", " ")


@dataclass
class Discrepancy:
    """One comparator output.

    Translated directly into a ``WatchdogFinding`` by the worker before
    ``_write_finding()`` is called.

    ``pair`` / ``secondary_risk`` / ``distance_m`` / ``distance_px``
    are populated only for ``kind="false_negative"`` so the worker can
    persist the shadow record (frame + detections) that backs the
    finding. They are ignored by the other two rule outputs.
    """

    kind: str  # "false_positive" | "false_negative" | "classification_mismatch"
    severity: str
    title: str
    detail: str
    fingerprint: str
    evidence: list[dict[str, str]] = field(default_factory=list)
    pair: Optional[tuple["Detection", "Detection"]] = None
    secondary_risk: str = ""
    distance_m: Optional[float] = None
    distance_px: float = 0.0
    event_type: str = ""


class DiscrepancyComparator:
    """Stateless comparator that produces zero or more ``Discrepancy`` per job.

    Pure logic — no disk, no network, no model calls. Easy to unit-test
    with hand-built ``Detection`` lists.
    """

    def __init__(self, iou_threshold: float = VALIDATOR_IOU_THRESHOLD) -> None:
        self.iou_threshold = iou_threshold

    # ---- Rule A: false positive -------------------------------------
    def check_false_positive(
        self,
        primary_event: dict,
        primary_detections: list[Detection],
        secondary_detections: list[Detection],
    ) -> Optional[Discrepancy]:
        """Primary raised an event; does the secondary see the key objects?

        We require the *two* primary-pair detections (selected from
        ``primary_detections`` by the event's ``track_ids``/``objects``)
        to both have IoU ≥ threshold against some secondary box. If
        either fails, the secondary failed to corroborate and we flag a
        false positive.
        """
        pair = self._pair_from_event(primary_event, primary_detections)
        if pair is None:
            return None
        a, b = pair
        match_a, iou_a = _best_iou_match(a, secondary_detections)
        match_b, iou_b = _best_iou_match(b, secondary_detections)
        if iou_a >= self.iou_threshold and iou_b >= self.iou_threshold:
            return None
        return Discrepancy(
            kind="false_positive",
            severity="warning",
            title="Secondary model cannot corroborate primary event",
            detail=(
                f"Primary emitted {_humanize(primary_event.get('event_type','?'))} at "
                f"{primary_event.get('risk_level','?')} risk, but the secondary "
                f"detector found no matching object pair "
                f"(best IoU: {iou_a:.2f}, {iou_b:.2f}; need ≥ {self.iou_threshold:.2f})."
            ),
            fingerprint="validator/false-positive",
            evidence=[
                {"label": "primary_event_id", "value": str(primary_event.get("event_id", ""))},
                {"label": "primary_event_type", "value": str(primary_event.get("event_type", ""))},
                {"label": "primary_risk", "value": str(primary_event.get("risk_level", ""))},
                {"label": "best_iou_a", "value": f"{iou_a:.3f}"},
                {"label": "best_iou_b", "value": f"{iou_b:.3f}"},
                {"label": "secondary_detections", "value": str(len(secondary_detections))},
            ],
        )

    # ---- Rule B: false negative --------------------------------------
    def check_false_negative(
        self,
        frame_height: int,
        primary_detections: list[Detection],
        secondary_detections: list[Detection],
        primary_emitted_recently: bool,
        *,
        calibration: Optional[CameraCalibration] = None,
    ) -> Optional[Discrepancy]:
        """On a sampled frame, did the secondary find a risky pair the primary missed?

        We re-run the primary's *own* interaction + distance gates on
        the secondary's bboxes. If those gates would have produced a
        medium-or-high risk pair AND the primary produced nothing for
        this slot in the surrounding window, we flag it as a miss.
        """
        if primary_emitted_recently:
            return None
        interactions = find_interactions(secondary_detections)
        if not interactions:
            return None

        for event_type, a, b, distance_px in interactions:
            dist_m = estimate_inter_distance_m(a, b, frame_height, calibration=calibration)
            if (
                event_type == "vehicle_close_interaction"
                and dist_m is not None
                and dist_m > VEHICLE_INTER_DISTANCE_GATE_M
            ):
                continue
            # No TTC (we don't have per-track history for secondary boxes
            # in this mode). classify_risk handles ``ttc_sec=None`` fine.
            risk = classify_risk(
                ttc_sec=None,
                distance_m=dist_m,
                fallback_px=distance_px,
            )
            if risk == "low":
                continue
            # Also require the primary to not already have an overlapping
            # detection for *either* object — otherwise we're just
            # re-flagging a pair the primary saw but gated out for
            # other legitimate reasons (convergence, ego-relative motion).
            _, iou_pa = _best_iou_match(a, primary_detections)
            _, iou_pb = _best_iou_match(b, primary_detections)
            if iou_pa >= self.iou_threshold and iou_pb >= self.iou_threshold:
                continue
            return Discrepancy(
                kind="false_negative",
                severity="warning",
                title="Secondary model found event primary did not emit",
                detail=(
                    f"Secondary detector flagged a {_humanize(event_type)} at {risk} risk "
                    f"on a frame where the primary produced no matching event."
                ),
                fingerprint="validator/false-negative",
                evidence=[
                    {"label": "event_type", "value": event_type},
                    {"label": "secondary_risk", "value": risk},
                    {"label": "distance_px", "value": f"{distance_px:.1f}"},
                    {"label": "distance_m", "value": f"{dist_m:.2f}" if dist_m is not None else "unknown"},
                    {"label": "secondary_pair_classes", "value": f"{a.cls},{b.cls}"},
                    {"label": "secondary_pair_confs", "value": f"{a.conf:.2f},{b.conf:.2f}"},
                ],
                pair=(a, b),
                secondary_risk=risk,
                distance_m=dist_m,
                distance_px=float(distance_px),
                event_type=event_type,
            )
        return None

    # ---- Rule C: classification / severity mismatch -----------------
    def check_classification_mismatch(
        self,
        primary_event: dict,
        primary_detections: list[Detection],
        secondary_detections: list[Detection],
        frame_height: int,
        *,
        calibration: Optional[CameraCalibration] = None,
    ) -> Optional[Discrepancy]:
        """Primary + secondary both see the pair, but classify it differently.

        Three sub-cases, collapsed into one finding:

        * Class disagreement on either member of the pair (e.g. primary
          "person", secondary "bicycle").
        * Risk-bucket disagreement when recomputing risk against
          secondary bboxes.
        """
        pair = self._pair_from_event(primary_event, primary_detections)
        if pair is None:
            return None
        a, b = pair
        match_a, iou_a = _best_iou_match(a, secondary_detections)
        match_b, iou_b = _best_iou_match(b, secondary_detections)
        if (
            match_a is None
            or match_b is None
            or iou_a < self.iou_threshold
            or iou_b < self.iou_threshold
        ):
            # Not a match; false-positive rule handled this case.
            return None

        class_mismatch = (match_a.cls != a.cls) or (match_b.cls != b.cls)

        dist_m = estimate_inter_distance_m(
            match_a, match_b, frame_height, calibration=calibration,
        )
        distance_px = bbox_edge_distance(match_a, match_b)
        secondary_risk = classify_risk(
            ttc_sec=None,
            distance_m=dist_m,
            fallback_px=distance_px,
        )
        primary_risk = str(primary_event.get("risk_level", ""))
        risk_mismatch = bool(primary_risk) and secondary_risk != primary_risk

        if not class_mismatch and not risk_mismatch:
            return None
        details = []
        if class_mismatch:
            details.append(
                f"class disagreement: primary={a.cls}/{b.cls}, "
                f"secondary={match_a.cls}/{match_b.cls}"
            )
        if risk_mismatch:
            details.append(
                f"risk disagreement: primary={primary_risk}, secondary={secondary_risk}"
            )
        return Discrepancy(
            kind="classification_mismatch",
            severity="info",
            title="Primary and secondary disagree on class or risk",
            detail="; ".join(details) + ".",
            fingerprint="validator/classification-mismatch",
            evidence=[
                {"label": "primary_event_id", "value": str(primary_event.get("event_id", ""))},
                {"label": "primary_classes", "value": f"{a.cls},{b.cls}"},
                {"label": "secondary_classes", "value": f"{match_a.cls},{match_b.cls}"},
                {"label": "primary_risk", "value": primary_risk},
                {"label": "secondary_risk", "value": secondary_risk},
                {"label": "iou_a", "value": f"{iou_a:.3f}"},
                {"label": "iou_b", "value": f"{iou_b:.3f}"},
            ],
        )

    # ---- helpers -----------------------------------------------------
    @staticmethod
    def _pair_from_event(
        primary_event: dict,
        primary_detections: list[Detection],
    ) -> Optional[tuple[Detection, Detection]]:
        """Recover the two pair-members from the event's track_ids / objects.

        Prefers exact track-id match; falls back to class-match when
        track ids are missing (untracked fallback case).
        """
        track_ids = primary_event.get("track_ids") or []
        if len(track_ids) == 2 and all(tid is not None for tid in track_ids):
            a = next((d for d in primary_detections if d.track_id == track_ids[0]), None)
            b = next((d for d in primary_detections if d.track_id == track_ids[1]), None)
            if a is not None and b is not None:
                return (a, b)
        # Fallback: pick by classes listed on the event.
        objs = primary_event.get("objects") or []
        if len(objs) == 2:
            a = next((d for d in primary_detections if d.cls == objs[0]), None)
            b = next((d for d in primary_detections if d.cls == objs[1] and d is not a), None)
            if a is not None and b is not None:
                return (a, b)
        return None


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------


class ValidatorWorker:
    """Async background worker — drains the queue, runs inference, emits findings.

    Lifecycle mirrors the existing ``edge_task`` / ``watchdog_task``
    pattern in ``server.py::lifespan``: one ``asyncio.create_task`` at
    startup, ``cancel()`` at shutdown.
    """

    def __init__(
        self,
        detector: SecondaryDetector,
        comparator: DiscrepancyComparator,
        write_finding: Callable[[Any], None],
        finding_ctor: Callable[..., Any],
        observer_record_skip: Optional[Callable[..., None]] = None,
        queue_max: int = VALIDATOR_QUEUE_MAX,
        sample_sec: float = VALIDATOR_SAMPLE_SEC,
        save_shadow_record: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.detector = detector
        self.comparator = comparator
        # Dependency-inject the watchdog writer + ctor so tests don't
        # touch the real JSONL file.
        self._write_finding = write_finding
        self._finding_ctor = finding_ctor
        self._observer_record_skip = observer_record_skip
        # Shadow-store writer — captures the frame + detection snapshot
        # backing every ``validator/false-negative`` finding so the UI
        # can explain the miss and offer "promote to event" / "re-run
        # primary" actions. Dependency-injected so tests can substitute
        # a spy. The default import is deferred to the first _emit call
        # so the store's cv2 dependency doesn't block worker construction.
        self._save_shadow_record = save_shadow_record
        self.queue: asyncio.Queue[ValidatorJob] = asyncio.Queue(maxsize=queue_max)
        # Unbounded overflow for episode jobs — every primary event earns a
        # re-check, so we never drop them even when the bounded queue is full.
        # Sampled jobs still drop on overflow (best-effort second-opinion).
        self._priority_overflow: collections.deque[ValidatorJob] = collections.deque()
        self.sample_sec = sample_sec
        # Last sampled-job acceptance time per source — used for rate
        # limiting the sampled stream tee.
        self._last_sample_ts: dict[str, float] = {}
        # Last time the primary emitted an event on a source — used by
        # rule B (false negative) to avoid firing immediately after a
        # primary event.
        self._last_primary_event_ts: dict[str, float] = {}
        # Observability counters.
        self.jobs_processed = 0
        self.jobs_dropped = 0
        self.findings_emitted = 0
        # Episode-specific counter: number of primary-event re-checks that
        # have been accepted (bounded queue OR overflow). The invariant is
        # ``episodes_enqueued >= primary events emitted`` — surfaced in the
        # status payload so operators can verify the guarantee holds.
        self.episodes_enqueued = 0
        self._running = False
        # Operator pause: when True, ``enqueue()`` short-circuits so no new
        # shadow jobs are accepted. The background worker keeps running so
        # we don't pay the model-reload cost when the operator flips it back
        # on. Defaults False so behaviour matches pre-toggle releases.
        self._paused = False

    # ---- status (operator-facing) -----------------------------------
    def status(self) -> dict:
        """Snapshot of worker health for the ``/api/validator/status`` route."""
        return {
            "running": self._running,
            "paused": self._paused,
            "backend": self.detector.backend,
            "model_path": self.detector.model_path,
            "device": self.detector.device or "auto",
            "queue_depth": self.queue.qsize(),
            "queue_max": self.queue.maxsize,
            "overflow_depth": len(self._priority_overflow),
            "sample_sec": self.sample_sec,
            "iou_threshold": self.comparator.iou_threshold,
            "jobs_processed": self.jobs_processed,
            "jobs_dropped": self.jobs_dropped,
            "findings_emitted": self.findings_emitted,
            "episodes_enqueued": self.episodes_enqueued,
        }

    def set_paused(self, paused: bool) -> None:
        """Flip the operator pause flag. Thread-safe (bool write is atomic)."""
        self._paused = bool(paused)

    # ---- producer API (called from primary thread / loop) -----------
    def should_sample(self, slot_id: str, wall_ts: float) -> bool:
        """Rate-limit sampled jobs to at most one every ``sample_sec`` per slot."""
        last = self._last_sample_ts.get(slot_id, 0.0)
        if wall_ts - last < self.sample_sec:
            return False
        self._last_sample_ts[slot_id] = wall_ts
        return True

    def mark_primary_event(self, slot_id: str, wall_ts: float) -> None:
        """Record that the primary just emitted an event for this source."""
        self._last_primary_event_ts[slot_id] = wall_ts

    def enqueue(self, job: ValidatorJob) -> bool:
        """Non-blocking put. Returns False when the queue is full or paused.

        Episode jobs (primary-event re-checks) are never dropped: if the
        bounded queue is full they spill into an unbounded overflow deque
        which the worker drains first. This upholds the invariant that
        the validator sees at least one re-check per primary event.
        """
        if self._paused:
            # Dropped on the floor without counting as a real drop (the
            # queue isn't backed up, the operator just turned us off).
            return False
        if job.kind == "episode":
            try:
                self.queue.put_nowait(job)
            except asyncio.QueueFull:
                self._priority_overflow.append(job)
                log.info(
                    "validator: episode queue full, spilled to overflow "
                    "slot=%s overflow_depth=%d queue=%d/%d",
                    job.slot_id,
                    len(self._priority_overflow),
                    self.queue.qsize(),
                    self.queue.maxsize,
                )
            self.episodes_enqueued += 1
            log.info(
                "validator: episode enqueued slot=%s episodes_enqueued=%d queue=%d/%d overflow=%d",
                job.slot_id,
                self.episodes_enqueued,
                self.queue.qsize(),
                self.queue.maxsize,
                len(self._priority_overflow),
            )
            return True
        try:
            self.queue.put_nowait(job)
            log.info(
                "validator: sampled enqueued slot=%s queue=%d/%d",
                job.slot_id,
                self.queue.qsize(),
                self.queue.maxsize,
            )
            return True
        except asyncio.QueueFull:
            self.jobs_dropped += 1
            log.debug(
                "validator: queue full, dropping %s job for slot=%s (drops=%d, max=%d)",
                job.kind,
                job.slot_id,
                self.jobs_dropped,
                self.queue.maxsize,
            )
            if self._observer_record_skip is not None:
                try:
                    self._observer_record_skip("validator", "queue_full")
                except Exception:  # noqa: BLE001 — never bubble to the hot path
                    pass
            return False

    # ---- worker loop ------------------------------------------------
    async def run_forever(self) -> None:
        """Drain the queue until cancelled. Never raises (except CancelledError)."""
        self._running = True
        # Load the model in a thread so the event loop isn't blocked by
        # a multi-hundred-MB torch weight deserialisation.
        try:
            await asyncio.to_thread(self.detector.load)
        except NotImplementedError as exc:
            log.warning("validator disabled: %s", exc)
            self._running = False
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("validator backend failed to load, disabling: %s", exc)
            self._running = False
            return
        log.info(
            "validator worker started queue_max=%d sample_sec=%.1f iou_threshold=%.2f",
            self.queue.maxsize,
            self.sample_sec,
            self.comparator.iou_threshold,
        )
        try:
            while True:
                # Priority: drain the episode overflow deque first so primary
                # event re-checks never get stuck behind a backlog of sampled
                # frames. ``_priority_overflow`` only holds episode jobs.
                if self._priority_overflow:
                    job = self._priority_overflow.popleft()
                else:
                    job = await self.queue.get()
                log.info(
                    "validator: dequeued slot=%s kind=%s queue=%d/%d overflow=%d",
                    job.slot_id,
                    job.kind,
                    self.queue.qsize(),
                    self.queue.maxsize,
                    len(self._priority_overflow),
                )
                try:
                    await self._process(job)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "validator job failed (%s): %r",
                        job.kind,
                        exc,
                        exc_info=True,
                    )
                finally:
                    self.jobs_processed += 1
        except asyncio.CancelledError:
            log.info("validator worker cancelled")
            raise

    async def _process(self, job: ValidatorJob) -> None:
        started = time.monotonic()
        secondary = await asyncio.to_thread(self.detector.predict, job.frame)
        latency_ms = (time.monotonic() - started) * 1000.0
        log.info(
            "validator: processed slot=%s kind=%s secondary_dets=%d primary_dets=%d latency_ms=%.0f",
            job.slot_id,
            job.kind,
            len(secondary),
            len(job.primary_detections),
            latency_ms,
        )
        frame_h = _frame_height(job.frame)
        findings: list[Any] = []
        if job.kind == "episode" and job.primary_event is not None:
            fp = self.comparator.check_false_positive(
                job.primary_event, job.primary_detections, secondary
            )
            if fp is not None:
                findings.append(fp)
            # Only emit classification-mismatch when false-positive didn't
            # already fire — the two are mutually exclusive by construction.
            if fp is None:
                cm = self.comparator.check_classification_mismatch(
                    job.primary_event, job.primary_detections, secondary,
                    frame_h, calibration=job.calibration,
                )
                if cm is not None:
                    findings.append(cm)
        elif job.kind == "sampled":
            primary_recently = (
                job.wall_ts - self._last_primary_event_ts.get(job.slot_id, 0.0) < 2.0
            )
            fn = self.comparator.check_false_negative(
                frame_h, job.primary_detections, secondary, primary_recently,
                calibration=job.calibration,
            )
            if fn is not None:
                findings.append(fn)

        for disc in findings:
            self._emit(disc, job, secondary)

    def _emit(
        self,
        disc: Discrepancy,
        job: ValidatorJob,
        secondary_detections: list[Detection],
    ) -> None:
        """Turn a Discrepancy into a WatchdogFinding and persist it.

        For false-negative findings we additionally snapshot the frame
        + primary/secondary detections into the shadow store, keyed by
        the finding's ``snapshot_id``. The stored record powers the UI
        dialog (frame, per-gate diagnostic, re-run primary, promote).
        Shadow-store failures are swallowed: the finding still emits.
        """
        try:
            extra_evidence = list(disc.evidence) + [
                {"label": "slot_id", "value": str(job.slot_id)},
                {"label": "wall_ts", "value": f"{job.wall_ts:.2f}"},
                {"label": "job_kind", "value": str(job.kind)},
            ]
            finding = self._finding_ctor(
                severity=disc.severity,
                category="validator",
                title=disc.title,
                detail=disc.detail,
                fingerprint=disc.fingerprint,
                source="rule",
                evidence=extra_evidence,
            )
            if disc.kind == "false_negative" and disc.pair is not None:
                self._persist_shadow_record(finding, disc, job, secondary_detections)
            self._write_finding(finding)
            self.findings_emitted += 1
            log.info(
                "validator finding emitted kind=%s severity=%s slot=%s fingerprint=%s",
                disc.kind,
                disc.severity,
                job.slot_id,
                disc.fingerprint,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("validator: failed to emit finding: %s", exc)

    def _persist_shadow_record(
        self,
        finding: Any,
        disc: Discrepancy,
        job: ValidatorJob,
        secondary_detections: list[Detection],
    ) -> None:
        """Save the shadow-store record + mutate ``finding.evidence`` in place.

        Keeps the shadow-store dependency optional: if the importer or
        writer fails (no cv2, read-only FS, OS error), the finding still
        goes out — just without the extra evidence chip that makes the
        frame addressable from the UI.
        """
        saver = self._save_shadow_record
        if saver is None:
            try:
                from road_safety.core import shadow_store
                saver = shadow_store.save
                self._save_shadow_record = saver
            except Exception as exc:  # noqa: BLE001
                log.warning("validator: shadow_store unavailable: %s", exc)
                return
        shadow_id = getattr(finding, "snapshot_id", None)
        if not shadow_id:
            return
        try:
            record = saver(
                shadow_id=shadow_id,
                slot_id=job.slot_id,
                wall_ts=job.wall_ts,
                event_type=disc.event_type or "unknown",
                secondary_risk=disc.secondary_risk,
                distance_m=disc.distance_m,
                distance_px=disc.distance_px,
                frame=job.frame,
                secondary_pair=disc.pair,
                secondary_detections=secondary_detections,
                primary_detections=job.primary_detections,
            )
        except Exception as exc:  # noqa: BLE001 — never bubble to the loop
            log.warning("validator: shadow_store.save failed: %s", exc)
            return
        if record is None:
            return
        # Attach the id so the frontend can address the stored record
        # without scanning the full records file.
        try:
            finding.evidence.append({"label": "shadow_id", "value": shadow_id})
        except AttributeError:
            # The finding_ctor might return a non-dataclass mock; in that
            # case the missing evidence attribute is acceptable.
            pass


def _frame_height(frame) -> int:
    """Return ``frame.shape[0]`` with a safe fallback for non-numpy inputs."""
    try:
        shape = getattr(frame, "shape", None)
        if shape is not None and len(shape) >= 1:
            return int(shape[0])
    except Exception:  # noqa: BLE001
        pass
    return 0
