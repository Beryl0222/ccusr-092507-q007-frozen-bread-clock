"""SQLite 事件存储与提醒账本。

写入保证：
- event_id 全局幂等：完全相同的重复提交返回 DUPLICATE_IDENTICAL；
  同 id 不同内容返回 CONFLICT，不覆盖原事件。
- (aggregate_id, version) 唯一：并发设备抢到同一版本位只有一条能写入。
- 版本必须从 1 连续递增，跳号直接拒绝（缺口不允许猜测）。
- 提醒以 (portion_id, milestone, deadline) 为幂等键落账，批量任务重启不重发。

家庭成员真实信息单独存放，事件查询接口永不返回该表。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id       TEXT PRIMARY KEY,
    event_type     TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id   TEXT NOT NULL,
    occurred_at    TEXT NOT NULL,
    version        INTEGER NOT NULL,
    payload_json   TEXT NOT NULL,
    content_hash   TEXT NOT NULL,
    recorded_at    TEXT NOT NULL,
    UNIQUE(aggregate_id, version)
);
CREATE INDEX IF NOT EXISTS idx_events_aggregate ON events(aggregate_type, aggregate_id);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);

CREATE TABLE IF NOT EXISTS reminder_ledger (
    ledger_key TEXT PRIMARY KEY,
    portion_id TEXT NOT NULL,
    milestone  TEXT NOT NULL,
    deadline   TEXT NOT NULL,
    sent_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS member_secrets (
    member_ref TEXT PRIMARY KEY,
    name       TEXT,
    phone      TEXT,
    note       TEXT
);
"""


class AppendResult:
    INSERTED = "INSERTED"
    DUPLICATE_IDENTICAL = "DUPLICATE_IDENTICAL"
    CONFLICT = "CONFLICT"
    VERSION_GAP = "VERSION_GAP"


@dataclass(frozen=True)
class AppendOutcome:
    result: str
    detail: str = ""


def _content_hash(event: Mapping[str, Any]) -> str:
    raw = json.dumps(
        {k: event.get(k) for k in ("event_type", "aggregate_type", "aggregate_id", "occurred_at",
                                   "version", "payload")},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class EventStore:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ---------------- 写入

    def append(self, event: Mapping[str, Any], *, recorded_at: datetime | None = None) -> AppendOutcome:
        """幂等写入；并发依赖 UNIQUE 约束，冲突时调用方应重读而非改写。"""
        event_id = str(event["event_id"])
        aggregate_id = str(event["aggregate_id"])
        version = int(event["version"])

        row = self._conn.execute(
            "SELECT content_hash FROM events WHERE event_id = ?", (event_id,)
        ).fetchone()
        if row is not None:
            if row["content_hash"] == _content_hash(event):
                return AppendOutcome(AppendResult.DUPLICATE_IDENTICAL)
            return AppendOutcome(AppendResult.CONFLICT, f"事件 {event_id} 已存在且内容不同")

        max_row = self._conn.execute(
            "SELECT MAX(version) AS v FROM events WHERE aggregate_id = ?", (aggregate_id,)
        ).fetchone()
        expected = (max_row["v"] or 0) + 1
        if version != expected:
            return AppendOutcome(
                AppendResult.VERSION_GAP,
                f"聚合 {aggregate_id} 下一版本应为 {expected}，收到 {version}",
            )

        stamp = (recorded_at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        try:
            self._conn.execute(
                "INSERT INTO events (event_id, event_type, aggregate_type, aggregate_id, occurred_at, "
                "version, payload_json, content_hash, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    str(event["event_type"]),
                    str(event["aggregate_type"]),
                    aggregate_id,
                    str(event["occurred_at"]),
                    version,
                    json.dumps(event["payload"], ensure_ascii=False, sort_keys=True),
                    _content_hash(event),
                    stamp,
                ),
            )
        except sqlite3.IntegrityError as exc:  # 并发抢位：另一设备已提交同版本
            return AppendOutcome(AppendResult.CONFLICT, f"聚合 {aggregate_id} 版本 {version} 已被占用: {exc}")
        self._conn.commit()
        return AppendOutcome(AppendResult.INSERTED)

    # ---------------- 读取

    def load_events(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM events ORDER BY occurred_at, event_id"
        ).fetchall()
        return [self._row_to_event(row) for row in rows]

    def load_aggregate(self, aggregate_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM events WHERE aggregate_id = ? ORDER BY version", (aggregate_id,)
        ).fetchall()
        return [self._row_to_event(row) for row in rows]

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> dict[str, Any]:
        event = {
            "event_id": row["event_id"],
            "event_type": row["event_type"],
            "aggregate_type": row["aggregate_type"],
            "aggregate_id": row["aggregate_id"],
            "occurred_at": row["occurred_at"],
            "version": row["version"],
            "payload": json.loads(row["payload_json"]),
        }
        event["_recorded_at"] = row["recorded_at"]
        return event

    # ---------------- 提醒账本

    def reminder_already_sent(self, ledger_key: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM reminder_ledger WHERE ledger_key = ?", (ledger_key,)
        ).fetchone() is not None

    def mark_reminder_sent(
        self, portion_id: str, milestone: str, deadline: str, *, sent_at: datetime | None = None
    ) -> str:
        """落账一次提醒；键已存在时不重复写入，返回统一键。"""
        key = self.reminder_key(portion_id, milestone, deadline)
        stamp = (sent_at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        self._conn.execute(
            "INSERT OR IGNORE INTO reminder_ledger (ledger_key, portion_id, milestone, deadline, sent_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (key, portion_id, milestone, deadline, stamp),
        )
        self._conn.commit()
        return key

    @staticmethod
    def reminder_key(portion_id: str, milestone: str, deadline: str) -> str:
        digest_src = f"{portion_id}|{milestone}|{deadline}"
        return hashlib.sha256(digest_src.encode("utf-8")).hexdigest()

    def ledger_size(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) AS n FROM reminder_ledger").fetchone()["n"])

    # ---------------- 成员敏感信息

    def put_member_secret(self, member_ref: str, name: str = "", phone: str = "", note: str = "") -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO member_secrets (member_ref, name, phone, note) VALUES (?, ?, ?, ?)",
            (member_ref, name, phone, note),
        )
        self._conn.commit()

    def member_secret(self, member_ref: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM member_secrets WHERE member_ref = ?", (member_ref,)
        ).fetchone()
        return dict(row) if row else None
