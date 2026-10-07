"""命令行入口：校验单个事件，或重放某份面包的时间线。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .contracts import validate_event
from .service import ClockService

USAGE = (
    "用法: python -m frozen_bread_clock.cli <schema.json> <event.json>\n"
    "      python -m frozen_bread_clock.cli replay <schema.json> <events.jsonl> <portion_id> [--at <时间>]"
)


def _load_events(path: str) -> list[dict]:
    text = Path(path).read_text(encoding="utf-8")
    if text.lstrip().startswith("["):
        return json.loads(text)
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _validate(args: list[str]) -> int:
    schema = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    event = json.loads(Path(args[1]).read_text(encoding="utf-8"))
    issues = validate_event(event, schema)
    if not issues:
        print("valid")
        return 0
    for issue in issues:
        print(f"{issue.field}\t{issue.code}\t{issue.message}")
    return 1


def _replay(args: list[str]) -> int:
    if len(args) < 3:
        print(USAGE, file=sys.stderr)
        return 2
    schema = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    events = _load_events(args[1])
    portion_id = args[2]
    now = None
    if "--at" in args[3:]:
        index = args.index("--at")
        if index + 1 >= len(args):
            print("--at 需要一个携带时区的时间", file=sys.stderr)
            return 2
        now = args[index + 1]

    service = ClockService(schema)
    results = service.ingest(events)
    for result in results:
        if result.outcome == "rejected":
            detail = "；".join(f"{issue.field}:{issue.message}" for issue in result.issues)
            print(f"事件 {result.event_id} 被拒绝：{detail}", file=sys.stderr)
        elif result.outcome == "conflict":
            print(f"事件 {result.event_id} 存在冲突记录，相关份额已暂停建议", file=sys.stderr)

    report = service.replay(portion_id, now)
    print("时间线（按发生时间归并）：")
    for item in report["timeline"]:
        summary = f"\t{item['summary']}" if item["summary"] else ""
        print(f"{item['at']}\t{item['event_type']}\t{item['event_id']}{summary}")
    if report["conflict_event_ids"]:
        print("冲突记录（已暂停建议）：" + "、".join(report["conflict_event_ids"]))
    print("结论（每条均可追溯产品版本与事件）：")
    for conclusion in report["conclusions"]:
        product = (
            f"{conclusion['product_ref']}@v{conclusion['recipe_version']}"
            if conclusion["product_ref"]
            else "-"
        )
        events_ref = "、".join(conclusion["event_ids"]) or "-"
        rule = conclusion["rule"] or "-"
        at = conclusion["at"] or "-"
        print(f"- {conclusion['conclusion']}｜规则={rule}｜产品={product}｜事件={events_ref}｜时间={at}")
    print("最终状态：")
    print(json.dumps(report["status"], ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "replay":
        return _replay(args[1:])
    if len(args) != 2:
        print(USAGE, file=sys.stderr)
        return 2
    return _validate(args)


if __name__ == "__main__":
    raise SystemExit(main())
