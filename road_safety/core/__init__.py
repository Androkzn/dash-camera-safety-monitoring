"""Core detection pipeline: YOLO + ByteTrack, ego-motion, scene context, quality.

This package is the perception layer — the "eyes" of the fleet-safety system.
Every frame that flows through the server passes through the modules defined
here before any business logic (alerting, Slack, cloud publish) is considered.

Package contents (read these in roughly this order to understand the pipeline):

  - ``stream.py``     — ``StreamReader``: pulls frames from HLS, RTSP, local
                         files, or webcams. Runs in a background OS thread so
                         OpenCV's blocking ``.read()`` call does not stall the
                         FastAPI event loop.
  - ``detection.py``  — YOLOv8 + ByteTrack detection, per-track history ring
                         buffers, monocular distance estimation (pinhole /
                         ground-plane geometry), time-to-collision (TTC) math,
                         convergence-angle filtering, and physical-unit risk
                         classification.
  - ``egomotion.py``  — optical-flow ego-speed proxy (how fast is *our* car
                         moving?). Used by the ``LOW_SPEED_FLOOR_MPS`` gate to
                         suppress false alerts when the ego vehicle is
                         stopped at a red light / parked.
  - ``context.py``    — scene classifier (urban vs. highway vs. parking).
                         Rescales TTC / distance thresholds via
                         ``AdaptiveThresholds`` because a "close call" means
                         very different things at 60 mph vs. 5 mph.
  - ``quality.py``    — ``QualityMonitor``: suppresses events when the camera
                         feed itself is degraded (blur, darkness, dropped
                         frames) so we don't alert on garbage inputs.

The hot-path conflict-detection *gates* live in ``server.py::_run_loop``.
Each module here contributes one or more gates; see ``CLAUDE.md`` for the
full gate list. The key invariant is: **a real conflict must pass all
gates; noise fails early.**
"""
