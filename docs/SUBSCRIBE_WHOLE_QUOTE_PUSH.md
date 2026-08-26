# subscribe_whole_quote 真推送方案（对齐 miniqmt）

> 状态：**待评审**（方案已成型，未动实现。实现严格按 TDD：先红→绿→回归）
> 分支：`feat/impl_subscribe_whole_quote`

## 1. 背景与现状诊断

### 1.1 miniqmt 语义（目标行为）

`xtdata.subscribe_whole_quote(code_list, callback)` 在 miniqmt 里是**真订阅**：

- 注册后，行情服务**持续推送**，每个行情周期触发一次 `callback(data)`，`data` 是 `{code: tick_dict}` 的全推快照。
- 返回一个 `seq`（订阅句柄）；`unsubscribe_quote(seq)` 后推送停止。
- 全市场用板块代码：`["SH"]`、`["SZ"]`、`["SH","SZ"]`（也支持 `"BJ"`、`"HK"`）。

### 1.2 当前实现（client 端假订阅，server 端空转）

- **client**（`xtquant_compat.py:781`）：`subscribe_whole_quote` 只是 `publish_event("subscribe_whole_quote", ...)` 到 redis stream（`bigqmt:quote_events:{account_id}`），然后**同步调一次 `get_full_tick` 触发一次 callback** 就返回 `seq`。之后**再无任何推送**。`callback` 不被持有，纯一次性。
- **server**：**全仓库没有任何代码消费 `quote_events` stream**，也没有任何代码调用大 QMT 的 `ContextInfo.subscribe_whole_quote` / `xtdata.subscribe_whole_quote`。事件发出去石沉大海。
- `unsubscribe_quote(seq)` 同样只发事件 + 删 redis 订阅记录，无实际效果。

**结论：miniqmt 的「注册 → 持续推送 → 回调」语义当前完全没有实现。**

### 1.3 transport 现状

- `RpcTransport`（`transports/base.py`）是**纯请求/响应**模型：client `send_request`、server `start_receiving`/`send_response`。**没有 server→client 的主动推送通道**。
- zmq transport 用 ROUTER/DEALER，请求响应式；redis transport 的 pubsub 只用于 RPC 请求/响应通道，不用于行情推送。

### 1.4 可复用的资产

- `full_tick_cache`：server 周期 `ContextInfo.get_full_tick` 拉快照写 redis，client 读缓存。**是轮询拉取，不是推送**，但有现成的 demand/TTL 机制（本方案不采用它做数据面，仅作对比参考）。
- `BigQmtRpcHandlers`（`redis_rpc.py:333`）：server 端白名单 dispatch，`market_data` 适配器持有 `ContextInfo` —— server 端订阅管理器复用此路径访问大 QMT 行情接口。
- 参考实现：`quant-qmt-proxy` 的 `SubscriptionManager` 已验证 `xtdata.subscribe_whole_quote(["SH","SZ"], callback)` 推送模式 + 心跳超时（默认 60s）在本环境可行。

---

## 2. 已确认的决策（来自讨论）

| # | 决策点 | 结论 |
|---|--------|------|
| Q1 | 数据通路 | **方案 A：真推送**。server 端大 QMT 订阅回调 → 推送通道 → client callback。新增 server→client 推送通道。 |
| Q2 | 去重粒度 | **按组合去重**：`frozenset(code_list)` 规范化后作为订阅单元 key。不同 client 传相同集合 → 共享同一个大 QMT 订阅。 |
| Q3 | 引用计数 & keepalive | client 分配 `client_id`，周期发 keepalive（带 `client_id`+组合）；server 维护 `{组合: {client_id: last_seen}}`，**超时阈值 = 10 个心跳周期**未收到才认为该 client 消亡；组合所有 client 消亡后才真正退订大 QMT。 |
| Q4 | server 重启恢复 | **client 重放**：client 记忆自己的订阅集合，检测到断连/server 重启后自动重放订阅；server 无状态、靠 client 重放/keepalive 重建订阅。**server 订阅表落盘不做**（实现阶段决定：client 重放已覆盖恢复路径，落盘引入 QMT 环境文件 IO 复杂度，无额外收益）。 |
| Q5 | 大 QMT 行情源 | **ContextInfo 优先**：server 用策略进程内 `ContextInfo.subscribe_whole_quote`。"建/退订阅"收敛为可替换适配层，按真实环境实测微调（见 §7 风险 1）。 |
| Q6 | 推送通道落地 | **zmq + redis 同阶段交付**：同一 `QuotePushChannel` 抽象下两个实现，按部署 transport 选择。 |
| Q7 | 推送编码 | **高效编码：msgpack 优先，json 兜底**。msgpack 作为可选依赖（`optional-dependencies`），全市场推送建议安装；未装时退化为 json。 |

