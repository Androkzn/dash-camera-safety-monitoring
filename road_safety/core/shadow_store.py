"""Shadow-only detection store.

Persists the frame + detection snapshot backing each
``validator/false-negative`` finding so the operator UI can:

  * render the redacted frame the secondary detector flagged,
  * display a per-gate "why did the primary miss this?" diagnostic,
  * re-run the primary detector on that exact frame on-demand,
  * optionally promote the shadow pair into the live event buffer.

Storage layout
--------------
``data/thumbnails/shadow_<id>.jpg``
    Public / redacted JPEG. Written through
    :func:`road_safety.services.redact.redact_for_egress` so every
    face + plate band is Gaussian-blurred before the pixel data hits
    disk. The pair bboxes are drawn on top of the blurred copy. The
    existing ``sweep_thumbnails`` retention pass picks these up like
    any other event thumbnail.

``data/shadow_records.jsonl``
    Append-only JSONL, one line per shadow finding, capped at
    ``SHADOW_STORE_MAX_RECORDS`` (env ``ROAD_SHADOW_STORE_MAX_RECORDS``,
    default 200). On overflow the oldest 25 % of lines are discarded
    in place and their thumbnails are unlinked, bounding both disk and
    memory cost regardless of how long the process has been running.

Design notes
------------
* The only pixel data we keep is the already-redacted public
  thumbnail. Raw frames are never written.
* Every public function is narrow-except and never raises into the
  validator worker loop — the validator is an observability layer
  whose failures must not disturb the perception critical path.
* Thread-safe writes via a module-level ``threading.Lock`` guarding
  the JSONL append + rotation, mirroring the watchdog writer.
"""

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from road_safety.config import DATA_DIR, THUMBS_DIR
from road_safety.core.detection import Detection
from road_safety.services.redact import redact_for_egress

log = logging.getLogger(__name__)

SHADOW_STORE_MAX_RECORDS = int(os.getenv("ROAD_SHADOW_STORE_MAX_RECORDS", "200"))

_RECORDS_PATH = DATA_DIR / "shadow_records.jsonl"
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Paths + naming
# ---------------------------------------------------------------------------


def _thumb_name(shadow_id: str) -> str:
    """Return the on-disk basename for a shadow record's thumbnail.

    The ``shadow_`` prefix keeps it sortable next to event thumbnails and
    the ``.jpg`` suffix matches the retention sweep's filter.
    """
    return f"shadow_{shadow_id}.jpg"


def thumbnail_path(shadow_id: str) -> Path:
    """Absolute on-disk path of a shadow record's thumbnail."""
    return THUMBS_DIR / _thumb_name(shadow_id)


def thumbnail_url(shadow_id: str) -> str:
    """Relative URL for the ``/thumbnails/{name}`` server route."""
    return f"thumbnails/{_thumb_name(shadow_id)}"


# ---------------------------------------------------------------------------
# Detection <-> dict coercion (stable JSON schema for the record)
# ---------------------------------------------------------------------------


def _det_to_dict(det: Detection) -> dict:
    return {
        "cls": det.cls,
        "conf": float(det.conf),
        "x1": int(det.x1),
        "y1": int(det.y1),
        "x2": int(det.x2),
        "y2": int(det.y2),
        "track_id": det.track_id,
    }


def _dict_to_det(d: dict) -> Detection:
    return Detection(
        cls=str(d["cls"]),
        conf=float(d["conf"]),
        x1=int(d["x1"]),
        y1=int(d["y1"]),
        x2=int(d["x2"]),
        y2=int(d["y2"]),
        track_id=d.get("track_id"),
    )


