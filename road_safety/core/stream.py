"""Live video stream capture: opens a source and reads frames in a background thread.

Role in the pipeline
--------------------
This module is the *first* stage of the perception pipeline — it produces the
raw frames that ``detection.py`` will then run YOLO on. Everything downstream
(tracking, TTC, risk classification, alerting) depends on a steady trickle of
frames from here.

Why a background OS thread (not asyncio)?
-----------------------------------------
The FastAPI server runs on an asyncio event loop. OpenCV's ``VideoCapture.read()``
is *synchronous* — it blocks the calling thread while waiting for the next
frame to arrive from the network. If we called it on the event loop, every
other HTTP request, SSE stream, watchdog tick, and Slack call would pause
until the next frame came in.

So we run capture in a *real OS thread* (``threading.Thread``) and hand each
frame to the asyncio side via a thin ``on_frame`` callback (which in turn
pushes into an asyncio queue — see ``server.py::_run_loop``). The thread does
the blocking I/O; the event loop consumes frames without blocking.

Supported sources
-----------------
  - Local files (looped in demo mode), plain HLS, RTSP, HTTP(S) MJPEG / mp4,
    and webcam device indices (``"0"``, ``"1"``, …). All are handed directly
    to ``cv2.VideoCapture``.

Target FPS
----------
Native feeds are 25-60 fps, but the perception stack runs at ``TARGET_FPS``
(default 2 fps, see ``road_safety/config.py``). We subsample by computing
``step = native_fps / target_fps`` and only invoking ``on_frame`` every
``step``-th frame — cheaper than decoding at full rate and throwing most
frames away downstream.
"""

# ``from __future__ import annotations`` makes all type hints lazy strings.
# That means ``str | None`` works on Python 3.9 even though the ``|`` union
# syntax is technically a 3.10 feature — at runtime the annotation is just a
# string, never evaluated.
from __future__ import annotations

import threading    # stdlib: real OS threads + sync primitives (Event, Lock)
import time         # stdlib: wall-clock timestamps for frame tagging
from pathlib import Path
from typing import Callable  # type hint for "any callable taking these args"

import cv2          # OpenCV: VideoCapture, frame decoding. Frames are numpy
                    #   arrays in BGR order (shape = (H, W, 3), dtype=uint8).


def classify_source(source: str) -> str:
    """Tag a source string with a UI-visible transport mode.

    Used to distinguish the demo "fake dashcam" MP4 loop from real live feeds
    in the admin grid and status API. The returned value is a stable short
    string suitable for a CSS class / badge label.

    Returns:
        ``"dashcam_file"``  — local video file (path points at an existing
                               file on disk; webcam indices use ``"webcam"``).
        ``"webcam"``        — ``"0"`` / ``"1"`` / etc. (webcam device index).
        ``"live_hls"``      — HTTP(S) / RTSP / RTMP URL.
        ``"unknown"``       — empty / unrecognised.
    """
    s = (source or "").strip()
    if not s:
        return "unknown"
    # Webcam source: ``cv2.VideoCapture("0")`` opens /dev/video0. Treat
    # short numeric strings as device indices, not as file paths.
    if s.isdigit() and len(s) <= 2:
        return "webcam"
    lowered = s.lower()
    if lowered.startswith(("http://", "https://", "rtsp://", "rtmp://")):
        return "live_hls"
    # Anything else is treated as a local file path. We don't stat it here
    # (classifier must be cheap + pure) — the reader itself will surface
    # "failed to open" if the path is wrong.
    return "dashcam_file"


def display_video_id(source: str) -> str:
    """Derive an egress-safe, human-readable identifier from a source string.

    Used as the ``video_id`` field on emitted events. Raw sources are not safe
    to emit: a local file path contains the operator's home directory (e.g.
    ``/Users/alice/...``) and leaks into the cloud receiver, Slack, and the
    admin UI. Basename is stable, readable, and non-identifying.

    Mapping:
        - empty               → ``"stream"``
        - webcam index        → ``"webcam:N"``
        - http/https/rtsp/... → URL as-is (already public)
        - local file path     → basename only (``"Left Cam.mp4"``)
    """
    s = (source or "").strip()
    if not s:
        return "stream"
    if s.isdigit() and len(s) <= 2:
        return f"webcam:{s}"
    lowered = s.lower()
    if lowered.startswith(("http://", "https://", "rtsp://", "rtmp://")):
        return s
    return Path(s).name or s


