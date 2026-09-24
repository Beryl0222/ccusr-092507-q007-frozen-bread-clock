# 冷冻面包食用时钟

表达冷冻面包产品版本、家庭分装与保存事件，提供可重放的期限计算输入契约。

## 目录

- `contracts/domain.schema.json`：事件信封、对象类型和事件载荷约定。
- `data/sample.json`：可直接校验的中文联调样例。
- `src/frozen_bread_clock/`：契约校验与命令行入口。
- `tests/`：基础字段、时间版本和事件载荷边界测试。
- `docs/domain.md`：领域对象与事件语义。

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
