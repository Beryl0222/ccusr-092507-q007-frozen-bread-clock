from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from frozen_bread_clock import cli

SCHEMA_PATH = ROOT / "contracts" / "domain.schema.json"
SAMPLE_PATH = ROOT / "data" / "sample.json"
TIMELINE_PATH = ROOT / "data" / "timeline.sample.jsonl"


def run_cli(args):
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = cli.main(args)
    return code, buffer.getvalue()


class CliTests(unittest.TestCase):
    def test_legacy_validate_still_works(self):
        code, out = run_cli([str(SCHEMA_PATH), str(SAMPLE_PATH)])
        self.assertEqual(0, code)
        self.assertEqual("valid", out.strip())

    def test_replay_shows_traceable_conclusions(self):
        code, out = run_cli([
            "replay", str(SCHEMA_PATH), str(TIMELINE_PATH), "portion-order-1001-a",
            "--at", "2026-09-28T12:00:00+08:00",
        ])
        self.assertEqual(0, code)
        self.assertIn("时间线", out)
        self.assertIn("结论", out)
        self.assertIn("frozen_bread_clock-001@v3", out)
        self.assertIn("evt-0927-thaw", out)
        # 家庭成员标识必须脱敏。
        self.assertNotIn("grandma-phone-13800138000", out)
        self.assertIn("member:", out)
        # 最终状态是可追溯的结构，而不是一个剩余天数。
        self.assertIn('"deadline_at": "2026-09-28T10:00:00Z"', out)

    def test_replay_reports_conflicts(self):
        events = [
            json.loads(SAMPLE_PATH.read_text(encoding="utf-8")),
            {
                "event_id": "evt-create", "event_type": "PORTION_CREATED",
                "aggregate_type": "household_portion", "aggregate_id": "portion-x",
                "occurred_at": "2026-09-24T20:00:00+08:00", "version": 1,
                "payload": {"parent_ref": "order-x", "quantity": 2, "product_ref": "frozen_bread_clock-001"},
            },
            {
                "event_id": "evt-dup", "event_type": "STORAGE_RECORDED",
                "aggregate_type": "storage_timeline", "aggregate_id": "portion-x",
                "occurred_at": "2026-09-25T09:00:00+08:00", "version": 1,
                "payload": {"method": "freezer", "occurred_at": "2026-09-25T09:00:00+08:00", "temperature_c": -18},
            },
            {
                "event_id": "evt-dup", "event_type": "STORAGE_RECORDED",
                "aggregate_type": "storage_timeline", "aggregate_id": "portion-x",
                "occurred_at": "2026-09-25T09:00:00+08:00", "version": 1,
                "payload": {"method": "freezer", "occurred_at": "2026-09-25T09:00:00+08:00", "temperature_c": -4},
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events), encoding="utf-8")
            code, out = run_cli(["replay", str(SCHEMA_PATH), str(path), "portion-x"])
        self.assertEqual(0, code)
        self.assertIn("evt-dup", out)
        self.assertIn('"level": "suspended"', out)


if __name__ == "__main__":
    unittest.main()
