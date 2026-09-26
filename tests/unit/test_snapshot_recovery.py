"""Malformed saved monitoring snapshots fail through the existing UI error path."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from pa_agent.monitoring.service import MonitoringService
from tests.unit.test_manual_monitor import AT, FakeClient


class SavedSnapshotRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "monitor"
        self.client = FakeClient()
        self.service = MonitoringService(self.root, Path(self.tmp.name), client=self.client)

    def tearDown(self):
        self.tmp.cleanup()

    def test_invalid_pointer_shape_is_value_error(self):
        self.root.mkdir()
        (self.root / "current.json").write_text("[]", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.service.latest(AT)

    def test_invalid_paths_config_is_value_error_without_replacement(self):
        self.root.mkdir()
        path = self.root / "paths.json"
        for raw in (b"[]", b'{"workspace":42}', b"\xff"):
            with self.subTest(raw=raw):
                path.write_bytes(raw)
                with self.assertRaisesRegex(ValueError, "MONITOR_PATHS_INVALID"):
                    MonitoringService(self.root, client=self.client)
                self.assertEqual(path.read_bytes(), raw)

    def test_non_utf8_pointer_is_value_error_without_replacement(self):
        self.root.mkdir()
        path = self.root / "current.json"
        path.write_bytes(b"\xff")
        with self.assertRaises(ValueError):
            self.service.latest(AT)
        self.assertEqual(path.read_bytes(), b"\xff")

    def test_checksum_consistent_invalid_bundle_shapes_are_value_error(self):
        result = self.service.refresh()
        bundle_path = self.root / "runs" / result["run_id"] / "bundle.json"
        bad_quote = {**result["data"]["assets"]["VOO"], "quote": []}
        bad_market = {**result["data"]["assets"]["VOO"], "market": []}
        bad_source = {**result["data"]["assets"]["VOO"], "source": [1]}
        bad_ma = {**result["data"]["assets"]["VOO"], "ma": [1]}
        candidates = [[], {**result, "orders": True},
                      {**result, "data": {**result["data"], "assets": []}},
                      {**result, "data": {**result["data"], "assets": {"VOO": []}}},
                      {**result, "data": {**result["data"], "assets": {"VOO": bad_quote}}},
                      {**result, "data": {**result["data"], "assets": {"VOO": bad_market}}},
                      {**result, "data": {**result["data"], "assets": {"VOO": bad_source}}},
                      {**result, "data": {**result["data"], "assets": {"VOO": bad_ma}}}]
        for bundle in candidates:
            with self.subTest(kind=type(bundle).__name__):
                raw = json.dumps(bundle).encode("utf-8")
                bundle_path.write_bytes(raw)
                (self.root / "current.json").write_text(json.dumps({"run_id": result["run_id"],
                                                                       "sha256": hashlib.sha256(raw).hexdigest()}), encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.service.latest(AT)
        self.assertEqual(self.client.calls, 1)


if __name__ == "__main__":
    unittest.main()
