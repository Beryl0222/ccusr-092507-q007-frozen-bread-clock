# 领域约定

表达冷冻面包产品版本、家庭分装与保存事件，提供可重放的期限计算输入契约。

聚合对象包括`product_version`、`household_portion`、`storage_timeline`、`safety_notice`。事件类型包括`PRODUCT_REGISTERED`、`PORTION_CREATED`、`STORAGE_RECORDED`、`THAW_STARTED`、`NOTICE_RECEIVED`。所有时间都必须携带时区，版本号从 1 开始递增，校验层不会替调用方改写输入。

## 事件载荷

- `PORTION_CREATED`：还需包含 `parent_ref`, `quantity`。
- `STORAGE_RECORDED`：还需包含 `method`, `occurred_at`。
- `NOTICE_RECEIVED`：还需包含 `lot_no`, `notice_version`。

同一事件标识的幂等与冲突处理属于上层业务服务职责；交换层只负责稳定报告结构、枚举、时间、版本和必需载荷问题。
