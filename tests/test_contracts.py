from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from frozen_bread_clock.clock import FixedClock
from frozen_bread_clock.contracts import validate_event
from frozen_bread_clock.engine import Fact, RulesRegistry, TimelineEngine
from frozen_bread_clock.model import (
    FoodRules,
    PortionStatus,
    StorageMethod,
    TransitionVia,
    parse_duration,
    parse_instant,
)
from frozen_bread_clock.privacy import redact_payload
from frozen_bread_clock.service import FrozenBreadService
from frozen_bread_clock.store import AppendResult, EventStore


def make_rules(version: int = 1, **overrides) -> FoodRules:
    payload = {
        "applicable_from": "2026-09-01T00:00:00+08:00",
        "shelf_life": {
            "AMBIENT": "P2D",
            "REFRIGERATED": "P5D",
            "FROZEN": "P30D",
            "REFROZEN_WITH_RULES": "P7D",
        },
        "freeze_window": {"AMBIENT": "P1D", "REFRIGERATED": "P2D"},
        "refreeze": {"allowed": True, "limit": 1},
        "thaw": {
            "THAW_IN_FRIDGE": {"max_after": "P2D", "reheat": "160C 8 分钟"},
            "THAW_AMBIENT": {"max_after": "PT8H", "reheat": "160C 8 分钟"},
        },
        "reheat_default": "180C 5 分钟",
    }
    payload.update(overrides)
    return FoodRules.from_payload("sku1", version, payload, f"rules-v{version}")


def order(**overrides):
    base = {
        "event_id": "o1",
        "order_no": "ord1",
        "product_sku": "sku1",
        "version_no": 2,
        "lot_no": "L1",
        "produced_at": "2026-09-20T08:00:00+08:00",
        "received_at": "2026-09-24T18:00:00+08:00",
        "initial_method": "AMBIENT",
        "quantity": 4,
        "unit": "个",
        "member_ref": "fam-a",
    }
    base.update(overrides)
    return base


def fact(kind, at, eid, data=None, *, recorded=None):
    return Fact(
        kind=kind,
        at=parse_instant(at),
        recorded_at=parse_instant(recorded or at),
        event_id=eid,
        data=data or {},
    )


def engine(rules=None, *, recalled=None, warning_hours=24) -> TimelineEngine:
    registry = RulesRegistry()
    registry.add(rules or make_rules())
    return TimelineEngine(
        registry,
        products={"sku1#v2": {"allergen_text": "小麦、乳"}},
        recalled_lots=recalled or {},
        warning_lead=__import__("datetime").timedelta(hours=warning_hours),
    )


class RulesTests(unittest.TestCase):
    def test_refrigerated_and_frozen_limits_are_independent(self) -> None:
        # 冷藏 P5D 与冷冻 P30D 是两个独立键，时长解析互不影响
        rules = make_rules()
        self.assertEqual(rules.shelf_life[StorageMethod.REFRIGERATED], parse_duration("P5D"))
        self.assertEqual(rules.shelf_life[StorageMethod.FROZEN], parse_duration("P30D"))
        self.assertNotEqual(
            rules.shelf_life[StorageMethod.REFRIGERATED],
            rules.shelf_life[StorageMethod.FROZEN],
        )

    def test_rules_revision_only_applies_to_new_lots(self) -> None:
        registry = RulesRegistry()
        v1 = make_rules(1, applicable_from="2026-09-01T00:00:00+08:00",
                        applicable_to="2026-09-25T00:00:00+08:00",
                        shelf_life={"AMBIENT": "P2D"})
        v2 = make_rules(2, applicable_from="2026-09-25T00:00:00+08:00",
                        applicable_to=None, shelf_life={"AMBIENT": "P3D"})
        registry.add(v2)
        registry.add(v1)
        old_lot = parse_instant("2026-09-20T08:00:00+08:00")
        new_lot = parse_instant("2026-09-26T08:00:00+08:00")
        self.assertEqual(registry.select("sku1", old_lot).rules_version, 1)
        self.assertEqual(registry.select("sku1", new_lot).rules_version, 2)

    def test_no_applicable_rules_blocks_advice(self) -> None:
        eng = engine()
        result = eng.replay(
            order(produced_at="2025-01-01T00:00:00+08:00"), {}, {}, "ord1",
            parse_instant("2026-09-24T19:00:00+08:00"),
        )
        self.assertEqual(result.status, PortionStatus.NO_APPLICABLE_RULES)