class StreamReader:
    """Background-threaded frame producer.

    Responsibility
    --------------
    Open a video source (file / URL / webcam), continuously decode frames,
    and invoke a user-supplied ``on_frame(timestamp, frame)`` callback at
    approximately ``target_fps`` frames per second.

    State held
    ----------
      - ``source_url``: the URL or path OpenCV will actually open.
      - ``original_source``: the original user input (preserved for logs /
        status even when the caller has pre-processed ``source_url``).
      - ``target_fps``: subsampling target; see module docstring.
      - ``_thread``: the OS thread running the capture loop. ``None`` until
        ``start()`` is called.
      - ``_stop``: a ``threading.Event`` used as a thread-safe boolean flag.
        The capture loop polls ``_stop.is_set()`` every iteration; calling
        ``stop()`` sets it and the loop exits cleanly.
      - ``started_at``, ``frames_read``, ``frames_processed``: cheap metrics
        exposed on the ``/api/live/status`` endpoint.

    Lifecycle
    ---------
        reader = StreamReader(url, target_fps=2.0)
        reader.start(my_callback)    # spawns the thread
        ...                           # my_callback is invoked ~2x per second
        reader.stop()                 # signals + joins the thread

    Called by ``road_safety/server.py::_run_loop`` which owns a single
    ``StreamReader`` instance per active stream.
    """

    def __init__(
        self,
        source_url: str,
        target_fps: float = 2.0,
        *,
        original_source: str = "",
        loop: bool = False,
    ):
        """Initialise without spawning the thread. Call ``start()`` to begin capture.

        The ``*`` in the signature forces later kwargs to be passed by keyword
        only — prevents accidental positional mix-ups with ``source_url``.

        Args:
            source_url: Stream URL or local path that OpenCV will open.
            target_fps: Desired frame rate for the ``on_frame`` callback.
                Default 2.0 matches ``TARGET_FPS`` in ``config.py``.
            original_source: The user-supplied source string, preserved for
                logging / status even when ``source_url`` has been
                pre-processed by the caller.
            loop: When True and the source is a finite local file, rewind and
                keep reading once EOF is reached instead of exiting. Used by
                the demo "fake dashcam" mode to keep the MP4 replaying forever.
                No effect on live network sources (you cannot rewind HLS).
        """
        self.source_url = source_url
        self.original_source = original_source or source_url
        self.target_fps = target_fps
        self.loop = loop
        self._thread: threading.Thread | None = None
        # ``threading.Event`` is a thread-safe boolean flag. ``set()`` flips it
        # true; ``is_set()`` reads it; ``wait()`` blocks until set. We use it
        # as a "please stop" signal from the main thread to the capture loop.
        self._stop = threading.Event()
        # Pause flag — set by ``pause()``, cleared by ``resume()``. When set,
        # the capture loop sleeps between iterations without releasing the
        # VideoCapture handle so the MP4 playback position is preserved and
        # ``resume()`` continues exactly where we paused. For live feeds the
        # network connection stays open but ``on_frame`` is skipped.
        self._paused = threading.Event()
        self.started_at: float | None = None
        self.frames_read = 0
        self.frames_processed = 0
        # Demo-mode: current playback position inside the MP4 (seconds) and
        # the MP4's total duration (seconds). Populated by ``_loop`` only
        # when ``self.loop`` is True — live feeds don't have a finite
        # duration. Readers expose these through ``playback_position()`` so
        # the frontend map overlay can sync its GPS marker to the actual
        # video loop instead of wallclock.
        self._video_pos_sec: float = 0.0
        self._video_duration_sec: float = 0.0

    def start(self, on_frame: Callable[[float, object], None]) -> None:
        """Spawn the capture thread. Returns immediately; capture runs in background.

        Args:
            on_frame: Callback invoked once per decoded frame, as
                ``on_frame(timestamp_seconds, frame_bgr_ndarray)``. This is
                called *on the capture thread* — if it does heavy work, it
                will slow the capture rate. In practice it just stuffs the
                frame into an asyncio queue for the event loop to consume.

        Note:
            ``daemon=True`` means the thread will not prevent Python from
            exiting — when the main process dies, the capture thread dies
            with it. Without this, a stuck network read could hang shutdown.
        """
        self.started_at = time.time()
        self._thread = threading.Thread(target=self._loop, args=(on_frame,), daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Request a clean shutdown: signal the loop and join.

        Idempotent — calling multiple times is safe. Blocks up to 5 seconds
        waiting for the thread to exit; if the thread is stuck in a blocking
        read that doesn't notice the stop flag, the 5s join timeout limits
        how long we hang.
        """
        self._stop.set()
        # Unblock anything waiting on the pause gate so the loop sees the
        # stop flag promptly instead of sleeping out its pause interval.
        self._paused.clear()
        if self._thread:
            self._thread.join(timeout=5)

    def pause(self) -> None:
        """Suspend frame emission without releasing the capture handle.

        For finite local files this keeps ``cv2.VideoCapture`` at its current
        frame, so ``resume()`` continues playback exactly where we paused —
        which is what the operator expects from a Start-after-Pause. For live
        feeds the network read keeps running (we can't rewind HLS) but
        ``on_frame`` is skipped while paused, so no new detections fire.
        """
        self._paused.set()

    def resume(self) -> None:
        """Undo :meth:`pause` and let the capture loop emit frames again."""
        self._paused.clear()

    def is_paused(self) -> bool:
        return self._paused.is_set()

    def uptime_sec(self) -> float:
        """Seconds since ``start()`` was called; ``0.0`` if never started.

        Intuition: exposed on the status endpoint so operators can see "the
        stream has been up for 12 minutes" at a glance.
        """
        return 0.0 if self.started_at is None else time.time() - self.started_at

    def playback_position(self) -> tuple[float, float]:
        """Return ``(current_pos_sec, duration_sec)`` of the backing file.

        Only meaningful for looped local-file sources; live feeds return
        ``(0.0, 0.0)``. The values are refreshed by the capture loop after
        every successful ``cap.read()`` — reading them from another thread
        is safe because Python float assignment is atomic in CPython.
        """
        return self._video_pos_sec, self._video_duration_sec

    def _loop(self, on_frame: Callable[[float, object], None]) -> None:
        """Capture loop for files, RTSP, plain HLS, and webcams — pure OpenCV.

        This is the blocking-read thread body. OpenCV's ``cap.read()`` blocks
        until a frame is available (or the source disconnects). That's why
        this must run on a dedicated OS thread, not the asyncio event loop.

        Subsampling logic
        -----------------
        Most sources produce 25-60 fps but we only want ~2 fps for perception.
        We read *every* frame (discarding it is cheaper than seeking) and
        invoke the callback only every ``step``-th frame. Example: native
        30 fps, target 2 fps → step=15 → callback fires on frames 0, 15, 30…

        Failure handling
        ----------------
        Transient read failures (network blip) are common — we sleep 100ms
        and retry. After 50 consecutive failures we give up and exit; the
        watchdog will notice and restart the stream.
        """
        cap = cv2.VideoCapture(self.source_url)
        if not cap.isOpened():
            # f-string = Python's interpolated string literal. ``{self.source_url[:80]}``
            # inserts the first 80 chars of the URL (to avoid printing a giant
            # signed URL to the logs).
            print(f"[stream] failed to open: {self.source_url[:80]}...")
            return

        # Many network streams lie about or omit their FPS; fall back to 25.
        native_fps = cap.get(cv2.CAP_PROP_FPS) or 25
        # ``max(..., 1)`` guards against step=0 when target_fps > native_fps,
        # which would cause a ZeroDivisionError in ``i % step`` below.
        step = max(int(native_fps / self.target_fps), 1)
        # For finite files, compute total duration once so the frontend can
        # align its GPS loop with the MP4 loop. ``CAP_PROP_FRAME_COUNT`` is
        # 0/negative for live sources, which we detect and ignore.
        total_frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        if self.loop and total_frames > 0 and native_fps > 0:
            self._video_duration_sec = total_frames / native_fps
        print(
            f"[stream] opened  native_fps={native_fps:.1f}  step={step}  "
            f"target_fps={self.target_fps}  duration={self._video_duration_sec:.1f}s"
        )

        i = 0
        consecutive_fail = 0
        # Realtime pacing for the demo dashcam loop. Local files decode as
        # fast as the CPU allows — without throttling, a 30s MP4 would play
        # through in a fraction of a second and the perception pipeline's
        # TTC math (which assumes wall-clock timestamps) would be meaningless.
        # Deadline-based: next callback fires at ``next_deadline``; if we're
        # ahead we sleep, if we're behind we fire immediately and catch up.
        next_deadline = time.time() if self.loop else 0.0
        while not self._stop.is_set():
            # Pause gate: if the operator paused the stream, sit on the stop
            # event so we wake promptly on either resume or shutdown. Resetting
            # ``next_deadline`` on wake prevents a burst of catch-up frames
            # when the pause duration exceeded the target frame interval.
            if self._paused.is_set():
                self._stop.wait(timeout=0.2)
                next_deadline = time.time()
                continue
            ok, frame = cap.read()
            if not ok:
                # Looping replay for finite local files (demo dashcam mode).
                # ``CAP_PROP_POS_FRAMES`` = 0 rewinds to the start; guarded by
                # ``self.loop`` so live network sources still bail on EOF.
                if self.loop:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    ok, frame = cap.read()
                    if ok:
                        consecutive_fail = 0
                    else:
                        # Rewind attempt also failed — fall through to retry /
                        # give-up path; prevents an infinite busy loop on a
                        # file that genuinely won't open.
                        consecutive_fail += 1
                        if consecutive_fail > 50:
                            print("[stream] loop: rewind keeps failing — stopping")
                            break
                        time.sleep(0.1)
                        continue
                else:
                    consecutive_fail += 1
                    # 50 * 100ms = 5s of continuous failures before giving up.
                    # Short enough to surface outages quickly; long enough to
                    # ride through typical network hiccups.
                    if consecutive_fail > 50:
                        print("[stream] too many read failures — stopping")
                        break
                    time.sleep(0.1)
                    continue
            consecutive_fail = 0
            self.frames_read += 1
            # Track the MP4 playback head so consumers can sync to the
            # actual video loop. ``CAP_PROP_POS_MSEC`` is 0 on live feeds
            # and on rewind, which is exactly the semantics we want (the
            # map marker snaps back when the video loops).
            if self.loop:
                pos_msec = cap.get(cv2.CAP_PROP_POS_MSEC) or 0.0
                self._video_pos_sec = pos_msec / 1000.0
            if i % step == 0:
                # This is the handoff to the asyncio side. The callback
                # implementation in server.py is a non-blocking ``queue.put_nowait``
                # style — we log and continue rather than kill the capture
                # thread if a downstream consumer misbehaves.
                try:
                    on_frame(time.time(), frame)
                    self.frames_processed += 1
                except Exception as exc:
                    print(f"[stream] on_frame error: {exc}")
                # Pace the looped dashcam replay in wall-clock so TTC / ego-
                # speed estimates reflect real seconds of motion, not CPU
                # decode speed. Live network sources self-throttle via their
                # own network pacing — no sleep needed.
                if self.loop and self.target_fps > 0:
                    next_deadline += 1.0 / self.target_fps
                    delay = next_deadline - time.time()
                    if delay > 0:
                        # ``Event.wait(timeout)`` returns early when ``stop()``
                        # flips the flag, so we stay responsive to shutdown
                        # even while sleeping between paced frames.
                        self._stop.wait(timeout=delay)
                    else:
                        # Fell behind — reset the deadline so we don't try
                        # to "catch up" with a burst of un-paced callbacks.
                        next_deadline = time.time()
            i += 1

        cap.release()
        print("[stream] capture loop ended")
