from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

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


def product(event_id="evt-product", ref="product-1", lot="L20260924", frozen_hours=720):
    rules = dict(RULES, frozen_hours=frozen_hours)
    return evt(
        event_id, "PRODUCT_REGISTERED", "product_version", ref, "2026-09-24T12:00:00+08:00",
        {
            "product_id": "cream-toast",
            "recipe_version": 3,
            "lot_no": lot,
            "produced_at": "2026-09-24T08:00:00+08:00",
            "allergens": ["麸质"],
            "filling": "奶油",
            "rules": rules,
        },
    )


def root_portion(portion_id="portion-1", product_ref="product-1", quantity=4, event_id="evt-create"):
    return evt(
        event_id, "PORTION_CREATED", "household_portion", portion_id, "2026-09-24T20:00:00+08:00",
        {"parent_ref": "order-1", "quantity": quantity, "product_ref": product_ref},
    )


def open_event(portion_id="portion-1", at="2026-09-25T08:00:00+08:00", event_id="evt-open"):
    return evt(event_id, "PACKAGE_OPENED", "household_portion", portion_id, at, {})


def storage(portion_id, method, at, event_id, temperature=None):
    payload = {"method": method, "occurred_at": at}
    if temperature is not None:
        payload["temperature_c"] = temperature
    return evt(event_id, "STORAGE_RECORDED", "storage_timeline", portion_id, at, payload)


def thaw(portion_id, at, event_id, method="fridge"):
    return evt(event_id, "THAW_STARTED", "household_portion", portion_id, at, {"method": method})


def consume(portion_id, quantity, at, event_id, member=None):
    payload = {"quantity": quantity}
    if member:
        payload["member_ref"] = member
    return evt(event_id, "PORTION_CONSUMED", "household_portion", portion_id, at, payload)


def split(child_id, parent_id, quantity, at, event_id):
    return evt(
        event_id, "PORTION_CREATED", "household_portion", child_id, at,
        {"parent_ref": parent_id, "quantity": quantity},
    )


def make_service(events):
    service = ClockService(SCHEMA, clock=lambda: datetime(2026, 10, 1, tzinfo=timezone.utc))
    results = service.ingest(events)
    return service, results


class IngestTests(unittest.TestCase):
    def test_identical_events_are_merged_once(self):
        events = [
            product(), root_portion(),
            storage("portion-1", "freezer", "2026-09-25T09:00:00+08:00", "evt-freeze", -18),
            storage("portion-1", "freezer", "2026-09-25T09:00:00+08:00", "evt-freeze", -18),
        ]
        service, results = make_service(events)
        self.assertEqual(["accepted", "accepted", "accepted", "duplicate"], [r.outcome for r in results])
        timeline = service.replay("portion-1")["timeline"]
        self.assertEqual(1, sum(1 for item in timeline if item["event_id"] == "evt-freeze"))

    def test_conflicting_event_id_suspends_advice(self):
        events = [
            product(), root_portion(),
            storage("portion-1", "freezer", "2026-09-25T09:00:00+08:00", "evt-dup", -18),
            storage("portion-1", "fridge", "2026-09-25T09:00:00+08:00", "evt-dup", 4),
        ]
        service, results = make_service(events)
        self.assertEqual("conflict", results[-1].outcome)
        status = service.status("portion-1")
        self.assertTrue(status["advice_suspended"])
        self.assertEqual("suspended", status["level"])
        self.assertIsNone(status["deadline_at"])
        conflicted = {eid for entry in status["explanation"] for eid in entry["event_ids"]}
        self.assertIn("evt-dup", conflicted)

    def test_conflicting_quantity_suspends_descendants(self):
        events = [
            product(), root_portion(),
            split("portion-1-a", "portion-1", 2, "2026-09-26T10:00:00+08:00", "evt-split"),
            consume("portion-1", 1, "2026-09-27T08:00:00+08:00", "evt-eat"),
            consume("portion-1", 2, "2026-09-27T08:00:00+08:00", "evt-eat"),
        ]
        service, _ = make_service(events)
        self.assertTrue(service.status("portion-1")["advice_suspended"])
        self.assertTrue(service.status("portion-1-a")["advice_suspended"])

    def test_invalid_events_are_rejected(self):
        service, results = make_service([
            product(), root_portion(),
            storage("portion-1", "oven", "2026-09-25T09:00:00+08:00", "evt-bad"),
        ])
        self.assertEqual("rejected", results[-1].outcome)
        self.assertIn("payload.method", {issue.field for issue in results[-1].issues})
        self.assertEqual("sealed", service.status("portion-1")["state"])

    def test_offline_backfill_merges_by_occurrence_time(self):
        freeze = storage("portion-1", "freezer", "2026-09-25T09:00:00+08:00", "evt-freeze")
        thawed = thaw("portion-1", "2026-09-27T18:00:00+08:00", "evt-thaw")
        in_order, _ = make_service([product(), root_portion(), freeze, thawed])
        backfilled, _ = make_service([product(), root_portion(), thawed, freeze])
        self.assertEqual(in_order.status("portion-1"), backfilled.status("portion-1"))
        self.assertEqual("thawed", backfilled.status("portion-1")["state"])