---

## 3. 总体架构

```
┌─────────────┐   subscribe_whole_quote   ┌──────────────────────────────────┐
│   client A  │ ────────────────────────► │            server (大 QMT 进程)    │
│  (xtdata)   │   RPC: subscribe_whole     │  QuoteSubscriptionManager         │
└─────────────┘   _quote {client_id,       │   ├─ 组合去重 frozenset            │
                  │      codes, sub_id}     │   ├─ refcount {combo: {cid: ts}} │
┌─────────────┐                             │   └─ ContextInfo/xtdata.          │
│   client B  │ ────────────────────────►  │      subscribe_whole_quote(      │
└─────────────┘   同组合 → 共享同一订阅      │         codes, on_push)          │
                  │                          └──────────┬───────────────────────┘
   keepalive RPC  │  周期心跳 {client_id, sub_id}        │ 大 QMT 行情回调 on_push(data)
        ──────────┼──────────────────────────►          │
                  │                                     ▼
                  │                          ┌──────────────────────────┐
   行情推送        │ ◄──────────────────────  │  QuotePusher (PUB socket)│
   (PUB/SUB)      │   topic=combo, {data}    │  按组合 topic 广播         │
                  │                          └──────────────────────────┘
```

**三条逻辑通道**（对应三个职责）：

1. **控制面 RPC（已有 transport 复用）**：`subscribe_whole_quote` / `unsubscribe_whole_quote` / `quote_keepalive` 三个新 RPC 方法，走现有请求/响应 transport（redis 或 zmq）。
2. **数据面推送（新增）**：server→client 单向 PUB/SUB 通道，承载行情推送。
3. **大 QMT 行情源**：server 端 `ContextInfo.subscribe_whole_quote`（或 `xtdata.subscribe_whole_quote`）回调。

---

## 4. 详细设计

### 4.1 订阅单元 key（Q2：组合去重）

```python
def combo_key(code_list):
    """规范化组合 → 唯一 key。顺序无关、大小写统一、去空白。"""
    return ",".join(sorted({str(c).strip().upper() for c in (code_list or []) if str(c).strip()}))
```

- `["SH","SZ"]` 与 `["sz","SH"]` → 同一 key `"SH,SZ"`，共享同一个大 QMT 订阅。
- 全市场 `["SH"]`、标的组合 `["000001.SZ","600000.SH"]` 都是合法 key。

### 4.2 client 端（`BigQmtXtData.subscribe_whole_quote` 重写）

- 入参 `code_list, callback` 不变（对齐 miniqmt 签名）。
- 生成 `client_id`（进程级唯一，复用 zmq DEALER identity 或 `uuid4`，**进程生命周期内稳定**，持久化到本地文件以便重启后识别同一 client）。
- 为本次订阅分配 `sub_id`（沿用现有 `_next_seq()`）。
- 记录到 client 侧订阅表 `{sub_id: {codes, callback, combo_key}}`（**用于重放恢复**）。
- 发 RPC `subscribe_whole_quote {client_id, sub_id, codes}` → server 返回 `{combo_key, push_endpoint, push_topic}`。
- 启动/复用一个 **SUB 接收线程**，订阅 `push_topic`，每收到一帧 → 解析 → 调用所有匹配该 topic 的本地 callback。
- 启动/复用一个 **keepalive 线程**，每 `heartbeat_interval` 秒对所有活跃 `sub_id` 发 `quote_keepalive {client_id, sub_id}`。
- **初始全量打底**：大 QMT 全推回调是**增量**的（见 §4.3 调研结论），不保证订阅后立即给全量。client 在订阅成功后**先主动调一次 `get_full_tick(code_list)` 触发 callback 打底**，随后由增量推送驱动 callback。这既保留现有"订阅即给一帧"的行为，又对齐大 QMT 的增量语义。
- 返回 `sub_id`。