class TimelineTests(unittest.TestCase):
    def test_freeze_within_window_sets_frozen_deadline(self) -> None:
        facts = {
            "ord1": [
                fact("STORAGE", "2026-09-25T10:00:00+08:00", "f1",
                     {"method": "FROZEN", "via": "FREEZE_FROM_AMBIENT"},
                     recorded="2026-10-07T10:00:00+08:00"),  # 离线补录
            ]
        }
        result = engine().replay(
            order(), {}, facts, "ord1", parse_instant("2026-09-26T09:00:00+08:00")
        )
        self.assertEqual(result.status, PortionStatus.ACTIVE)
        # 收货 P2D 常温期限 09-26 18:00；09-25 10:00 入冻后按 P30D -> 10-25 10:00
        self.assertEqual(result.deadline, parse_instant("2026-10-25T10:00:00+08:00"))
        self.assertEqual(result.freeze_generation, 1)

    def test_late_freeze_is_rule_violation_not_a_frozen_deadline(self) -> None:
        # 超过常温入冻窗口 P1D（收货 18:00 -> 次日 18:00），不能按冷冻期限算
        facts = {
            "ord1": [
                fact("STORAGE", "2026-09-26T20:00:00+08:00", "f1",
                     {"method": "FROZEN", "via": "FREEZE_FROM_AMBIENT"})
            ]
        }
        result = engine().replay(
            order(), {}, facts, "ord1", parse_instant("2026-09-26T21:00:00+08:00")
        )
        self.assertEqual(result.status, PortionStatus.RULE_VIOLATION)
        self.assertIn("入冻窗口", result.explanation)

    def test_cannot_treat_refrigerated_days_as_frozen_window(self) -> None:
        # 冷藏入冻窗口 P2D；登记成常温来源且实际为冷藏状态 -> 冲突阻断
        facts = {
            "ord1": [
                fact("STORAGE", "2026-09-24T20:00:00+08:00", "c1", {"method": "REFRIGERATED"}),
                fact("STORAGE", "2026-09-26T20:00:00+08:00", "f1",
                     {"method": "FROZEN", "via": "FREEZE_FROM_AMBIENT"}),
            ]
        }
        result = engine().replay(
            order(), {}, facts, "ord1", parse_instant("2026-09-26T21:00:00+08:00")
        )
        self.assertEqual(result.status, PortionStatus.RULE_VIOLATION)

    def test_thaw_then_refreeze_creates_new_generation(self) -> None:
        facts = {
            "ord1": [
                fact("STORAGE", "2026-09-25T10:00:00+08:00", "f1",
                     {"method": "FROZEN", "via": "FREEZE_FROM_AMBIENT"}),
                fact("THAW", "2026-10-10T09:00:00+08:00", "t1", {"via": "THAW_IN_FRIDGE"}),
                fact("STORAGE", "2026-10-10T20:00:00+08:00", "r1",
                     {"method": "REFROZEN_WITH_RULES", "via": "REFREEZE_AFTER_THAW"}),
            ]
        }
        result = engine().replay(
            order(), {}, facts, "ord1", parse_instant("2026-10-12T09:00:00+08:00")
        )
        self.assertEqual(result.status, PortionStatus.ACTIVE)
        self.assertEqual(result.freeze_generation, 2)
        self.assertEqual(result.current_method, StorageMethod.REFROZEN_WITH_RULES)
        self.assertEqual(result.deadline, parse_instant("2026-10-17T20:00:00+08:00"))

    def test_refreeze_beyond_limit_is_blocked(self) -> None:
        facts = {
            "ord1": [
                fact("STORAGE", "2026-09-25T10:00:00+08:00", "f1",
                     {"method": "FROZEN", "via": "FREEZE_FROM_AMBIENT"}),
                fact("THAW", "2026-10-01T09:00:00+08:00", "t1", {"via": "THAW_IN_FRIDGE"}),
                fact("STORAGE", "2026-10-01T20:00:00+08:00", "r1",
                     {"method": "REFROZEN_WITH_RULES", "via": "REFREEZE_AFTER_THAW"}),
                fact("THAW", "2026-10-05T09:00:00+08:00", "t2", {"via": "THAW_IN_FRIDGE"}),
                fact("STORAGE", "2026-10-05T20:00:00+08:00", "r2",
                     {"method": "REFROZEN_WITH_RULES", "via": "REFREEZE_AFTER_THAW"}),
            ]
        }
        result = engine().replay(
            order(), {}, facts, "ord1", parse_instant("2026-10-05T21:00:00+08:00")
        )
        self.assertEqual(result.status, PortionStatus.RULE_VIOLATION)

    def test_thawed_portion_cannot_revert_to_frozen_without_refreeze_rule(self) -> None:
        facts = {
            "ord1": [
                fact("STORAGE", "2026-09-25T10:00:00+08:00", "f1",
                     {"method": "FROZEN", "via": "FREEZE_FROM_AMBIENT"}),
                fact("THAW", "2026-10-10T09:00:00+08:00", "t1", {"via": "THAW_IN_FRIDGE"}),
                # 直接把冷藏改回普通冷冻是不允许的
                fact("STORAGE", "2026-10-10T20:00:00+08:00", "b1",
                     {"method": "FROZEN", "via": "FREEZE_FROM_REFRIGERATED"}),
            ]
        }
        result = engine().replay(
            order(), {}, facts, "ord1", parse_instant("2026-10-10T21:00:00+08:00")
        )
        self.assertEqual(result.status, PortionStatus.RULE_VIOLATION)

    def test_partial_consumption_closes_at_zero_and_overeat_conflicts(self) -> None:
        eaten = {"ord1": [fact("CONSUME", "2026-09-25T20:00:00+08:00", "e1", {"quantity": 4})]}
        result = engine().replay(
            order(), {}, eaten, "ord1", parse_instant("2026-09-25T21:00:00+08:00")
        )
        self.assertEqual(result.status, PortionStatus.CONSUMED)
        self.assertEqual(result.remaining_qty, 0.0)

        over = {"ord1": [fact("CONSUME", "2026-09-25T20:00:00+08:00", "e1", {"quantity": 5})]}
        result = engine().replay(
            order(), {}, over, "ord1", parse_instant("2026-09-25T21:00:00+08:00")
        )
        self.assertEqual(result.status, PortionStatus.CONFLICT)


