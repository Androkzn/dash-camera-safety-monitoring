#!/usr/bin/env python3
"""One-command launcher: builds frontend, runs tests, starts the server,
waits for it to be healthy, then opens the admin dashboard in the browser.

Usage:
    python start.py                # build + test + start + open browser
    python start.py --skip-tests   # start without running tests (fast loop)
    python start.py --cloud        # also start cloud_receiver on port 8001
    python start.py --no-browser   # headless (CI, servers without a GUI)
    python start.py --port 3000    # override the main server port

Role:
    Convenience wrapper for local development. Production deployments use
    systemd or Docker (see ``docker-compose.yml``); this launcher does **not**
    replace those — it exists so a developer running ``python start.py`` gets
    a working environment without memorising the uvicorn invocation.

Order of operations is deliberate; changing it breaks the contract with
developers:

    1. Build frontend (``npm run build``) so the server can serve the bundled
       React app. Skipped gracefully if ``frontend/`` doesn't exist.
    2. Run the pytest suite (``--skip-tests`` to skip). Failures **do not
       abort** — we still start the server so devs can iterate even when a
       test is broken. The exit code is just printed.
    3. Spawn uvicorn as a subprocess pointing at ``road_safety.server:app``.
    4. Optionally spawn the cloud receiver (``cloud.receiver:app``) on 8001.
    5. Poll ``/api/live/status`` until it returns 200 (up to 120 s).
    6. Print a status table and open the admin UI in the default browser.
    7. Block on ``server_proc.wait()`` until the user hits Ctrl+C, at which
       point the SIGINT handler cleanly terminates every child process.

Shell-safety notes (first-time Python idioms used below):

    * ``#!/usr/bin/env python3`` — "shebang" line. On Unix this makes the
      file directly executable (``./start.py``) by dispatching to the first
      ``python3`` on ``$PATH``. Irrelevant on Windows; harmless there.
    * ``from __future__ import annotations`` — defers evaluation of type
      hints so newer syntax (``dict | None``) works on older Pythons and
      avoids import cycles when typing is heavy.
    * ``argparse`` — stdlib CLI-flag parser. Each ``add_argument`` defines
      a flag, type coercion, help text, and default.
    * ``subprocess.Popen`` — launches a child process *asynchronously*
      (unlike ``subprocess.run`` which blocks). We keep the handle so we can
      terminate it later.
    * ``signal.signal(SIGINT, cleanup)`` — register a handler for Ctrl+C so
      child processes don't get orphaned when the user interrupts.
    * ``webbrowser.open`` — stdlib helper that asks the OS to open a URL in
      the user's default browser. No-ops gracefully over SSH.
    * ``if __name__ == "__main__":`` — Python's "run only as a script, not
      when imported" guard. Python sets ``__name__`` to ``"__main__"`` only
      for the entrypoint, so this block is skipped if something imports
      ``start.py`` as a module.
"""

from __future__ import annotations

# Stdlib only. No third-party dependencies — start.py must run on a fresh
# checkout before the project's own requirements are installed (it *is* the
# thing that runs the installer indirectly via npm).
import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

# ``__file__`` is the path to this script. ``.parent`` yields the directory
# the script lives in (the project root). Using ``pathlib`` avoids brittle
# string concatenation.
ROOT = Path(__file__).parent
FRONTEND_DIR = ROOT / "frontend"
# Prefer the project's virtualenv interpreter if it exists; otherwise fall
# back to whichever ``python`` invoked this script. This lets devs run
# ``python start.py`` from anywhere and still pick up the pinned deps.
VENV_PYTHON = ROOT / ".venv" / "bin" / "python"
PYTHON = str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable

# Network binding defaults — overridable via env so the same launcher can
# drive both the local dev box (loopback) and CI fixtures (all-interfaces).
SERVER_HOST = os.getenv("ROAD_HOST", "127.0.0.1")
SERVER_PORT = int(os.getenv("ROAD_PORT", "3000"))
CLOUD_PORT = int(os.getenv("ROAD_CLOUD_PORT", "8001"))

