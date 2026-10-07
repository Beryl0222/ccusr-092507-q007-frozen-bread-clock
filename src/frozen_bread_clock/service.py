"""冷冻面包食用时钟应用服务。

从事件存储重建只读投影，再交给纯函数引擎重放；
写入走契约校验与存储幂等，查询时刻由可注入时钟决定，结论可重复、可追溯。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from .clock import Clock, SystemClock, display_instant
from .contracts import validate_event
from .engine import Fact, RulesRegistry, TimelineEngine
from .model import FoodRules, NoticeKind, PortionStatus, parse_instant
from .privacy import redact_payload
from .store import AppendOutcome, AppendResult, EventStore

FACT_TYPES = {
    "OPENING_RECORDED": "OPEN",
    "STORAGE_RECORDED": "STORAGE",
    "THAW_STARTED": "THAW",
    "CONSUMPTION_RECORDED": "CONSUME",
}


@dataclass(frozen=True)
class Reminder:
    portion_id: str
    order_no: str
    member_alias: str
    milestone: str
    deadline: str
    deadline_key: str
    message: str
    ledger_key: str
    already_sent: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "portion_id": self.portion_id,
            "order_no": self.order_no,
            "member_alias": self.member_alias,
            "milestone": self.milestone,
            "deadline": self.deadline,
            "deadline_key": self.deadline_key,
            "message": self.message,
            "already_sent": self.already_sent,
        }


class FrozenBreadService:
    def __init__(
        self,
        store: EventStore,
        schema: Mapping[str, Any],
        clock: Clock | None = None,
        zone: str = "Asia/Shanghai",
        warning_lead: timedelta = timedelta(hours=24),
    ) -> None:
        self.store = store
        self.schema = schema
        self.clock = clock or SystemClock()
        self.zone = zone
        self.warning_lead = warning_lead

    # ---------------- 写入

    def ingest(self, event: Mapping[str, Any]) -> AppendOutcome:
        """校验信封 -> 幂等落库。敏感家庭成员字段在落库前剥离。"""
        issues = validate_event(event, self.schema)
        if issues:
            return AppendOutcome(
                AppendResult.CONFLICT,
                "; ".join(f"{issue.field}:{issue.code}" for issue in issues),
            )
        clean = dict(event)
        clean["payload"] = redact_payload(event["payload"])
        return self.store.append(clean)

    # ---------------- 投影

    def _projection(self, as_of: datetime | None = None) -> dict[str, Any]:
        rules_registry = RulesRegistry()
        products: dict[str, dict[str, Any]] = {}
        orders: dict[str, dict[str, Any]] = {}
        splits: dict[str, dict[str, Any]] = {}
        facts_by_portion: dict[str, list[Fact]] = {}
        recalls: dict[str, str] = {}
        guide_notes: list[dict[str, Any]] = []

        for event in self.store.load_events():
            payload = event["payload"]
            kind_event = event["event_type"]
            recorded_at = parse_instant(str(event.get("_recorded_at") or event["occurred_at"]))

            if kind_event == "RULES_PUBLISHED":
                rules_registry.add(
                    FoodRules.from_payload(
                        str(payload["product_sku"]),
                        int(payload["rules_version"]),
                        payload,
                        event["event_id"],
                    )
                )
            elif kind_event == "PRODUCT_REGISTERED":
                key = f"{payload['product_sku']}#v{int(payload['version_no'])}"
                products.setdefault(key, {**payload, "event_id": event["event_id"]})
            elif kind_event == "ORDER_PLACED":
                root_id = str(event["aggregate_id"])
                orders[root_id] = {**payload, "aggregate_id": root_id, "event_id": event["event_id"]}
            elif kind_event == "PORTION_SPLIT":
                split_at = parse_instant(str(payload.get("occurred_at", event["occurred_at"])))
                if as_of is not None and split_at > as_of:
                    continue  # 查询时刻之后才发生的拆分当时不存在
                portion_id = str(event["aggregate_id"])
                splits.setdefault(
                    portion_id,
                    {
                        **payload,
                        "portion_id": portion_id,
                        "event_id": event["event_id"],
                        "occurred_at": payload.get("occurred_at", event["occurred_at"]),
                    },
                )
            elif kind_event in FACT_TYPES:
                fact = Fact(
                    kind=FACT_TYPES[kind_event],
                    at=parse_instant(str(payload["occurred_at"])),
                    recorded_at=recorded_at,
                    event_id=event["event_id"],
                    data=payload,
                )
                facts_by_portion.setdefault(str(payload["portion_id"]), []).append(fact)
            elif kind_event == "NOTICE_RECEIVED":
                notice_kind = str(payload["kind"])
                issued_at = parse_instant(str(payload["issued_at"]))
                if as_of is not None and issued_at > as_of:
                    continue  # 查询时刻之后才发布的通知当时不生效
                if notice_kind == NoticeKind.RECALL.value:
                    # 同一 lot 的通知取最高 notice_version（召回内容修订）
                    prev = recalls.get(str(payload["lot_no"]))
                    marker = f"[v{int(payload['notice_version'])}] {payload['summary']}"
                    recalls[str(payload["lot_no"])] = (
                        marker if prev is None else f"{prev}；{marker}"
                    )
                else:
                    guide_notes.append(
                        {
                            "notice_no": payload.get("notice_no"),
                            "notice_version": payload["notice_version"],
                            "summary": payload["summary"],
                            "event_id": event["event_id"],
                        }
                    )

        return {
            "rules": rules_registry,
            "products": products,
            "orders": orders,
            "splits": splits,
            "facts": facts_by_portion,
            "recalls": recalls,
            "guide_notes": guide_notes,
        }

    def _engine(self, projection: Mapping[str, Any]) -> TimelineEngine:
        return TimelineEngine(
            rules=projection["rules"],
            products=projection["products"],
            recalled_lots=projection["recalls"],
            warning_lead=self.warning_lead,
            zone=self.zone,
        )

    # ---------------- 谱系定位

    def _resolve_order(self, portion_id: str, projection: Mapping[str, Any]) -> str | None:
        if portion_id in projection["orders"]:
            return portion_id
        cursor = portion_id
        splits = projection["splits"]
        for _ in range(10_000):
            split = splits.get(cursor)
            if split is None:
                return None
            parent = str(split["parent_ref"])
            if parent in projection["orders"]:
                return parent
            cursor = parent
        return None

    def lineage_portions(self, root_id: str, projection: Mapping[str, Any]) -> list[str]:
        """订单根份额及其全部拆分子份额（安全召回沿此谱系定位）。"""
        result = [root_id]
        children: dict[str, list[str]] = {}
        for portion_id, split in projection["splits"].items():
            children.setdefault(str(split["parent_ref"]), []).append(portion_id)
        stack = list(children.get(root_id, ()))
        while stack:
            node = stack.pop()
            result.append(node)
            stack.extend(children.get(node, ()))
        return sorted(set(result))

    def recalled_portions(self, lot_no: str) -> list[str]:
        """供召回处置使用：按批次命中订单并沿拆分谱系返回全部份额。"""
        projection = self._projection()
        hits = [
            root_id
            for root_id, order in projection["orders"].items()
            if str(order["lot_no"]) == lot_no
        ]
        portions: list[str] = []
        for root_id in hits:
            portions.extend(self.lineage_portions(root_id, projection))
        return sorted(set(portions))

    # ---------------- 查询 / 重放

    def status(self, portion_id: str, *, at: datetime | None = None) -> dict[str, Any]:
        now = (at or self.clock.now()).astimezone(timezone.utc)
        projection = self._projection(as_of=now)
        root_id = self._resolve_order(portion_id, projection)
        if root_id is None:
            return {
                "portion_id": portion_id,
                "status": PortionStatus.UNKNOWN_PORTION.value,
                "explanation": "未登记的份额：在任何订单拆分谱系中均找不到。",
                "queried_at": display_instant(now, self.zone),
                "zone": self.zone,
            }
        engine = self._engine(projection)
        conclusion = engine.replay(
            order=projection["orders"][root_id],
            splits=projection["splits"],
            facts_by_portion=projection["facts"],
            portion_id=portion_id,
            now=now,
        )
        result = conclusion.as_dict(zone=self.zone, now=now)
        result["queried_at"] = display_instant(now, self.zone)
        result["zone"] = self.zone
        return result

    def replay_timeline(self, portion_id: str, *, at: datetime | None = None) -> dict[str, Any]:
        """客服命令：重放时间线，逐段给出采用的事实事件、产品版本、规则版本。"""
        status = self.status(portion_id, at=at)
        return {
            "portion_id": portion_id,
            "queried_at": status.get("queried_at"),
            "status": status.get("status"),
            "explanation": status.get("explanation"),
            "current": {
                key: status.get(key)
                for key in (
                    "current_method", "current_method_zh", "opened", "freeze_generation",
                    "remaining_qty", "unit", "deadline", "remaining", "expired",
                    "reheat_hint", "product_version", "product_sku", "lot_no", "allergen_text",
                    "member_alias",
                )
            },
            "segments": status.get("segments", []),
            "conflicts": status.get("conflicts", []),
            "timeline": status.get("timeline", []),
        }

    # ---------------- 批量提醒

    def due_reminders(self, *, at: datetime | None = None) -> list[Reminder]:
        """扫描全部份额，返回当前到期的提醒（含已发标记，不重复发送）。"""
        now = (at or self.clock.now()).astimezone(timezone.utc)
        projection = self._projection(as_of=now)
        engine = self._engine(projection)
        due: list[Reminder] = []

        for root_id, order in projection["orders"].items():
            for portion_id in self.lineage_portions(root_id, projection):
                conclusion = engine.replay(
                    order=order,
                    splits=projection["splits"],
                    facts_by_portion=projection["facts"],
                    portion_id=portion_id,
                    now=now,
                )
                reminder = self._reminder_for(conclusion, now)
                if reminder is not None:
                    due.append(reminder)
        due.sort(key=lambda item: (item.deadline, item.portion_id, item.milestone))
        return due

    def _reminder_for(self, conclusion: Any, now: datetime) -> Reminder | None:
        if conclusion.status is PortionStatus.RECALLED:
            deadline_key = "RECALL"
            milestone = "RECALL"
            deadline_display = "RECALL"
            message = conclusion.explanation
        elif conclusion.status in (
            PortionStatus.ACTIVE, PortionStatus.EXPIRED,
        ) and conclusion.deadline is not None:
            deadline_key = conclusion.deadline.astimezone(timezone.utc).isoformat()
            if conclusion.status is PortionStatus.EXPIRED:
                milestone = "DEADLINE"
                message = conclusion.explanation
            elif conclusion.deadline - now <= self.warning_lead:
                milestone = "WARNING"
                message = conclusion.explanation
            else:
                return None
            deadline_display = display_instant(conclusion.deadline, self.zone)
        else:
            # 冲突/违规/无规则/已食完：不推送期限提醒，由客服流程处理
            return None

        key = EventStore.reminder_key(conclusion.portion_id, milestone, deadline_key)
        return Reminder(
            portion_id=conclusion.portion_id,
            order_no=conclusion.order_no,
            member_alias=conclusion.member_alias,
            milestone=milestone,
            deadline=deadline_display,
            deadline_key=deadline_key,
            message=message,
            ledger_key=key,
            already_sent=self.store.reminder_already_sent(key),
        )

    def send_due_reminders(self, *, at: datetime | None = None) -> list[dict[str, Any]]:
        """投递未发送的到期提醒并落账；重启后再次调用不会重发。"""
        sent: list[dict[str, Any]] = []
        now = (at or self.clock.now()).astimezone(timezone.utc)
        for reminder in self.due_reminders(at=now):
            if reminder.already_sent:
                continue
            self.store.mark_reminder_sent(
                reminder.portion_id, reminder.milestone, reminder.deadline_key, sent_at=now
            )
            sent.append(reminder.as_dict())
        return sent
