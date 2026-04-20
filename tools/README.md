# tools/

Offline utility scripts. None of these run as part of the live edge server — they are invoked by hand (or from `make`) to analyse recordings, benchmark detection quality, or score LLM enrichment. They all import from the `road_safety` package, so run them from the project root with the virtualenv active.

## Scripts

### `analyze.py` — batch event extraction from a video file

Runs the same detection pipeline as the live server (`road_safety/core/detection.py`) against a pre-recorded clip and writes structured events + thumbnails to `data/`.

```bash
python tools/analyze.py data/input.mp4
```

Use when you have a clip and want to see what the detector would have produced, without starting the full server/UI.

### `eval_detect.py` — detection quality harness

Scores detector output against hand-labelled ground truth. Three modes:

```bash
# 1. Single-clip (legacy): reads data/events.json + data/labels.json
python tools/eval_detect.py

# 2. Suite: runs analyze.py across every clip in the test manifest
python tools/eval_detect.py --suite --manifest data/test_suite/manifest.json

# 3. Compare two prior suite reports (flags regressions > 3%)
python tools/eval_detect.py --compare baseline.json current.json
```

Outputs precision / recall / F1. A detection counts as a true positive only if the `event_type` matches an unclaimed label within its tolerance window.

### `eval_enrich.py` — LLM enrichment quality harness

Tiny deterministic scorer for `enrich_event()` in `road_safety/services/llm.py`. Drop fixture JPGs in `data/eval_fixtures/` (see that directory's README for required filenames) and run:

```bash
python tools/eval_enrich.py
```

Prints per-case scores and a summary line `EVAL_SCORE=X/12`. Exits 0 if fixtures are missing, so it is safe to wire into CI without blocking builds on fixture availability.

## Notes

- All three scripts import paths from `road_safety/config.py` — never hard-code paths here.
- Output lives under `data/`; most of those paths are git-ignored. See the project root `.gitignore`.