`unsubscribe_quote(sub_id)`：发 RPC `unsubscribe_whole_quote {client_id, sub_id}`，从本地表删除；该 sub_id 停止 keepalive。返回 0（对齐 miniqmt）。

### 4.3 server 端 `QuoteSubscriptionManager`（新模块）

挂在 server 进程内，持有 **ContextInfo**（Q5：优先用策略进程内 `ContextInfo.subscribe_whole_quote`；复用 `market_data` 适配器的 ContextInfo 引用）与 `QuotePusher`。

**大 QMT 订阅适配层**（Q5：ContextInfo 优先）。已调研确认大 QMT 真实签名（见下方"调研结论"），适配层对外只暴露两个方法：

```python
class QuoteSourceAdapter:           # ContextInfo 优先实现
    def subscribe(self, codes, on_push) -> handle: ...
    def unsubscribe(self, handle) -> None: ...
```

**调研结论（真实大 QMT 环境，官方文档 + 隔壁 quant-qmt-proxy 实测交叉验证）**：

| 项 | 结论 |
|---|---|
| 订阅签名 | `ContextInfo.subscribe_whole_quote(code_list, callback=None)` |
| `code_list` | 市场代码 `['SH','SZ']` 或品种代码 `['600000.SH','000001.SZ']` |
| 返回值 | `int` 订阅号 `subId`；**`< 0` 表示失败**（quant-qmt-proxy 实测） |
| 退订 | `ContextInfo.unsubscribe_quote(subId)`（与 `subscribe_quote` 共用同一退订方法） |
| **回调数据** | **增量推送**：每次回调只含**有变化**品种的最新 tick；`get_full_tick` 才是全量快照 |
| 回调线程 | 独立于 `handlebar` 的推送线程（官方建议回调内不阻塞、扔队列处理） |

`QuoteSubscriptionManager` 只跟 `QuoteSourceAdapter` 打交道，不直接碰 ContextInfo——`subscribe` 内部调 `ContextInfo.subscribe_whole_quote(codes, on_push)`、`unsubscribe` 内部调 `ContextInfo.unsubscribe_quote(handle)`；若实测仍有出入，只改这一层。

**核心状态（内存，权威）**：

```python
{
  combo_key: {
    "codes": [...],              # 原始 code_list
    "qmt_sub_handle": <大QMT订阅句柄>,   # subscribe_whole_quote 返回值
    "clients": {client_id: last_seen_ts},  # 引用计数 + 心跳
    "topic": push_topic,
  }
}
```

另维护 `sub_id → (client_id, combo_key)` 反向索引，用于按 sub_id 退订。

**三个 RPC handler**（加入 `BigQmtRpcHandlers.allowed_methods`，走现有 dispatch）：

- `subscribe_whole_quote {client_id, sub_id, codes}`：
  - `key = combo_key(codes)`。
  - 若该 combo 不存在：调 `ContextInfo.subscribe_whole_quote(codes, on_push)` 建订阅，记录 handle，`on_push` 闭包绑定 `key`；建 `topic`。
  - `clients[client_id] = now`；登记 `sub_id → (client_id, key)`。
  - 返回 `{combo_key, topic, push_endpoint}`。
- `unsubscribe_whole_quote {client_id, sub_id}`：
  - 由 `sub_id` 找到 `(client_id, key)`，`clients.pop(client_id)`，删除 `sub_id` 索引。
  - 若该 combo 的 `clients` 为空 → `ContextInfo.unsubscribe_whole_quote(handle)`（或对应退订 API），销毁 combo。
  - 返回 `{}`。
- `quote_keepalive {client_id, sub_id}`：
  - 由 `sub_id` 找 combo，`clients[client_id] = now`。返回 `{}`。

**reaper（后台周期任务，挂在 server 的 adjust/调度循环上）**：

- 每 `reap_interval` 秒扫描：对每 combo，删除 `now - last_seen > 10 * heartbeat_interval` 的 client。
- combo 的 `clients` 清空后 → 退订大 QMT、销毁 combo。

**on_push 回调**（大 QMT 行情线程触发）：

- 收到 `data`（`{code: tick}`）→ 调 `QuotePusher.publish(topic, data)`。
- **注意线程安全**：大 QMT 回调在行情线程，zmq PUB socket 的 send 需串行化（入队给专属发送线程，或加锁）。参照 zmq transport 已有的"socket 只能由创建它的线程关闭/使用"约束，推送也走队列 + 专属线程。

