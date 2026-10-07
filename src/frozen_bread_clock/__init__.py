"""冷冻面包食用时钟。

公开入口：
- validate_event：交换层事件校验；
- FrozenBreadService：写入、状态查询、时间线重放、提醒与召回定位；
- EventStore：SQLite 事件存储与提醒账本；
- FixedClock / SystemClock：可注入时钟。
"""

from .clock import FixedClock, SystemClock
from .contracts import ContractIssue, validate_event
from .engine import Fact, RulesRegistry, TimelineEngine
from .model import FoodRules, PortionStatus
from .service import FrozenBreadService
from .store import AppendResult, EventStore

__all__ = [
    "ContractIssue",
    "validate_event",
    "FrozenBreadService",
    "EventStore",
    "AppendResult",
    "FixedClock",
    "SystemClock",
    "TimelineEngine",
    "RulesRegistry",
    "FoodRules",
    "Fact",
    "PortionStatus",
]
