"""Tests for road_safety.config — paths, env defaults, and constants."""

from pathlib import Path

from road_safety import __version__
from road_safety.config import (
    ALPR_MODE,
    DATA_DIR,
    DEFAULT_STREAM_SOURCE,
    DRIVER_ID,
    EPISODE_IDLE_FLUSH_SEC,
    ROAD_ID,
    MAX_RECENT_EVENTS,
    MODEL_PATH,
    PAIR_COOLDOWN_SEC,
    PLATE_SALT,
    PUBLIC_THUMBS_REQUIRE_TOKEN,
    PROJECT_ROOT,
    SCORE_DECAY_INTERVAL_SEC,
    SERVER_HOST,
    SERVER_PORT,
    SSE_REPLAY_COUNT,
    STATIC_DIR,
    TARGET_FPS,
    THUMB_SIGNING_SECRET,
    THUMBS_DIR,
    VEHICLE_ID,
)


class TestProjectPaths:
    def test_project_root_exists(self):
        assert PROJECT_ROOT.exists()
        assert PROJECT_ROOT.is_dir()

    def test_project_root_contains_pyproject(self):
        assert (PROJECT_ROOT / "pyproject.toml").exists()

    def test_static_dir_exists(self):
        assert STATIC_DIR.exists()
        assert (STATIC_DIR / "index.html").exists()

    def test_model_path_is_string(self):
        assert isinstance(MODEL_PATH, str)
        assert MODEL_PATH.endswith(".pt")


class TestDefaults:
    def test_target_fps_default(self):
        assert TARGET_FPS > 0

    def test_max_recent_events(self):
        assert MAX_RECENT_EVENTS > 0

    def test_sse_replay_count(self):
        assert SSE_REPLAY_COUNT > 0

    def test_pair_cooldown(self):
        assert PAIR_COOLDOWN_SEC > 0

    def test_episode_idle_flush(self):
        assert EPISODE_IDLE_FLUSH_SEC > 0

    def test_server_host_is_string(self):
        assert isinstance(SERVER_HOST, str)
        assert len(SERVER_HOST) > 0

    def test_server_port_default(self):
        assert SERVER_PORT == 8000

    def test_vehicle_id_is_string(self):
        assert isinstance(VEHICLE_ID, str)

    def test_road_id_is_string(self):
        assert isinstance(ROAD_ID, str)

    def test_driver_id_is_string(self):
        assert isinstance(DRIVER_ID, str)

    def test_stream_source_is_string(self):
        assert isinstance(DEFAULT_STREAM_SOURCE, str)

    def test_plate_salt_is_nonempty(self):
        assert isinstance(PLATE_SALT, str)
        assert len(PLATE_SALT) > 0

    def test_public_thumbs_guard_is_bool(self):
        assert isinstance(PUBLIC_THUMBS_REQUIRE_TOKEN, bool)

    def test_thumb_signing_secret_is_string(self):
        assert isinstance(THUMB_SIGNING_SECRET, str)

    def test_alpr_mode_is_string(self):
        assert isinstance(ALPR_MODE, str)

    def test_score_decay_interval_non_negative(self):
        assert SCORE_DECAY_INTERVAL_SEC >= 0


class TestPackageVersion:
    def test_version_string(self):
        assert isinstance(__version__, str)
        parts = __version__.split(".")
        assert len(parts) == 3


class TestCameraCalibration:
    """Per-slot camera calibration: defaults + env overrides."""

    def test_default_calibration_matches_globals(self):
        from road_safety.config import (
            CAMERA_FOCAL_PX,
            CAMERA_HEIGHT_M,
            CAMERA_HORIZON_FRAC,
            DEFAULT_CAMERA_CALIBRATION,
        )
        assert DEFAULT_CAMERA_CALIBRATION.focal_px == CAMERA_FOCAL_PX
        assert DEFAULT_CAMERA_CALIBRATION.height_m == CAMERA_HEIGHT_M
        assert DEFAULT_CAMERA_CALIBRATION.horizon_frac == CAMERA_HORIZON_FRAC
        assert DEFAULT_CAMERA_CALIBRATION.orientation == "forward"
        assert DEFAULT_CAMERA_CALIBRATION.bumper_offset_m == 0.0

    def test_primary_slot_uses_front_dashcam_defaults(self):
        from road_safety.config import camera_calibration_for
        cal = camera_calibration_for("primary")
        # Front dashcam (iPhone 1× wide) on a Nissan Rogue rearview mirror.
        assert cal.focal_px == 600.0
        assert cal.height_m == 1.25
        assert cal.orientation == "forward"
        assert cal.bumper_offset_m == 1.7

    def test_rear_slot_uses_ultrawide_defaults(self):
        from road_safety.config import camera_calibration_for
        cal = camera_calibration_for("rear")
        # iPhone 0.5× ultra-wide on the rear window.
        assert cal.focal_px == 260.0
        assert cal.height_m == 1.10
        assert cal.orientation == "rear"
        assert cal.bumper_offset_m == 0.3

    def test_left_slot_marked_side_orientation(self):
        from road_safety.config import camera_calibration_for
        cal = camera_calibration_for("left")
        # Side cam: ground-plane prior is invalid; downstream code skips it.
        assert cal.focal_px == 260.0
        assert cal.orientation == "side"
        assert cal.horizon_frac == 0.50  # level mount → horizon at image center
        assert cal.bumper_offset_m == 0.1

    def test_unknown_slot_falls_back_to_default(self):
        from road_safety.config import (
            DEFAULT_CAMERA_CALIBRATION,
            camera_calibration_for,
        )
        cal = camera_calibration_for("operator_defined_slot_xyz")
        assert cal == DEFAULT_CAMERA_CALIBRATION

    def test_env_override_per_slot(self, monkeypatch):
        from road_safety.config import camera_calibration_for
        monkeypatch.setenv("ROAD_CAMERA_FOCAL_PX__PRIMARY", "742.5")
        monkeypatch.setenv("ROAD_CAMERA_BUMPER_OFFSET_M__PRIMARY", "2.1")
        cal = camera_calibration_for("primary")
        assert cal.focal_px == 742.5
        assert cal.bumper_offset_m == 2.1
        # Other fields still come from the slot defaults.
        assert cal.orientation == "forward"
        assert cal.height_m == 1.25

    def test_env_override_orientation(self, monkeypatch):
        from road_safety.config import camera_calibration_for
        monkeypatch.setenv("ROAD_CAMERA_ORIENTATION__PRIMARY", "side")
        cal = camera_calibration_for("primary")
        assert cal.orientation == "side"

    def test_unparseable_env_falls_back(self, monkeypatch):
        """Bad numeric override doesn't crash the slot — it logs and uses default."""
        from road_safety.config import camera_calibration_for
        monkeypatch.setenv("ROAD_CAMERA_FOCAL_PX__PRIMARY", "not_a_number")
        cal = camera_calibration_for("primary")
        assert cal.focal_px == 600.0  # falls back to slot default
