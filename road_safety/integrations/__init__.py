"""External service connectors: Slack, edge-to-cloud publisher, FNOL.

This package ("integrations") holds everything that talks to the **outside
world** from the edge node — any code path whose job is to cross a trust
boundary (HTTPS to a cloud service, a third-party webhook, etc.).

Python primer (read once, applies to the whole codebase)
--------------------------------------------------------
- A directory becomes a Python "package" when it contains an ``__init__.py``
  file (even an empty one). The triple-quoted string at the top of this
  module — the one you are reading — is called a *module docstring*; Python
  stores it on the package object as ``road_safety.integrations.__doc__``
  and tools like ``help()`` and IDEs surface it to developers.
- ``from road_safety.integrations import slack`` works because this file
  exists. Without ``__init__.py`` the import system would raise
  ``ModuleNotFoundError``.

Modules living here
-------------------
- ``edge_publisher``  — batched, HMAC-signed delivery of safety events from
  this edge node up to the cloud receiver. Handles queuing, retries, and
  privacy-safe thumbnail URL construction. Never sends raw frames. Never
  sends raw plate text.
- ``slack``           — tiered notifications to a Slack Incoming Webhook.
  Uses only *redacted* (public) thumbnails. High-risk alerts fire
  immediately (subject to a sustained-evidence quality gate); medium-risk
  events buffer into an hourly digest; low-risk events buffer into a daily
  digest.
- ``fnol``            — "First Notice of Loss" payload shaping for insurer
  intake. Builds the shaped record only — no HTTP transport wired up.
  Treat this as a stub that needs an insurer-specific adapter before
  production use.

Design rule for this package: no module here does network I/O at import
time. All outbound calls are lazy / async and only fire after explicit
configuration (env vars present, webhook URL set, etc.). This keeps imports
fast and keeps unit tests from accidentally hitting live endpoints.
"""