# ANSI terminal colour codes used purely for nicer console output. Most
# terminals render these; Windows cmd.exe on old builds does not but
# degrades to visible garbage rather than breaking anything.
#   G=green, Y=yellow, R=red, C=cyan, B=bold, D=dim, Z=reset.
G = "\033[92m"
Y = "\033[93m"
R = "\033[91m"
C = "\033[96m"
B = "\033[1m"
D = "\033[2m"
Z = "\033[0m"


def banner():
    """Print the start-of-run banner. Purely cosmetic."""
    print(f"""
{C}{B}  Road Safety{Z}
{D}  ─────────────────────────────────────{Z}
""")


def build_frontend() -> bool:
    """Install JS deps (if needed) and build the React frontend.

    The server serves the bundled ``frontend/dist/`` if it exists
    (see ``road_safety/config.py::STATIC_DIR``). Building here means the
    admin UI is live the moment uvicorn reports healthy.

    Returns:
        ``True`` when the build succeeded *or* when there is no frontend
        directory at all (a pure-API deployment). ``False`` on explicit
        npm failure — the caller aborts the launch in that case.
    """
    if not FRONTEND_DIR.exists():
        # Running from a slimmed-down checkout / test container without the
        # frontend source. The static-files mount in the server will fail at
        # boot if ``frontend/dist/`` is also absent — that's the intended
        # signal to provision the build before launching.
        print(f"  {D}No frontend/ directory — skipping React build{Z}")
        return True

    # ``node_modules`` missing means either first-ever run or a deliberate
    # clean. Run ``npm install`` once before ``npm run build``.
    node_modules = FRONTEND_DIR / "node_modules"
    if not node_modules.exists():
        print(f"  {Y}Installing frontend dependencies…{Z}")
        # ``subprocess.run`` blocks until the child exits, returning a
        # ``CompletedProcess`` whose ``.returncode`` is the exit status.
        # ``cwd=`` changes the working directory for the child only.
        result = subprocess.run(["npm", "install"], cwd=str(FRONTEND_DIR))
        if result.returncode != 0:
            print(f"  {R}npm install failed{Z}")
            return False

    print(f"  {Y}Building React frontend…{Z}")
    result = subprocess.run(["npm", "run", "build"], cwd=str(FRONTEND_DIR))
    if result.returncode == 0:
        print(f"  {G}Frontend built successfully{Z}\n")
        return True
    print(f"  {R}Frontend build failed{Z}\n")
    return False


def run_tests() -> bool:
    """Run the pytest suite.

    Flags passed:
        * ``-x``        — stop on first failure (faster signal, less noise).
        * ``-q``        — quieter output (one char per test).
        * ``--tb=short``— compact traceback format.
        * ``--no-header``— suppress platform/plugins banner.

    Returns:
        ``True`` if all tests pass. Note: the caller does **not** abort on
        failure — a broken test should not block local iteration. We still
        print the exit code so devs see it.
    """
    print(f"  {Y}Running tests…{Z}")
    result = subprocess.run(
        [PYTHON, "-m", "pytest", "tests/", "-x", "-q", "--tb=short", "--no-header"],
        cwd=str(ROOT),
    )
    if result.returncode == 0:
        print(f"  {G}All tests passed{Z}\n")
        return True
    print(f"  {R}Some tests failed (exit {result.returncode}){Z}\n")
    return False