### 4.4 推送通道（新增 `QuotePushChannel`，zmq + redis 同阶段交付）

同一抽象下**两个实现同阶段交付**（Q6），按 client 当前 transport 选择：

```python
class QuotePushChannel:            # 抽象
    def publish(self, topic, data) -> None: ...        # server 端
    def subscribe(self, topic, on_msg) -> None: ...    # client 端
```

**zmq PUB/SUB 实现**（无 redis 部署的原生通道）：

- server 端绑定一个 `PUB` socket（独立于现有 ROUTER，单独端口，地址随 RPC discovery 下发或在 subscribe 响应里返回 `push_endpoint`）。
- client 端 `SUB` socket connect，`setsockopt(SUBSCRIBE, topic)` 按组合过滤。
- 帧格式：`[topic_bytes][payload_bytes]`。
- topic = `combo_key`（即 `"SH,SZ"`），SUB 端精确匹配前缀即可。

**redis pub/sub 实现**（redis 部署）：

- 复用现有 redis 连接，channel 名 `bigqmt:quote_push:{account_id}:{combo_key}`。
- server `publish`、client `subscribe` 同一 channel。

**编码（Q7：msgpack 优先，json 兜底）**：

- 推送 payload 用 **msgpack** 序列化（对 `{code: {field: number}}` 这类结构，比 json 快数倍、体积更小，是全市场推送的标准选择）。
- msgpack 列为**可选依赖**（`pyproject.toml` 的 `optional-dependencies`，与 redis/mysql 同组织方式），避免给最小安装（仅 pyzmq）增加硬依赖。
- 未安装 msgpack 时**退化为 json**（stdlib），保证功能可用、仅吞吐降级。编码选择封装在 `QuotePushChannel` 内部，对上层透明。

> 取舍说明：zmq PUB/SUB 是 **fire-and-forget**，client 掉线期间推送被丢弃（符合行情推送语义——增量丢了就等下一帧，无需逐条补发）。**但增量推送不自带全量**，client 重连/重放后必须由 §4.2 的"初始全量打底"（`get_full_tick`）重建本地状态，再接收增量。这与 miniqmt 行为一致。

### 4.5 keepalive 与超时（Q3）

- `heartbeat_interval`（client 发心跳周期），**默认 3s**。
- 超时阈值 = `10 * heartbeat_interval = 30s`（Q3 已确认 10 个周期）。server 端某 client 超过 30s 无心跳 → 判定消亡，从所有 combo 的 `clients` 移除。
- 心跳与数据面解耦：即便行情静默（盘后用快照），心跳照发，保证引用计数准确。

### 4.6 server 重启恢复（Q4）

- **client 重放**：client 的 SUB 接收线程检测到推送通道断开/server ping 失败 → 触发重连；重连成功后，对本地订阅表里**所有活跃 sub_id 重新发 `subscribe_whole_quote`**（幂等：server 按 `(client_id, combo)` 去重，重放不会重复建大 QMT 订阅）。
- **server 落盘（不做）**：实现阶段决定 server 订阅表**不落盘**。client 重放已覆盖恢复路径（重启后 client 重放即可重建全部订阅），落盘只增加 QMT 环境文件 IO 与状态一致性复杂度，无额外收益。
- **幂等性**：`subscribe_whole_quote` handler 对相同 `(client_id, sub_id, combo)` 重复调用安全（重建 last_seen，不重复建大 QMT 订阅）。

---

## 5. 配置项

server 端（`config["quote_push"]`，见 `bigqmt_signal_trader_strategy.py`）：

| 配置键 | 默认 | 说明 |
|------|------|------|
| `enabled` | `true` | 是否启用全推推送服务 |
| `heartbeat_timeout_seconds` | `30.0` | client 无心跳超时阈值（= 10 个心跳周期 × 3s） |
| `zmq_bind_address` | RPC zmq 端口 + 1 | zmq PUB 绑定地址（仅 transport=zmq 时用） |

> 推送通道跟随 RPC transport（zmq/redis），reaper 挂在 RPC drain 上（随 drain 周期执行，无独立 interval）。server 订阅表不落盘（见 §4.6）。

client 端（`bigqmt_signal_trader_client_config.py` / 环境变量）：