@dataclass
class ShadowRecord:
    """One shadow-only finding with everything needed to explain + promote.

    Fields:
        shadow_id: Stable identifier, matches the parent
            ``WatchdogFinding.snapshot_id``.
        slot_id: Perception slot (camera) the miss was observed on.
        wall_ts: Wall-clock seconds when the sampled frame was captured.
        event_type: ``"pedestrian_proximity"`` or
            ``"vehicle_close_interaction"``.
        secondary_risk: Risk label the secondary assigned to the pair.
        distance_m / distance_px: Pair distances from the secondary's
            detections.
        frame_h / frame_w: Saved frame dimensions (useful for re-render
            or for the /analysis gate math when we re-evaluate the
            primary thresholds).
        secondary_pair: The two pair-member detections the secondary
            flagged, serialised as dicts.
        secondary_detections / primary_detections: All detections the
            respective models produced on the saved frame. The primary
            list lets us diagnose miss reasons without a re-run.
        thumbnail: Relative URL under the server root.
    """

    shadow_id: str
    slot_id: str
    wall_ts: float
    event_type: str
    secondary_risk: str
    distance_m: Optional[float]
    distance_px: float
    frame_h: int
    frame_w: int
    secondary_pair: list[dict]
    secondary_detections: list[dict]
    primary_detections: list[dict]
    thumbnail: str

    def as_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Save / load
# ---------------------------------------------------------------------------


def save(
    *,
    shadow_id: str,
    slot_id: str,
    wall_ts: float,
    event_type: str,
    secondary_risk: str,
    distance_m: Optional[float],
    distance_px: float,
    frame,
    secondary_pair: tuple[Detection, Detection],
    secondary_detections: list[Detection],
    primary_detections: list[Detection],
) -> Optional[ShadowRecord]:
    """Persist redacted thumbnail + JSONL record for a false-negative finding.

    Never raises. Returns the record on success, ``None`` on any I/O
    error so the validator keeps emitting findings even when disk is
    full or the thumbnails volume is read-only.

    Args:
        shadow_id: Stable id (use the parent finding's ``snapshot_id``).
        slot_id / wall_ts: Source + timestamp for provenance.
        event_type: As emitted by ``find_interactions``.
        secondary_risk: ``"high"`` / ``"medium"``.
        distance_m / distance_px: Pair-distance readouts.
        frame: BGR numpy ``ndarray`` (H×W×3). Not mutated — redaction
            runs on a copy.
        secondary_pair: ``(a, b)`` pair members.
        secondary_detections: Every box the secondary produced on this
            frame (used for both analysis and redaction coverage).
        primary_detections: Every box the primary produced on this
            frame (may be empty — that's the whole point of the miss).
    """
    try:
        frame_h = int(frame.shape[0])
        frame_w = int(frame.shape[1])
    except Exception:  # noqa: BLE001 — opaque ndarray-like inputs
        frame_h = 0
        frame_w = 0

    try:
        THUMBS_DIR.mkdir(parents=True, exist_ok=True)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("shadow_store: cannot create dirs: %s", exc)
        return None

    thumb_path = thumbnail_path(shadow_id)
    if not _write_thumbnail(thumb_path, frame, secondary_pair, secondary_detections, primary_detections):
        return None

    record = ShadowRecord(
        shadow_id=shadow_id,
        slot_id=str(slot_id),
        wall_ts=float(wall_ts),
        event_type=str(event_type),
        secondary_risk=str(secondary_risk),
        distance_m=None if distance_m is None else float(distance_m),
        distance_px=float(distance_px),
        frame_h=frame_h,
        frame_w=frame_w,
        secondary_pair=[_det_to_dict(d) for d in secondary_pair],
        secondary_detections=[_det_to_dict(d) for d in secondary_detections],
        primary_detections=[_det_to_dict(d) for d in primary_detections],
        thumbnail=thumbnail_url(shadow_id),
    )
    _append_record(record)
    return record


