"""HTTP API route modules.

This package groups FastAPI *sub-routers* — groups of related HTTP
endpoints that are registered onto the main FastAPI app inside
``road_safety/server.py`` at startup.

Python primer for this package
------------------------------
- FastAPI is a web framework. The main application object lives in
  ``server.py`` (``app = FastAPI()``). Rather than define every endpoint on
  that single object, we factor route groups into modules here and each
  module exposes a ``mount(app, ...)`` function that attaches its routes
  back onto the main app. This keeps ``server.py`` readable and lets us
  unit-test route groups in isolation.
- A directory becomes a Python "package" when it contains an
  ``__init__.py`` (even an empty one). The triple-quoted string at the top
  of a module is a *docstring*; it appears in ``help()`` and IDE tooltips.

Modules living here
-------------------
- ``feedback`` — operator "thumbs up / thumbs down" routes. Accepts
  verdicts on individual safety events (true-positive vs false-positive)
  and exposes a coaching queue of medium-risk events awaiting review.

Most server routes still live directly in ``road_safety/server.py``; only
self-contained, operator-facing feature slices (like feedback) get lifted
out here.
"""
