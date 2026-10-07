from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from frozen_bread_clock.reminders import ReminderStore, plan_reminders, run_batch
from frozen_bread_clock.service import ClockService

SCHEMA = json.loads((ROOT / "contracts" / "domain.schema.json").read_text(encoding="utf-8"))

RULES = {
    "ambient_hours": 8,
    "fridge_hours": 48,
    "freeze_window_hours": 24,
    "frozen_hours": 720,
    "thawed_ambient_hours": 4,
    "thawed_fridge_hours": 24,
    "refreeze_allowed": False,
    "warn_hours": 6,
    "reheat_methods": ["烤箱"],
}


def evt(event_id, event_type, aggregate_type, aggregate_id, occurred_at, payload=None, version=1):
    return {
        "event_id": event_id,
        "event_type": event_type,
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id,
        "occurred_at": occurred_at,
        "version": version,
        "payload": payload or {},
    }


def frozen_portion_service():
    events = [
        evt(
            "evt-product", "PRODUCT_REGISTERED", "product_version", "product-1",
            "2026-09-24T12:00:00+08:00",
            {
                "product_id": "cream-toast",
                "recipe_version": 3,
                "lot_no": "L20260924",
                "produced_at": "2026-09-24T08:00:00+08:00",
                "allergens": ["麸质"],
                "rules": RULES,
            },
        ),
        evt(
            "evt-create", "PORTION_CREATED", "household_portion", "portion-1",
            "2026-09-24T20:00:00+08:00",
            {"parent_ref": "order-1", "quantity": 2, "product_ref": "product-1"},
        ),
        evt(
            "evt-freeze", "STORAGE_RECORDED", "storage_timeline", "portion-1",
            "2026-09-25T09:00:00+08:00",
            {"method": "freezer", "occurred_at": "2026-09-25T09:00:00+08:00"},
        ),
    ]
    service = ClockService(SCHEMA, clock=lambda: datetime(2026, 10, 1, tzinfo=timezone.utc))
    service.ingest(events)
    return service


class ReminderTests(unittest.TestCase):
    def test_batch_does_not_resend_after_restart(self):
        service = frozen_portion_service()
        # 期限 2026-10-25T01:00Z，2026-10-24T20:00Z 进入 use_soon 窗口。
        now = "2026-10-24T20:00:00+00:00"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sent.json"
            first = run_batch(service, ["portion-1"], ReminderStore(path), now)
            self.assertEqual(1, len(first))
            self.assertEqual("use_soon", first[0].kind)
            # 任务重启：换一个 ReminderStore 实例读取同一文件。
            second = run_batch(service, ["portion-1"], ReminderStore(path), now)
            self.assertEqual([], second)

    def test_same_conclusion_not_resent_within_process(self):
        service = frozen_portion_service()
        now = "2026-10-24T20:00:00+00:00"
        store = ReminderStore()
        self.assertEqual(1, len(run_batch(service, ["portion-1"], store, now)))
        self.assertEqual([], run_batch(service, ["portion-1"], store, now))

    def test_changed_deadline_produces_new_reminder(self):
        service = frozen_portion_service()
        store = ReminderStore()
        first = run_batch(service, ["portion-1"], store, "2026-10-24T20:00:00+00:00")
        # 指南修订适用本批次，期限变化后应产生新键并再次提醒。
        service.ingest([
            evt(
                "evt-notice", "NOTICE_RECEIVED", "safety_notice", "notice-1",
                "2026-10-24T21:00:00+08:00",
                {
                    "lot_no": "L20260924",
                    "notice_version": 1,
                    "kind": "guideline_revision",
                    "applies_to_lots": ["L20260924"],
                    "rules": dict(RULES, frozen_hours=600),
                },
            )
        ])
        second = run_batch(service, ["portion-1"], store, "2026-10-24T20:00:00+00:00")
        self.assertEqual(1, len(first))
        self.assertEqual(1, len(second))
        self.assertNotEqual(first[0].key, second[0].key)

    def test_recall_reminder_key_includes_notice_version(self):
        status = {
            "portion_id": "portion-1",
            "level": "recalled",
            "recall": {"notice_ref": "n1", "notice_version": 2, "instruction": "停止食用", "event_id": "e"},
            "advice_suspended": False,
        }
        reminders = plan_reminders(status)
        self.assertEqual(["portion-1:recall:2"], [r.key for r in reminders])

    def test_suspended_portion_sends_no_eating_reminder(self):
        status = {
            "portion_id": "portion-1",
            "level": "suspended",
            "recall": None,
            "advice_suspended": True,
            "deadline_at": None,
        }
        self.assertEqual([], plan_reminders(status))


if __name__ == "__main__":
    unittest.main()
