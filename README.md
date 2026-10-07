# 冷冻面包食用时钟

表达冷冻面包产品版本、家庭分装与保存事件，提供可重放的期限计算输入契约，
并在业务服务层给出逐份状态、期限与操作提示。服务只根据已登记的食品规则
给出结论，不替代感官异常判断与医疗建议。

## 目录

- `contracts/domain.schema.json`：事件信封、对象类型和事件载荷约定。
- `data/sample.json`：可直接校验的中文联调样例（单个事件）。
- `data/timeline.sample.jsonl`：完整时间线联调样例（登记→分装→冷冻→解冻→食用→通知）。
- `src/frozen_bread_clock/`：契约校验、领域服务、幂等提醒与命令行入口。
  - `contracts.py`：事件信封与必需载荷校验。
  - `service.py`：`ClockService`，幂等归并、冲突暂停、逐份状态机、
    批次作用域规则、召回谱系定位、成员脱敏与可追溯解释。
  - `reminders.py`：批量提醒，重启后不重复发送。
  - `cli.py`：单事件校验与时间线重放。
- `tests/`：契约边界、服务语义、提醒幂等与 CLI 测试。
- `docs/domain.md`：领域对象、事件语义与业务规则。

## 测试

```bash
python3 -m unittest discover -s tests
```

## 编译检查

```bash
python3 -m compileall -q src tests
```

## 样例校验

```bash
PYTHONPATH=src python3 -m frozen_bread_clock.cli contracts/domain.schema.json data/sample.json
```

命令成功时输出 `valid`；校验失败时逐行输出字段、代码和中文说明，并以非零状态结束。

## 时间线重放

```bash
PYTHONPATH=src python3 -m frozen_bread_clock.cli replay contracts/domain.schema.json data/timeline.sample.jsonl portion-order-1001-a --at 2026-09-28T12:00:00+08:00
```

按发生时间输出该份额的时间线，每条结论标注采用的产品版本、规则与事件标识，
最后给出当前状态（等级、期限、风险提示、操作提示与免责声明）。
