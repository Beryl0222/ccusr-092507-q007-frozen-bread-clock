# 领域约定

表达冷冻面包产品版本、家庭分装与保存事件，提供可重放的期限计算输入契约，
并在业务服务层给出逐份状态、期限与操作提示。服务只根据已登记的食品规则
给出结论，不替代感官异常判断与医疗建议（所有状态输出均附带该免责声明）。

## 交换契约

聚合对象包括 `product_version`、`household_portion`、`storage_timeline`、`safety_notice`。
事件类型包括 `PRODUCT_REGISTERED`、`PORTION_CREATED`、`PACKAGE_OPENED`、
`STORAGE_RECORDED`、`THAW_STARTED`、`PORTION_CONSUMED`、`NOTICE_RECEIVED`。
所有时间都必须携带时区，版本号从 1 开始递增，校验层不会替调用方改写输入。

### 事件载荷

- `PRODUCT_REGISTERED`：还需包含 `product_id`, `recipe_version`, `lot_no`, `produced_at`, `rules`。
- `PORTION_CREATED`：还需包含 `parent_ref`, `quantity`；根份额的 `parent_ref` 是订单号，
  并需以 `product_ref` 指向产品版本聚合。
- `STORAGE_RECORDED`：还需包含 `method`（`ambient`/`fridge`/`freezer`）与 `occurred_at`，
  可附 `temperature_c`。
- `PORTION_CONSUMED`：还需包含 `quantity`，支持部分食用。
- `NOTICE_RECEIVED`：还需包含 `lot_no`, `notice_version`, `kind`
  （`guideline_revision`/`safety_recall`）。

## 事件归并与冲突

- 完全相同（标识与内容一致）的记录只保留一次，重复摄入返回 `duplicate`。
- 标识相同而温度、方式、份数等内容不同的记录视为冲突：冲突事件不参与计算，
  相关份额（含拆分后代；产品或通知冲突则含整批次）暂停建议，
  状态输出 `advice_suspended=true` 且等级为 `suspended`。
- 离线补录的事件按发生时间归并：`STORAGE_RECORDED` 以载荷中的 `occurred_at`
  为准，其余以信封 `occurred_at` 为准，与摄入顺序无关。

## 份额状态机

`sealed → opened → frozen → thawed → refrozen`，另有用尽（`finished`）、
召回（`recalled`）、暂停（`suspended`）等覆盖等级。

- 子份额继承父份额在拆分时刻之前的全部时间线，因此已解冻的份额
  不能倒回未开封库存，拆分也不会重置时钟。
- 离开冷冻环境（记录到非冷冻保存方式）视为开始解冻。
- 解冻后再次冷冻按登记规则建立新的风险状态：规则允许再冻时使用
  `refreeze_frozen_hours` 计算新期限；不允许时标记 `refreeze_not_allowed`，
  期限仍按解冻后保守计算。
- 剩余份数 = 登记份数 − 已食用 − 已分出；出现负值时按 0 处理并标记
  `quantity_inconsistent`。

## 期限规则

- 冷藏与冷冻期限相互独立：冷藏天数绝不折算为冷冻期限，
  冷冻期限自冷冻开始时刻单独计算；先冷藏后冷冻的份额会在解释中
  明确记录“冷藏时段不计入冷冻期限”。
- 冷冻须在 `freeze_window_hours` 内完成，超出标记 `freeze_window_exceeded`。
- 解冻后期限按解冻方式（`thawed_fridge_hours`/`thawed_ambient_hours`）计算。
- 临界时刻语义稳定：`now >= deadline` 判定为 `expired`，
  进入 `warn_hours` 窗口判定为 `use_soon`；全部计算在 UTC 上进行，
  跨时区旅行与多设备并发下，相同事件与相同瞬间必然得到相同解释。

## 指南修订与安全召回

- `guideline_revision` 通知携带修订规则与 `applies_to_lots`，
  只影响适用批次，取 `notice_version` 最高者。
- `safety_recall` 沿订单与拆分谱系定位同批次的所有份额，
  召回输出只含通知信息，不包含家庭成员数据。
- 事件中的 `member_ref` 在重放输出中一律脱敏为 `member:<摘要>`。

## 批量提醒

提醒键由份额、等级与期限（或召回版本）组成；结论不变则键不变。
已发送记录持久化在 `ReminderStore` 文件中，任务重启后不会重复发送；
期限因新事件变化时产生新键并再次提醒。暂停建议的份额不发送食用提醒。

## 时间线重放

`ClockService.replay`（或 CLI `replay` 子命令）按发生时间输出份额时间线，
每条结论都记录采用的产品版本（`product_ref`+`recipe_version`）、
规则来源事件、规则名与证据事件标识，而不是一个不可追溯的剩余天数。
