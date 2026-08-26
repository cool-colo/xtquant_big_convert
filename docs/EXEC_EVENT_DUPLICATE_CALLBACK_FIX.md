# Big QMT 执行回调重复触发修复记录

日期：2026-08-12

## 现象

实盘下单后，客户端收到的委托回报和成交回报各触发两次，例如：

```text
委托回报: 159518 50 635042239
委托回报: 159518 50 635042239
成交回报: 159518 635042239 100 1.204
成交回报: 159518 635042239 100 1.204
```

同一笔委托的同一状态、同一笔成交被重复推送到客户端 callback。

## 原因

服务端策略脚本同时暴露了两套执行回调入口：

- `on_order` / `on_trade`
- `order_callback` / `deal_callback`

其中 `order_callback()` 内部又调用 `on_order()`，`deal_callback()` 内部又调用 `on_trade()`。

如果 Big QMT 运行时同时识别并触发这两套入口，同一个原始委托/成交事件会进入服务端两次。每次都会执行：

1. 归一化 QMT 回调对象；
2. 发布 Redis 执行事件；
3. 转发给本地 app runner。

客户端订阅执行事件频道后，就会看到同一条委托回报/成交回报各触发两次。

## 修复方案

只保留 Big QMT 标准回调入口：

- `order_callback(ContextInfo, orderInfo)`
- `deal_callback(ContextInfo, dealInfo)`

删除服务端策略入口中的别名回调：

- `on_order`
- `on_trade`

`order_callback` 和 `deal_callback` 现在直接完成原来别名函数里的工作：

- `_publish_exec_event("order", orderInfo)`
- `_publish_exec_event("trade", dealInfo)`
- `forward_order_event(...)`
- `forward_trade_event(...)`

这样 Big QMT 运行时只会看到一套执行回调入口，不需要依赖客户端或服务端去重。

## 修改范围

- `src/bigqmt_signal_trader_strategy.py`
  - 删除 `on_order` / `on_trade`
  - `order_callback` / `deal_callback` 直接发布和转发事件

- `src/bigqmt_signal_trader_redis_rpc_runtime.py`
  - 不再导入或导出 `on_order` / `on_trade`

- `src/bigqmt_signal_trader_dryrun.py`
  - 不再导入 `on_order` / `on_trade`

- `src/bigqmt_signal_trader_redis_dryrun.py`
  - 不再导入 `on_order` / `on_trade`

- `tests/bigqmt_signal_trader/test_runner.py`
  - 删除别名回调测试
  - 新增断言：策略模块不暴露 `on_order` / `on_trade`，只暴露 `order_callback` / `deal_callback`

- `src/bigqmt_signal_trader/README.md`
  - 更新策略入口说明

- `docs/BIG_QMT_SIGNAL_TRADER_RUNBOOK.md`
  - 更新 QMT 策略导入示例

## 验证

相关测试：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\bigqmt_signal_trader\test_runner.py tests\bigqmt_signal_trader\test_exec_events.py -q
```

结果：

```text
38 passed
```

全量测试：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

结果：

```text
275 passed, 4 skipped
```

## 注意

这次修复只处理“同一执行事件回调两次”的问题。

`order_stock()` 同步返回 `-1`，但随后又收到真实委托/成交回报，是另一类问题：同步下单路径没有及时拿到 `order_sys_id`，而异步执行事件稍后能拿到真实系统委托号。该问题不在本次修复范围内。
