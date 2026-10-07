"""领域枚举、已登记食品规则与时间原语。

规则来自 RULES_PUBLISHED 事件，引擎只读取、不创造规则；
冷藏时限与冷冻时限是相互独立的键，避免把冷藏天数当作冷冻期限。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Mapping


class StorageMethod(str, Enum):
    AMBIENT = "AMBIENT"
    REFRIGERATED = "REFRIGERATED"
    FROZEN = "FROZEN"
    DEEP_FROZEN = "DEEP_FROZEN"
    REFROZEN_WITH_RULES = "REFROZEN_WITH_RULES"


FRESH_METHODS = (StorageMethod.AMBIENT, StorageMethod.REFRIGERATED)
FROZEN_METHODS = (StorageMethod.FROZEN, StorageMethod.DEEP_FROZEN, StorageMethod.REFROZEN_WITH_RULES)


class TransitionVia(str, Enum):
    FREEZE_FROM_AMBIENT = "FREEZE_FROM_AMBIENT"
    FREEZE_FROM_REFRIGERATED = "FREEZE_FROM_REFRIGERATED"
    REFREEZE_AFTER_THAW = "REFREEZE_AFTER_THAW"
    THAW_IN_FRIDGE = "THAW_IN_FRIDGE"
    THAW_AMBIENT = "THAW_AMBIENT"
    THAW_MICROWAVE = "THAW_MICROWAVE"


FREEZE_VIAS = {
    TransitionVia.FREEZE_FROM_AMBIENT,
    TransitionVia.FREEZE_FROM_REFRIGERATED,
    TransitionVia.REFREEZE_AFTER_THAW,
}
THAW_VIAS = {
    TransitionVia.THAW_IN_FRIDGE,
    TransitionVia.THAW_AMBIENT,
    TransitionVia.THAW_MICROWAVE,
}

VIA_LABELS = {
    TransitionVia.FREEZE_FROM_AMBIENT: "常温入冻",
    TransitionVia.FREEZE_FROM_REFRIGERATED: "冷藏入冻",
    TransitionVia.REFREEZE_AFTER_THAW: "解冻后按规则重新冷冻",
    TransitionVia.THAW_IN_FRIDGE: "冷藏室解冻",
    TransitionVia.THAW_AMBIENT: "常温解冻",
    TransitionVia.THAW_MICROWAVE: "微波炉解冻",
}

METHOD_LABELS = {
    StorageMethod.AMBIENT: "常温",
    StorageMethod.REFRIGERATED: "冷藏",
    StorageMethod.FROZEN: "冷冻",
    StorageMethod.DEEP_FROZEN: "深冻",
    StorageMethod.REFROZEN_WITH_RULES: "重新冷冻（新风险代次）",
}


class NoticeKind(str, Enum):
    GUIDE_REVISION = "GUIDE_REVISION"
    RECALL = "RECALL"


class PortionStatus(str, Enum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    RECALLED = "RECALLED"
    CONFLICT = "CONFLICT"
    RULE_VIOLATION = "RULE_VIOLATION"
    CONSUMED = "CONSUMED"
    NO_APPLICABLE_RULES = "NO_APPLICABLE_RULES"
    UNKNOWN_PORTION = "UNKNOWN_PORTION"


_DURATION_RE = re.compile(
    r"^P(?:(?P<weeks>\d+[.,]?\d*)W)?"
    r"(?:(?P<days>\d+[.,]?\d*)D)?"
    r"(?:T(?:(?P<hours>\d+[.,]?\d*)H)?(?:(?P<minutes>\d+[.,]?\d*)M)?(?:(?P<seconds>\d+[.,]?\d*)S)?)?$"
)


def parse_instant(value: str) -> datetime:
    """解析 ISO-8601 时间，拒绝朴素时间（无时区不允许猜测）。"""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"时间缺少时区: {value}")
    return parsed.astimezone(timezone.utc)


def parse_duration(value: str) -> timedelta:
    """解析 PnDTnH 形式的 ISO-8601 时长；不接受年月（食物规则只需要天/小时）。"""
    match = _DURATION_RE.match(value)
    if not match or value in ("P", "PT"):
        raise ValueError(f"无法解析时长: {value}")
    parts = {key: float(raw.replace(",", ".")) for key, raw in match.groupdict(default=None).items() if raw}
    return timedelta(
        weeks=parts.get("weeks", 0.0),
        days=parts.get("days", 0.0),
        hours=parts.get("hours", 0.0),
        minutes=parts.get("minutes", 0.0),
        seconds=parts.get("seconds", 0.0),
    )


def format_duration(value: timedelta) -> str:
    """稳定的中文剩余时长表达。"""
    total_seconds = int(value.total_seconds())
    if total_seconds == 0:
        return "0 分钟"
    sign = "已超期 " if total_seconds < 0 else ""
    total_seconds = abs(total_seconds)
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    pieces = []
    if days:
        pieces.append(f"{days} 天")
    if hours:
        pieces.append(f"{hours} 小时")
    if minutes and not days:
        pieces.append(f"{minutes} 分钟")
    return sign + "".join(pieces) if pieces else sign + "不足 1 分钟"


@dataclass(frozen=True)
class ThawRule:
    max_after: timedelta
    reheat: str


@dataclass(frozen=True)
class FoodRules:
    """某一版本、适用于某个生产时间窗的已登记食品规则。"""

    product_sku: str
    rules_version: int
    applicable_from: datetime
    applicable_to: datetime | None
    shelf_life: Mapping[StorageMethod, timedelta]
    freeze_window: Mapping[StorageMethod, timedelta]
    refreeze_allowed: bool
    refreeze_limit: int
    thaw: Mapping[TransitionVia, ThawRule]
    reheat_default: str
    source_event_id: str

    @classmethod
    def from_payload(
        cls, product_sku: str, rules_version: int, payload: Mapping[str, Any], source_event_id: str
    ) -> "FoodRules":
        rules = payload.get("rules", payload)
        applied_from = payload.get("applicable_from") or rules.get("applicable_from")
        applied_to = payload.get("applicable_to") or rules.get("applicable_to")
        if not applied_from:
            raise ValueError("规则缺少 applicable_from")
        shelf = {
            StorageMethod(key): parse_duration(raw)
            for key, raw in rules.get("shelf_life", {}).items()
            if key in StorageMethod._value2member_map_
        }
        window = {
            StorageMethod(key): parse_duration(raw)
            for key, raw in rules.get("freeze_window", {}).items()
            if key in StorageMethod._value2member_map_
        }
        refreeze = rules.get("refreeze", {})
        thaw_rules = {
            TransitionVia(key): ThawRule(
                max_after=parse_duration(raw["max_after"]), reheat=str(raw.get("reheat", ""))
            )
            for key, raw in rules.get("thaw", {}).items()
            if key in THAW_VIAS
        }
        return cls(
            product_sku=product_sku,
            rules_version=int(rules_version),
            applicable_from=parse_instant(applied_from),
            applicable_to=parse_instant(applied_to) if applied_to else None,
            shelf_life=shelf,
            freeze_window=window,
            refreeze_allowed=bool(refreeze.get("allowed", False)),
            refreeze_limit=int(refreeze.get("limit", 0)),
            thaw=thaw_rules,
            reheat_default=str(rules.get("reheat_default", "")),
            source_event_id=source_event_id,
        )

    def covers(self, produced_at: datetime) -> bool:
        if produced_at < self.applicable_from:
            return False
        return self.applicable_to is None or produced_at < self.applicable_to
