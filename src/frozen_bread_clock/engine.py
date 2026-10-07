"""逐份面包时间线引擎。

纯函数式重放：输入 = 某份额谱系的全部已登记事件 + 选定规则 + 召回集合 + 注入时钟，
输出 = 每个时间段的结论及其采用的事实事件、产品版本与规则版本。
相同输入必须得到完全相同的结论（不读系统时钟、不碰随机数）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence

from .clock import display_instant
from .model import (
    FROZEN_METHODS,
    FREEZE_VIAS,
    THAW_VIAS,
    FoodRules,
    METHOD_LABELS,
    PortionStatus,
    StorageMethod,
    TransitionVia,
    VIA_LABELS,
    format_duration,
    parse_instant,
)


# ---------------------------------------------------------------- 结论结构


@dataclass(frozen=True)
class Basis:
    """一条结论的依据：采用了哪些事实、哪份产品版本、哪版规则。"""

    event_ids: tuple[str, ...]
    product_version: int | None
    rules_version: int | None
    fact: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_ids": list(self.event_ids),
            "product_version": self.product_version,
            "rules_version": self.rules_version,
            "fact": self.fact,
        }


@dataclass(frozen=True)
class Segment:
    start: datetime
    end: datetime | None
    method: StorageMethod
    opened: bool
    generation: int
    deadline: datetime | None
    advice: str
    basis: Basis

    def as_dict(self, zone: str) -> dict[str, Any]:
        return {
            "from": display_instant(self.start, zone),
            "to": display_instant(self.end, zone) if self.end else None,
            "method": self.method.value,
            "method_zh": METHOD_LABELS[self.method],
            "opened": self.opened,
            "freeze_generation": self.generation,
            "deadline": display_instant(self.deadline, zone) if self.deadline else None,
            "advice": self.advice,
            "basis": self.basis.as_dict(),
        }


@dataclass(frozen=True)
class Conflict:
    logical_key: str
    reason: str
    event_ids: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "logical_key": self.logical_key,
            "reason": self.reason,
            "event_ids": list(self.event_ids),
        }


@dataclass(frozen=True)
class PortionConclusion:
    portion_id: str
    order_no: str
    lot_no: str
    sku: str
    product_version: int
    allergens: str
    status: PortionStatus
    current_method: StorageMethod | None
    opened: bool
    freeze_generation: int
    remaining_qty: float
    unit: str
    deadline: datetime | None
    member_alias: str
    segments: tuple[Segment, ...]
    conflicts: tuple[Conflict, ...]
    timeline_events: tuple[dict[str, Any], ...]
    reheat_hint: str
    explanation: str

    def as_dict(self, *, zone: str = "Asia/Shanghai", now: datetime | None = None) -> dict[str, Any]:
        remaining: timedelta | None = None
        expired = False
        if self.deadline is not None and now is not None:
            remaining = self.deadline - now
            expired = now >= self.deadline
        return {
            "portion_id": self.portion_id,
            "order_no": self.order_no,
            "lot_no": self.lot_no,
            "product_sku": self.sku,
            "product_version": self.product_version,
            "allergen_text": self.allergens,
            "status": self.status.value,
            "current_method": self.current_method.value if self.current_method else None,
            "current_method_zh": METHOD_LABELS[self.current_method] if self.current_method else None,
            "opened": self.opened,
            "freeze_generation": self.freeze_generation,
            "remaining_qty": self.remaining_qty,
            "unit": self.unit,
            "deadline": display_instant(self.deadline, zone) if self.deadline else None,
            "remaining": format_duration(remaining) if remaining is not None else None,
            "expired": expired,
            "member_alias": self.member_alias,
            "reheat_hint": self.reheat_hint,
            "explanation": self.explanation,
            "segments": [segment.as_dict(zone) for segment in self.segments],
            "conflicts": [conflict.as_dict() for conflict in self.conflicts],
            "timeline": list(self.timeline_events),
        }


# ---------------------------------------------------------------- 规则登记


class RulesRegistry:
    """按 SKU 登记规则，选择覆盖生产时刻的最高 rules_version（改版只影响适用批次）。"""

    def __init__(self) -> None:
        self._rules: dict[str, list[FoodRules]] = {}

    def add(self, rules: FoodRules) -> None:
        bucket = self._rules.setdefault(rules.product_sku, [])
        if rules not in bucket:
            bucket.append(rules)
            bucket.sort(key=lambda item: (item.applicable_from, item.rules_version))

    def select(self, sku: str, produced_at: datetime) -> FoodRules | None:
        candidates = [item for item in self._rules.get(sku, []) if item.covers(produced_at)]
        return max(candidates, key=lambda item: item.rules_version, default=None)


# ---------------------------------------------------------------- 归一化事实


@dataclass(frozen=True)
class Fact:
    kind: str  # OPEN / STORAGE / THAW / CONSUME
    at: datetime
    recorded_at: datetime
    event_id: str
    data: Mapping[str, Any]


def fact_logical_key(kind: str, at: datetime) -> str:
    return f"{kind}@{at.isoformat()}"


# ---------------------------------------------------------------- 内部状态


@dataclass
class _State:
    at: datetime
    method: StorageMethod
    opened: bool
    generation: int
    deadline: datetime | None
    frozen_since: datetime | None
    fresh_since: datetime  # 本代鲜食阶段起点，用于冷冻窗口校验
    advice: str
    basis_events: list[str]
    blocked: str | None = None


@dataclass(frozen=True)
class _Frame:
    """一个结论段在起点时刻的状态快照。"""

    at: datetime
    method: StorageMethod
    opened: bool
    generation: int
    deadline: datetime | None
    advice: str
    basis_events: tuple[str, ...]
    blocked: str | None = None


# ---------------------------------------------------------------- 引擎


class TimelineEngine:
    def __init__(
        self,
        rules: RulesRegistry,
        products: Mapping[str, Mapping[str, Any]] | None = None,
        recalled_lots: Mapping[str, str] | None = None,
        warning_lead: timedelta = timedelta(hours=24),
        zone: str = "UTC",
    ) -> None:
        self.rules = rules
        # 键 f"{sku}#v{version_no}" -> PRODUCT_REGISTERED 载荷
        self.products: Mapping[str, Mapping[str, Any]] = products or {}
        # lot_no -> 召回通知说明（只含 issued_at <= 查询时刻的通知）
        self.recalled_lots = dict(recalled_lots or {})
        self.warning_lead = warning_lead
        self.zone = zone

    # ================= 公共入口

    def replay(
        self,
        order: Mapping[str, Any],
        splits: Mapping[str, Mapping[str, Any]],
        facts_by_portion: Mapping[str, Sequence[Fact]],
        portion_id: str,
        now: datetime,
    ) -> PortionConclusion:
        root_id = str(order.get("aggregate_id") or order["order_no"])
        product_version = int(order["version_no"])
        product = self.products.get(f"{order['product_sku']}#v{product_version}", {})
        allergens = str(product.get("allergen_text", ""))
        recalled_reason = self.recalled_lots.get(str(order["lot_no"]))

        conflicts: list[Conflict] = []

        if portion_id != root_id and portion_id not in splits:
            return self._unknown(portion_id, order, product_version, allergens)
        # 查询时刻之前尚未拆分出来的份额在当时不存在
        if portion_id != root_id:
            born_at = parse_instant(str(splits[portion_id]["occurred_at"]))
            if born_at > now:
                return self._unknown(portion_id, order, product_version, allergens)

        # 各份额先做幂等归并与冲突检测（只关心本份额祖先链上的事实冲突）
        chain = self._lineage_chain(root_id, splits, portion_id) or [root_id, portion_id]
        chain_set = set(chain)
        subtree = self._descendants(root_id, splits)
        merged: dict[str, list[Fact]] = {}
        for pid, raw_facts in facts_by_portion.items():
            facts, fact_conflicts = self._merge_facts(raw_facts)
            merged[pid] = facts
            if pid in chain_set:
                conflicts.extend(fact_conflicts)

        state, frames, rules_version = self._walk_lineage(
            order=order,
            splits=splits,
            merged=merged,
            portion_id=portion_id,
            root_id=root_id,
            now=now,
            conflicts=conflicts,
        )

        # 份数账只计算查询时刻之前已发生的食用（未来登记不影响当时结论）
        visible = {
            pid: [f for f in facts if f.at <= now]
            for pid, facts in merged.items()
        }
        remaining, unit, qty_conflict = self._quantity(
            order, splits, visible, portion_id, root_id, subtree
        )
        if qty_conflict:
            conflicts.append(qty_conflict)

        timeline_events = self._timeline_view(order, splits, merged, portion_id, root_id, now)
        segments = (
            self._frames_to_segments(frames, order, product_version, rules_version)
            if state and rules_version is not None else ()
        )

        no_rules = any(c.logical_key == "RULES" for c in conflicts)
        if no_rules:
            status = PortionStatus.NO_APPLICABLE_RULES
        elif conflicts:
            status = PortionStatus.CONFLICT
        elif recalled_reason:
            status = PortionStatus.RECALLED
        elif state is not None and state.blocked:
            status = PortionStatus.RULE_VIOLATION
        elif remaining <= 0:
            status = PortionStatus.CONSUMED
        elif state is not None and state.deadline is not None and now >= state.deadline:
            status = PortionStatus.EXPIRED
        else:
            status = PortionStatus.ACTIVE

        explanation = self._explain(
            status, order, recalled_reason, conflicts, state, now
        )

        return PortionConclusion(
            portion_id=portion_id,
            order_no=str(order["order_no"]),
            lot_no=str(order["lot_no"]),
            sku=str(order["product_sku"]),
            product_version=product_version,
            allergens=allergens,
            status=status,
            current_method=state.method if state and status not in (
                PortionStatus.CONFLICT, PortionStatus.NO_APPLICABLE_RULES
            ) else None,
            opened=state.opened if state else False,
            freeze_generation=state.generation if state else 0,
            remaining_qty=remaining,
            unit=unit,
            deadline=state.deadline if state else None,
            member_alias=str(order.get("member_ref") or "家庭-匿名"),
            segments=tuple(segments),
            conflicts=tuple(conflicts),
            timeline_events=tuple(timeline_events),
            reheat_hint=state.advice if state else "",
            explanation=explanation,
        )

    # ================= 谱系回放

    @staticmethod
    def _lineage_chain(root_id: str, splits: Mapping[str, Mapping[str, Any]], portion_id: str) -> list[str] | None:
        """root -> 目标份额 的链路；谱系断裂或成环返回 None。"""
        chain: list[str] = [portion_id]
        cursor = portion_id
        for _ in range(10_000):
            if cursor == root_id:
                chain.reverse()
                return chain
            split = splits.get(cursor)
            if split is None:
                return None
            chain.append(str(split["parent_ref"]))
            cursor = str(split["parent_ref"])
        return None

    @staticmethod
    def _descendants(root_id: str, splits: Mapping[str, Mapping[str, Any]]) -> set[str]:
        """订单根及其整棵拆分子树（含根）。"""
        children: dict[str, list[str]] = {}
        for portion_id, split in splits.items():
            children.setdefault(str(split["parent_ref"]), []).append(portion_id)
        result = {root_id}
        stack = list(children.get(root_id, ()))
        while stack:
            node = stack.pop()
            if node in result:
                continue
            result.add(node)
            stack.extend(children.get(node, ()))
        return result

    def _walk_lineage(
        self,
        *,
        order: Mapping[str, Any],
        splits: Mapping[str, Mapping[str, Any]],
        merged: Mapping[str, Sequence[Fact]],
        portion_id: str,
        root_id: str,
        now: datetime,
        conflicts: list[Conflict],
    ) -> tuple[_State | None, list[_Frame], int | None]:
        sku = str(order["product_sku"])
        produced_at = parse_instant(str(order["produced_at"]))
        received_at = parse_instant(str(order["received_at"]))
        rules = self.rules.select(sku, produced_at)
        if rules is None:
            conflicts.append(
                Conflict(
                    "RULES",
                    f"没有覆盖生产时刻 {produced_at.isoformat()} 的已登记规则（sku={sku}），不能给出期限",
                    (),
                )
            )
            return None, [], None

        # 链路：root -> ... -> 目标份额
        chain = self._lineage_chain(root_id, splits, portion_id)
        if chain is None:
            conflicts.append(
                Conflict("LINEAGE", f"份额 {portion_id} 的拆分谱系断裂或成环", ())
            )
            return None, [], None
        chain_set = set(chain)

        initial = StorageMethod(str(order.get("initial_method", "AMBIENT")))
        if initial not in (StorageMethod.AMBIENT, StorageMethod.REFRIGERATED, *FROZEN_METHODS):
            conflicts.append(Conflict("ORDER", f"未知的收货保存方式 {initial.value}", ()))
            return None, [], None

        state = _State(
            at=received_at,
            method=initial,
            opened=False,
            # 到货即冷冻视为第 1 个冷冻代次；常温/冷藏到货为 0（尚未入冻）
            generation=1 if initial in FROZEN_METHODS else 0,
            deadline=self._deadline_for(rules, received_at, initial, 1 if initial in FROZEN_METHODS else 0),
            frozen_since=received_at if initial in FROZEN_METHODS else None,
            fresh_since=received_at,
            advice=rules.reheat_default if initial in FROZEN_METHODS else "",
            basis_events=[str(order.get("event_id", ""))],
        )
        frames: list[_Frame] = [self._snapshot(state)]
        window_start = received_at
        basis_stack = list(state.basis_events)

        for depth, node in enumerate(chain):
            next_split_at = (
                parse_instant(str(splits[chain[depth + 1]]["occurred_at"]))
                if depth + 1 < len(chain)
                else now
            )
            if depth > 0:
                split = splits[node]
                split_at = parse_instant(str(split["occurred_at"]))
                if split_at < state.at:
                    conflicts.append(
                        Conflict(
                            fact_logical_key("SPLIT", split_at),
                            "拆分时间早于父份额的最近事实，无法继承状态",
                            (str(split.get("event_id", "")),),
                        )
                    )
                    return None, frames, rules.rules_version
                window_start = split_at
                basis_stack = basis_stack + [str(split.get("event_id", ""))]
                state.at = split_at
                state.basis_events = list(basis_stack)
                frames.append(self._snapshot(state))
                if state.blocked:
                    # 父份额在拆分前已违规/阻断，物理同批的子份额继承阻断
                    break

            node_split_at = split_at if depth > 0 else received_at
            is_target = depth + 1 == len(chain)
            for fact in merged.get(node, []):
                if is_target and fact.at > now:
                    continue  # 查询时刻之后才发生的事实不参与当时结论
                if depth > 0 and fact.at < node_split_at:
                    conflicts.append(
                        Conflict(
                            fact_logical_key(fact.kind, fact.at),
                            f"{fact.kind} 事实早于该份额的拆分时间 "
                            f"{(node_split_at or received_at).isoformat()}",
                            (fact.event_id,),
                        )
                    )
                    continue
                if fact.at < window_start or (depth + 1 < len(chain) and fact.at >= next_split_at):
                    continue  # 窗口外的事实属于谱系中的其他份额
                if fact.at < state.at:
                    conflicts.append(
                        Conflict(
                            fact_logical_key(fact.kind, fact.at),
                            f"{fact.kind} 事实早于其所在份额的建立时间 {state.at.isoformat()}",
                            (fact.event_id,),
                        )
                    )
                    continue
                if state.blocked:
                    break
                self._apply(state, fact, rules, conflicts)
                frames.append(self._snapshot(state))
                if state.blocked:
                    break

        return state, frames, rules.rules_version

    # ================= 状态转移

    def _apply(self, state: _State, fact: Fact, rules: FoodRules, conflicts: list[Conflict]) -> None:
        kind, at, data = fact.kind, fact.at, fact.data
        state.at = at
        state.basis_events = state.basis_events + [fact.event_id]

        if kind == "OPEN":
            state.opened = True
            state.advice = "已开封：需密封并与未开封库存分开，已开封份额不能倒回库存。"
            return

        if kind == "CONSUME":
            state.advice = "部分食用后，剩余份额继续按当前期限计时，不可退回未开封库存。"
            return

        if kind == "STORAGE":
            self._apply_storage(state, at, data, rules, conflicts)
            return

        if kind == "THAW":
            try:
                via = TransitionVia(str(data["via"]))
            except ValueError:
                state.blocked = "解冻方式未在契约中登记。"
                return
            if via not in THAW_VIAS:
                state.blocked = "解冻方式未在契约中登记。"
                return
            if state.method not in FROZEN_METHODS:
                state.blocked = "解冻事实与当前状态不符：该份额当时并未冷冻。"
                return
            if state.deadline is not None and at > state.deadline:
                state.blocked = "冷冻期限已过后才开始解冻，按规则不能再按正常期限食用。"
                return
            thaw_rule = rules.thaw.get(via)
            if thaw_rule is None:
                state.blocked = f"规则 v{rules.rules_version} 未登记 {VIA_LABELS[via]} 的解冻后期限。"
                return
            state.method = (
                StorageMethod.REFRIGERATED if via is TransitionVia.THAW_IN_FRIDGE else StorageMethod.AMBIENT
            )
            state.frozen_since = None
            state.fresh_since = at
            state.deadline = at + thaw_rule.max_after
            state.advice = thaw_rule.reheat or rules.reheat_default
            if not rules.refreeze_allowed or state.generation >= rules.refreeze_limit:
                state.advice += "（按规则本份额已不可再次冷冻）"
            return

    def _apply_storage(
        self, state: _State, at: datetime, data: Mapping[str, Any], rules: FoodRules,
        conflicts: list[Conflict],
    ) -> None:
        try:
            method = StorageMethod(str(data["method"]))
        except ValueError:
            state.blocked = f"保存方式未在契约中登记: {data.get('method')}"
            return
        try:
            via = TransitionVia(str(data["via"])) if data.get("via") else None
        except ValueError:
            via = None

        if method in FROZEN_METHODS:
            self._apply_freeze(state, at, method, via, rules)
            return

        if method not in (StorageMethod.AMBIENT, StorageMethod.REFRIGERATED):
            state.blocked = f"未支持的保存方式 {method.value}。"
            return

        if state.method in FROZEN_METHODS:
            state.blocked = "冷冻份额必须先登记解冻开始（THAW_STARTED），不能直接改回常温/冷藏。"
            return

        # 鲜食互切不延寿：取原期限与新方式期限的较早者
        candidate = self._deadline_for(rules, at, method, state.generation)
        state.method = method
        state.fresh_since = at
        if candidate is not None:
            state.deadline = min(d for d in (state.deadline, candidate) if d is not None)
        state.advice = ""

    def _apply_freeze(
        self, state: _State, at: datetime, method: StorageMethod,
        via: TransitionVia | None, rules: FoodRules,
    ) -> None:
        if state.method in FROZEN_METHODS:
            if method is StorageMethod.REFROZEN_WITH_RULES:
                state.blocked = "未经过解冻事实，不能直接登记为重新冷冻。"
                return
            # 调冷柜温度不延长既有冷冻期限，也不能抹掉重新冷冻的风险代次标识
            candidate = self._deadline_for(rules, state.frozen_since or at, method, state.generation)
            if candidate is not None and state.deadline is not None:
                state.deadline = min(state.deadline, candidate)
            if state.method is not StorageMethod.REFROZEN_WITH_RULES:
                state.method = method
            return

        if via not in FREEZE_VIAS:
            state.blocked = "入冻事件缺少登记的入冻方式（via），无法建立冷冻期限。"
            return

        if via is TransitionVia.REFREEZE_AFTER_THAW:
            if state.frozen_since is not None or state.generation < 1:
                state.blocked = "重新冷冻必须发生在一次解冻之后。"
                return
            if not rules.refreeze_allowed or state.generation > rules.refreeze_limit:
                state.blocked = (
                    f"规则 v{rules.rules_version} 不允许第 {state.generation} 次解冻后重新冷冻，"
                    "须按解冻后期限食用。"
                )
                return
            new_generation = state.generation + 1
            source_note = ""
        else:
            if state.generation >= 1:
                # 已经历过冷冻->解冻，普通入冻路径不适用，必须走 REFREEZE_AFTER_THAW
                state.blocked = (
                    "该份额已解冻过，不能按普通入冻处理；只有规则允许时以"
                    " REFREEZE_AFTER_THAW 重新冷冻并建立新风险代次。"
                )
                return
            source = (
                StorageMethod.REFRIGERATED
                if via is TransitionVia.FREEZE_FROM_REFRIGERATED
                else StorageMethod.AMBIENT
            )
            if state.method is not source:
                state.blocked = (
                    f"登记的入冻方式（{VIA_LABELS[via]}）与当时保存方式"
                    f"（{METHOD_LABELS[state.method]}）不一致，已暂停建议。"
                )
                return
            # 冷冻窗口：与冷冻期限是相互独立的规则键，禁止把冷藏天数当作冷冻期限
            window = rules.freeze_window.get(source)
            if window is None:
                state.blocked = f"规则 v{rules.rules_version} 未登记 {METHOD_LABELS[source]} 入冻窗口。"
                return
            fresh_elapsed = at - state.fresh_since
            if fresh_elapsed > window:
                state.blocked = (
                    f"超过入冻窗口 {format_duration(window)}（实际鲜食存放 {format_duration(fresh_elapsed)}），"
                    "不能再按冷冻期限计算。"
                )
                return
            new_generation = 1
            source_note = ""

        state.generation = new_generation
        state.method = StorageMethod.REFROZEN_WITH_RULES if via is TransitionVia.REFREEZE_AFTER_THAW else method
        state.frozen_since = at
        state.fresh_since = at
        state.deadline = self._deadline_for(rules, at, state.method, new_generation)
        if state.deadline is None:
            state.blocked = f"规则 v{rules.rules_version} 未登记该冷冻方式的期限。"
            return
        state.advice = rules.reheat_default + source_note

    # ================= 规则读数

    def _deadline_for(
        self, rules: FoodRules, start: datetime, method: StorageMethod, generation: int
    ) -> datetime | None:
        limit = rules.shelf_life.get(method)
        if limit is None and method is StorageMethod.REFROZEN_WITH_RULES:
            limit = rules.shelf_life.get(StorageMethod.FROZEN)
        if limit is None:
            return None
        return start + limit

    # ================= 归并 / 份数 / 视图

    def _merge_facts(self, facts: Sequence[Fact]) -> tuple[list[Fact], list[Conflict]]:
        """按发生时间归并离线补录：event_id 去重；逻辑键相同且签名一致保持一次；不一致即冲突。"""
        ordered = sorted(facts, key=lambda f: (f.at, f.recorded_at, f.event_id))
        seen_ids: dict[str, Fact] = {}
        by_key: dict[str, Fact] = {}
        conflicts: list[Conflict] = []
        merged: list[Fact] = []

        for fact in ordered:
            if fact.event_id in seen_ids:
                first = seen_ids[fact.event_id]
                if self._signature(first) != self._signature(fact):
                    conflicts.append(
                        Conflict(
                            fact.event_id,
                            "同一事件标识登记了不同内容（温度方式或份数不同）",
                            (first.event_id, fact.event_id),
                        )
                    )
                continue
            seen_ids[fact.event_id] = fact

            key = fact_logical_key(fact.kind, fact.at)
            if key in by_key:
                first = by_key[key]
                if self._signature(first) != self._signature(fact):
                    conflicts.append(
                        Conflict(
                            key,
                            "同一保存事实的温度方式或份数不一致，已暂停建议",
                            (first.event_id, fact.event_id),
                        )
                    )
                    continue
                continue  # 完全相同的记录保持一次
            by_key[key] = fact
            merged.append(fact)

        return merged, conflicts

    @staticmethod
    def _signature(fact: Fact) -> tuple[Any, ...]:
        return (
            fact.kind,
            fact.data.get("method"),
            fact.data.get("via"),
            fact.data.get("quantity"),
            fact.at.isoformat(),
        )

    def _quantity(
        self,
        order: Mapping[str, Any],
        splits: Mapping[str, Mapping[str, Any]],
        merged: Mapping[str, Sequence[Fact]],
        portion_id: str,
        root_id: str,
        subtree: set[str],
    ) -> tuple[float, str, Conflict | None]:
        unit = str(order.get("unit", "份"))

        def unit_conflict(event: Mapping[str, Any]) -> Conflict:
            return Conflict(
                f"SPLIT@{event.get('event_id')}",
                "分装单位与订单单位不一致，已暂停建议",
                (str(event.get("event_id", "")),),
            )

        # 直接子份额映射（嵌套拆分只从其父份额扣减，不在各层重复计数）
        direct_children: dict[str, list[str]] = {}
        for pid in subtree:
            if pid == root_id:
                continue
            event = splits[pid]
            if str(event.get("unit", unit)) != unit:
                return 0.0, unit, unit_conflict(event)
            direct_children.setdefault(str(event["parent_ref"]), []).append(pid)

        def inbound_qty(node: str) -> float:
            return float(order["quantity"]) if node == root_id else float(splits[node]["quantity"])

        # 每个节点：剩余 = 进入份数 - 直接分出份数 - 自己吃掉的份数
        def remaining_of(node: str) -> float:
            split_out = sum(float(splits[child]["quantity"])
                            for child in direct_children.get(node, ()))
            return inbound_qty(node) - split_out - self._eaten(merged.get(node, ()))

        # 任一节点超分/超吃都冲突（含整条子树，保证订单总账守恒）
        for node in (root_id, *[pid for pid in subtree if pid != root_id]):
            remaining = remaining_of(node)
            if remaining < -1e-9:
                label = "订单" if node == root_id else f"份额 {node}"
                return remaining, unit, Conflict(
                    "QUANTITY",
                    f"{label}的分装与食用合计超过进入份数 {inbound_qty(node):g}",
                    (str(splits[node].get("event_id", "")),) if node != root_id else (),
                )
        return remaining_of(portion_id), unit, None

    @staticmethod
    def _eaten(facts: Iterable[Fact]) -> float:
        return sum(float(f.data.get("quantity", 0)) for f in facts if f.kind == "CONSUME")

    def _frames_to_segments(
        self, frames: Sequence[_Frame], order: Mapping[str, Any],
        product_version: int, rules_version: int,
    ) -> list[Segment]:
        if not frames:
            return []
        segments: list[Segment] = []
        for index, frame in enumerate(frames):
            end = frames[index + 1].at if index + 1 < len(frames) else None
            segments.append(
                Segment(
                    start=frame.at,
                    end=end,
                    method=frame.method,
                    opened=frame.opened,
                    generation=frame.generation,
                    deadline=frame.deadline,
                    advice=frame.blocked or frame.advice,
                    basis=Basis(
                        event_ids=frame.basis_events,
                        product_version=product_version,
                        rules_version=rules_version,
                        fact=("规则违规阻断：" + frame.blocked) if frame.blocked
                        else f"{METHOD_LABELS[frame.method]}段（冷冻代次 {frame.generation}）",
                    ),
                )
            )
        return segments

    def _timeline_view(
        self,
        order: Mapping[str, Any],
        splits: Mapping[str, Mapping[str, Any]],
        merged: Mapping[str, Sequence[Fact]],
        portion_id: str,
        root_id: str,
        now: datetime,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = [
            {
                "at": display_instant(parse_instant(str(order["received_at"])), self.zone),
                "event": "ORDER_PLACED",
                "event_id": order.get("event_id"),
                "product_version": int(order["version_no"]),
                "detail": f"收货 {order.get('quantity')}{order.get('unit', '份')}，批次 {order['lot_no']}",
            }
        ]
        if portion_id != root_id:
            split = splits[portion_id]
            rows.append(
                {
                    "at": display_instant(parse_instant(str(split["occurred_at"])), self.zone),
                    "event": "PORTION_SPLIT",
                    "event_id": split.get("event_id"),
                    "detail": f"从 {split['parent_ref']} 分出 {split['quantity']}{split.get('unit', '份')}",
                }
            )
        for fact in merged.get(portion_id, ()):
            if fact.at > now:
                continue  # 查询时刻之后的事实不展示在当时的时间线里
            if fact.kind == "OPEN":
                detail = "开封"
            elif fact.kind == "STORAGE":
                method = StorageMethod(str(fact.data["method"]))
                detail = f"保存方式→{METHOD_LABELS[method]}"
                if fact.data.get("via"):
                    detail += f"（{VIA_LABELS[TransitionVia(str(fact.data['via']))]}）"
            elif fact.kind == "THAW":
                detail = f"开始解冻（{VIA_LABELS[TransitionVia(str(fact.data['via']))]}）"
            else:
                detail = f"食用 {fact.data.get('quantity')}"
            rows.append(
                {
                    "at": display_instant(fact.at, self.zone),
                    "event": fact.kind,
                    "event_id": fact.event_id,
                    "recorded_at": display_instant(fact.recorded_at, self.zone),
                    "detail": detail,
                }
            )
        rows.sort(key=lambda row: (row["at"], str(row["event_id"])))
        return rows

    # ================= 解释文案

    def _explain(
        self,
        status: PortionStatus,
        order: Mapping[str, Any],
        recalled_reason: str | None,
        conflicts: Sequence[Conflict],
        state: _State | None,
        now: datetime,
    ) -> str:
        if status is PortionStatus.UNKNOWN_PORTION:
            return f"未登记的份额: 无法在订单 {order.get('order_no')} 的拆分谱系中找到该份额。"
        if status is PortionStatus.NO_APPLICABLE_RULES:
            return "没有适用于该批次的已登记食品规则，系统不自行猜测期限，请等待产品方发布规则。"
        if status is PortionStatus.CONFLICT:
            first = conflicts[0]
            return f"登记事实互相冲突，已暂停期限建议（{first.reason}）。请核对温度方式与份数后重新登记。"
        if status is PortionStatus.RECALLED:
            return (
                f"该份额属于安全召回批次 {order['lot_no']}：{recalled_reason} "
                "请勿食用，按召回通知处理并保留包装。"
            )
        if status is PortionStatus.RULE_VIOLATION:
            return (
                f"违反已登记食品规则，已暂停期限建议：{state.blocked if state else ''} "
                "系统不替代感官判断，如有异味、异色、霉点请丢弃。"
            )
        if status is PortionStatus.CONSUMED:
            return "该份额已全部食用，时钟关闭。"
        if status is PortionStatus.EXPIRED:
            overdue = format_duration(now - (state.deadline or now)) if state else ""
            return (
                f"按规则期限已到期（超期 {overdue}），系统不建议仅凭外观食用；"
                "感官异常请丢弃，身体不适应就医。"
            )
        left = (state.deadline - now) if state and state.deadline else None
        if state and state.frozen_since is not None:
            base = (
                f"冷冻保存中（第 {state.generation} 代），剩余 {format_duration(left) if left is not None else '—'}；"
                f"食用前按提示复热：{state.advice or '按包装提示'}"
            )
        elif left is not None and left <= self.warning_lead:
            base = f"已进入临期窗口（{format_duration(self.warning_lead)}内），剩余 {format_duration(left)}，请尽快食用。"
        else:
            base = (
                f"{'已开封，' if state and state.opened else ''}"
                f"{METHOD_LABELS[state.method] if state else ''}保存中，"
                f"剩余 {format_duration(left) if left is not None else '—'}。"
            )
        return base + " 本结论仅依据已登记规则，不替代感官异常判断或医疗建议。"

    def _unknown(
        self, portion_id: str, order: Mapping[str, Any],
        product_version: int, allergens: str,
    ) -> PortionConclusion:
        return PortionConclusion(
            portion_id=portion_id,
            order_no=str(order["order_no"]),
            lot_no=str(order["lot_no"]),
            sku=str(order["product_sku"]),
            product_version=product_version,
            allergens=allergens,
            status=PortionStatus.UNKNOWN_PORTION,
            current_method=None,
            opened=False,
            freeze_generation=0,
            remaining_qty=0.0,
            unit=str(order.get("unit", "份")),
            deadline=None,
            member_alias="家庭-匿名",
            segments=(),
            conflicts=(Conflict("PORTION", f"份额 {portion_id} 不在订单拆分谱系中", ()),),
            timeline_events=(),
            reheat_hint="",
            explanation=self._explain(PortionStatus.UNKNOWN_PORTION, order, None, (), None,
                                      parse_instant(str(order["received_at"]))),
        )

    @staticmethod
    def _snapshot(state: _State) -> _Frame:
        return _Frame(
            at=state.at,
            method=state.method,
            opened=state.opened,
            generation=state.generation,
            deadline=state.deadline,
            advice=state.advice,
            basis_events=tuple(state.basis_events),
            blocked=state.blocked,
        )