class DeadlineTests(unittest.TestCase):
    def test_fridge_days_never_used_as_frozen_deadline(self):
        events = [
            product(), root_portion(), open_event(),
            storage("portion-1", "fridge", "2026-09-25T08:00:00+08:00", "evt-fridge", 4),
            storage("portion-1", "freezer", "2026-09-26T08:00:00+08:00", "evt-freeze", -18),
        ]
        service, _ = make_service(events)
        status = service.status("portion-1")
        # 冷冻期限 = 冷冻开始 2026-09-26T00:00Z + 720h，与冷藏 48h 无关。
        self.assertEqual("2026-10-26T00:00:00Z", status["deadline_at"])
        conclusions = [entry["conclusion"] for entry in status["explanation"]]
        self.assertTrue(any("冷藏时段不计入冷冻期限" in text for text in conclusions))

    def test_fridge_storage_uses_fridge_hours_not_frozen(self):
        events = [
            product(), root_portion(), open_event(),
            storage("portion-1", "fridge", "2026-09-25T08:00:00+08:00", "evt-fridge", 4),
        ]
        service, _ = make_service(events)
        status = service.status("portion-1")
        # 开封 2026-09-25T00:00Z + 冷藏 48h。
        self.assertEqual("2026-09-27T00:00:00Z", status["deadline_at"])

    def test_thawed_portion_cannot_return_to_unopened(self):
        events = [
            product(), root_portion(),
            storage("portion-1", "freezer", "2026-09-25T09:00:00+08:00", "evt-freeze"),
            thaw("portion-1", "2026-09-27T18:00:00+08:00", "evt-thaw"),
            split("portion-1-a", "portion-1", 2, "2026-09-28T08:00:00+08:00", "evt-split"),
        ]
        service, _ = make_service(events)
        child = service.status("portion-1-a")
        self.assertEqual("thawed", child["state"])
        self.assertEqual("2026-09-28T10:00:00Z", child["deadline_at"])
        states = {service.status(pid)["state"] for pid in ("portion-1", "portion-1-a")}
        self.assertNotIn("sealed", states)

    def test_refreeze_creates_new_risk_state(self):
        events = [
            product(), root_portion(),
            storage("portion-1", "freezer", "2026-09-25T09:00:00+08:00", "evt-freeze"),
            thaw("portion-1", "2026-09-27T18:00:00+08:00", "evt-thaw"),
            storage("portion-1", "freezer", "2026-09-28T06:00:00+08:00", "evt-refreeze"),
        ]
        service, _ = make_service(events)
        status = service.status("portion-1")
        self.assertEqual("refrozen", status["state"])
        self.assertIn("refreeze_not_allowed", status["risk_flags"])
        # 规则不允许再冻：期限仍按解冻后计算（解冻 2026-09-27T10:00Z + 冷藏解冻 24h）。
        self.assertEqual("2026-09-28T10:00:00Z", status["deadline_at"])

    def test_deadline_boundary_is_stable(self):
        events = [
            product(), root_portion(),
            storage("portion-1", "freezer", "2026-09-26T00:00:00+00:00", "evt-freeze"),
        ]
        service, _ = make_service(events)
        deadline = "2026-10-26T00:00:00+00:00"
        self.assertEqual("expired", service.status("portion-1", deadline)["level"])
        self.assertEqual("use_soon", service.status("portion-1", "2026-10-25T23:59:59+00:00")["level"])
        self.assertEqual("ok", service.status("portion-1", "2026-10-25T17:59:59+00:00")["level"])

    def test_status_is_stable_across_timezones(self):
        events = [
            product(), root_portion(),
            storage("portion-1", "freezer", "2026-09-25T09:00:00+08:00", "evt-freeze"),
            thaw("portion-1", "2026-09-27T18:00:00+08:00", "evt-thaw"),
        ]
        service, _ = make_service(events)
        shanghai = service.status("portion-1", "2026-09-28T12:00:00+08:00")
        utc = service.status("portion-1", "2026-09-28T04:00:00+00:00")
        self.assertEqual(shanghai, utc)


