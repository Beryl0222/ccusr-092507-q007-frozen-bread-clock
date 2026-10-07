# 领域约定

表达冷冻面包产品配方版本、过敏原、已登记食品规则、订单与家庭分装谱系、保存/开封/解冻/食用事实与安全通知，
为每份面包提供**可重放、可追溯**的期限结论输入契约。

服务只根据已登记的食品规则给出期限与操作提示，不替代感官异常判断或医疗建议。

## 聚合与事件

聚合对象：`rules_catalog`、`product_version`、`household_portion`、`storage_timeline`、`safety_notice`。

所有时间必须携带时区；信封 `occurred_at` 是登记（上传）时间，业务发生时间一律以 `payload.occurred_at`
为准，离线补录的事件按业务发生时间归并。`version` 是事件在其聚合内从 1 开始连续递增的版本号，
校验层不会替调用方改写输入。

| 事件 | 聚合 | 关键载荷 |
| --- | --- | --- |
| `RULES_PUBLISHED` | rules_catalog | `product_sku`, `rules_version`, `rules`（含 `applicable_from`/`applicable_to` 生产时间适用窗） |
| `PRODUCT_REGISTERED` | product_version | `product_sku`, `version_no`, `allergen_text`, `applicable_lot_from` |
| `ORDER_PLACED` | household_portion | `order_no`, `product_sku`, `version_no`, `lot_no`, `produced_at`, `received_at`, `initial_method`, `quantity`, `unit`, `member_ref` |
| `PORTION_SPLIT` | household_portion | `parent_ref`, `quantity`, `unit` |
| `OPENING_RECORDED` | storage_timeline | `portion_id`, `occurred_at` |
| `STORAGE_RECORDED` | storage_timeline | `portion_id`, `method`, `occurred_at`，入冻时附 `via` |
| `THAW_STARTED` | storage_timeline | `portion_id`, `via`, `occurred_at` |
| `CONSUMPTION_RECORDED` | storage_timeline | `portion_id`, `quantity`, `occurred_at` |
| `NOTICE_RECEIVED` | safety_notice | `notice_no`, `kind`（GUIDE_REVISION/RECALL）, `notice_version`, `issued_at`, `summary`；RECALL 必须带 `lot_no` |

枚举：

- `method`：`AMBIENT` / `REFRIGERATED` / `FROZEN` / `DEEP_FROZEN` / `REFROZEN_WITH_RULES`
- `via`（入冻或解冻方式）：`FREEZE_FROM_AMBIENT` / `FREEZE_FROM_REFRIGERATED` / `REFREEZE_AFTER_THAW`
  / `THAW_IN_FRIDGE` / `THAW_AMBIENT` / `THAW_MICROWAVE`

## 上层业务规则（不属于交换层）

1. **幂等**：`event_id` 完全相同的记录只保留一次；同一逻辑键（聚合 + 业务发生时间 + 事实类型）
   内容完全相同也视为重复。
2. **冲突暂停**：同一事件标识内容不同，或同一逻辑事实的温度方式、份数不同，该份面包立即进入
   `CONFLICT`，暂停一切期限建议，直到冲突事实被纠正。
3. **规则选择按批次适用**：规则按 `product_sku` 与生产时间适用窗登记，引擎只采用覆盖本份生产时刻
   的最高 `rules_version`；产品方改版指南不会追溯改变旧批次结论，结论必须记录所用 `rules_version`
   与产品 `version_no`。
4. **冷冻窗口**：常温/冷藏起始的份额必须在规则允许的窗口内入冻，逾期入冻构成 `RULE_VIOLATION`，
   暂停建议；冷藏时限与冷冻时限是规则中相互独立的键，系统不得把冷藏天数当作冷冻期限。
5. **解冻不可逆**：已解冻份额不能倒回未开封库存或普通冷冻；只有规则允许 `REFREEZE_AFTER_THAW`
   时，重新冷冻才建立**新的风险代次**（`REFROZEN_WITH_RULES`，新期限、保留全部历史）。
6. **部分食用**：`CONSUMPTION_RECORDED` 扣减剩余份数，扣到 0 为 `CONSUMED` 终态；超扣为冲突。
7. **召回沿谱系定位**：RECALL 通知按 `lot_no` 命中订单，再沿 `parent_ref` 拆分谱系标记所有份额为
   `RECALLED`；GUIDE_REVISION 不改变既有批次的适用规则。
8. **家庭成员保护**：`member_ref` 为化名标识，`member_name`、`member_phone` 等敏感字段只存原始库，
   API/客服重放投影一律脱敏。
9. **可注入时钟**：查询时刻与时区由调用方注入；跨时区只改变展示，不改变绝对期限。临界时刻
   `now >= deadline` 即过期，结论对相同输入（事件集 + 时钟）必须完全确定。
10. **提醒不重发**：批量提醒以（份额、里程碑、期限）为幂等键落账，任务重启后已发键不重复发送。
11. **时间线可重放**：客服重放命令必须看到每个结论段所采用的事实事件、产品版本与规则版本，
    而不是一个不可追溯的剩余天数。