class MergeAndConflictTests(unittest.TestCase):
    def test_identical_offline_records_collapse_to_one(self) -> None:
        same = {
            "ord1": [
                fact("STORAGE", "2026-09-25T10:00:00+08:00", "f1",
                     {"method": "FROZEN", "via": "FREEZE_FROM_AMBIENT"},
                     recorded="2026-09-25T12:00:00+08:00"),
                fact("STORAGE", "2026-09-25T10:00:00+08:00", "f1",
                     {"method": "FROZEN", "via": "FREEZE_FROM_AMBIENT"},
                     recorded="2026-10-07T09:00:00+08:00"),
            ]
        }
        result = engine().replay(
            order(), {}, same, "ord1", parse_instant("2026-09-26T09:00:00+08:00")
        )
        self.assertEqual(result.status, PortionStatus.ACTIVE)
        self.assertEqual(len(result.timeline_events), 2)  # 订单 + 1 条保存事实

    def test_same_event_id_different_content_pauses(self) -> None:
        clash = {
            "ord1": [
                fact("STORAGE", "2026-09-25T10:00:00+08:00", "dup",
                     {"method": "FROZEN", "via": "FREEZE_FROM_AMBIENT"}),
                fact("STORAGE", "2026-09-25T10:00:00+08:00", "dup",
                     {"method": "REFRIGERATED"}),
            ]
        }
        result = engine().replay(
            order(), {}, clash, "ord1", parse_instant("2026-09-26T09:00:00+08:00")
        )
        self.assertEqual(result.status, PortionStatus.CONFLICT)

    def test_same_logical_fact_different_method_pauses(self) -> None:
        clash = {
            "ord1": [
                fact("STORAGE", "2026-09-25T10:00:00+08:00", "f1", {"method": "FROZEN",
                     "via": "FREEZE_FROM_AMBIENT"}),
                fact("STORAGE", "2026-09-25T10:00:00+08:00", "f2", {"method": "REFRIGERATED"}),
            ]
        }
        result = engine().replay(
            order(), {}, clash, "ord1", parse_instant("2026-09-26T09:00:00+08:00")
        )
        self.assertEqual(result.status, PortionStatus.CONFLICT)
        self.assertTrue(result.conflicts)


class SplitLineageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.splits = {
            "ord1-b": {
                "event_id": "s1",
                "parent_ref": "ord1",
                "quantity": 2,
                "unit": "个",
                "occurred_at": "2026-09-25T09:00:00+08:00",
            }
        }

    def test_split_inherits_parent_state_and_keeps_own_quantity(self) -> None:
        facts = {
            "ord1": [
                fact("STORAGE", "2026-09-25T08:00:00+08:00", "f0",
                     {"method": "FROZEN", "via": "FREEZE_FROM_AMBIENT"}),
            ],
            "ord1-b": [],
        }
        result = engine().replay(
            order(), self.splits, facts, "ord1-b",
            parse_instant("2026-09-26T09:00:00+08:00"),
        )
        self.assertEqual(result.status, PortionStatus.ACTIVE)
        self.assertEqual(result.current_method, StorageMethod.FROZEN)
        self.assertEqual(result.remaining_qty, 2.0)
        # 依据链包含订单与拆分事件，可追溯
        basis_ids = result.segments[-1].basis.event_ids
        self.assertIn("o1", basis_ids)
        self.assertIn("s1", basis_ids)
        self.assertEqual(result.segments[-1].basis.rules_version, 1)
        self.assertEqual(result.segments[-1].basis.product_version, 2)

    def test_root_remaining_excludes_only_its_own_splits(self) -> None:
        # 另一订单的拆分不得计入本订单
        splits = dict(self.splits)
        splits["ord9-x"] = {
            "event_id": "s9", "parent_ref": "ord9", "quantity": 99, "unit": "个",
            "occurred_at": "2026-09-25T09:00:00+08:00",
        }
        result = engine().replay(
            order(), splits, {"ord1": [], "ord1-b": []}, "ord1",
            parse_instant("2026-09-25T10:00:00+08:00"),
        )
        self.assertEqual(result.remaining_qty, 2.0)

    def test_nested_split_quantity_is_conserved(self) -> None:
        # root 4 -> b 分 2 -> c 从 b 分 1：根剩 2、b 剩 1、c 剩 1
        splits = dict(self.splits)
        splits["ord1-c"] = {
            "event_id": "s2", "parent_ref": "ord1-b", "quantity": 1, "unit": "个",
            "occurred_at": "2026-09-25T09:30:00+08:00",
        }
        facts = {"ord1": [], "ord1-b": [], "ord1-c": []}
        at = parse_instant("2026-09-25T10:00:00+08:00")
        self.assertEqual(engine().replay(order(), splits, facts, "ord1", at).remaining_qty, 2.0)
        self.assertEqual(engine().replay(order(), splits, facts, "ord1-b", at).remaining_qty, 1.0)
        self.assertEqual(engine().replay(order(), splits, facts, "ord1-c", at).remaining_qty, 1.0)

    def test_over_split_at_nested_level_conflicts(self) -> None:
        splits = dict(self.splits)
        splits["ord1-c"] = {
            "event_id": "s2", "parent_ref": "ord1-b", "quantity": 3, "unit": "个",
            "occurred_at": "2026-09-25T09:30:00+08:00",
        }
        at = parse_instant("2026-09-25T10:00:00+08:00")
        # c 只分到 3 但 b 只有 2；查根与 c 都应看到冲突
        self.assertEqual(
            engine().replay(order(), splits, {"ord1": [], "ord1-b": [], "ord1-c": []},
                            "ord1-c", at).status,
            PortionStatus.CONFLICT,
        )

    def test_fact_before_split_conflicts(self) -> None:
        facts = {"ord1-b": [
            fact("OPEN", "2026-09-24T20:00:00+08:00", "early", {}),
        ]}
        result = engine().replay(
            order(), self.splits, facts, "ord1-b",
            parse_instant("2026-09-26T09:00:00+08:00"),
        )
        self.assertEqual(result.status, PortionStatus.CONFLICT)

    def test_unknown_portion(self) -> None:
        result = engine().replay(
            order(), self.splits, {}, "nope",
            parse_instant("2026-09-26T09:00:00+08:00"),
        )
        self.assertEqual(result.status, PortionStatus.UNKNOWN_PORTION)


