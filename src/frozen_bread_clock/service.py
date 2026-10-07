"""冷冻面包食用时钟业务服务。

在事件交换契约之上提供：

- 幂等归并：完全相同的事件只保留一次；标识相同而内容（温度、方式、份数等）
  不同的事件视为冲突，相关份额暂停建议。
- 逐份状态机：按发生时间归并离线补录事件，沿订单与拆分谱系重放，
  推导保存状态、期限与风险；已解冻份额不能倒回未开封库存，
  重新冷冻按登记规则建立新的风险状态。
- 规则解析：指南修订只作用于适用批次；安全召回沿谱系定位份额。
- 可重放解释：每个结论都记录采用的产品版本、规则与事件标识，
  而不是一个不可追溯的剩余天数。

本服务只根据已登记的食品规则给出期限与操作提示，
不替代感官异常判断与医疗建议。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping

from .contracts import ContractIssue, validate_event

STORAGE_METHODS = ("ambient", "fridge", "freezer")
THAW_METHODS = ("ambient", "fridge")
NOTICE_KINDS = ("guideline_revision", "safety_recall")
PORTION_AGGREGATES = ("household_portion", "storage_timeline")

DISCLAIMER = "仅依据已登记规则给出期限与操作提示，不替代感官异常判断与医疗建议。"


def parse_instant(value: str) -> datetime:
    """解析必须携带时区的时间，统一转为 UTC。"""
    if not isinstance(value, str):
        raise ValueError("时间必须是字符串")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"无法解析时间：{value!r}") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("时间必须携带时区")
    return parsed.astimezone(timezone.utc)


def format_instant(value: datetime) -> str:
    """以稳定的 UTC 形式输出，保证跨时区旅行时解释一致。"""
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def mask_member(member_ref: str) -> str:
    """家庭成员标识脱敏为稳定伪名，避免在重放与召回输出中泄露。"""
    digest = hashlib.sha256(str(member_ref).encode("utf-8")).hexdigest()[:10]
    return f"member:{digest}"


def _canonical(event: Mapping[str, Any]) -> str:
    return json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _timezone_ok(value: Any) -> bool:
    try:
        parse_instant(value)
    except ValueError:
        return False
    return True


def _positive_number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and value > 0


def _positive_int(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= 1


def _number(value: float) -> int | float:
    return int(value) if float(value).is_integer() else value


@dataclass(frozen=True)
class FoodRules:
    """已登记的食品规则；冷藏与冷冻期限相互独立，绝不折算。"""

    ambient_hours: float = 8.0
    fridge_hours: float = 48.0
    freeze_window_hours: float = 24.0
    frozen_hours: float = 720.0
    thawed_ambient_hours: float = 4.0
    thawed_fridge_hours: float = 24.0
    refreeze_allowed: bool = False
    refreeze_frozen_hours: float = 0.0
    warn_hours: float = 24.0
    reheat_methods: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, data: Any) -> "FoodRules":
        if not isinstance(data, Mapping):
            return cls()

        def hours(name: str, default: float) -> float:
            raw = data.get(name, default)
            if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw < 0:
                return default
            return float(raw)

        reheat = data.get("reheat_methods", ())
        if not isinstance(reheat, (list, tuple)):
            reheat = ()
        allowed = data.get("refreeze_allowed", False)
        return cls(
            ambient_hours=hours("ambient_hours", 8.0),
            fridge_hours=hours("fridge_hours", 48.0),
            freeze_window_hours=hours("freeze_window_hours", 24.0),
            frozen_hours=hours("frozen_hours", 720.0),
            thawed_ambient_hours=hours("thawed_ambient_hours", 4.0),
            thawed_fridge_hours=hours("thawed_fridge_hours", 24.0),
            refreeze_allowed=allowed if isinstance(allowed, bool) else False,
            refreeze_frozen_hours=hours("refreeze_frozen_hours", 0.0),
            warn_hours=hours("warn_hours", 24.0),
            reheat_methods=tuple(str(item) for item in reheat),
        )


@dataclass(frozen=True)
class ProductVersion:
    product_ref: str
    product_id: str
    recipe_version: int
    lot_no: str
    produced_at: datetime
    allergens: tuple[str, ...]
    filling: str | None
    rules: FoodRules
    source_event_id: str


@dataclass(frozen=True)
class IngestResult:
    event_id: str
    outcome: str  # accepted | duplicate | conflict | rejected
    issues: tuple[ContractIssue, ...] = ()


@dataclass
class _Derivation:
    """重放谱系事件得到的份额状态。"""

    state: str = "sealed"
    opened_at: datetime | None = None
    method: str | None = None
    freeze_start: datetime | None = None
    thaw_start: datetime | None = None
    thaw_method: str | None = None
    refrozen_at: datetime | None = None
    thaw_cycles: int = 0
    fridge_before_freeze: bool = False
    consumed: float = 0.0
    risk_flags: list[str] = field(default_factory=list)
    explanation: list[dict] = field(default_factory=list)
    evidence: dict[str, str] = field(default_factory=dict)


class ClockService:
    """可注入时钟的食用时钟服务；状态是事件的纯函数，多设备并发结果一致。"""

    def __init__(
        self,
        schema: Mapping[str, Any],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._schema = schema
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._events: dict[str, Mapping[str, Any]] = {}
        self._canonical: dict[str, str] = {}
        self._conflicted_ids: set[str] = set()
        self._conflict_events: dict[str, list[Mapping[str, Any]]] = {}

    # ------------------------------------------------------------------ 摄入

    def ingest(self, events: Iterable[Mapping[str, Any]]) -> list[IngestResult]:
        return [self._ingest_one(event) for event in events]

    def _ingest_one(self, event: Any) -> IngestResult:
        event_id = event.get("event_id") if isinstance(event, Mapping) else None
        label = str(event_id) if event_id else "<unknown>"
        issues = validate_event(event, self._schema)
        if not issues and isinstance(event, Mapping):
            issues = self._semantic_issues(event)
        if issues:
            return IngestResult(label, "rejected", tuple(issues))
        canonical = _canonical(event)
        if event_id in self._events:
            if self._canonical[event_id] == canonical:
                return IngestResult(event_id, "duplicate")
            self._conflicted_ids.add(event_id)
            self._conflict_events.setdefault(event_id, [self._events[event_id]]).append(event)
            return IngestResult(event_id, "conflict")
        self._events[event_id] = event
        self._canonical[event_id] = canonical
        return IngestResult(event_id, "accepted")

    @staticmethod
    def _semantic_issues(event: Mapping[str, Any]) -> list[ContractIssue]:
        issues: list[ContractIssue] = []
        payload = event.get("payload", {})
        event_type = event["event_type"]
        if event_type == "PRODUCT_REGISTERED":
            if not _timezone_ok(payload.get("produced_at")):
                issues.append(ContractIssue("payload.produced_at", "timezone_required", "生产时间必须包含时区"))
            if not _positive_int(payload.get("recipe_version")):
                issues.append(ContractIssue("payload.recipe_version", "positive_integer", "配方版本必须是正整数"))
            if not isinstance(payload.get("rules"), Mapping):
                issues.append(ContractIssue("payload.rules", "object_required", "规则必须是 JSON 对象"))
        elif event_type == "PORTION_CREATED":
            if not _positive_number(payload.get("quantity")):
                issues.append(ContractIssue("payload.quantity", "positive_number", "份数必须是正数"))
            parent = payload.get("parent_ref")
            if not isinstance(parent, str) or not parent.strip():
                issues.append(ContractIssue("payload.parent_ref", "non_empty_string", "来源必须是非空字符串"))
        elif event_type == "STORAGE_RECORDED":
            if payload.get("method") not in STORAGE_METHODS:
                issues.append(ContractIssue("payload.method", "unsupported_value", "保存方式必须是 ambient、fridge 或 freezer"))
            if not _timezone_ok(payload.get("occurred_at")):
                issues.append(ContractIssue("payload.occurred_at", "timezone_required", "保存事件发生时间必须包含时区"))
            temperature = payload.get("temperature_c")
            if temperature is not None and (isinstance(temperature, bool) or not isinstance(temperature, (int, float))):
                issues.append(ContractIssue("payload.temperature_c", "number", "温度必须是数字"))
        elif event_type == "THAW_STARTED":
            if payload.get("method", "ambient") not in THAW_METHODS:
                issues.append(ContractIssue("payload.method", "unsupported_value", "解冻方式必须是 ambient 或 fridge"))
        elif event_type == "PORTION_CONSUMED":
            if not _positive_number(payload.get("quantity")):
                issues.append(ContractIssue("payload.quantity", "positive_number", "食用份数必须是正数"))
        elif event_type == "NOTICE_RECEIVED":
            kind = payload.get("kind")
            if kind not in NOTICE_KINDS:
                issues.append(ContractIssue("payload.kind", "unsupported_value", "通知类型必须是 guideline_revision 或 safety_recall"))
            if not _positive_int(payload.get("notice_version")):
                issues.append(ContractIssue("payload.notice_version", "positive_integer", "通知版本必须是正整数"))
            if kind == "guideline_revision" and not isinstance(payload.get("rules"), Mapping):
                issues.append(ContractIssue("payload.rules", "object_required", "指南修订必须携带规则"))
        return issues

    # ------------------------------------------------------------------ 索引

    def _active_events(self) -> list[Mapping[str, Any]]:
        return [event for event_id, event in self._events.items() if event_id not in self._conflicted_ids]

    @staticmethod
    def _effective_time(event: Mapping[str, Any]) -> datetime:
        payload = event.get("payload", {})
        occurred = payload.get("occurred_at") if isinstance(payload, Mapping) else None
        if _timezone_ok(occurred):
            return parse_instant(occurred)
        return parse_instant(event["occurred_at"])

    def _portion_creations(self) -> dict[str, Mapping[str, Any]]:
        return {
            event["aggregate_id"]: event
            for event in self._active_events()
            if event["event_type"] == "PORTION_CREATED" and event["aggregate_type"] == "household_portion"
        }

    def _children_map(self) -> dict[str, list[str]]:
        children: dict[str, list[str]] = {}
        for portion_id, event in self._portion_creations().items():
            parent = event["payload"].get("parent_ref")
            children.setdefault(parent, []).append(portion_id)
        return children

    def _descendants(self, portion_id: str) -> set[str]:
        children = self._children_map()
        found: set[str] = set()
        stack = [portion_id]
        while stack:
            current = stack.pop()
            for child in children.get(current, []):
                if child not in found:
                    found.add(child)
                    stack.append(child)
        return found

    def _ancestors(self, portion_id: str) -> list[str]:
        """根份额在前的谱系链；订单号等非份额标识终止上溯。"""
        creations = self._portion_creations()
        chain: list[str] = []
        current: Any = portion_id
        while current in creations and current not in chain:
            chain.append(current)
            current = creations[current]["payload"].get("parent_ref")
        chain.reverse()
        return chain

    def _product_map(self) -> dict[str, ProductVersion]:
        latest: dict[str, Mapping[str, Any]] = {}
        for event in self._active_events():
            if event["event_type"] != "PRODUCT_REGISTERED":
                continue
            ref = event["aggregate_id"]
            if ref not in latest or event["version"] > latest[ref]["version"]:
                latest[ref] = event
        return {ref: self._to_product(event) for ref, event in latest.items()}

    @staticmethod
    def _to_product(event: Mapping[str, Any]) -> ProductVersion:
        payload = event["payload"]
        allergens = payload.get("allergens") or ()
        return ProductVersion(
            product_ref=event["aggregate_id"],
            product_id=str(payload.get("product_id", "")),
            recipe_version=int(payload.get("recipe_version", 1)),
            lot_no=str(payload.get("lot_no", "")),
            produced_at=parse_instant(payload["produced_at"]),
            allergens=tuple(str(item) for item in allergens),
            filling=payload.get("filling"),
            rules=FoodRules.from_mapping(payload.get("rules")),
            source_event_id=event["event_id"],
        )

    def _product_for(self, portion_id: str) -> ProductVersion | None:
        chain = self._ancestors(portion_id)
        if not chain:
            return None
        root = self._portion_creations()[chain[0]]
        product_ref = root["payload"].get("product_ref")
        if not product_ref:
            return None
        return self._product_map().get(product_ref)

    def _portions_of_product(self, product_ref: str) -> set[str]:
        creations = self._portion_creations()
        roots = [
            portion_id
            for portion_id, event in creations.items()
            if event["payload"].get("product_ref") == product_ref
        ]
        found = set(roots)
        for root in roots:
            found |= self._descendants(root)
        return found

    def _portions_of_lot(self, lot_no: str) -> set[str]:
        products = self._product_map()
        found: set[str] = set()
        for ref, product in products.items():
            if product.lot_no == lot_no:
                found |= self._portions_of_product(ref)
        return found

    def portion_ids(self) -> list[str]:
        return sorted(self._portion_creations())

    # ------------------------------------------------------- 冲突与暂停

    def _suspension_reasons(self) -> dict[str, set[str]]:
        """冲突事件标识 → 受影响份额（含后代与整批次）。"""
        reasons: dict[str, set[str]] = {}
        for event_id in sorted(self._conflicted_ids):
            targets: set[str] = set()
            for variant in self._conflict_events.get(event_id, []):
                aggregate_type = variant.get("aggregate_type")
                aggregate_id = variant.get("aggregate_id")
                if aggregate_type in PORTION_AGGREGATES:
                    targets.add(aggregate_id)
                    targets |= self._descendants(aggregate_id)
                elif aggregate_type == "product_version":
                    targets |= self._portions_of_product(aggregate_id)
                elif aggregate_type == "safety_notice":
                    lot_no = variant.get("payload", {}).get("lot_no")
                    if lot_no:
                        targets |= self._portions_of_lot(lot_no)
            for portion_id in targets:
                reasons.setdefault(portion_id, set()).add(event_id)
        return reasons

    # ------------------------------------------------------- 规则与通知

    def _rules_for(self, product: ProductVersion) -> tuple[FoodRules, str]:
        """指南修订只作用于适用批次；返回 (规则, 来源事件标识)。"""
        best: tuple[int, Mapping[str, Any]] | None = None
        for event in self._active_events():
            if event["event_type"] != "NOTICE_RECEIVED":
                continue
            payload = event["payload"]
            if payload.get("kind") != "guideline_revision":
                continue
            lots = payload.get("applies_to_lots") or [payload.get("lot_no")]
            if product.lot_no not in lots:
                continue
            version = payload.get("notice_version", 0)
            if best is None or version > best[0]:
                best = (version, event)
        if best is not None:
            return FoodRules.from_mapping(best[1]["payload"].get("rules")), best[1]["event_id"]
        return product.rules, product.source_event_id

    def _recall_for(self, lot_no: str) -> dict[str, Any] | None:
        best: tuple[int, Mapping[str, Any]] | None = None
        for event in self._active_events():
            if event["event_type"] != "NOTICE_RECEIVED":
                continue
            payload = event["payload"]
            if payload.get("kind") != "safety_recall" or payload.get("lot_no") != lot_no:
                continue
            version = payload.get("notice_version", 0)
            if best is None or version > best[0]:
                best = (version, event)
        if best is None:
            return None
        event = best[1]
        return {
            "notice_ref": event["aggregate_id"],
            "notice_version": best[0],
            "instruction": event["payload"].get("instruction", "按召回通知处理"),
            "event_id": event["event_id"],
        }

    # ------------------------------------------------------- 时间线重放

    def _portion_events(self, portion_id: str) -> list[Mapping[str, Any]]:
        return [
            event
            for event in self._active_events()
            if event["aggregate_type"] in PORTION_AGGREGATES and event["aggregate_id"] == portion_id
        ]

    def _timeline_events(self, portion_id: str) -> list[Mapping[str, Any]]:
        """份额的时间线：祖先事件截止到拆分时刻，之后只计自身事件。"""
        creations = self._portion_creations()
        if portion_id not in creations:
            return []
        chain = self._ancestors(portion_id)
        events: list[Mapping[str, Any]] = []
        for index, ancestor in enumerate(chain):
            cutoff = None
            if index + 1 < len(chain):
                cutoff = self._effective_time(creations[chain[index + 1]])
            for event in self._portion_events(ancestor):
                if cutoff is not None and self._effective_time(event) > cutoff:
                    continue
                events.append(event)
        events.sort(key=lambda event: (self._effective_time(event), event["event_id"]))
        return events

    def _explain(
        self,
        derivation: _Derivation,
        conclusion: str,
        *,
        rule: str | None = None,
        event_ids: Iterable[str] = (),
        at: datetime | None = None,
    ) -> None:
        derivation.explanation.append(
            {
                "conclusion": conclusion,
                "rule": rule,
                "product_ref": None,
                "recipe_version": None,
                "rules_source": None,
                "event_ids": sorted(set(event_ids)),
                "at": format_instant(at) if at else None,
            }
        )

    def _derive(self, portion_id: str, product: ProductVersion, rules: FoodRules) -> _Derivation:
        d = _Derivation()
        for event in self._timeline_events(portion_id):
            event_type = event["event_type"]
            at = self._effective_time(event)
            payload = event.get("payload", {})
            event_id = event["event_id"]
            if event_type == "PACKAGE_OPENED":
                if d.opened_at is None:
                    d.opened_at = at
                    d.evidence["opened"] = event_id
                    if d.state == "sealed":
                        d.state = "opened"
                    self._explain(d, "记录开封事实", event_ids=[event_id], at=at)
            elif event_type == "STORAGE_RECORDED":
                method = payload["method"]
                if method == "freezer":
                    if d.state == "thawed":
                        d.state = "refrozen"
                        if d.refrozen_at is None:
                            d.refrozen_at = at
                            d.evidence["refrozen"] = event_id
                        flag = "refrozen" if rules.refreeze_allowed else "refreeze_not_allowed"
                        if flag not in d.risk_flags:
                            d.risk_flags.append(flag)
                        self._explain(d, "解冻后再次冷冻，按登记规则建立新的风险状态", event_ids=[event_id], at=at)
                    elif d.state != "refrozen":
                        if d.freeze_start is None:
                            d.freeze_start = at
                            d.evidence["freeze"] = event_id
                            base = d.opened_at or product.produced_at
                            if at - base > timedelta(hours=rules.freeze_window_hours):
                                d.risk_flags.append("freeze_window_exceeded")
                                self._explain(d, "超过冷冻窗口才进入冷冻", rule="freeze_window_hours", event_ids=[event_id], at=at)
                            else:
                                self._explain(d, "在冷冻窗口内进入冷冻", rule="freeze_window_hours", event_ids=[event_id], at=at)
                        d.state = "frozen"
                    d.method = "freezer"
                else:
                    if d.state in ("frozen", "refrozen"):
                        d.thaw_start = at
                        d.thaw_method = method
                        d.thaw_cycles += 1
                        d.state = "thawed"
                        d.evidence["thaw"] = event_id
                        self._explain(d, "离开冷冻环境，视为开始解冻", event_ids=[event_id], at=at)
                    if d.freeze_start is None and method == "fridge":
                        d.fridge_before_freeze = True
                    d.method = method
            elif event_type == "THAW_STARTED":
                if d.state != "thawed":
                    d.thaw_start = at
                    d.thaw_method = payload.get("method", "ambient")
                    d.thaw_cycles += 1
                    d.state = "thawed"
                    d.method = d.thaw_method
                    d.evidence["thaw"] = event_id
                    self._explain(d, "记录解冻开始", event_ids=[event_id], at=at)
            elif event_type == "PORTION_CONSUMED":
                d.consumed += float(payload["quantity"])
                self._explain(d, f"记录食用 {_number(float(payload['quantity']))} 份", event_ids=[event_id], at=at)
        return d

    def _deadline(
        self,
        d: _Derivation,
        product: ProductVersion,
        rules: FoodRules,
    ) -> tuple[datetime | None, str | None, str, list[str]]:
        """返回 (期限, 规则名, 结论, 证据事件)；冷藏天数绝不折算为冷冻期限。"""
        base = d.opened_at or product.produced_at
        if d.state in ("sealed", "opened"):
            evidence = [d.evidence.get("opened", product.source_event_id)]
            if d.method == "fridge":
                return base + timedelta(hours=rules.fridge_hours), "fridge_hours", "冷藏保存期限", evidence
            return base + timedelta(hours=rules.ambient_hours), "ambient_hours", "常温保存期限", evidence
        if d.state == "frozen":
            return (
                d.freeze_start + timedelta(hours=rules.frozen_hours),
                "frozen_hours",
                "冷冻保存期限（自冷冻开始单独计算）",
                [d.evidence["freeze"]],
            )
        if d.state == "thawed":
            evidence = [d.evidence["thaw"]]
            if d.thaw_method == "fridge":
                return d.thaw_start + timedelta(hours=rules.thawed_fridge_hours), "thawed_fridge_hours", "冷藏解冻后食用期限", evidence
            return d.thaw_start + timedelta(hours=rules.thawed_ambient_hours), "thawed_ambient_hours", "常温解冻后食用期限", evidence
        if d.state == "refrozen":
            if rules.refreeze_allowed and rules.refreeze_frozen_hours > 0:
                return (
                    d.refrozen_at + timedelta(hours=rules.refreeze_frozen_hours),
                    "refreeze_frozen_hours",
                    "再次冷冻后期限（按登记规则建立的新风险状态）",
                    [d.evidence["refrozen"]],
                )
            evidence = [d.evidence["refrozen"], d.evidence["thaw"]]
            if d.thaw_method == "fridge":
                return d.thaw_start + timedelta(hours=rules.thawed_fridge_hours), "thawed_fridge_hours", "再次冷冻不符合登记规则，期限仍按解冻后计算", evidence
            return d.thaw_start + timedelta(hours=rules.thawed_ambient_hours), "thawed_ambient_hours", "再次冷冻不符合登记规则，期限仍按解冻后计算", evidence
        return None, None, "", []

    # ------------------------------------------------------------------ 状态

    def _moment(self, now: datetime | str | None) -> datetime:
        if now is None:
            now = self._clock()
        if isinstance(now, str):
            return parse_instant(now)
        if isinstance(now, datetime):
            if now.tzinfo is None or now.utcoffset() is None:
                raise ValueError("当前时间必须携带时区")
            return now.astimezone(timezone.utc)
        raise TypeError("不支持的时间类型")

    @staticmethod
    def _stamp(entries: list[dict], product: ProductVersion | None, rules_source: str | None) -> list[dict]:
        for entry in entries:
            entry["product_ref"] = product.product_ref if product else None
            entry["recipe_version"] = product.recipe_version if product else None
            entry["rules_source"] = rules_source
        return entries

    def status(self, portion_id: str, now: datetime | str | None = None) -> dict[str, Any]:
        moment = self._moment(now)
        creations = self._portion_creations()
        base: dict[str, Any] = {
            "portion_id": portion_id,
            "now": format_instant(moment),
            "disclaimer": DISCLAIMER,
        }
        if portion_id not in creations:
            return {
                **base,
                "level": "unknown",
                "state": "unknown",
                "explanation": self._stamp(
                    [{"conclusion": "份额未登记", "rule": None, "product_ref": None,
                      "recipe_version": None, "rules_source": None, "event_ids": [], "at": None}],
                    None, None,
                ),
            }

        product = self._product_for(portion_id)
        if product is None:
            return {
                **base,
                "level": "unknown",
                "state": "unknown",
                "explanation": self._stamp(
                    [{"conclusion": "未找到产品登记，无法计算期限", "rule": None, "product_ref": None,
                      "recipe_version": None, "rules_source": None, "event_ids": [], "at": None}],
                    None, None,
                ),
            }

        rules, rules_source = self._rules_for(product)
        d = self._derive(portion_id, product, rules)

        children = self._children_map()
        quantity = float(creations[portion_id]["payload"]["quantity"])
        child_quantity = sum(
            float(creations[child]["payload"]["quantity"]) for child in children.get(portion_id, [])
        )
        remaining = quantity - d.consumed - child_quantity
        if remaining < 0:
            remaining = 0.0
            if "quantity_inconsistent" not in d.risk_flags:
                d.risk_flags.append("quantity_inconsistent")
            self._explain(d, "食用与分装份数超过登记总量，剩余按 0 处理")

        if d.fridge_before_freeze and d.freeze_start is not None:
            self._explain(d, "冷藏时段不计入冷冻期限，冷冻期限自冷冻开始单独计算", rule="frozen_hours")

        deadline, rule_name, deadline_label, evidence = self._deadline(d, product, rules)
        if deadline is not None:
            self._explain(
                d,
                f"{deadline_label}：{format_instant(deadline)}",
                rule=rule_name,
                event_ids=evidence,
                at=deadline,
            )

        recall = self._recall_for(product.lot_no)
        suspension = self._suspension_reasons().get(portion_id, set())
        suspended = bool(suspension)
        if suspended:
            self._explain(d, "存在标识相同但内容冲突的记录，已暂停建议", event_ids=sorted(suspension))
        if recall:
            self._explain(d, f"安全召回：{recall['instruction']}", event_ids=[recall["event_id"]])

        if recall:
            level = "recalled"
        elif suspended:
            level = "suspended"
        elif remaining <= 0:
            level = "finished"
        elif deadline is not None and moment >= deadline:
            level = "expired"
        elif deadline is not None and moment >= deadline - timedelta(hours=rules.warn_hours):
            level = "use_soon"
        else:
            level = "ok"
        self._explain(d, f"在 {format_instant(moment)} 判定为 {level}", at=moment)

        hints: list[str] = []
        if recall:
            hints.append(f"安全召回：{recall['instruction']}")
        if suspended:
            hints.append("存在冲突记录，已暂停食用建议，请人工核对")
        if not recall and not suspended:
            if d.state == "frozen":
                hints.append("冷冻保存中")
                if rules.reheat_methods:
                    hints.append("复热方式：" + "、".join(rules.reheat_methods))
            elif d.state == "thawed":
                hints.append("已解冻，请尽快食用")
                if not rules.refreeze_allowed:
                    hints.append("请勿再次冷冻")
            elif d.state == "refrozen":
                hints.append("已再次冷冻，存在品质与安全风险")
            if deadline is not None:
                hints.append(f"请于 {format_instant(deadline)} 前食用")

        return {
            **base,
            "level": level,
            "state": d.state,
            "quantity": _number(quantity),
            "remaining": _number(remaining),
            "storage_method": d.method,
            "deadline_at": format_instant(deadline) if deadline and not suspended else None,
            "risk_flags": list(d.risk_flags),
            "hints": hints,
            "allergens": list(product.allergens),
            "filling": product.filling,
            "recall": recall,
            "advice_suspended": suspended,
            "explanation": self._stamp(d.explanation, product, rules_source),
        }

    # ------------------------------------------------------------------ 重放

    def replay(self, portion_id: str, now: datetime | str | None = None) -> dict[str, Any]:
        """供客服重放某份面包的时间线；结论可追溯到产品版本与事件。"""
        moment = self._moment(now)
        timeline = [
            {
                "at": format_instant(self._effective_time(event)),
                "event_id": event["event_id"],
                "event_type": event["event_type"],
                "summary": self._summarize(event),
            }
            for event in self._timeline_events(portion_id)
        ]
        conflicts = sorted(self._suspension_reasons().get(portion_id, ()))
        return {
            "portion_id": portion_id,
            "now": format_instant(moment),
            "timeline": timeline,
            "conflict_event_ids": conflicts,
            "conclusions": self.status(portion_id, moment)["explanation"],
            "status": self.status(portion_id, moment),
        }

    @staticmethod
    def _summarize(event: Mapping[str, Any]) -> str:
        payload = event.get("payload", {})
        if not isinstance(payload, Mapping):
            return ""
        parts: list[str] = []
        if "product_id" in payload:
            parts.append(f"产品={payload['product_id']}")
        if "recipe_version" in payload:
            parts.append(f"配方版本={payload['recipe_version']}")
        if "lot_no" in payload:
            parts.append(f"批次={payload['lot_no']}")
        if "parent_ref" in payload:
            parts.append(f"来源={payload['parent_ref']}")
        if "quantity" in payload:
            parts.append(f"份数={payload['quantity']}")
        if "method" in payload:
            parts.append(f"方式={payload['method']}")
        if "temperature_c" in payload:
            parts.append(f"温度={payload['temperature_c']}℃")
        if "kind" in payload:
            parts.append(f"通知类型={payload['kind']}")
        if "member_ref" in payload:
            parts.append(f"成员={mask_member(payload['member_ref'])}")
        return "，".join(parts)
