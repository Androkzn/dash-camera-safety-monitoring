"""Road Safety — live dashcam safety review system.

This file is the package marker for the ``road_safety`` Python package.

Python concept — package markers:
    A directory becomes an *importable package* when it contains a file named
    ``__init__.py``. The file can be empty, but by convention it holds the
    package's docstring and top-level metadata (like ``__version__``). Any code
    here runs exactly once the first time the package is imported anywhere in
    the process (e.g. ``import road_safety``).

Responsibility:
    - Declare the public version string (``__version__``) used for diagnostics
      and for the ``/api/live/status`` health endpoint.
    - Keep this file tiny. Heavy imports here would slow down every entry point
      (CLI, tests, uvicorn) because they all go through this module first.

Role in the project:
    - ``road_safety/`` is the main edge-node application (perception + FastAPI
      server). See ``road_safety/server.py`` for the app factory and
      ``road_safety/config.py`` for paths/env-var settings.
    - The separate ``cloud/`` package hosts the cloud receiver (port 8001)
      which ingests signed event batches from this edge node.
"""

# ``__version__`` is a Python community convention (see PEP 396). It is a
# plain string so it can be read by tooling without importing side effects.
# Bump on user-visible changes; the frontend pins against this via the health
# endpoint.
__version__ = "1.0.0"