class BoundaryAndZoneTests(unittest.TestCase):
    def test_deadline_is_expired_at_exact_instant(self) -> None:
        eng = engine()
        deadline = parse_instant("2026-09-26T18:00:00+08:00")  # 收货 + P2D
        before = eng.replay(order(), {}, {}, "ord1", deadline - parse_duration("PT1S"))
        at = eng.replay(order(), {}, {}, "ord1", deadline)
        self.assertEqual(before.status, PortionStatus.ACTIVE)
        self.assertEqual(at.status, PortionStatus.EXPIRED)

    def test_timezone_changes_display_not_deadline(self) -> None:
        eng = engine()
        instant = parse_instant("2026-09-26T18:00:00+08:00")
        view_cn = eng.replay(order(), {}, {}, "ord1", instant).as_dict(
            now=instant, zone="Asia/Shanghai")
        view_de = eng.replay(order(), {}, {}, "ord1", instant).as_dict(
            now=instant, zone="Europe/Berlin")
        self.assertEqual(view_cn["status"], view_de["status"])
        self.assertIn("+08:00", view_cn["deadline"])
        self.assertIn("+02:00", view_de["deadline"])

    def test_fixed_clock_is_deterministic(self) -> None:
        fixed = FixedClock(parse_instant("2026-09-26T18:00:00+08:00"))
        self.assertEqual(fixed.now(), fixed.now())
        with self.assertRaises(ValueError):
            FixedClock(__import__("datetime").datetime(2026, 9, 26, 18, 0))


class TraceabilityTests(unittest.TestCase):
    def test_segments_carry_product_and_rules_version(self) -> None:
        facts = {
            "ord1": [
                fact("STORAGE", "2026-09-25T10:00:00+08:00", "f1",
                     {"method": "FROZEN", "via": "FREEZE_FROM_AMBIENT"}),
            ]
        }
        result = engine().replay(
            order(), {}, facts, "ord1", parse_instant("2026-09-26T09:00:00+08:00")
        )
        for segment in result.segments:
            self.assertEqual(segment.basis.product_version, 2)
            self.assertEqual(segment.basis.rules_version, 1)
            self.assertTrue(segment.basis.event_ids)
        self.assertEqual(result.product_version, 2)


class ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads((ROOT / "contracts" / "domain.schema.json").read_text("utf-8"))
        cls.sample = json.loads((ROOT / "data" / "sample.json").read_text("utf-8"))

    def test_sample_is_valid(self) -> None:
        self.assertEqual([], validate_event(self.sample, self.schema))

    def test_time_and_version_boundaries(self) -> None:
        event = dict(self.sample, occurred_at="2026-09-24T12:00:00", version=0)
        codes = {(i.field, i.code) for i in validate_event(event, self.schema)}
        self.assertIn(("occurred_at", "timezone_required"), codes)
        self.assertIn(("version", "positive_integer"), codes)

    def test_recall_requires_lot_but_guide_does_not(self) -> None:
        base = {
            "event_id": "n1", "event_type": "NOTICE_RECEIVED",
            "aggregate_type": "safety_notice", "aggregate_id": "x",
            "occurred_at": "2026-10-01T10:00:00+08:00", "version": 1,
            "payload": {"notice_no": "N", "kind": "RECALL", "notice_version": 1,
                        "issued_at": "2026-10-01T09:00:00+08:00", "summary": "x"},
        }
        self.assertTrue(any(i.field == "payload.lot_no" for i in validate_event(base, self.schema)))
        base["payload"]["kind"] = "GUIDE_REVISION"
        self.assertFalse(any(i.field == "payload.lot_no" for i in validate_event(base, self.schema)))

    def test_payload_enums_are_checked(self) -> None:
        event = dict(self.sample, event_type="STORAGE_RECORDED",
                     payload={"portion_id": "p", "method": "SUN", "occurred_at":
                              "2026-09-25T10:00:00+08:00"})
        codes = {(i.field, i.code) for i in validate_event(event, self.schema)}
        self.assertIn(("payload.method", "unsupported_value"), codes)


def _event(eid, etype, atype, aid, at, version, payload):
    return {
        "event_id": eid, "event_type": etype, "aggregate_type": atype,
        "aggregate_id": aid, "occurred_at": at, "version": version, "payload": payload,
    }


RULES_PAYLOAD = {
    "product_sku": "sku1", "rules_version": 1,
    "applicable_from": "2026-09-01T00:00:00+08:00",
    "rules": {
        "shelf_life": {"AMBIENT": "P2D", "REFRIGERATED": "P5D", "FROZEN": "P30D",
                       "REFROZEN_WITH_RULES": "P7D"},
        "freeze_window": {"AMBIENT": "P1D", "REFRIGERATED": "P2D"},
        "refreeze": {"allowed": True, "limit": 1},
        "thaw": {"THAW_IN_FRIDGE": {"max_after": "P2D", "reheat": "160C"}},
        "reheat_default": "180C",
    },
}


class ServicePersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "events.db")
        self.schema = json.loads((ROOT / "contracts" / "domain.schema.json").read_text("utf-8"))
        self.service = FrozenBreadService(EventStore(self.db_path), self.schema)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _seed_order(self, lot="L1", order_id="ord1", produced="2026-09-20T08:00:00+08:00"):
        self.service.ingest(_event("r1", "RULES_PUBLISHED", "rules_catalog", "cat",
                                   "2026-09-01T10:00:00+08:00", 1, RULES_PAYLOAD))
        self.service.ingest(_event("p1", "PRODUCT_REGISTERED", "product_version", "pv",
                                   "2026-09-01T11:00:00+08:00", 1,
                                   {"product_sku": "sku1", "version_no": 2,
                                    "allergen_text": "麦",
                                    "applicable_lot_from": "2026-09-01T00:00:00+08:00"}))
        self.service.ingest(_event("o1", "ORDER_PLACED", "household_portion", order_id,
                                   "2026-09-24T18:10:00+08:00", 1,
                                   {"order_no": order_id, "product_sku": "sku1", "version_no": 2,
                                    "lot_no": lot, "produced_at": produced,
                                    "received_at": "2026-09-24T18:00:00+08:00",
                                    "initial_method": "AMBIENT", "quantity": 4, "unit": "个",
                                    "member_ref": "fam-a"}))

    def test_event_id_idempotent_and_conflict(self) -> None:
        self._seed_order()
        good = _event("e9", "OPENING_RECORDED", "storage_timeline", "tl",
                      "2026-09-25T12:00:00+08:00", 1,
                      {"portion_id": "ord1", "occurred_at": "2026-09-25T12:00:00+08:00"})
        self.assertEqual(self.service.ingest(good).result, AppendResult.INSERTED)
        self.assertEqual(self.service.ingest(good).result, AppendResult.DUPLICATE_IDENTICAL)
        bad = dict(good, payload={"portion_id": "ord1", "occurred_at":
                                  "2026-09-25T13:00:00+08:00"})
        self.assertEqual(self.service.ingest(bad).result, AppendResult.CONFLICT)

    def test_version_gap_rejected(self) -> None:
        self._seed_order()
        out = self.service.ingest(_event("e9", "OPENING_RECORDED", "storage_timeline", "tl",
                                         "2026-09-25T12:00:00+08:00", 3,
                                         {"portion_id": "ord1",
                                          "occurred_at": "2026-09-25T12:00:00+08:00"}))
        self.assertEqual(out.result, AppendResult.VERSION_GAP)

    def test_sensitive_member_fields_are_stripped(self) -> None:
        self.service.ingest(_event("r1", "RULES_PUBLISHED", "rules_catalog", "cat",
                                   "2026-09-01T10:00:00+08:00", 1, RULES_PAYLOAD))
        outcome = self.service.ingest(_event(
            "o1", "ORDER_PLACED", "household_portion", "ord1", "2026-09-24T18:10:00+08:00", 1,
            {"order_no": "ord1", "product_sku": "sku1", "version_no": 2, "lot_no": "L1",
             "produced_at": "2026-09-20T08:00:00+08:00",
             "received_at": "2026-09-24T18:00:00+08:00", "initial_method": "AMBIENT",
             "quantity": 1, "unit": "个", "member_ref": "fam-a",
             "member_name": "张三", "member_phone": "13800000000"}))
        self.assertEqual(outcome.result, AppendResult.INSERTED)
        raw = self.service.store.load_aggregate("ord1")[0]["payload"]
        self.assertNotIn("member_name", raw)
        self.assertNotIn("member_phone", raw)
        view = self.service.status("ord1", at=parse_instant("2026-09-24T19:00:00+08:00"))
        self.assertNotIn("张三", json.dumps(view, ensure_ascii=False))
        self.assertEqual(view["member_alias"], "fam-a")

    def test_recall_traverses_split_lineage(self) -> None:
        self._seed_order(lot="BAD")
        self.service.ingest(_event("s1", "PORTION_SPLIT", "household_portion", "ord1-b",
                                   "2026-09-25T09:00:00+08:00", 1,
                                   {"parent_ref": "ord1", "quantity": 2, "unit": "个",
                                    "occurred_at": "2026-09-25T09:00:00+08:00"}))
        self.service.ingest(_event("s2", "PORTION_SPLIT", "household_portion", "ord1-c",
                                   "2026-09-25T09:30:00+08:00", 1,
                                   {"parent_ref": "ord1-b", "quantity": 1, "unit": "个",
                                    "occurred_at": "2026-09-25T09:30:00+08:00"}))
        self.service.ingest(_event("n1", "NOTICE_RECEIVED", "safety_notice", "rec",
                                   "2026-10-01T10:00:00+08:00", 1,
                                   {"notice_no": "N", "kind": "RECALL", "notice_version": 1,
                                    "issued_at": "2026-10-01T09:00:00+08:00",
                                    "lot_no": "BAD", "summary": "异物召回"}))
        affected = set(self.service.recalled_portions("BAD"))
        self.assertEqual(affected, {"ord1", "ord1-b", "ord1-c"})
        view = self.service.status("ord1-c", at=parse_instant("2026-10-02T09:00:00+08:00"))
        self.assertEqual(view["status"], "RECALLED")

    def test_reminders_do_not_resend_after_restart(self) -> None:
        self._seed_order()
        at = parse_instant("2026-09-26T17:00:00+08:00")  # 到期前 1 小时
        first = self.service.send_due_reminders(at=at)
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["milestone"], "WARNING")
        # 模拟任务重启：新服务实例打开同一个库文件
        restarted = FrozenBreadService(EventStore(self.db_path), self.schema)
        second = restarted.send_due_reminders(at=at)
        self.assertEqual(second, [])
        scanned = restarted.due_reminders(at=at)
        self.assertTrue(all(item.already_sent for item in scanned))

    def test_replay_is_stable_across_rebuilds(self) -> None:
        self._seed_order()
        self.service.ingest(_event("f1", "STORAGE_RECORDED", "storage_timeline", "tl",
                                   "2026-10-07T08:00:00+08:00", 1,
                                   {"portion_id": "ord1", "method": "FROZEN",
                                    "via": "FREEZE_FROM_AMBIENT",
                                    "occurred_at": "2026-09-25T10:00:00+08:00"}))
        at = parse_instant("2026-09-26T09:00:00+08:00")
        first = self.service.replay_timeline("ord1", at=at)
        rebuilt = FrozenBreadService(EventStore(self.db_path), self.schema)
        second = rebuilt.replay_timeline("ord1", at=at)
        self.assertEqual(
            json.dumps(first, ensure_ascii=False, sort_keys=True),
            json.dumps(second, ensure_ascii=False, sort_keys=True),
        )

    def test_historical_replay_ignores_later_recall_and_facts(self) -> None:
        self._seed_order()
        self.service.ingest(_event("s1", "PORTION_SPLIT", "household_portion", "ord1-b",
                                   "2026-09-25T09:00:00+08:00", 1,
                                   {"parent_ref": "ord1", "quantity": 2, "unit": "个",
                                    "occurred_at": "2026-09-25T09:00:00+08:00"}))
        # 10 月 1 日食用完毕，10 月 12 日召回
        self.service.ingest(_event("e1", "CONSUMPTION_RECORDED", "storage_timeline", "tl-b",
                                   "2026-10-01T09:00:00+08:00", 1,
                                   {"portion_id": "ord1-b", "quantity": 2,
                                    "occurred_at": "2026-10-01T08:00:00+08:00"}))
        self.service.ingest(_event("n1", "NOTICE_RECEIVED", "safety_notice", "rec",
                                   "2026-10-12T10:00:00+08:00", 1,
                                   {"notice_no": "N", "kind": "RECALL", "notice_version": 1,
                                    "issued_at": "2026-10-12T09:00:00+08:00",
                                    "lot_no": "L1", "summary": "异物召回"}))
        # 历史时刻（9 月 26 日）：召回尚未发布，食用尚未发生
        past = self.service.status("ord1-b", at=parse_instant("2026-09-26T09:00:00+08:00"))
        self.assertNotEqual(past["status"], "RECALLED")
        self.assertEqual(past["remaining_qty"], 2.0)
        # 当前：召回生效
        now_view = self.service.status("ord1-b", at=parse_instant("2026-10-13T09:00:00+08:00"))
        self.assertEqual(now_view["status"], "RECALLED")
class PrivacyUnitTests(unittest.TestCase):
    def test_redact_keeps_alias(self) -> None:
        clean = redact_payload({"member_ref": "fam-a", "member_name": "张三",
                                "note": "ok", "member_phone": "13800000000"})
        self.assertEqual(clean, {"member_ref": "fam-a", "note": "ok"})


if __name__ == "__main__":
    unittest.main()