class NoticeTests(unittest.TestCase):
    def test_guideline_revision_only_affects_listed_lots(self):
        events = [
            product(event_id="evt-product-a", ref="product-a", lot="LA"),
            product(event_id="evt-product-b", ref="product-b", lot="LB"),
            root_portion("portion-a", "product-a", event_id="evt-create-a"),
            root_portion("portion-b", "product-b", event_id="evt-create-b"),
            storage("portion-a", "freezer", "2026-09-26T00:00:00+00:00", "evt-freeze-a"),
            storage("portion-b", "freezer", "2026-09-26T00:00:00+00:00", "evt-freeze-b"),
            evt(
                "evt-notice", "NOTICE_RECEIVED", "safety_notice", "notice-1",
                "2026-09-27T09:00:00+08:00",
                {
                    "lot_no": "LB",
                    "notice_version": 1,
                    "kind": "guideline_revision",
                    "applies_to_lots": ["LB"],
                    "rules": dict(RULES, frozen_hours=96),
                },
            ),
        ]
        service, _ = make_service(events)
        self.assertEqual("2026-10-26T00:00:00Z", service.status("portion-a")["deadline_at"])
        self.assertEqual("2026-09-30T00:00:00Z", service.status("portion-b")["deadline_at"])

    def test_safety_recall_traces_split_lineage(self):
        events = [
            product(event_id="evt-product-a", ref="product-a", lot="LA"),
            product(event_id="evt-product-b", ref="product-b", lot="LB"),
            root_portion("portion-a", "product-a", event_id="evt-create-a"),
            root_portion("portion-b", "product-b", event_id="evt-create-b"),
            split("portion-a-1", "portion-a", 2, "2026-09-26T10:00:00+08:00", "evt-split"),
            evt(
                "evt-recall", "NOTICE_RECEIVED", "safety_notice", "notice-9",
                "2026-09-28T09:00:00+08:00",
                {
                    "lot_no": "LA",
                    "notice_version": 2,
                    "kind": "safety_recall",
                    "instruction": "停止食用并退回门店",
                },
            ),
        ]
        service, _ = make_service(events)
        for portion_id in ("portion-a", "portion-a-1"):
            status = service.status(portion_id)
            self.assertEqual("recalled", status["level"])
            self.assertEqual("停止食用并退回门店", status["recall"]["instruction"])
        self.assertNotEqual("recalled", service.status("portion-b")["level"])
        # 召回输出只含通知信息，不携带家庭成员数据。
        self.assertEqual(
            {"notice_ref", "notice_version", "instruction", "event_id"},
            set(service.status("portion-a")["recall"]),
        )


class PortionTests(unittest.TestCase):
    def test_partial_consumption_tracks_remaining(self):
        events = [
            product(), root_portion(quantity=4),
            consume("portion-1", 1, "2026-09-27T08:00:00+08:00", "evt-eat-1"),
            split("portion-1-a", "portion-1", 2, "2026-09-27T10:00:00+08:00", "evt-split"),
        ]
        service, _ = make_service(events)
        status = service.status("portion-1")
        self.assertEqual(4, status["quantity"])
        self.assertEqual(1, status["remaining"])

    def test_fully_consumed_portion_is_finished(self):
        events = [
            product(), root_portion(quantity=2),
            consume("portion-1", 1, "2026-09-27T08:00:00+08:00", "evt-eat-1"),
            consume("portion-1", 1, "2026-09-28T08:00:00+08:00", "evt-eat-2"),
        ]
        service, _ = make_service(events)
        self.assertEqual("finished", service.status("portion-1")["level"])
        self.assertEqual(0, service.status("portion-1")["remaining"])

    def test_explanation_traces_product_version_and_events(self):
        events = [
            product(), root_portion(),
            storage("portion-1", "freezer", "2026-09-25T09:00:00+08:00", "evt-freeze"),
        ]
        service, _ = make_service(events)
        status = service.status("portion-1")
        self.assertTrue(status["explanation"])
        for entry in status["explanation"]:
            self.assertEqual("product-1", entry["product_ref"])
            self.assertEqual(3, entry["recipe_version"])
        deadline_entries = [e for e in status["explanation"] if e["rule"] == "frozen_hours"]
        self.assertTrue(any("evt-freeze" in e["event_ids"] for e in deadline_entries))
        self.assertIn("仅依据已登记规则", status["disclaimer"])

    def test_member_ref_is_masked_in_replay(self):
        events = [
            product(), root_portion(),
            consume("portion-1", 1, "2026-09-27T08:00:00+08:00", "evt-eat", member="grandma-phone-13800138000"),
        ]
        service, _ = make_service(events)
        report = json.dumps(service.replay("portion-1"), ensure_ascii=False)
        self.assertNotIn("grandma-phone-13800138000", report)
        self.assertIn("member:", report)


if __name__ == "__main__":
    unittest.main()
