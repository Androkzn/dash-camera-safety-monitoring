# Road-safety test suite

Regression corpus for the dashcam event detector. Each clip is a short video
plus a hand-labelled JSON of the events it should produce. `tools/eval_detect.py --suite`
runs `tools/analyze.py` against every clip and scores precision / recall / F1 split
by risk band and event type.

## Layout

```
data/test_suite/
├── README.md                 (this file)
├── example_manifest.json     (template; copy to manifest.json)
├── manifest.json             (real manifest, gitignored if you prefer)
├── clips/
│   ├── pedestrian_crossing_day.mp4
│   ├── pedestrian_crossing_day.labels.json
│   ├── tailgate_highway_dusk.mp4
│   └── tailgate_highway_dusk.labels.json
└── results.json              (written by eval_detect.py --suite)
```

## Adding a clip

1. Drop the video into `data/test_suite/clips/<name>.mp4`.
2. Create `data/test_suite/clips/<name>.labels.json` — a list of ground-truth
   event dicts (schema below).
3. Append an entry to `manifest.json`:
   ```json
   {
     "name":   "<name>",
     "video":  "data/test_suite/clips/<name>.mp4",
     "labels": "data/test_suite/clips/<name>.labels.json"
   }
   ```

## Label schema

Each label file is a JSON array of objects:

| field           | type                                    | required | notes                                                               |
|-----------------|-----------------------------------------|----------|---------------------------------------------------------------------|
| `timestamp_sec` | float                                   | yes      | Seconds from start of clip.                                          |
| `event_type`    | string                                  | yes      | Must match an `event_type` emitted by `tools/analyze.py` (e.g. `pedestrian_proximity`). |
| `risk_level`    | `"high"` / `"medium"` / `"low"`         | no       | If omitted, matcher treats risk as a wildcard (backwards compat).    |
| `tolerance_sec` | float                                   | no       | Per-label match window. Defaults to 1.5s.                            |

Example:

```json
[
  {"timestamp_sec": 12.5, "event_type": "pedestrian_proximity", "risk_level": "high"},
  {"timestamp_sec": 27.1, "event_type": "tailgating", "risk_level": "medium", "tolerance_sec": 2.0}
]
```

## Running the suite

```bash
python tools/eval_detect.py --suite
# or with an alternate manifest
python tools/eval_detect.py --suite --manifest path/to/manifest.json
```

This prints a markdown matrix (one row per clip) and writes a full JSON
report to `data/test_suite/results.json`. Missing videos or labels are
reported as per-clip warnings — they do not crash the suite.

## CI regression gate

Freeze a `baseline.json` (a known-good `results.json`) alongside the suite.
In CI, run:

```bash
python tools/eval_detect.py --suite
python tools/eval_detect.py --compare baseline.json data/test_suite/results.json
```

`--compare` exits non-zero if any metric (`macro.{P,R,F1}`, per-clip overall,
or per-clip per-risk) drops by more than 3% vs baseline. The markdown matrix
also auto-flags rows where:

- `high`-risk recall falls below 0.80 (missed near-collisions are
  catastrophic), or
- overall precision falls below 0.70 (alert fatigue from false positives).

Treat those flags as blocking for a release build.