def wait_for_health(url: str, timeout: int = 120) -> dict | None:
    """Poll a health URL until it returns a JSON response.

    Polls once every ~1.5 s, up to ``timeout`` seconds. Each failed attempt
    updates an animated "Waiting for server…" line so the developer sees
    progress. Success returns the decoded JSON body so the caller can print
    live status.

    Args:
        url: Fully-qualified health endpoint (e.g.
            ``http://127.0.0.1:8000/api/live/status``).
        timeout: Upper bound in seconds. Default 120 is generous — YOLO
            model warm-up alone takes 20-40 s on a cold cache.

    Returns:
        The decoded JSON dict on the first successful response, or ``None``
        if the timeout expired without a 2xx.

    Edge cases:
        * Server returns non-JSON: ``json.loads`` raises and we keep polling.
        * Server returns 500: ``urlopen`` raises ``HTTPError``, same.
        * Connection refused (server not yet listening): ``URLError``, same.
    """
    start = time.time()
    attempt = 0
    while time.time() - start < timeout:
        attempt += 1
        # Cycle 0..3 dots for a spinner-like effect. ``:<4`` pads to width 4
        # so shorter strings overwrite the previous frame cleanly.
        dots = "." * (attempt % 4)
        print(f"\r  {Y}Waiting for server{dots:<4}{Z}", end="", flush=True)
        try:
            # Build an explicit Request so we can set headers. The Accept
            # header is a mild hint to the server/logs that we expect JSON.
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            # ``with`` closes the HTTP response when the block exits, even
            # if parsing raises. ``timeout=3`` prevents a single slow call
            # from stalling the whole wait loop.
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode())
                # ``\r`` + spaces overwrites the "Waiting…" line cleanly.
                print(f"\r  {G}Server is up!{' ' * 20}{Z}")
                return data
        except Exception:
            # Intentionally broad: connection refused, timeout, JSON errors
            # all mean "not ready yet, try again". No need to differentiate.
            time.sleep(1.5)
    print(f"\r  {R}Timed out waiting for server ({timeout}s){Z}")
    return None


def print_status(data: dict, port: int):
    """Render the status table shown once the server is healthy.

    Args:
        data: Decoded JSON from ``/api/live/status``. Every field is looked
            up via ``.get(...)`` with a sensible default so a partial
            response still renders something readable.
        port: The port the server bound to, used to construct the admin /
            dashboard URLs printed at the bottom.
    """
    running = data.get("running", False)
    source = data.get("source", "—")
    fps = data.get("target_fps", "—")
    events = data.get("event_count", 0)
    frames = data.get("frames_processed", 0)
    llm = data.get("llm_configured", False)
    slack = data.get("slack_configured", False)
    tracker = data.get("tracker", "—")
    risk_model = data.get("risk_model", "—")
    perception = data.get("perception", {})
    p_state = perception.get("state", "—")
    # Green dot when the loop is running, red when idle — a one-glance cue.
    dot = f"{G}●{Z}" if running else f"{R}●{Z}"
    dashboard_url = f"http://{SERVER_HOST}:{port}/dashboard"
    health_url = f"http://{SERVER_HOST}:{port}/api/live/status"

    print(f"""
  {B}Server Status{Z}
  ─────────────────────────────────────
  {dot} Stream        {D}{source[:60]}{Z}
    Target FPS    {fps}
    Frames done   {frames}
    Events        {events}
    Tracker       {tracker}
    Risk model    {risk_model}
    Perception    {p_state}
    LLM           {"configured" if llm else "not configured"}
    Slack         {"configured" if slack else "not configured"}
  ─────────────────────────────────────
  {C}Admin UI{Z}      http://{SERVER_HOST}:{port}/
  {C}Dashboard{Z}     {dashboard_url}
  {C}API status{Z}    {health_url}
  ─────────────────────────────────────
""")


