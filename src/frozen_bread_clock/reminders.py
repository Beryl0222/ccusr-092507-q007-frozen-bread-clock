"""批量提醒：结论不变不重复发送，任务重启后不重复发送。

提醒键由份额、等级与期限（或召回版本）组成：结论变化会产生新键，
结论不变则键不变，配合已发送记录实现幂等。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class Reminder:
    key: str
    portion_id: str
    kind: str
    message: str
    deadline_at: str | None


def plan_reminders(status: Mapping[str, Any]) -> list[Reminder]:
    """由份额状态推导应发提醒；暂停建议时不发送食用提醒。"""
    portion_id = status.get("portion_id")
    if not portion_id or status.get("level") in (None, "unknown", "finished"):
        return []
    recall = status.get("recall")
    if recall:
        return [
            Reminder(
                key=f"{portion_id}:recall:{recall['notice_version']}",
                portion_id=portion_id,
                kind="recall",
                message=f"安全召回：{recall['instruction']}",
                deadline_at=None,
            )
        ]
    if status.get("advice_suspended"):
        return []
    level = status.get("level")
    deadline = status.get("deadline_at")
    if level == "use_soon":
        return [
            Reminder(
                key=f"{portion_id}:use_soon:{deadline}",
                portion_id=portion_id,
                kind="use_soon",
                message=f"份额将于 {deadline} 到期，请尽快食用",
                deadline_at=deadline,
            )
        ]
    if level == "expired":
        return [
            Reminder(
                key=f"{portion_id}:expired:{deadline}",
                portion_id=portion_id,
                kind="expired",
                message=f"份额已于 {deadline} 到期，请勿食用",
                deadline_at=deadline,
            )
        ]
    return []


class ReminderStore:
    """已发送记录的持久化；重启后从文件恢复，避免重复发送。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path else None
        self._sent: set[str] = set()
        if self._path and self._path.exists():
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._sent = {str(key) for key in data}

    def has(self, key: str) -> bool:
        return key in self._sent

    def mark(self, key: str) -> None:
        self._sent.add(key)

    def save(self) -> None:
        if not self._path:
            return
        self._path.write_text(
            json.dumps(sorted(self._sent), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def run_batch(
    service: Any,
    portion_ids: Iterable[str],
    store: ReminderStore,
    now: Any = None,
) -> list[Reminder]:
    """执行一轮批量提醒，返回本轮实际发送的提醒。"""
    sent: list[Reminder] = []
    for portion_id in sorted(portion_ids):
        for reminder in plan_reminders(service.status(portion_id, now)):
            if store.has(reminder.key):
                continue
            store.mark(reminder.key)
            sent.append(reminder)
    store.save()
    return sent
