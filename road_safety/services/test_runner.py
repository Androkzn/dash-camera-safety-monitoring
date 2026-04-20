"""Background test runner — executes pytest and streams structured results.

Runs the test suite in a background thread during server startup.  Exposes
live progress via a simple in-memory state object that the API layer polls.

Role in the system
------------------
The dashboard lets operators trigger a test run from the UI (``POST
/api/tests/run``) and then polls for progress (``GET /api/tests/status``).
Because pytest itself is CPU-heavy and spawns a subprocess, we don't want
to block the FastAPI event loop; instead we run it on a daemon thread and
expose a mutable state object the API layer can serialise on each poll.

Frontend polling is **adaptive**: while a run is in progress the UI polls
every ~1.5s for snappy feedback; once idle it drops to ~10s to avoid
wasting cycles.

Pytest output is captured two ways:

  1. **Primary** — ``pytest-json-report`` writes a structured report
     file.  We read/parse it via ``_parse_json_report``.
  2. **Fallback** — if the plugin is missing, ``_run_pytest_basic`` scrapes
     ``PASSED``/``FAILED`` lines from stdout with a regex.

Key paths
---------
* ``.venv/bin/python``   — preferred interpreter; falls back to ``python``
                            on ``PATH`` if the venv is absent.
* ``.test-report.json``  — temporary pytest-json-report output file; deleted
                            after each run.

Env vars
--------
None directly; behaviour is hard-coded except for the interpreter
discovery (which checks for ``.venv``).

Python idioms used in this file
-------------------------------
* ``@dataclass`` — auto-generates ``__init__``/``__repr__`` from the field
  annotations.
* ``field(default_factory=...)`` — gives each instance its own fresh
  mutable default (list/dict/Lock).  A bare ``= []`` would share one list
  across all instances.
* ``threading.Lock`` + ``with self._lock:`` — mutex; the ``with`` form
  guarantees release even on exception.
* ``threading.Thread(target=fn, daemon=True)`` — a daemon thread is killed
  automatically when the main process exits (no lingering pytest runs).
* ``subprocess.run([...], capture_output=True, text=True, timeout=N)`` —
  runs a child process synchronously, collects stdout/stderr as strings,
  and kills it if it exceeds ``timeout`` seconds.  ``capture_output=True``
  is shorthand for ``stdout=PIPE, stderr=PIPE``.
* ``subprocess.TimeoutExpired`` — specific exception raised on timeout;
  lets us distinguish "slow tests" from other failures.
* ``pathlib.Path`` — object-oriented paths; ``.exists()``, ``.unlink()``,
  ``.read_text()`` methods are all pathlib conveniences.
* ``re.match(pattern, line)`` — anchored regex match at the start of a
  string.  ``m.group(n)`` fetches capture groups.
* ``json.loads`` / ``json.JSONDecodeError`` — parse text JSON; the
  exception fires when the input is malformed.
* ``"…"`` at end of truncated string — explicit ellipsis in the UI.
* ``from __future__ import annotations`` — defers type-hint evaluation.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------
# subprocess — spawn the pytest child process and capture its output.
# json       — parse the pytest-json-report output file.
# re         — regex scraping in the stdout-fallback parser.
# threading  — background thread + mutex on shared state.
# time       — wall-clock timestamps on started_at / finished_at.
# dataclasses — concise container classes for state and results.
# pathlib    — filesystem paths for interpreter discovery and report file.
import subprocess
import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

# PROJECT_ROOT is the single-source-of-truth anchor for "where the repo
# lives on disk".  Comes from config.py per project rules.
from road_safety.config import PROJECT_ROOT


# ===========================================================================
# Data containers
# ===========================================================================
@dataclass
class TestResult:
    """One row of the test report.

    A dataclass — see the module docstring for why we use them.

    Fields:
      * ``node_id``     — pytest's full identifier (``path::Class::test``).
      * ``file`` / ``name`` — parsed out of ``node_id`` for convenience.
      * ``outcome``     — one of ``passed``, ``failed``, ``error``, ``skipped``.
      * ``duration_ms`` — test wall time in milliseconds.
      * ``message``     — truncated failure traceback (passing tests → "").
    """

    node_id: str
    file: str
    name: str
    outcome: str  # "passed", "failed", "error", "skipped"
    duration_ms: float = 0.0
    message: str = ""


@dataclass
class TestRunState:
    """Shared, mutable state that the API layer polls.

    Lifecycle: one global instance (``run_state`` below).  Mutated by the
    background runner thread; read by HTTP request handlers.  All mutation
    and reading goes through ``self._lock`` to avoid torn reads.

    Fields:
      * ``status``      — ``idle``, ``running``, ``passed``, or ``failed``.
      * ``started_at`` / ``finished_at`` — Unix timestamps.
      * ``total`` / ``passed`` / ``failed`` / ``errors`` / ``skipped`` —
        running counters.
      * ``progress``    — how many tests have a known outcome so far.
      * ``results``     — list of ``TestResult`` rows.
      * ``error_output`` — tail of pytest stdout when the run fails.
      * ``_lock``        — guards every field above.  Leading underscore
                            marks it private; ``repr=False`` excludes it
                            from the auto-generated ``__repr__``.
    """

    status: str = "idle"  # idle | running | passed | failed
    started_at: float = 0.0
    finished_at: float = 0.0
    total: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    progress: int = 0
    results: list[TestResult] = field(default_factory=list)
    error_output: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def as_dict(self) -> dict:
        """Snapshot the state as a JSON-serialisable dict.

        Uses the lock to ensure we don't observe a half-updated state.

        Returns:
            A dict mirroring the fields, with ``results`` expanded to
            per-test dicts and an ``elapsed_sec`` computed from
            ``started_at`` / ``finished_at`` (or "now" if still running).
        """
        with self._lock:
            elapsed = 0.0
            if self.started_at:
                # If we've finished, measure against finished_at.  If still
                # running, measure against "now" — gives the UI a live
                # ticking elapsed counter.
                end = self.finished_at or time.time()
                elapsed = round(end - self.started_at, 2)
            return {
                "status": self.status,
                "elapsed_sec": elapsed,
                "total": self.total,
                "passed": self.passed,
                "failed": self.failed,
                "errors": self.errors,
                "skipped": self.skipped,
                "progress": self.progress,
                # List comprehension: transform each TestResult into a dict.
                "results": [
                    {
                        "node_id": r.node_id,
                        "file": r.file,
                        "name": r.name,
                        "outcome": r.outcome,
                        "duration_ms": round(r.duration_ms, 1),
                        "message": r.message,
                    }
                    for r in self.results
                ],
                "error_output": self.error_output,
            }


# ---------------------------------------------------------------------------
# Module-level singleton.  Import this, don't construct your own.
# ---------------------------------------------------------------------------
run_state = TestRunState()

# ---------------------------------------------------------------------------
# Interpreter discovery
# ---------------------------------------------------------------------------
# Prefer the project's virtualenv Python so tests run against the same
# dependencies the server is using.  Fall back to whatever ``python`` is on
# PATH when there's no venv (CI, fresh clone).
_VENV_PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"
_PYTHON = str(_VENV_PYTHON) if _VENV_PYTHON.exists() else "python"


# ===========================================================================
# JSON-report parser (primary path)
# ===========================================================================
def _parse_json_report(report_path: Path) -> None:
    """Parse a pytest-json-report file and populate ``run_state`` in place.

    The ``pytest-json-report`` plugin writes a structured description of
    the run — test ids, outcomes, durations, full tracebacks — which is
    much richer and easier to parse than stdout.  See
    https://github.com/numirias/pytest-json-report for the schema.

    Args:
        report_path: Path to the JSON file the plugin wrote.

    Returns:
        None.  Mutates the global ``run_state``.

    Raises:
        Nothing — missing file or malformed JSON degrades silently.
    """
    if not report_path.exists():
        return
    try:
        # ``read_text`` returns the whole file as a string; ``json.loads``
        # parses it into Python objects.
        data = json.loads(report_path.read_text())
    except (json.JSONDecodeError, OSError):
        # Corrupt report or I/O error — give up on structured parsing.
        # The caller may still report a top-level failure via stdout.
        return

    tests = data.get("tests", [])
    # Acquire the lock for the whole update so polls see either the old
    # snapshot or the new one, never a torn mix.
    with run_state._lock:
        run_state.total = len(tests)
        run_state.passed = 0
        run_state.failed = 0
        run_state.errors = 0
        run_state.skipped = 0
        run_state.results = []

        for t in tests:
            node_id = t.get("nodeid", "")
            outcome = t.get("outcome", "unknown")
            # Plugin reports seconds; we expose milliseconds to the UI.
            duration = t.get("duration", 0.0) * 1000
            # Extract file and test name from node_id like "tests/test_core.py::TestX::test_y"
            parts = node_id.split("::")
            file_part = parts[0] if parts else ""
            # Join everything after the first segment — handles nested
            # class::method as well as plain function tests.
            name_part = "::".join(parts[1:]) if len(parts) > 1 else node_id

            message = ""
            call = t.get("call", {})
            if outcome == "failed" and call:
                longrepr = call.get("longrepr", "")
                if longrepr:
                    # Truncate long tracebacks so the API payload stays
                    # reasonable.  500 chars shows the essential "where
                    # and why" while keeping the JSON small.
                    message = longrepr if len(longrepr) < 500 else longrepr[:500] + "…"

            result = TestResult(
                node_id=node_id,
                file=file_part,
                name=name_part,
                outcome=outcome,
                duration_ms=duration,
                message=message,
            )
            run_state.results.append(result)

            # Bucket by outcome.  Anything we don't recognise is ignored
            # rather than mis-counted.
            if outcome == "passed":
                run_state.passed += 1
            elif outcome == "failed":
                run_state.failed += 1
            elif outcome == "error":
                run_state.errors += 1
            elif outcome == "skipped":
                run_state.skipped += 1

        # After a successful parse we know ``progress == total``.
        run_state.progress = run_state.total


# ===========================================================================
# Primary subprocess driver
# ===========================================================================
def _run_pytest() -> None:
    """Run pytest in a subprocess, capturing structured output.

    Two-phase: first a cheap ``--collect-only`` pass to learn the total
    count (for the progress bar), then the real run with the JSON-report
    plugin.  Any transient error falls back to the stdout-scraping path.

    Args:
        None — reads/writes the global ``run_state``.

    Returns:
        None.

    Raises:
        Nothing.  All exceptions are caught and surfaced via
        ``run_state.error_output``.
    """
    # Temp file for pytest-json-report output.  Lives at the repo root so
    # it's obvious / greppable during debugging.
    report_path = PROJECT_ROOT / ".test-report.json"
    if report_path.exists():
        # Remove any stale report from a previous run before starting.
        report_path.unlink()

    # Reset all counters under the lock so partial state never leaks out
    # to the first status poll of this run.
    with run_state._lock:
        run_state.status = "running"
        run_state.started_at = time.time()
        run_state.finished_at = 0.0
        run_state.total = 0
        run_state.passed = 0
        run_state.failed = 0
        run_state.errors = 0
        run_state.skipped = 0
        run_state.progress = 0
        run_state.results = []
        run_state.error_output = ""

    # --- Phase 1: collect-only pass to count tests for the progress bar.
    # 30s timeout is generous — collection is I/O-light and should finish
    # in well under a second for this codebase.
    try:
        collect = subprocess.run(
            [_PYTHON, "-m", "pytest", "tests/", "--collect-only", "-q", "--no-header"],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT), timeout=30,
        )
        # Count lines like "tests/test_core.py::TestBbox::test_x"
        count = 0
        for line in collect.stdout.strip().splitlines():
            if "::" in line:
                count += 1
        if count > 0:
            with run_state._lock:
                run_state.total = count
    except Exception:
        # Collection is a nice-to-have for the progress bar; if it fails
        # we continue without a known total — the UI just shows "running".
        pass

    # --- Phase 2: run the actual tests with JSON report.
    #
    # Flags explained:
    #   -x                   stop at first failure (faster feedback in UI)
    #   --tb=short           compact traceback format
    #   --no-header          suppress pytest's banner in stdout
    #   --json-report-file   where pytest-json-report writes its output
    #   --json-report        enable the JSON reporter
    #
    # Timeout of 120s is deliberate: this suite is intended to complete in
    # under a minute.  A 2x safety margin catches pathological slowdowns
    # (e.g. disk thrashing) while bounding worst-case dashboard latency.
    try:
        result = subprocess.run(
            [
                _PYTHON, "-m", "pytest", "tests/",
                "-x",
                "--tb=short",
                "--no-header",
                f"--json-report-file={report_path}",
                "--json-report",
            ],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT), timeout=120,
        )
        _parse_json_report(report_path)

        with run_state._lock:
            run_state.finished_at = time.time()
            if run_state.failed > 0 or run_state.errors > 0:
                run_state.status = "failed"
                if result.stdout:
                    # Last 20 stdout lines usually contain pytest's
                    # summary section — useful context for the UI.
                    lines = result.stdout.strip().splitlines()
                    run_state.error_output = "\n".join(lines[-20:])
            else:
                run_state.status = "passed"

    except subprocess.TimeoutExpired:
        # Dedicated branch so the UI can show a clear message instead of
        # a generic failure.
        with run_state._lock:
            run_state.status = "failed"
            run_state.finished_at = time.time()
            run_state.error_output = "Test suite timed out after 120 seconds"
    except FileNotFoundError:
        # pytest-json-report not installed, fall back to basic parsing
        _run_pytest_basic()
    except Exception as exc:
        # Catch-all safety net — report the error string and move on.
        with run_state._lock:
            run_state.status = "failed"
            run_state.finished_at = time.time()
            run_state.error_output = str(exc)
    finally:
        # Always clean up the temp file, even on exceptions — keeps the
        # repo tidy between runs.  The ``finally`` block runs no matter
        # how the ``try`` exits (success, exception, or return).
        if report_path.exists():
            try:
                report_path.unlink()
            except OSError:
                pass


# ===========================================================================
# Fallback subprocess driver (no pytest-json-report installed)
# ===========================================================================
def _run_pytest_basic() -> None:
    """Fallback: run pytest verbosely and scrape results from stdout.

    Used when ``pytest-json-report`` is not installed.  Less accurate
    (no per-test durations, no failure traceback), but enough for the
    dashboard's pass/fail counters.

    Args:
        None — mutates ``run_state``.

    Returns:
        None.

    Raises:
        Nothing — all exceptions are surfaced via ``run_state.error_output``.
    """
    try:
        result = subprocess.run(
            [
                _PYTHON, "-m", "pytest", "tests/",
                "-v", "--tb=short", "--no-header",
            ],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT), timeout=120,
        )

        with run_state._lock:
            run_state.results = []
            run_state.passed = 0
            run_state.failed = 0
            run_state.errors = 0
            run_state.skipped = 0

            for line in result.stdout.splitlines():
                # Match lines like: tests/test_core.py::TestX::test_y PASSED
                #
                # ``\S+`` = one-or-more non-whitespace chars.  The anchor
                # ``^`` ensures we only match pytest's verbose output lines
                # and not any stray "PASSED" appearing mid-traceback.
                m = re.match(r'^(tests/\S+::\S+)\s+(PASSED|FAILED|ERROR|SKIPPED)', line)
                if not m:
                    continue
                # ``group(1)`` → first capture group (the node id),
                # ``group(2)`` → second (the outcome keyword).
                node_id = m.group(1)
                outcome = m.group(2).lower()
                parts = node_id.split("::")
                file_part = parts[0] if parts else ""
                name_part = "::".join(parts[1:]) if len(parts) > 1 else node_id

                run_state.results.append(TestResult(
                    node_id=node_id,
                    file=file_part,
                    name=name_part,
                    outcome=outcome,
                ))

                if outcome == "passed":
                    run_state.passed += 1
                elif outcome == "failed":
                    run_state.failed += 1
                elif outcome == "error":
                    run_state.errors += 1
                elif outcome == "skipped":
                    run_state.skipped += 1

            run_state.total = len(run_state.results)
            run_state.progress = run_state.total
            run_state.finished_at = time.time()

            if run_state.failed > 0 or run_state.errors > 0:
                run_state.status = "failed"
                lines = result.stdout.strip().splitlines()
                run_state.error_output = "\n".join(lines[-20:])
            else:
                run_state.status = "passed"

    except subprocess.TimeoutExpired:
        with run_state._lock:
            run_state.status = "failed"
            run_state.finished_at = time.time()
            run_state.error_output = "Test suite timed out after 120 seconds"
    except Exception as exc:
        with run_state._lock:
            run_state.status = "failed"
            run_state.finished_at = time.time()
            run_state.error_output = str(exc)


# ===========================================================================
# Public API
# ===========================================================================
def start_test_run() -> None:
    """Launch the test suite in a background thread. Non-blocking.

    Called by ``POST /api/tests/run``.  Returns immediately after spawning
    the daemon thread.  While the thread is running, ``run_state`` is
    updated live so ``GET /api/tests/status`` can report progress.

    Args:
        None.

    Returns:
        None.  If a run is already in progress this is a no-op — we never
        let two pytest processes race.

    Raises:
        Nothing.  Any subprocess error surfaces via ``run_state``, not as
        a raised exception here.
    """
    # Idempotency guard: already running → do nothing.  Prevents a double
    # click on the dashboard button from forking a parallel run.
    if run_state.status == "running":
        return
    # ``daemon=True`` means the thread is killed when the main process
    # exits — no orphaned pytest processes after a Ctrl-C.
    # ``name="test-runner"`` makes it identifiable in ``threading.enumerate()``.
    t = threading.Thread(target=_run_pytest, daemon=True, name="test-runner")
    t.start()