def _write_thumbnail(
    path: Path,
    frame,
    secondary_pair: tuple[Detection, Detection],
    secondary_detections: list[Detection],
    primary_detections: list[Detection],
) -> bool:
    """Redact a copy of ``frame``, draw pair bboxes, write JPEG. Never raises."""
    try:
        import cv2  # local import — validator worker already loads torch lazily
    except Exception as exc:  # noqa: BLE001 — no cv2 → cannot save, but also rare
        log.warning("shadow_store: cv2 unavailable: %s", exc)
        return False
    try:
        # Redact against the union of both models' boxes so a pedestrian
        # the primary alone saw still has its face blurred.
        redacted = redact_for_egress(
            frame, list(secondary_detections) + list(primary_detections)
        )
        a, b = secondary_pair
        # BGR — red for the first pair member, amber for the second, to
        # mirror EventDialog's existing primary/secondary colour mapping.
        for det, color in ((a, (0, 0, 255)), (b, (0, 200, 255))):
            cv2.rectangle(redacted, (det.x1, det.y1), (det.x2, det.y2), color, 3)
        cv2.imwrite(str(path), redacted)
        return True
    except Exception as exc:  # noqa: BLE001 — cv2/IO/ndarray shape edge cases
        log.warning("shadow_store: failed to write thumbnail %s: %s", path, exc)
        return False


def _append_record(record: ShadowRecord) -> None:
    """Append one line to the records JSONL. Never raises."""
    line = json.dumps(record.as_dict(), ensure_ascii=False)
    try:
        with _lock:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with _RECORDS_PATH.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
            _maybe_rotate_locked()
    except OSError as exc:
        log.warning("shadow_store: failed to append record: %s", exc)


def _maybe_rotate_locked() -> None:
    """Trim the JSONL if it has grown past the cap. Caller holds ``_lock``.

    Drops the oldest 25 % past the cap so we amortise the rotate cost:
    shrinking by exactly one line would rewrite on every subsequent
    append. Stale thumbnails are best-effort unlinked so disk stays
    bounded.
    """
    if not _RECORDS_PATH.exists():
        return
    try:
        lines = _RECORDS_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    cap = max(1, SHADOW_STORE_MAX_RECORDS)
    if len(lines) <= cap:
        return
    drop = max(1, (len(lines) - cap) + cap // 4)
    dropped, kept = lines[:drop], lines[drop:]
    for raw in dropped:
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        sid = obj.get("shadow_id")
        if not sid:
            continue
        try:
            thumbnail_path(sid).unlink(missing_ok=True)
        except OSError:
            pass
    try:
        _RECORDS_PATH.write_text(
            "\n".join(kept) + ("\n" if kept else ""),
            encoding="utf-8",
        )
    except OSError as exc:
        log.warning("shadow_store: rotate rewrite failed: %s", exc)


def load(shadow_id: str) -> Optional[ShadowRecord]:
    """Return the most recent record matching ``shadow_id``, or ``None``.

    The JSONL is capped, so a full scan is bounded at
    ``SHADOW_STORE_MAX_RECORDS`` lines. Scanning from the tail means
    the common case (a just-emitted finding) returns after one read.
    """
    if not _RECORDS_PATH.exists():
        return None
    try:
        lines = _RECORDS_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if obj.get("shadow_id") != shadow_id:
            continue
        try:
            return ShadowRecord(**obj)
        except TypeError:
            # Schema drift from an older row — treat as "not found" so
            # the caller 404s instead of crashing.
            return None
    return None


def read_frame(shadow_id: str):
    """Read the stored redacted frame as a BGR numpy array, or ``None``."""
    path = thumbnail_path(shadow_id)
    if not path.exists():
        return None
    try:
        import cv2
        return cv2.imread(str(path))
    except Exception as exc:  # noqa: BLE001
        log.warning("shadow_store: read_frame failed for %s: %s", shadow_id, exc)
        return None


# ---------------------------------------------------------------------------
# Rehydration helpers — convert dicts back to Detection objects
# ---------------------------------------------------------------------------


def record_primary_detections(record: ShadowRecord) -> list[Detection]:
    return [_dict_to_det(d) for d in record.primary_detections]


def record_secondary_detections(record: ShadowRecord) -> list[Detection]:
    return [_dict_to_det(d) for d in record.secondary_detections]


def record_secondary_pair(record: ShadowRecord) -> tuple[Detection, Detection]:
    if len(record.secondary_pair) < 2:
        raise ValueError("shadow record pair is malformed")
    return _dict_to_det(record.secondary_pair[0]), _dict_to_det(record.secondary_pair[1])
