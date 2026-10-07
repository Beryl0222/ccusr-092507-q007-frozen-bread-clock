# 冷冻面包食用时钟

以产品配方版本、过敏原、生产与收货事实、分装份数、保存方式、冷冻窗口、解冻开始、复热方法和召回通知，
为**每份**面包给出可重放、可追溯的食用期限状态。系统只依据**已登记的食品规则**给出期限与操作提示，
不替代感官异常判断或医疗建议。

## 领域语义（摘要）

- 事件信封见 `contracts/domain.schema.json`，所有时间必须带时区；信封时间是登记时间，
  业务发生时间以 `payload.occurred_at` 为准，离线补录按发生时间归并。
- **幂等与冲突**：同一 `event_id` 完全相同只保留一次；同 id 内容不同、或同一逻辑事实的温度方式/份数不同，
  该份额进入 `CONFLICT`，暂停建议。
- **规则按批次适用**：规则按 SKU + 生产时间窗登记，只采用覆盖本份生产时刻的最高版本；
  指南改版不追溯旧批次；冷藏期限与冷冻期限是独立规则键。
- **冷冻窗口**：超过常温/冷藏入冻窗口即 `RULE_VIOLATION`，不能再按冷冻期限计算。
- **解冻不可逆**：解冻份额不能倒回未开封库存；仅当规则允许时以 `REFREEZE_AFTER_THAW`
  建立新的风险代次（`REFROZEN_WITH_RULES`）和新期限。
- **召回**：RECALL 按 `lot_no` 命中订单，沿 `parent_ref` 拆分谱系标记全部份额；
  GUIDE_REVISION 不改变既有批次。
- **隐私**：事件载荷中的成员真实姓名/电话等在落库前剥离，只保留化名 `member_ref`。
- **可注入时钟**：查询时刻与时区由调用方注入，跨时区只改变展示；`now >= deadline` 即到期。
- **提醒账本**：提醒以（份额、里程碑、期限）哈希键落账，批量任务重启不重发。
- **可重放**：`replay` 返回逐段结论，每段附带采用的事实事件、`product_version`、`rules_version`。

## 目录

- `contracts/domain.schema.json`：事件信封、聚合、事件类型、载荷枚举与必填约定。
- `data/sample.json`：单事件校验样例；`data/sample_events.jsonl`：全流程事件流（规则两版、
  两个配方版本、两笔订单、分装、离线补录入冻、解冻、部分食用、改版通知、召回）。
- `src/frozen_bread_clock/`
  - `contracts.py` 交换层校验；`model.py` 规则与时间原语；`clock.py` 可注入时钟；
  - `engine.py` 纯函数时间线引擎；`store.py` SQLite 事件存储与提醒账本；
  - `privacy.py` 家庭成员脱敏；`service.py` 应用服务；`cli.py` 命令行。
- `docs/domain.md`：领域对象、事件与业务规则。
- `tests/`：契约、引擎、存储、提醒与隐私测试。

## 测试

```bash
python3 -m unittest discover -s tests
python3 -m compileall -q src tests
```

## 命令行

```bash
# 校验单个事件
PYTHONPATH=src python3 -m frozen_bread_clock.cli validate contracts/domain.schema.json data/sample.json

# 幂等导入事件流（可重复执行；重复报告 DUPLICATE，同 id 异内容报告 CONFLICT）
PYTHONPATH=src python3 -m frozen_bread_clock.cli import /tmp/fbc.db contracts/domain.schema.json data/sample_events.jsonl

# 注入时刻查询 / 客服时间线重放（--at 省略则用系统时钟，--zone 切换展示时区）
PYTHONPATH=src python3 -m frozen_bread_clock.cli status /tmp/fbc.db contracts/domain.schema.json ord-2401-b --at "2026-10-11T12:00:00+08:00"
PYTHONPATH=src python3 -m frozen_bread_clock.cli replay /tmp/fbc.db contracts/domain.schema.json ord-2401-b --at "2026-10-11T12:00:00+08:00" --zone Europe/Berlin

# 批量提醒：scan 只查看（含 already_sent 标记），send 落账发送；重启后不重发
PYTHONPATH=src python3 -m frozen_bread_clock.cli reminders /tmp/fbc.db contracts/domain.schema.json send

# 沿订单与拆分谱系定位召回份额
PYTHONPATH=src python3 -m frozen_bread_clock.cli recall /tmp/fbc.db contracts/domain.schema.json L240920

# 家庭成员真实信息独立存放（输出脱敏投影）
PYTHONPATH=src python3 -m frozen_bread_clock.cli member /tmp/fbc.db fam-a --name 张三 --phone 13800000000
```

## 作为库使用

```python
from frozen_bread_clock.clock import FixedClock
from frozen_bread_clock.service import FrozenBreadService
from frozen_bread_clock.store import EventStore
from frozen_bread_clock.model import parse_instant

service = FrozenBreadService(EventStore("/tmp/fbc.db"), schema, zone="Asia/Shanghai")
service.ingest(event)                      # 校验 + 幂等落库
service.status("ord-2401-b")               # 用系统时钟
service.status("ord-2401-b", at=FixedClock(parse_instant("2026-10-11T12:00:00+08:00")).now)
service.replay_timeline("ord-2401-b")      # 客服重放：逐段依据
service.send_due_reminders()               # 提醒落账，重启不重发
```

结论 JSON 含 `status`、`current_method`、`deadline`、`remaining`、`freeze_generation`、
`remaining_qty`、`allergen_text`、`reheat_hint`、`segments[].basis`、`timeline`、`conflicts`，
以及固定结尾的免责声明。
