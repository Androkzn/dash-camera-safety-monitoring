"""Tests for road_safety.compliance — audit logging and data retention."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest


# ═══════════════════════════════════════════════════════════════════
# Audit
# ═══════════════════════════════════════════════════════════════════

class TestAuditLog:
    def test_log_writes_record(self, _isolate_data_dir):
        from road_safety.compliance import audit
        with patch.object(audit, "_DATA_DIR", _isolate_data_dir), \
             patch.object(audit, "_AUDIT_PATH", _isolate_data_dir / "audit.jsonl"), \
             patch.object(audit, "_ENABLED", True):
            audit.log("test_action", "test_resource")
            path = _isolate_data_dir / "audit.jsonl"
            assert path.exists()
            lines = path.read_text().strip().splitlines()
            assert len(lines) == 1
            rec = json.loads(lines[0])
            assert rec["action"] == "test_action"
            assert rec["resource"] == "test_resource"
            assert rec["actor"] == "system"
            assert rec["outcome"] == "success"
            assert "ts" in rec

    def test_log_with_optional_fields(self, _isolate_data_dir):
        from road_safety.compliance import audit
        with patch.object(audit, "_DATA_DIR", _isolate_data_dir), \
             patch.object(audit, "_AUDIT_PATH", _isolate_data_dir / "audit.jsonl"), \
             patch.object(audit, "_ENABLED", True):
            audit.log(
                "access_thumbnail", "thumb_001",
                actor="operator_1", outcome="denied",
                detail={"reason": "invalid_token"}, ip="1.2.3.4",
            )
            lines = (_isolate_data_dir / "audit.jsonl").read_text().strip().splitlines()
            rec = json.loads(lines[0])
            assert rec["actor"] == "operator_1"
            assert rec["outcome"] == "denied"
            assert rec["detail"]["reason"] == "invalid_token"
            assert rec["ip"] == "1.2.3.4"

    def test_log_disabled(self, _isolate_data_dir):
        from road_safety.compliance import audit
        with patch.object(audit, "_DATA_DIR", _isolate_data_dir), \
             patch.object(audit, "_AUDIT_PATH", _isolate_data_dir / "audit.jsonl"), \
             patch.object(audit, "_ENABLED", False):
            audit.log("test_action", "test_resource")
            path = _isolate_data_dir / "audit.jsonl"
            assert not path.exists()

    def test_tail_empty(self, _isolate_data_dir):
        from road_safety.compliance import audit
        with patch.object(audit, "_AUDIT_PATH", _isolate_data_dir / "audit.jsonl"):
            result = audit.tail()
            assert result == []

    def test_tail_returns_records(self, _isolate_data_dir):
        from road_safety.compliance import audit
        audit_path = _isolate_data_dir / "audit.jsonl"
        with patch.object(audit, "_DATA_DIR", _isolate_data_dir), \
             patch.object(audit, "_AUDIT_PATH", audit_path), \
             patch.object(audit, "_ENABLED", True):
            for i in range(5):
                audit.log(f"action_{i}", f"resource_{i}")
            records = audit.tail()
            assert len(records) == 5

    def test_tail_respects_limit(self, _isolate_data_dir):
        from road_safety.compliance import audit
        audit_path = _isolate_data_dir / "audit.jsonl"
        with patch.object(audit, "_DATA_DIR", _isolate_data_dir), \
             patch.object(audit, "_AUDIT_PATH", audit_path), \
             patch.object(audit, "_ENABLED", True):
            for i in range(10):
                audit.log(f"action_{i}", f"resource_{i}")
            records = audit.tail(n=3)
            assert len(records) == 3

    def test_stats(self, _isolate_data_dir):
        from road_safety.compliance import audit
        audit_path = _isolate_data_dir / "audit.jsonl"
        with patch.object(audit, "_DATA_DIR", _isolate_data_dir), \
             patch.object(audit, "_AUDIT_PATH", audit_path), \
             patch.object(audit, "_ENABLED", True):
            audit.log("access", "res1")
            audit.log("access", "res2")
            audit.log("feedback", "res3")
            audit.log("access", "res4", outcome="denied")
            s = audit.stats()
            assert s["total_records"] == 4
            assert s["by_action"]["access"] == 3
            assert s["by_action"]["feedback"] == 1
            assert s["denied_count"] == 1
            assert s["audit_enabled"] is True


# ═══════════════════════════════════════════════════════════════════
# Retention
# ═══════════════════════════════════════════════════════════════════

class TestRetention:
    def test_sweep_empty(self, _isolate_data_dir):
        from road_safety.compliance.retention import run_sweep
        with patch("road_safety.compliance.retention.DATA_DIR", _isolate_data_dir):
            result = run_sweep()
            assert isinstance(result, dict)
            assert "thumbnails_removed" in result

    def test_sweep_removes_old_thumbnails(self, _isolate_data_dir):
        from road_safety.compliance.retention import run_sweep
        import time
        thumbs = _isolate_data_dir / "thumbnails"
        thumbs.mkdir(exist_ok=True)
        old_file = thumbs / "old_thumb.jpg"
        old_file.write_text("fake")
        import os
        old_time = time.time() - (31 * 86400)
        os.utime(old_file, (old_time, old_time))
        with patch("road_safety.compliance.retention.DATA_DIR", _isolate_data_dir):
            result = run_sweep()
            assert result["thumbnails_removed"] >= 1
            assert not old_file.exists()
