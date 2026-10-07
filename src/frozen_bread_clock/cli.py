"""冷冻面包食用时钟命令行。

子命令：
  validate <schema> <event.json>              校验单个事件信封
  import <db> <schema> <events.jsonl>          幂等导入事件流（逐行报告结果）
  status <db> <schema> <portion_id> [--at]    查询某份额当前结论
  replay <db> <schema> <portion_id> [--at]    客服重放：逐段依据 + 时间线
  reminders <db> <schema> {scan,send} [--at]  批量提醒（重启不重发）
  recall <db> <schema> <lot_no>               沿订单与拆分谱系定位召回份额
  member <db> <member_ref> [--name --phone]   家庭成员敏感信息独立存放
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .contracts import validate_event
from .model import parse_instant
from .privacy import public_member_view
from .service import FrozenBreadService
from .store import EventStore


def _load_schema(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _service(db_path: str, schema_path: str, zone: str) -> tuple[FrozenBreadService, EventStore]:
    store = EventStore(db_path)
    service = FrozenBreadService(store, _load_schema(schema_path), zone=zone)
    return service, store


def _print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def cmd_validate(args: argparse.Namespace) -> int:
    schema = _load_schema(args.schema)
    event = json.loads(Path(args.event).read_text(encoding="utf-8"))
    issues = validate_event(event, schema)
    if not issues:
        print("valid")
        return 0
    for issue in issues:
        print(f"{issue.field}\t{issue.code}\t{issue.message}")
    return 1


def cmd_import(args: argparse.Namespace) -> int:
    service, store = _service(args.db, args.schema, args.zone)
    inserted = duplicated = conflicts = 0
    for line_no, line in enumerate(Path(args.events).read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            print(f"第 {line_no} 行不是合法 JSON: {exc}", file=sys.stderr)
            conflicts += 1
            continue
        outcome = service.ingest(event)
        print(f"第 {line_no} 行 {event.get('event_id', '?')}: {outcome.result} {outcome.detail}".rstrip())
        if outcome.result == "INSERTED":
            inserted += 1
        elif outcome.result == "DUPLICATE_IDENTICAL":
            duplicated += 1
        else:
            conflicts += 1
    print(f"汇总: 新增 {inserted}，重复 {duplicated}，冲突/拒绝 {conflicts}")
    store.close()
    return 1 if conflicts else 0


def _at(args: argparse.Namespace):
    return parse_instant(args.at) if args.at else None


def cmd_status(args: argparse.Namespace) -> int:
    service, store = _service(args.db, args.schema, args.zone)
    _print_json(service.status(args.portion, at=_at(args)))
    store.close()
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    service, store = _service(args.db, args.schema, args.zone)
    _print_json(service.replay_timeline(args.portion, at=_at(args)))
    store.close()
    return 0


def cmd_reminders(args: argparse.Namespace) -> int:
    service, store = _service(args.db, args.schema, args.zone)
    if args.action == "scan":
        rows = [item.as_dict() for item in service.due_reminders(at=_at(args))]
    else:
        rows = service.send_due_reminders(at=_at(args))
    _print_json(rows)
    store.close()
    return 0


def cmd_recall(args: argparse.Namespace) -> int:
    service, store = _service(args.db, args.schema, args.zone)
    portions = service.recalled_portions(args.lot_no)
    _print_json({"lot_no": args.lot_no, "affected_portions": portions, "count": len(portions)})
    store.close()
    return 0 if portions else 3


def cmd_member(args: argparse.Namespace) -> int:
    """登记家庭成员真实信息（独立密钥表），并展示脱敏投影。"""
    store = EventStore(args.db)
    store.put_member_secret(args.member_ref, args.name or "", args.phone or "")
    secret = store.member_secret(args.member_ref)
    _print_json(public_member_view(secret or {}))
    store.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="frozen-bread-clock", description="冷冻面包食用时钟")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("validate", help="校验单个事件")
    p.add_argument("schema")
    p.add_argument("event")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("import", help="幂等导入 JSONL 事件流")
    p.add_argument("db")
    p.add_argument("schema")
    p.add_argument("events")
    p.add_argument("--zone", default="Asia/Shanghai")
    p.set_defaults(func=cmd_import)

    for name, func, help_text in (
        ("status", cmd_status, "查询份额结论"),
        ("replay", cmd_replay, "客服重放时间线"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("db")
        p.add_argument("schema")
        p.add_argument("portion")
        p.add_argument("--at", help="注入查询时刻（带时区 ISO-8601），缺省用系统时钟")
        p.add_argument("--zone", default="Asia/Shanghai")
        p.set_defaults(func=func)

    p = sub.add_parser("reminders", help="批量提醒扫描/发送")
    p.add_argument("db")
    p.add_argument("schema")
    p.add_argument("action", choices=("scan", "send"))
    p.add_argument("--at")
    p.add_argument("--zone", default="Asia/Shanghai")
    p.set_defaults(func=cmd_reminders)

    p = sub.add_parser("recall", help="按批次定位召回份额")
    p.add_argument("db")
    p.add_argument("schema")
    p.add_argument("lot_no")
    p.add_argument("--zone", default="Asia/Shanghai")
    p.set_defaults(func=cmd_recall)

    p = sub.add_parser("member", help="登记家庭成员敏感信息（独立存放，输出脱敏投影）")
    p.add_argument("db")
    p.add_argument("member_ref")
    p.add_argument("--name")
    p.add_argument("--phone")
    p.set_defaults(func=cmd_member)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