| 配置 | 默认 | 说明 |
|------|------|------|
| `BIGQMT_QUOTE_CLIENT_ID` | 自动生成并持久化到 `~/.cache/bigqmt/quote_client_id` | client 唯一 id（重启稳定，用于重放识别） |
| `BIGQMT_QUOTE_HEARTBEAT_SECONDS` | `3.0` | 心跳周期（环境变量），须 < server 超时 |

---

## 6. TDD 实施计划（先红→绿→回归）

按依赖顺序分 6 个增量，每个增量都是「先写失败测试 → 实现 → 回归全套」。

**阶段 1 — 组合 key + 引用计数核心（纯逻辑，无 IO）**
- 红：`tests/bigqmt_signal_trader/test_quote_subscription_manager.py`
  - `combo_key` 顺序无关/大小写统一。
  - 两个 client 订阅同组合 → 只建一次大 QMT 订阅（mock ContextInfo），refcount=2。
  - 一个 client 退 → 不退大 QMT；全部退 → 退一次大 QMT。
  - 心跳超时：构造 `last_seen` 过期 → reaper 移除 client；combo 空 → 退订。
- 绿：`src/bigqmt_signal_trader/quote_subscription_manager.py`（`combo_key` + `QuoteSubscriptionManager`，ContextInfo 用注入的 mock）。
- 回归：全套测试。

**阶段 2 — server RPC handler 接线**
- 红：`subscribe_whole_quote` / `unsubscribe_whole_quote` / `quote_keepalive` 三个方法经 `BigQmtRpcHandlers.handle` 可达、白名单放行、参数校验、幂等重放。
- 绿：在 `redis_rpc.py` 加 handler 方法 + `allowed_methods`，注入 `QuoteSubscriptionManager`。
- 回归。

**阶段 3 — 推送通道抽象 + zmq/redis 双实现 + 编码**
- 红：`tests/bigqmt_signal_trader/test_quote_push_channel.py`
  - `QuotePushChannel` 接口（`publish(topic, data)` / `subscribe(topic, on_msg)`）。
  - zmq PUB/SUB 回环：bind PUB → SUB connect → publish → SUB 收到且 topic 过滤正确。
  - redis pub/sub 回环：fake redis 下 publish → subscribe 收到。
  - 编码：msgpack 可用时 payload 用 msgpack（解出结构与原始一致），未装时退化 json。
- 绿：`src/bigqmt_signal_trader/quote_push_channel.py`（抽象 + zmq 实现 + redis 实现 + msgpack/json 编码选择）。
- 回归。

**阶段 4 — server on_push → 推送通道接线**
- 红：mock ContextInfo 触发 `on_push(data)` → `QuotePushChannel.publish` 被以正确 topic+data 调用；线程安全（并发回调不竞态）。
- 绿：`QuoteSubscriptionManager` 接 `QuotePushChannel`。
- 回归。

**阶段 5 — client 端重写（订阅 + SUB 接收 + keepalive 线程）**
- 红：`tests/bigqmt_signal_trader/test_whole_quote_client.py`
  - `subscribe_whole_quote` 发正确 RPC、注册本地 callback、启动 keepalive。
  - 收到推送帧 → 触发对应 callback。
  - `unsubscribe_quote` 发 RPC、停心跳。
  - 重放：模拟断连后 → 对所有活跃 sub_id 重发 subscribe。
- 绿：重写 `BigQmtXtData.subscribe_whole_quote` / `unsubscribe_quote`，新增 client 侧 SUB 接收与 keepalive 线程、`client_id` 管理。
- 回归。

**阶段 6 — 端到端 + 多 client 共享 + server 重启恢复**
- 红：端到端测试（in-proc fake server + 两 client）
  - 两 client 订同组合 → 各自 callback 都收到推送；server 只对大 QMT 建一次订阅。
  - 一 client 退 → 另一个仍收；全退 → server 退订大 QMT。
  - server "重启"（重建 manager + 推送通道）→ client 重放 → 恢复推送。
  - client 静默超 30s → server 清引用 → 退订。
- 绿：补齐集成胶水（server 启动时装配 manager+push channel，client 重连逻辑）。
- 回归：全套 + 现有 `test_all_apis.py` 端到端不破坏。

---

## 7. 风险与开放问题