def main():
    """Orchestration entrypoint — runs the full launch sequence.

    The function is structured top-down as a story:
    parse flags → build → test → spawn → wait → report → block. Each step
    is commented inline so readers can follow the orchestration without
    jumping between helpers.
    """
    # ─── Step 1: parse CLI flags. ``argparse`` auto-generates ``--help``.
    parser = argparse.ArgumentParser(description="Start Road Safety servers")
    parser.add_argument("--cloud", action="store_true", help="Also start cloud_receiver on port 8001")
    parser.add_argument("--no-browser", action="store_true", help="Don't auto-open the browser")
    parser.add_argument("--skip-tests", action="store_true", help="Skip running the test suite")
    parser.add_argument("--port", type=int, default=SERVER_PORT, help="Server port (default 3000)")
    args = parser.parse_args()

    port = args.port
    health_url = f"http://{SERVER_HOST}:{port}/api/live/status"
    admin_url = f"http://{SERVER_HOST}:{port}/"

    banner()

    # ``procs`` collects every child we spawn so the cleanup handler can
    # terminate all of them at once, in any order, on shutdown.
    procs: list[subprocess.Popen] = []

    def cleanup(sig=None, frame=None):
        """Terminate all child processes and exit.

        Registered as the SIGINT / SIGTERM handler. Arguments are required
        by Python's signal-handler contract (``signum, frame``) but unused.

        Graceful first (``terminate`` = SIGTERM, give 5 s to flush logs),
        force kill (``kill`` = SIGKILL) if the child ignores SIGTERM. Never
        raises — the goal is to exit cleanly no matter what state the
        children are in.
        """
        print(f"\n  {Y}Shutting down…{Z}")
        for p in procs:
            try:
                p.terminate()
                p.wait(timeout=5)
            except Exception:
                p.kill()
        sys.exit(0)

    # ``signal.signal`` replaces the default handler for the given signal.
    # SIGINT is what Ctrl+C sends; SIGTERM is what ``kill <pid>`` or
    # ``docker stop`` sends. Handling both keeps cleanup uniform.
    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    # ─── Step 2: build the React frontend. Abort if it fails — running
    # with a stale bundle would confuse devs chasing UI bugs.
    if not build_frontend():
        print(f"  {R}Aborting — frontend build failed.{Z}")
        sys.exit(1)

    # ─── Step 3: run tests unless the dev explicitly opted out. A test
    # failure does NOT abort (see ``run_tests`` docstring).
    if not args.skip_tests:
        run_tests()

    # ─── Step 4: spawn the main server. ``Popen`` returns immediately —
    # the server starts loading YOLO weights etc. in the background while
    # we continue. ``uvicorn road_safety.server:app`` is the ASGI entry.
    print(f"  {D}Starting main server on :{port}…{Z}")
    server_proc = subprocess.Popen(
        [PYTHON, "-m", "uvicorn", "road_safety.server:app",
         "--host", SERVER_HOST, "--port", str(port),
         "--log-level", "warning"],
        cwd=str(ROOT),
    )
    procs.append(server_proc)

    # ─── Step 4b: optionally spawn the cloud receiver. It runs completely
    # independently — no shared memory, no shared state, only HMAC-signed
    # HTTP calls from the edge node.
    if args.cloud:
        print(f"  {D}Starting cloud receiver on :{CLOUD_PORT}…{Z}")
        cloud_proc = subprocess.Popen(
            [PYTHON, "-m", "uvicorn", "cloud.receiver:app",
             "--host", SERVER_HOST, "--port", str(CLOUD_PORT),
             "--log-level", "warning"],
            cwd=str(ROOT),
        )
        procs.append(cloud_proc)

    # ─── Step 5: poll the health endpoint until ready (or timeout).
    data = wait_for_health(health_url)
    if data is None:
        # Server never came up — cleanup() also calls sys.exit(0) so the
        # ``return`` afterwards is unreachable, but keep it as a safety
        # net in case cleanup's exit is ever removed.
        print(f"  {R}Server failed to start. Check logs above.{Z}")
        cleanup()
        return

    # ─── Step 6: print the status table and optionally open the browser.
    print_status(data, port)

    if not args.no_browser:
        print(f"  {G}Opening browser…{Z}")
        # ``webbrowser.open`` is best-effort: returns True if it thinks it
        # launched something, False otherwise. We never check — the URL is
        # printed above anyway so the dev can click it manually.
        webbrowser.open(admin_url)

    print(f"  {D}Press Ctrl+C to stop.{Z}\n")

    # ─── Step 7: block on the server process. When uvicorn exits (either
    # because we Ctrl+C'd or because it crashed), we fall through to the
    # cleanup handler. ``try/except KeyboardInterrupt`` is defensive — on
    # most platforms SIGINT is handled by ``cleanup`` above, but on Windows
    # the signal handling is less reliable so we catch it here too.
    try:
        server_proc.wait()
    except KeyboardInterrupt:
        cleanup()


# Standard Python entrypoint guard. See module docstring for the
# ``__name__ == "__main__"`` explanation.
if __name__ == "__main__":
    main()
