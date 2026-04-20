"""
Road-safety evaluation harness.

Three modes:
  1. Single-clip (backwards compatible):
        python tools/eval_detect.py
     Reads data/events.json + data/labels.json, prints/writes the legacy
     {tp,fp,fn,precision,recall,f1} JSON report (shape preserved for
     downstream consumers).

  2. Suite:
        python tools/eval_detect.py --suite [--manifest data/test_suite/manifest.json]
     Runs tools/analyze.py as a subprocess against each clip in the manifest,
     evaluates against its labels, prints a markdown matrix, and writes a
     full JSON report to data/test_suite/results.json.

  3. Compare two suite reports:
        python tools/eval_detect.py --compare <baseline.json> <current.json>
     Diffs per-clip and macro metrics; flags regressions > 3%.

Label schema (per entry):
  {
    "timestamp_sec": float,
    "event_type":    str,
    "risk_level":    "high" | "medium" | "low"   (optional — wildcard if omitted),
    "tolerance_sec": float                         (optional — default 1.5)
  }

A detection is a TP if it matches an unclaimed label with:
  * same event_type
  * |timestamp delta| <= tolerance_sec (from label, else 1.5)
  * risk_level equal (or label omits risk_level).
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from road_safety.config import DATA_DIR, PROJECT_ROOT

ROOT = PROJECT_ROOT
DEFAULT_TOLERANCE_SEC = 1.5
DEFAULT_MANIFEST = DATA_DIR / "test_suite" / "manifest.json"
DEFAULT_SUITE_RESULTS = DATA_DIR / "test_suite" / "results.json"

RISK_BANDS = ("high", "medium", "low")
REGRESSION_THRESHOLD = 0.03            # flag > 3% drop on any metric
HIGH_RISK_RECALL_FLOOR = 0.80
OVERALL_PRECISION_FLOOR = 0.70

ANALYZE_FRAMES_RE = re.compile(
    r"Processed\s+(\d+)\s+frames\s+in\s+([0-9.]+)s", re.IGNORECASE
)


# ---------------------------------------------------------------------------
# Core matching
# ---------------------------------------------------------------------------

def _match(
    events: list[dict],
    labels: list[dict],
    *,
    risk_filter: str | None = None,
    event_type_filter: str | None = None,
) -> tuple[int, int, int]:
    """Return (tp, fp, fn) for the optionally-filtered slice.

    We filter BOTH events and labels by the same predicate so that:
      * FP counts only detections whose own risk/type falls in the slice
      * FN counts only labels whose own risk/type falls in the slice
    A TP requires a joint match within each slice.
    """
    def keep_event(ev: dict) -> bool:
        if event_type_filter and ev.get("event_type") != event_type_filter:
            return False
        if risk_filter and ev.get("risk_level") != risk_filter:
            return False
        return True

    def keep_label(lbl: dict) -> bool:
        if event_type_filter and lbl.get("event_type") != event_type_filter:
            return False
        if risk_filter and lbl.get("risk_level", risk_filter) != risk_filter:
            # If the label omits risk_level it is a wildcard and passes.
            return False
        return True

    f_events = [e for e in events if keep_event(e)]
    f_labels = [l for l in labels if keep_label(l)]

    matched: set[int] = set()
    tp = 0
    fp = 0
    for ev in f_events:
        hit = None
        for i, lbl in enumerate(f_labels):
            if i in matched:
                continue
            if lbl.get("event_type") != ev.get("event_type"):
                continue
            lbl_risk = lbl.get("risk_level")
            if lbl_risk is not None and lbl_risk != ev.get("risk_level"):
                continue
            tol = float(lbl.get("tolerance_sec", DEFAULT_TOLERANCE_SEC))
            if abs(float(lbl["timestamp_sec"]) - float(ev["timestamp_sec"])) <= tol:
                hit = i
                break
        if hit is None:
            fp += 1
        else:
            tp += 1
            matched.add(hit)
    fn = len(f_labels) - len(matched)
    return tp, fp, fn


def _prf(tp: int, fp: int, fn: int) -> dict:
    p = tp / (tp + fp) if (tp + fp) else 0.0
    r = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * p * r / (p + r) if (p + r) else 0.0
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": round(p, 3),
        "recall": round(r, 3),
        "f1": round(f1, 3),
    }


def evaluate_clip(events: list[dict], labels: list[dict]) -> dict:
    """Full per-clip breakdown: overall + by-risk + by-event-type."""
    overall_tp, overall_fp, overall_fn = _match(events, labels)
    overall = _prf(overall_tp, overall_fp, overall_fn)

    by_risk: dict[str, dict] = {}
    for risk in RISK_BANDS:
        tp, fp, fn = _match(events, labels, risk_filter=risk)
        if (tp + fp + fn) == 0:
            continue
        by_risk[risk] = _prf(tp, fp, fn)

    event_types = sorted({e.get("event_type") for e in events} |
                         {l.get("event_type") for l in labels})
    by_event_type: dict[str, dict] = {}
    for et in event_types:
        if et is None:
            continue
        tp, fp, fn = _match(events, labels, event_type_filter=et)
        by_event_type[et] = _prf(tp, fp, fn)

    return {
        "overall": overall,
        "by_risk": by_risk,
        "by_event_type": by_event_type,
    }


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ClipResult:
    name: str
    counts: dict
    by_risk: dict
    by_event_type: dict
    overall: dict
    latency: dict
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "counts": self.counts,
            "overall": self.overall,
            "by_risk": self.by_risk,
            "by_event_type": self.by_event_type,
            "latency": self.latency,
            "warnings": self.warnings,
        }


@dataclass
class SuiteReport:
    clips: list[ClipResult]
    macro: dict
    generated_at: str

    def to_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "macro": self.macro,
            "clips": [c.to_dict() for c in self.clips],
        }

    def to_markdown(self) -> str:
        header = (
            "| clip | overall-P | overall-R | overall-F1 | high-R | "
            "med-FP-rate | events/sec | flags |\n"
            "|------|-----------|-----------|------------|--------|"
            "-------------|------------|-------|"
        )
        rows = [header]
        for c in self.clips:
            op = c.overall.get("precision", 0.0)
            orc = c.overall.get("recall", 0.0)
            of1 = c.overall.get("f1", 0.0)
            high = c.by_risk.get("high", {})
            hr = high.get("recall", None)
            med = c.by_risk.get("medium", {})
            med_tp = med.get("tp", 0)
            med_fp = med.get("fp", 0)
            med_fp_rate = (
                med_fp / (med_tp + med_fp) if (med_tp + med_fp) else None
            )
            eps = c.latency.get("events_per_sec")
            flags: list[str] = []
            if hr is not None and hr < HIGH_RISK_RECALL_FLOOR:
                flags.append(f"WARN high-R<{HIGH_RISK_RECALL_FLOOR}")
            if op < OVERALL_PRECISION_FLOOR:
                flags.append(f"WARN overall-P<{OVERALL_PRECISION_FLOOR}")
            if c.warnings:
                flags.extend(c.warnings)
            rows.append(
                "| {name} | {op:.3f} | {orc:.3f} | {of1:.3f} | "
                "{hr} | {mfp} | {eps} | {flags} |".format(
                    name=c.name,
                    op=op, orc=orc, of1=of1,
                    hr=f"{hr:.3f}" if hr is not None else "-",
                    mfp=f"{med_fp_rate:.3f}" if med_fp_rate is not None else "-",
                    eps=f"{eps:.2f}" if isinstance(eps, (int, float)) else "-",
                    flags=", ".join(flags) if flags else "",
                )
            )
        macro_line = (
            f"\nmacro: P={self.macro['precision']:.3f} "
            f"R={self.macro['recall']:.3f} "
            f"F1={self.macro['f1']:.3f} "
            f"(clips={self.macro['n_clips']})"
        )
        return "\n".join(rows) + macro_line


# ---------------------------------------------------------------------------
# Single-clip mode (legacy — preserve output shape)
# ---------------------------------------------------------------------------

def run_single_clip() -> int:
    events_path = DATA_DIR / "events.json"
    labels_path = DATA_DIR / "labels.json"
    if not events_path.exists():
        print("Run analyze.py first — events.json missing.", file=sys.stderr)
        return 1
    if not labels_path.exists():
        print(
            "Create data/labels.json with ground-truth events to evaluate.",
            file=sys.stderr,
        )
        return 1

    events = json.loads(events_path.read_text())
    labels = json.loads(labels_path.read_text())
    tp, fp, fn = _match(events, labels)
    prf = _prf(tp, fp, fn)
    # Legacy shape — keep exact keys.
    report = {
        "true_positives": prf["tp"],
        "false_positives": prf["fp"],
        "false_negatives": prf["fn"],
        "precision": prf["precision"],
        "recall": prf["recall"],
        "f1": prf["f1"],
    }
    (DATA_DIR / "eval.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    return 0


# ---------------------------------------------------------------------------
# Suite mode
# ---------------------------------------------------------------------------

def _run_analyze(video_path: Path) -> tuple[float, int | None, float | None, str]:
    """Run analyze.py as subprocess. Returns (wall_sec, frames, analyze_sec, stdout).

    frames / analyze_sec are extracted from analyze.py stdout regex, so they
    reflect processing work rather than python startup.
    """
    started = time.time()
    proc = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "analyze.py"), str(video_path)],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    wall = time.time() - started
    stdout = proc.stdout + "\n" + proc.stderr
    frames = None
    analyze_sec = None
    m = ANALYZE_FRAMES_RE.search(stdout)
    if m:
        frames = int(m.group(1))
        analyze_sec = float(m.group(2))
    if proc.returncode != 0:
        raise RuntimeError(
            f"analyze.py exited {proc.returncode}:\n{stdout[-500:]}"
        )
    return wall, frames, analyze_sec, stdout


def _evaluate_one(clip: dict) -> ClipResult:
    name = clip.get("name") or Path(clip.get("video", "unknown")).stem
    video_path = (ROOT / clip["video"]).resolve() if "video" in clip else None
    labels_path = (ROOT / clip["labels"]).resolve() if "labels" in clip else None
    warnings: list[str] = []

    if not video_path or not video_path.exists():
        warnings.append(f"missing video: {clip.get('video')}")
        return ClipResult(
            name=name,
            counts={"tp": 0, "fp": 0, "fn": 0},
            by_risk={},
            by_event_type={},
            overall=_prf(0, 0, 0),
            latency={"wall_sec": None, "events_per_sec": None},
            warnings=warnings,
        )
    if not labels_path or not labels_path.exists():
        warnings.append(f"missing labels: {clip.get('labels')}")
        return ClipResult(
            name=name,
            counts={"tp": 0, "fp": 0, "fn": 0},
            by_risk={},
            by_event_type={},
            overall=_prf(0, 0, 0),
            latency={"wall_sec": None, "events_per_sec": None},
            warnings=warnings,
        )

    try:
        wall, frames, analyze_sec, _ = _run_analyze(video_path)
    except Exception as exc:  # clip-local failure; keep suite going
        warnings.append(f"analyze.py failed: {exc}")
        return ClipResult(
            name=name,
            counts={"tp": 0, "fp": 0, "fn": 0},
            by_risk={},
            by_event_type={},
            overall=_prf(0, 0, 0),
            latency={"wall_sec": None, "events_per_sec": None},
            warnings=warnings,
        )

    events_path = DATA_DIR / "events.json"
    if not events_path.exists():
        warnings.append("analyze.py produced no events.json")
        events = []
    else:
        events = json.loads(events_path.read_text())
    labels = json.loads(labels_path.read_text())

    breakdown = evaluate_clip(events, labels)
    overall = breakdown["overall"]
    events_per_sec = (
        frames / analyze_sec
        if frames is not None and analyze_sec and analyze_sec > 0
        else None
    )
    return ClipResult(
        name=name,
        counts={
            "tp": overall["tp"],
            "fp": overall["fp"],
            "fn": overall["fn"],
        },
        by_risk=breakdown["by_risk"],
        by_event_type=breakdown["by_event_type"],
        overall={
            "precision": overall["precision"],
            "recall": overall["recall"],
            "f1": overall["f1"],
        },
        latency={
            "wall_sec": round(wall, 2),
            "analyze_sec": round(analyze_sec, 2) if analyze_sec else None,
            "frames_processed": frames,
            "events_per_sec": round(events_per_sec, 2)
            if events_per_sec is not None
            else None,
        },
        warnings=warnings,
    )


def _macro(clips: Iterable[ClipResult]) -> dict:
    clips = [c for c in clips if not c.warnings or c.counts["tp"] + c.counts["fp"] + c.counts["fn"] > 0]
    if not clips:
        return {"precision": 0.0, "recall": 0.0, "f1": 0.0, "n_clips": 0}
    p = statistics.fmean(c.overall["precision"] for c in clips)
    r = statistics.fmean(c.overall["recall"] for c in clips)
    f1 = statistics.fmean(c.overall["f1"] for c in clips)
    return {
        "precision": round(p, 3),
        "recall": round(r, 3),
        "f1": round(f1, 3),
        "n_clips": len(clips),
    }


def run_suite(manifest_path: Path) -> int:
    if not manifest_path.exists():
        print(
            f"Manifest not found: {manifest_path}\n"
            f"Create one (see data/test_suite/example_manifest.json) or "
            f"pass --manifest <path>.",
            file=sys.stderr,
        )
        return 2
    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as exc:
        print(f"Manifest is not valid JSON: {exc}", file=sys.stderr)
        return 2
    clips_spec = manifest.get("clips", [])
    if not clips_spec:
        print("Manifest has no 'clips' entries.", file=sys.stderr)
        return 2

    results: list[ClipResult] = []
    for spec in clips_spec:
        print(f"[eval] running clip: {spec.get('name', spec.get('video'))}")
        results.append(_evaluate_one(spec))

    report = SuiteReport(
        clips=results,
        macro=_macro(results),
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
    DEFAULT_SUITE_RESULTS.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_SUITE_RESULTS.write_text(json.dumps(report.to_dict(), indent=2))
    print(report.to_markdown())
    print(f"\nFull JSON: {DEFAULT_SUITE_RESULTS}")
    return 0


# ---------------------------------------------------------------------------
# Compare mode
# ---------------------------------------------------------------------------

def _flat_metrics(report: dict) -> dict[str, float]:
    """Flatten macro + per-clip overall metrics into dotted keys."""
    out: dict[str, float] = {}
    macro = report.get("macro", {})
    for k in ("precision", "recall", "f1"):
        if k in macro:
            out[f"macro.{k}"] = float(macro[k])
    for clip in report.get("clips", []):
        name = clip["name"]
        ov = clip.get("overall", {})
        for k in ("precision", "recall", "f1"):
            if k in ov:
                out[f"{name}.overall.{k}"] = float(ov[k])
        for risk, band in clip.get("by_risk", {}).items():
            for k in ("precision", "recall", "f1"):
                if k in band:
                    out[f"{name}.risk.{risk}.{k}"] = float(band[k])
    return out


def run_compare(baseline_path: Path, current_path: Path) -> int:
    if not baseline_path.exists():
        print(f"Baseline not found: {baseline_path}", file=sys.stderr)
        return 2
    if not current_path.exists():
        print(f"Current not found: {current_path}", file=sys.stderr)
        return 2
    base = json.loads(baseline_path.read_text())
    cur = json.loads(current_path.read_text())
    base_m = _flat_metrics(base)
    cur_m = _flat_metrics(cur)

    regressions: list[tuple[str, float, float, float]] = []
    improvements: list[tuple[str, float, float, float]] = []
    for key in sorted(set(base_m) | set(cur_m)):
        b = base_m.get(key)
        c = cur_m.get(key)
        if b is None or c is None:
            continue
        delta = c - b
        if delta < -REGRESSION_THRESHOLD:
            regressions.append((key, b, c, delta))
        elif delta > REGRESSION_THRESHOLD:
            improvements.append((key, b, c, delta))

    print("## Regressions (>3% drop)")
    if not regressions:
        print("  none")
    for key, b, c, d in regressions:
        print(f"  WARN {key}: {b:.3f} -> {c:.3f} (Δ {d:+.3f})")

    print("\n## Improvements (>3% gain)")
    if not improvements:
        print("  none")
    for key, b, c, d in improvements:
        print(f"  OK   {key}: {b:.3f} -> {c:.3f} (Δ {d:+.3f})")

    return 1 if regressions else 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Road-safety eval harness")
    parser.add_argument(
        "--suite",
        action="store_true",
        help="Run the full test-suite (manifest-driven).",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help=f"Manifest path (default: {DEFAULT_MANIFEST}).",
    )
    parser.add_argument(
        "--compare",
        nargs=2,
        metavar=("BASELINE", "CURRENT"),
        help="Diff two suite JSON reports; exit nonzero on regressions > 3%%.",
    )
    args = parser.parse_args(argv)

    if args.compare:
        return run_compare(Path(args.compare[0]), Path(args.compare[1]))
    if args.suite:
        return run_suite(args.manifest)
    return run_single_clip()


if __name__ == "__main__":
    sys.exit(main())
