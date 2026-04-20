"""Neural monocular-depth estimator — optional fusion with the pinhole prior.

Role
----
The project's default distance estimate is the pinhole + ground-plane prior
in ``detection.estimate_distance_m``. That's fast (pure math, no GPU) but
breaks on elevated sidewalks, unusual poses, or when the bbox bottom is
occluded. This module adds an opt-in neural fallback based on a pretrained
monocular-depth model.

Activation
----------
Controlled by the ``ROAD_DEPTH_MODEL`` env var (read in ``config.py``):

* ``off``       — never run the neural depth (default). Keeps startup time
                  short and avoids the one-time torch-hub download.
* ``neural``    — use neural depth for every detection; pinhole only as
                  fallback when the neural model fails to load.
* ``fused``     — run both and pick the more conservative (larger) value.

Design
------
* Lazy import: ``torch`` is only imported inside ``_load()``, so projects
  that never enable the neural path don't pay the import cost.
* Single-process model cache: the model is loaded on first use and reused
  for every subsequent frame.
* Graceful degradation: if the hub download, the device selection, or the
  forward pass fails, we log once and flip the module into a permanent
  "disabled" state. The caller falls back to the pinhole estimate.

Scale problem
-------------
MiDaS-small outputs *relative* inverse depth (arbitrary units). To convert
to metres we calibrate against the pinhole prior: whichever detection has
the most confident pinhole distance sets the scale factor for that frame.
This is cheap and self-correcting — bad pinhole estimates don't poison the
whole frame because we only use the median depth inside each bbox.

Why not ZoeDepth / Depth-Anything-V2?
-------------------------------------
ZoeDepth returns metric depth directly but costs ~400 ms per frame on CPU.
Depth-Anything-V2-small is the best 2024 option but adds a pip dep. MiDaS
small hits the sweet spot for this codebase (torch-hub only, ~25 ms on MPS).
Operators can swap backends via ``ROAD_DEPTH_BACKEND`` without code change.
"""

import logging
import os
import threading
from typing import Any, Optional

import numpy as np

log = logging.getLogger(__name__)

# Cached model + transform + device. Populated by ``_load()`` on first use.
# Tuple so we can atomically swap "loaded" / "failed" states without a lock
# on the common read path.
_MODEL: Optional[Any] = None
_TRANSFORM: Optional[Any] = None
_DEVICE: Optional[str] = None
_LOAD_LOCK = threading.Lock()
_LOAD_FAILED = False


def _pick_device() -> str:
    """Prefer MPS (Apple Silicon) > CUDA > CPU.

    The project already defaults to MPS for YOLO on Mac hardware, so we
    mirror that to keep both models on the same device and avoid a
    host↔device copy on every frame.
    """
    try:
        import torch

        if torch.backends.mps.is_available():
            return "mps"
        if torch.cuda.is_available():
            return "cuda"
    except Exception:  # noqa: BLE001 — any failure → CPU fallback.
        pass
    return "cpu"


def _load(backend: str) -> bool:
    """Load the model once. Returns True on success, False on permanent failure.

    Thread-safe: concurrent callers wait on a single lock so only one
    torch-hub download / weight load happens per process.
    """
    global _MODEL, _TRANSFORM, _DEVICE, _LOAD_FAILED
    if _MODEL is not None:
        return True
    if _LOAD_FAILED:
        return False

    with _LOAD_LOCK:
        # Double-check after acquiring the lock — another thread may have
        # finished loading while we were waiting.
        if _MODEL is not None:
            return True
        if _LOAD_FAILED:
            return False

        try:
            import torch

            device = _pick_device()
            log.info(
                "depth_neural: loading backend=%s on device=%s", backend, device
            )
            # MiDaS-small is the default — fast and small (~90 MB). Other
            # backends can be swapped in without touching call sites.
            hub_name = {
                "midas_small": "MiDaS_small",
                "midas_hybrid": "DPT_Hybrid",
                "midas_large": "DPT_Large",
            }.get(backend, "MiDaS_small")
            model = torch.hub.load("intel-isl/MiDaS", hub_name, trust_repo=True)
            model.to(device).eval()
            transforms = torch.hub.load(
                "intel-isl/MiDaS", "transforms", trust_repo=True
            )
            transform = (
                transforms.small_transform
                if hub_name == "MiDaS_small"
                else transforms.dpt_transform
            )
            _MODEL = model
            _TRANSFORM = transform
            _DEVICE = device
            log.info("depth_neural: ready")
            return True
        except Exception as exc:  # noqa: BLE001 — log once and fall back.
            log.warning(
                "depth_neural: failed to load (%s) — falling back to pinhole",
                exc,
            )
            _LOAD_FAILED = True
            return False


def estimate_relative_depth(frame: np.ndarray) -> Optional[np.ndarray]:
    """Return a 2-D relative-depth map (same H×W as ``frame``) or None.

    Values are arbitrary units — larger means farther. Caller is expected
    to calibrate against a known-metric reference (the pinhole prior) to
    recover metres. Returns ``None`` when the model is disabled or failed.
    """
    backend = os.getenv("ROAD_DEPTH_BACKEND", "midas_small").strip().lower()
    if not _load(backend):
        return None

    try:
        import torch

        # MiDaS expects RGB (project uses BGR internally).
        img_rgb = frame[:, :, ::-1]
        input_batch = _TRANSFORM(img_rgb).to(_DEVICE)  # type: ignore[misc]
        with torch.no_grad():
            pred = _MODEL(input_batch)  # type: ignore[misc]
            pred = torch.nn.functional.interpolate(
                pred.unsqueeze(1),
                size=img_rgb.shape[:2],
                mode="bicubic",
                align_corners=False,
            ).squeeze()
        # MiDaS output is inverse depth (bigger = closer). Invert so bigger
        # = farther, matching the rest of the pipeline.
        inv = pred.detach().cpu().numpy()
        # Guard against zeros / negatives before the reciprocal.
        eps = max(1e-6, float(np.percentile(inv[inv > 0], 1)) if np.any(inv > 0) else 1e-6)
        return 1.0 / np.clip(inv, eps, None)
    except Exception as exc:  # noqa: BLE001 — model runs are best-effort.
        log.debug("depth_neural: forward pass failed: %s", exc)
        return None


def bbox_depth(depth_map: np.ndarray, x1: int, y1: int, x2: int, y2: int) -> Optional[float]:
    """Return the median relative depth inside a bbox, or None if degenerate.

    Using the median (not the mean) is robust against bright patches and
    occlusion edges on the object's silhouette.
    """
    h, w = depth_map.shape[:2]
    x1 = max(0, min(x1, w - 1))
    x2 = max(x1 + 1, min(x2, w))
    y1 = max(0, min(y1, h - 1))
    y2 = max(y1 + 1, min(y2, h))
    region = depth_map[y1:y2, x1:x2]
    if region.size == 0:
        return None
    return float(np.median(region))
