"""Multi-vehicle road safety readiness layer.

Adds vehicle identity, system-wide event aggregation, and cross-vehicle
pattern detection. This module sits between the single-stream processing
(server.py) and the API layer, providing the data model that a production
multi-vehicle deployment needs.

Design goals:
  * Backwards-compatible -- a single-vehicle deployment still works; vehicle_id
    defaults to the ROAD_VEHICLE_ID env var.
  * System-wide queries -- aggregate events across vehicles, find hotspots.
  * Driver scoring -- rolling safety score per driver based on events +
    feedback.
  * Pattern detection -- flag when multiple vehicles report events at the
    same location/time window (intersection hotspot).

In a scaled deployment, the registry would live in a database. The current
implementation uses an in-memory dict keyed by vehicle_id, populated from
events as they arrive.

In-memory storage
-----------------
**There is no persistent store.**  ``RoadRegistry`` holds everything in a
plain Python dict (``self._vehicles``) at process lifetime.  When the
server restarts the registry starts empty and repopulates itself from
incoming events.  That is fine for a single-host edge deployment — if
durable cross-process storage is ever needed, swap the dict for a SQLite
table without touching the public API.

Safety-score intuition
----------------------
Each vehicle starts at ``MAX_SCORE`` (100).  Two forces push the score:

  * **Events subtract.**  A high-risk event costs 10 points, medium 3, low 1.
    Repeated close calls therefore accumulate damage quickly.
  * **Time restores.**  A background scheduler calls :meth:`decay_scores`
    once per hour (interval controlled by ``ROAD_SCORE_DECAY_INTERVAL_SEC``;
    set it to ``0`` to disable entirely), which adds ``SCORE_DECAY_PER_HOUR``
    (0.5) back up to ``MAX_SCORE``.

Intuitively: *"every event subtracts a chunk, every uneventful hour adds a
half-point back".*  The model rewards long stretches without incidents
without letting the score balloon past 100.

API surface
-----------
The FastAPI server exposes this registry at:

  * ``GET /api/road/summary``        — ``road_summary()``
  * ``GET /api/road/vehicle/{id}``   — ``get_vehicle()``
  * ``GET /api/road/drivers``        — ``driver_leaderboard()``

Env vars that shape behaviour
-----------------------------
* ``ROAD_VEHICLE_ID`` / ``ROAD_ID`` / ``ROAD_DRIVER_ID`` — identity
  defaults when an event arrives with no fleet identity fields set.
  (Read from ``road_safety.config``.)
* ``ROAD_SCORE_DECAY_INTERVAL_SEC`` — how often :meth:`decay_scores` is
  called by the scheduler.  ``0`` disables decay.

Python idioms used in this file (one-line explanations)
-------------------------------------------------------
* ``@dataclass`` — decorator that generates ``__init__``, ``__repr__``, and
  ``__eq__`` from the class-level type annotations.  Turns a "fields
  container" class into a few lines.
* ``field(default_factory=...)`` — lets a dataclass field default to a
  *new* mutable object per instance (never share a single dict between
  instances; that would be a classic Python bug).
* ``dict | None`` (type hint) — "either a dict or None".  Requires
  ``from __future__ import annotations`` on older Pythons.
* ``from __future__ import annotations`` — treats type hints as strings,
  enabling modern syntax on older interpreters and speeding up import.
* ``defaultdict(int)`` — a dict that auto-creates a ``0`` value for any
  missing key, so ``counter[k] += 1`` works without a prior ``if k in``.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------
# time          — wall-clock timestamps on ``last_event_ts``.
# collections   — ``defaultdict`` for cheap counters.
# dataclasses   — generates ``__init__`` for ``VehicleState``.
# typing.Any    — return type on ``road_summary`` (heterogeneous dict).
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

# Identity defaults — used when an incoming event has no fleet-identity
# fields.  All three come from env vars set at process start.
from road_safety.config import DRIVER_ID, ROAD_ID, VEHICLE_ID

# ---------------------------------------------------------------------------
# Scoring constants
# ---------------------------------------------------------------------------
# Per-event penalties.  These weights encode the product decision that one
# "high" risk incident (near miss) should cost ~3x more than a medium and
# ~10x more than a low.  Tune carefully — a change ripples through every
# dashboard number and any downstream driver-ranking contract.
RISK_WEIGHTS = {"high": 10, "medium": 3, "low": 1}

# Decay amount added back per call to ``decay_scores``.  At the default
# hourly cadence, an idle driver recovers ``0.5`` points per hour — roughly
# one "low" incident forgiven per two hours.  Slow on purpose: we do not
# want recovery to drown out long-term bad patterns.
SCORE_DECAY_PER_HOUR = 0.5

# Upper bound for scores.  Starting value for new vehicles and the ceiling
# that decay can restore towards.  100 is chosen for dashboard legibility
# ("out of 100" reads naturally to humans).
MAX_SCORE = 100.0


# ===========================================================================
# Per-vehicle state container
# ===========================================================================
@dataclass
class VehicleState:
    """Rolling state for one vehicle.

    A dataclass (see module docstring) that bundles everything the
    registry knows about one vehicle: identity, running counters,
    timestamps, and feedback tallies.  Every field has a sensible default
    so a new ``VehicleState(vehicle_id=..., road_id=...)`` works without
    extra setup.

    Lifecycle: created lazily by ``RoadRegistry._ensure`` the first time
    an event mentions this vehicle, then mutated in place forever (or
    until the process restarts — nothing is persisted).

    Callers: ``RoadRegistry.record_event``, ``record_feedback``,
    ``road_summary``, ``driver_leaderboard``.
    """

    vehicle_id: str
    road_id: str
    # ``| None`` types (unions) need the future-annotations import at the
    # top of the file on older Pythons.
    driver_id: str | None = None
    total_events: int = 0
    # ``field(default_factory=...)`` gives each new VehicleState its own
    # fresh dict.  Using a bare default like ``={"high": 0, ...}`` would
    # share one dict across every instance — a classic Python footgun.
    events_by_risk: dict[str, int] = field(default_factory=lambda: {"high": 0, "medium": 0, "low": 0})
    events_by_type: dict[str, int] = field(default_factory=dict)
    # Unix timestamp of the most recent event; None if we've never seen one.
    last_event_ts: float | None = None
    # Current safety score; decays back towards MAX_SCORE when idle.
    safety_score: float = MAX_SCORE
    # Operator feedback counters — "true positive" and "false positive"
    # verdicts from the dashboard's thumbs-up/down buttons.
    feedback_tp: int = 0
    feedback_fp: int = 0

    def as_dict(self) -> dict:
        """Render state as a JSON-serialisable dict for API responses.

        Returns:
            A plain dict with all counters, the rounded score, and a
            derived ``precision`` metric (tp / (tp + fp)).  When no
            feedback exists we divide by 1 instead of 0 (``max(..., 1)``)
            to keep precision at 0.0 rather than crashing.
        """
        return {
            "vehicle_id": self.vehicle_id,
            "road_id": self.road_id,
            "driver_id": self.driver_id,
            "total_events": self.total_events,
            # ``dict(self.events_by_risk)`` creates a shallow copy so the
            # caller cannot mutate our internal counter dict.
            "events_by_risk": dict(self.events_by_risk),
            "events_by_type": dict(self.events_by_type),
            "last_event_ts": self.last_event_ts,
            # 1 decimal place is enough for dashboard display; raw floats
            # would drift ugly-looking "83.29999999" into the UI.
            "safety_score": round(self.safety_score, 1),
            "feedback_tp": self.feedback_tp,
            "feedback_fp": self.feedback_fp,
            "precision": round(
                # ``max(..., 1)`` guards against ZeroDivisionError when no
                # feedback has been submitted yet.
                self.feedback_tp / max(self.feedback_tp + self.feedback_fp, 1), 3
            ),
        }


# ===========================================================================
# The registry itself
# ===========================================================================
class RoadRegistry:
    """In-memory vehicle registry for multi-vehicle aggregation.

    Thread model: single-threaded writes assumed (the server's event loop
    calls ``record_event`` from one place).  If you ever call this from
    multiple workers, add a lock around the mutating methods.

    State:
      * ``_vehicles``        — vehicle_id → ``VehicleState``, the core table.
      * ``_event_locations`` — reserved for future hotspot detection (kept
        as a list so the field exists; currently unused).

    Lifecycle: instantiated once at module import time (see
    ``road_registry`` at the bottom of the file).  Callers use that
    module-level singleton; do not build your own instances.
    """

    def __init__(self):
        # Primary table: vehicle_id -> VehicleState.  Plain dict is fine
        # because all access is single-threaded.
        self._vehicles: dict[str, VehicleState] = {}
        # Stash for a future hotspot-clustering feature.  Kept to avoid
        # breaking any code that might already reference it.
        self._event_locations: list[dict] = []

    def _ensure(self, vehicle_id: str, road_id: str = ROAD_ID, driver_id: str | None = None) -> VehicleState:
        """Fetch the ``VehicleState`` for ``vehicle_id`` or create a new one.

        The leading underscore is a Python convention meaning "private
        helper — not part of the public API".

        Args:
            vehicle_id: Identifier for the vehicle.  Case-sensitive.
            road_id:    Road/region id to attach to new vehicles.
            driver_id:  Driver id to attach to new vehicles.

        Returns:
            The (possibly freshly created) ``VehicleState``.  Never None.

        Raises:
            Nothing.
        """
        if vehicle_id not in self._vehicles:
            self._vehicles[vehicle_id] = VehicleState(
                vehicle_id=vehicle_id, road_id=road_id, driver_id=driver_id,
            )
        return self._vehicles[vehicle_id]

    def record_event(self, event: dict) -> None:
        """Apply one detection event to the registry.

        Increments counters, refreshes ``last_event_ts``, and subtracts a
        risk-weighted penalty from ``safety_score``.

        Args:
            event: The event dict as produced by the detection pipeline.
                   Expected optional fields: ``vehicle_id``, ``road_id``,
                   ``driver_id``, ``risk_level`` (high/medium/low),
                   ``event_type``.  Missing fields fall back to
                   env-configured identity defaults and safe "low"/"unknown".

        Returns:
            None.  Mutates in place.

        Raises:
            Nothing.
        """
        # ``.get(key, default)`` returns the default when the key is
        # absent — safer than ``event[key]`` which would raise KeyError.
        vid = event.get("vehicle_id", VEHICLE_ID)
        rid = event.get("road_id", ROAD_ID)
        did = event.get("driver_id", DRIVER_ID)
        v = self._ensure(vid, rid, did)

        v.total_events += 1
        # Default risk to "low" so a malformed event still increments a
        # counter rather than silently vanishing.
        risk = event.get("risk_level", "low")
        v.events_by_risk[risk] = v.events_by_risk.get(risk, 0) + 1
        etype = event.get("event_type", "unknown")
        v.events_by_type[etype] = v.events_by_type.get(etype, 0) + 1
        # Plain Unix time — stored as float seconds since epoch so the
        # dashboard can render a "last seen X minutes ago" label.
        v.last_event_ts = time.time()

        # Penalty lookup.  Unknown risk levels fall back to 1 (low-ish)
        # so an unexpected value still nudges the score rather than
        # leaving the vehicle looking pristine.
        penalty = RISK_WEIGHTS.get(risk, 1)
        # Floor at 0 — we never display negative scores.
        v.safety_score = max(0.0, v.safety_score - penalty)

    def record_feedback(self, event_id: str, verdict: str, vehicle_id: str | None = None) -> None:
        """Record a human verdict on a past event (TP/FP).

        Args:
            event_id:   The original event id the operator reviewed.
                        Stored externally; the registry only bumps counters.
            verdict:    ``"tp"`` (model was right) or ``"fp"`` (model was
                        wrong).  Any other string is silently ignored.
            vehicle_id: Optional vehicle to attribute the feedback to.
                        Falls back to the env default when omitted.

        Returns:
            None.

        Raises:
            Nothing — unrecognised verdicts are a no-op by design.
        """
        # ``a or b`` — the "use b if a is falsy (None/empty string)" idiom.
        vid = vehicle_id or VEHICLE_ID
        v = self._ensure(vid)
        if verdict == "tp":
            v.feedback_tp += 1
        elif verdict == "fp":
            v.feedback_fp += 1
        # else: silently ignore unknown verdicts (e.g. "maybe").

    def decay_scores(self) -> None:
        """Call periodically to let safety scores recover over time.

        Called by the external scheduler every
        ``ROAD_SCORE_DECAY_INTERVAL_SEC`` seconds.  Each call adds
        ``SCORE_DECAY_PER_HOUR`` (0.5) to every vehicle's score, capped at
        ``MAX_SCORE`` (100).

        In English: each uneventful interval adds half a point back.
        Compare to record_event, which subtracts 1/3/10 per event — so
        one high-risk incident roughly wipes out 20 hours of good driving.

        Args:
            None.

        Returns:
            None.

        Raises:
            Nothing.
        """
        for v in self._vehicles.values():
            # ``min(MAX, cur + delta)`` caps the score at the ceiling.
            v.safety_score = min(MAX_SCORE, v.safety_score + SCORE_DECAY_PER_HOUR)

    def get_vehicle(self, vehicle_id: str) -> dict | None:
        """Look up one vehicle's rendered state.

        Args:
            vehicle_id: The id to look up.

        Returns:
            A dict from ``VehicleState.as_dict`` or ``None`` if the
            registry has never seen this vehicle.  The ``None`` return
            is important — the API layer uses it to emit a 404.
        """
        v = self._vehicles.get(vehicle_id)
        # Ternary: "value if condition else fallback".  Concise way to
        # return the dict only when we actually have a state.
        return v.as_dict() if v else None

    def road_summary(self) -> dict[str, Any]:
        """Aggregate every vehicle into a single fleet-wide summary dict.

        Used by ``GET /api/road/summary`` to power the dashboard overview.

        Args:
            None.

        Returns:
            A dict with:
              * ``road_id``              — current deployment's road id
              * ``vehicle_count``        — how many vehicles the registry knows
              * ``total_events``         — events across the whole fleet
              * ``aggregate_by_risk``    — {high, medium, low} totals
              * ``aggregate_by_type``    — event-type totals
              * ``lowest_score_vehicle`` — {id, score, driver} of the worst
                performer — or None when the fleet is empty
              * ``vehicles``             — every vehicle, sorted worst→best

            When the fleet is empty we return a complete-but-zero structure
            rather than None/404 so the UI can render without special cases.

        Raises:
            Nothing.
        """
        vehicles = list(self._vehicles.values())
        # Empty-fleet early return: keeps every key present so the front
        # end doesn't need to branch on "vehicles is None".
        if not vehicles:
            return {
                "road_id": ROAD_ID,
                "vehicle_count": 0,
                "total_events": 0,
                "aggregate_by_risk": {},
                "aggregate_by_type": {},
                "lowest_score_vehicle": None,
                "vehicles": [],
            }

        # ``defaultdict(int)`` auto-initialises missing keys to 0, so we
        # can do ``agg[k] += c`` without a prior existence check.
        agg_risk: dict[str, int] = defaultdict(int)
        agg_type: dict[str, int] = defaultdict(int)
        total = 0
        for v in vehicles:
            total += v.total_events
            for k, c in v.events_by_risk.items():
                agg_risk[k] += c
            for k, c in v.events_by_type.items():
                agg_type[k] += c

        # ``min(seq, key=fn)`` returns the element with the smallest
        # ``fn(elem)``.  Lambda here is an inline anonymous function.
        worst = min(vehicles, key=lambda v: v.safety_score)

        return {
            "road_id": ROAD_ID,
            "vehicle_count": len(vehicles),
            "total_events": total,
            # ``dict(defaultdict)`` strips the defaultdict wrapper so
            # JSON encoders see a plain dict.
            "aggregate_by_risk": dict(agg_risk),
            "aggregate_by_type": dict(agg_type),
            "lowest_score_vehicle": {
                "vehicle_id": worst.vehicle_id,
                "safety_score": round(worst.safety_score, 1),
                "driver_id": worst.driver_id,
            },
            # List comprehension: build a list by transforming each item.
            # Sort ascending by score → worst first, matching the "who
            # needs attention" framing in the dashboard.
            "vehicles": [v.as_dict() for v in sorted(vehicles, key=lambda v: v.safety_score)],
        }

    def driver_leaderboard(self, limit: int = 20) -> list[dict]:
        """Rank drivers by safety score (ascending = worst first).

        One driver can be associated with multiple vehicles; this method
        folds per-vehicle stats up to the driver level.  The driver's
        "score" is the minimum score across their vehicles — deliberately
        pessimistic because one bad vehicle should reflect on the driver.

        Args:
            limit: Max number of drivers to return.  Default 20 matches
                   the dashboard's leaderboard table size.

        Returns:
            Up to ``limit`` driver dicts, each with:
              * ``driver_id``
              * ``vehicles``         — list of vehicle ids they drive
              * ``total_events``     — sum across those vehicles
              * ``high_risk_events`` — sum of the "high" bucket
              * ``safety_score``     — their worst vehicle's score

            Sorted worst → best.  Empty list if no vehicles exist.

        Raises:
            Nothing.
        """
        drivers: dict[str, dict] = {}
        for v in self._vehicles.values():
            # Fall back to vehicle_id when no driver is attached — keeps
            # orphan vehicles from disappearing from the leaderboard.
            did = v.driver_id or v.vehicle_id
            if did not in drivers:
                drivers[did] = {
                    "driver_id": did,
                    "vehicles": [],
                    "total_events": 0,
                    "high_risk_events": 0,
                    "safety_score": MAX_SCORE,
                }
            d = drivers[did]
            d["vehicles"].append(v.vehicle_id)
            d["total_events"] += v.total_events
            d["high_risk_events"] += v.events_by_risk.get("high", 0)
            # ``min`` here = "take the worst of this driver's vehicles".
            d["safety_score"] = min(d["safety_score"], v.safety_score)

        ranked = sorted(drivers.values(), key=lambda d: d["safety_score"])
        # ``[:limit]`` is list-slicing: first ``limit`` elements.  Safe
        # even when the list is shorter than ``limit``.
        return ranked[:limit]


# ---------------------------------------------------------------------------
# Module-level singleton — this is what every caller should import.
# ---------------------------------------------------------------------------
# ``from road_safety.services.registry import road_registry`` gives you the
# one shared instance used by the whole server.  Do not instantiate your
# own ``RoadRegistry()`` — you'd end up with a second copy that sees none
# of the live events.
road_registry = RoadRegistry()