1. **大 QMT 订阅/退订 API（已调研确认，风险解除）**：签名、返回值（`int` 订阅号，`<0` 失败）、退订方法 `unsubscribe_quote(subId)` 均已确认（见 §4.3 调研结论）。`QuoteSourceAdapter` 仍保留，作为唯一接触 ContextInfo 的层，便于真实环境联调时微调。
2. **行情回调线程模型**：大 QMT `on_push` 在独立的推送线程触发（非 `handlebar` 线程），zmq send 必须跨线程安全（队列 + 专属发送线程）；官方亦建议回调内不阻塞、扔队列。阶段 4 专门覆盖。
3. **增量推送语义**：大 QMT 全推回调是**增量**（只推变化品种），不是全量快照。client 端必须用 `get_full_tick` 打底 + 增量更新（§4.2），不能假设订阅后即得全量。这点与早期假设不同，已在 §4.2/§4.4 修正。
4. **全市场推送量级**：`["SH","SZ"]` 全推增量仍可能每帧数千条。已按 Q7 采用 **msgpack** 编码（可选依赖，未装退化 json）压低开销；PUB 广播吞吐仍需在真实环境实测，若仍不足再评估压缩/分片（不过早优化）。
5. **msgpack 依赖**：新增可选依赖 `msgpack`（`optional-dependencies`，对齐 redis/mysql 的组织方式），最小安装（仅 pyzmq）不受影响；未装时推送通道退化 json 编码。
6. **与 full_tick_cache 关系**：二者独立。full_tick_cache 服务 `get_full_tick` 按需拉取；本方案服务 `subscribe_whole_quote` 推送。不冲突，不合并。

---

## 8. 交付物清单（已全部交付）

- 新增：`src/bigqmt_signal_trader/quote_subscription_manager.py`（`combo_key`、`QuoteSubscriptionManager`、`QuoteSourceAdapter`/`ContextInfoQuoteSource`、`build_quote_subscription_service`）
- 新增：`src/bigqmt_signal_trader/quote_push_channel.py`（抽象 + zmq/redis 实现 + msgpack/json 编码）
- 新增：`src/bigqmt_signal_trader/whole_quote_session.py`（client 端订阅会话：订阅表 + 推送路由 + 心跳线程 + 重放）
- 修改：`src/bigqmt_signal_trader/redis_rpc.py`（`QUOTE_SUBSCRIPTION_METHODS` + 3 个 handler + 白名单 + `quote_subscription_manager` 注入）
- 修改：`src/bigqmt_signal_trader/xtquant_compat.py`（`subscribe_whole_quote`/`unsubscribe_quote` 重写 + session 懒建 + client_id 持久化 + push channel 选择 + `get_full_tick` 打底）
- 修改：`src/bigqmt_signal_trader_strategy.py`（server 启动装配 `_build_quote_subscription_service` + publisher 启动 + reaper 挂 RPC drain）
- 修改：`pyproject.toml`（`optional-dependencies` 增加 `msgpack`）
- 新增测试：`test_quote_subscription_manager.py` / `test_quote_push_channel.py` / `test_quote_on_push_wiring.py` / `test_whole_quote_client.py` / `test_xtdata_whole_quote.py` / `test_quote_subscription_service.py` / `test_whole_quote_e2e.py`
- 文档：本文档 + `RPC_API_REFERENCE.md` §2.5 增补 3 个新方法 + `bigqmt_signal_trader_client_config.example.py` 增补 client 配置

**实现期间 TDD 抓到的两个真实 bug**：
1. **`_sub_index` 键冲突**：两 client 各自 `sub_id` 从 1 开始，server 以单 `sub_id` 为键互相覆盖 → 引用计数错乱、"全退才退订"失效。e2e 测试暴露后改为 `(client_id, sub_id)` 复合键。这是多 client 场景的核心正确性问题，单 client 测试无法覆盖。
2. **`load_client_config` 漏 `quote_client_id` 键**：导致 client_id 配置读不到、静默退回持久化文件路径。

**未实现**：server 订阅表落盘兜底（§4.6，经决策不做，靠 client 重放恢复）。

**待真实环境联调**（不阻塞交付，均已在 §7 标注）：`ContextInfo.subscribe_whole_quote` 实际句柄/退订微调、`["SH","SZ"]` 全推吞吐实测、zmq/redis 推送通道在真实部署的连通性。
