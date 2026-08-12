# 大 QMT RPC 桥接架构：核心问答

本文档针对大 QMT 策略框架下 RPC 桥接的几个核心设计问题，逐一给出**总结性答案**，再展开相关解释与代码依据。

---

## 目录

1. [策略无法创建独立线程，RPC 服务如何运行？](#q1)
2. [Redis 通知机制：是主动拉取还是被动回调？](#q2)
3. [既然后台线程能正常工作，为什么 Redis 默认不用后台线程？](#q3)
4. [把 `run_time` 间隔从 500ms 改成 5ms 能降低延迟吗？](#q4)
5. [RPC 如何响应客户端？同步/异步 QMT API 有何不同？QMT 回调如何送达客户端？](#q5)
6. ["进程被冻结 ~490ms" 的冻结间隔是多少？](#q6)

---

<a name="q1"></a>
## 1. 策略无法创建独立线程，RPC 服务如何运行？

### 总结

**借用 QMT 的回调线程。** 策略通过 `ContextInfo.run_time("adjust", "500nMilliSecond")` **注册一个定时器**，让 QMT 每隔约 500ms 在它自己的策略线程上回调 `adjust()`。RPC 服务就在这个 `adjust` 回调里排空 Redis 队列、同步执行 QMT API——整个 RPC "服务器" 完全跑在 QMT 施舍给你的回调窗口里，**不创建任何线程**。

### 详细解释

QMT 策略框架只给你三个入口，都是 QMT 在它自己的线程上回调你：

- `init(ContextInfo)` —— 启动时调用一次
- `handlebar(ContextInfo)` —— 每根 Bar 调用
- `adjust(ContextInfo)` —— 你通过 `run_time(...)` 注册的定时器

你永远不拥有线程，只能"注册时间 / 等待 Bar / 等待事件回调"。本项目正是为绕开这个约束而生。它有两种运行方式：

**① Redis 默认 —— 借 QMT 线程（完全不建线程）**

`rpc_background_threads: False`（Redis 默认）时：

- `init()` 调用 `context_info.run_time("adjust", "500nMilliSecond", ...)`（`strategy.py:477`），**注册一个定时器**——正是你说的"注册时间"机制。
- 每约 500ms QMT 调用 `adjust(ContextInfo)`，进而 `_drain_rpc_service()` → `drain_request_queue()` + `drain_pending()`（`strategy.py:380-388`）。
- 排空动作从 Redis 拉出排队的 RPC 请求，**在 QMT 自己的策略线程上同步执行** QMT API。

所以 `adjust` 间隔本身就是 RPC 轮询循环。README 反复强调 `schedule_adjust=True` 必须开着——关掉它，RPC 泵就只能按 Bar 节奏跑。

**② ZMQ / MySQL —— 确实会建后台线程（`rpc_background_threads: True`）**

此时代码确实创建自己的 `threading.Thread`（transport 的 ROUTER/轮询循环）。这可行是因为 QMT 策略跑在普通 CPython 进程里——`threading` 并没被禁，只是 QMT 从不主动给你线程、也不在这个线程上回调你。

但这会重新引入单线程模型本要保护你规避的危险：**部分 QMT API 只有在主策略线程调用时才返回有效数据。** 这就是 `LISTENER_DEFERRED_METHODS`（`redis_rpc.py:101-126`）存在的原因：

- **行情读取**（`get_ticks` / `get_market_data`…）线程安全 → 直接在后台线程 inline 执行，快（~0.7ms）。
- **交易上下文查询**（`get_asset` / `get_positions` / `query_orders`…）走 QMT 的 `get_trade_detail_data`，**在非主线程调用会返回空**。所以即便有后台线程，这些也被推入 `self.pending` 延迟处理，仍要等下一次 `adjust` 在 QMT 线程上跑（代价是最多一个 ~500ms 间隔，但能拿到真实数据）。

---

<a name="q2"></a>
## 2. Redis 通知机制：是主动拉取还是被动回调？

### 总结

**分两个方向，行为相反：**

- **客户端 → QMT（请求投递）**：在 Redis 默认模式下，**纯主动拉取，无被动回调**。客户端 `RPUSH` 把请求丢进队列，QMT 里没有任何订阅、没有阻塞等待；靠 `adjust` 定时器触发的 `lpop`（非阻塞拉取）循环把请求扫走。
- **QMT → 客户端（响应/事件回传）**：这一侧是**被动/响应式的，但响应方是客户端，不是 QMT**。QMT 用 `PUBLISH`/`RPUSH` 把结果推出去，客户端阻塞在 `BLPOP` 上被瞬间唤醒。

### 详细解释

不要把两个方向混为一谈：

**A. 客户端 → QMT（请求进入策略）**

这正是"没有独立线程"约束发作的地方。Redis 默认（`background_threads: False`）下：

- 客户端 `RPUSH bigqmt:rpc:queue:{account}`（`_call_redis_rpc`, `redis_transport.py:370`）。
- QMT 内部**没有任何订阅、没有阻塞**，没有监听线程。
- `adjust` 回调按 QMT 定时器（~500ms）触发，调 `drain_request_queue()` → **`self.listen_redis.lpop(...)`** 循环（`redis_transport.py:308`）。`lpop` 是**非阻塞拉取**——有就取、空就立即返回。

所以请求拾取 100% 是轮询，节奏由 `run_time` 注册的 `adjust` 定时器决定。客户端的 `RPUSH` 并不"通知" QMT，只是把请求留在 list 里等下次排空扫走。这是不拥有线程的直接后果。

同一文件内有个重要的**不对称**：**后台线程**循环（`_queue_loop`, `redis_transport.py:205`）用的是 `brpop`（阻塞）——线程*可以*阻塞在 Redis 上被响应式唤醒。但那条路径只在 `background_threads=True`（ZMQ/MySQL）时才启动。纯 Redis 默认模式下这些循环永不启动，只剩 `lpop` 轮询。

**B. QMT → 客户端（响应/事件回传）**

这里方向相反，确实是响应式的——但**响应方是客户端**：

- 排空跑完请求后，`send_response`（`redis_transport.py:233`）做三件写：`SETEX` key、`RPUSH` reply list、**`PUBLISH` reply channel**（`redis_transport.py:251`）。
- 等待的客户端阻塞在 `BLPOP response_list`（`redis_transport.py:373`）——真正的阻塞等待，QMT 一 `RPUSH` 就被唤醒。客户端能这么做，是因为它是普通外部程序，*拥有*自己的线程。

所以你想的"发布通知"是 QMT 向一个真正阻塞/响应式的客户端向外推送。QMT 发布几乎不耗线程——`PUBLISH` 只是排空里同步执行的一条 Redis 命令。

| 方向 | Redis 默认模式的机制 | 响应式还是轮询？ |
|---|---|---|
| 客户端 → QMT（请求） | `RPUSH` 后靠 `adjust` 定时器 `lpop` 排空 | **轮询**——QMT 无线程可阻塞，只能定时扫 |
| QMT → 客户端（响应） | QMT inline `PUBLISH`/`RPUSH`；客户端 `BLPOP` 阻塞 | **响应式**——但响应方是*客户端*，它拥有线程 |

**结论**：无线程的 Redis 默认配置下，**QMT 从不响应 Redis 回调**——它做不到，因为 Redis 订阅需要线程来跑 `get_message`。它只在借来的 `adjust` 线程上用 `lpop` 主动拉取。唯一存在阻塞/响应式 Redis 等待的地方，是 (1) 为 ZMQ/MySQL 显式创建的后台线程内，或 (2) 外部客户端上（普通进程可自由阻塞）。

---

<a name="q3"></a>
## 3. 既然后台线程能正常工作，为什么 Redis 默认不用后台线程？

### 总结

**这是一个"可靠性优先于延迟"的刻意选择。** 在 QMT 进程内，Redis 后台线程不仅带不来明显好处，反而更糟，原因有三：① 收益极小——GIL 被 QMT 的 C++ 主循环把持，后台线程即便在 Redis 层被唤醒，也得排队等 GIL；② 后台线程让线程不安全的 QMT 交易查询 API *无法工作*，而不只是变慢——你仍然需要 `adjust` 排空；③ ZMQ 是*被迫*用后台线程（它的 ROUTER 只在后台线程收包），Redis 有选择权，于是选了更简单、可跨机、无 GIL 饿死风险的路径。

### 详细解释

**① 收益极小——GIL 被 QMT 的 C++ 主循环把持**

这是决定性原因。`gil_probe` 显示进程被**周期性冻结 ~490ms**——冻结来自 *QMT 自己的 C++ 主循环持有 GIL*，`setswitchinterval` 都抢不动它。

后台线程只有在请求到达时能*运行*才有用。但你的 Python 后台线程只有持有 GIL 才能执行，而 QMT 的 C++ 终端会一把抓住 GIL ~490ms。所以阻塞在 `brpop` 的 Redis 后台线程会在 Redis 层被响应式唤醒——然后**立即卡在等 GIL 上**。这正是 ZMQ 那句"30% 请求撞上 500ms GIL 尖峰"。后台线程躲不掉尾延迟，只是把等待的位置挪了地方。

`adjust` 排空相反，跑在*QMT 已经调度好的线程*上——它运行时已持有 GIL 和 CPU，无需争抢。

**② 后台线程让线程不安全的 QMT API *不可能*工作，而非只是变慢**

延迟档方法（`get_asset` / `query_orders`…）走 `get_trade_detail_data`，在非主线程返回**空**。`adjust` 模型天然有个主线程排空点来跑它们。若 Redis 走纯后台线程且没有 `adjust` 排空，这些方法就无处合法执行——你无论如何都还得要 `adjust` 排空。所以后台线程不能替代排空，只是在你仍需要的机制上又叠了一层。

**③ ZMQ 被迫用后台线程，Redis 不是，于是走更简单的路**

注意意图上的不对称。提交 `b1fdd2e`（"强制 ZMQ `background_threads=True`，否则服务起不来"）和文档说明 ZMQ 的 ROUTER socket **只在后台线程里收包**——它没法只靠 `adjust` 排空。ZMQ *必须*建线程才能工作。

Redis 有 ZMQ 没有的选择：从 `adjust` 回调里 `lpop` 队列完全没问题。有得选时，项目挑了这个模型，因为它：

- **部件更少**（无线程生命周期、无跨线程 socket 关闭、无 QMT reload-不退出时的守护线程清理）；
- **支持跨机**（Redis 走网络；低延迟线程方案只是同机优化）；
- **QMT 端零额外依赖、无 GIL 饿死风险**（文档警告热循环会"占满 GIL 饿死接收线程"——饿死它本要服务的那个线程）。

有意思的是，文档甚至推荐 **ZMQ 也用 `background_threads=False`** 跑低延迟实盘，正是*为了避免*后台线程被 GIL 调度拖累。这说明维护者的深思结论是：在这个进程里 `adjust` 排空模型总体上*更可取*，后台线程是被 ZMQ 的 socket 语义*逼出来的*必要，而非乐于采用的性能提升。

**尾延迟的干净修复**不是"加线程"，而是把 serving 移出 QMT 进程、进独立 GIL 的 sidecar——这正是 `shm_transport.py` 桩预留的方向。

---

<a name="q4"></a>
## 4. 把 `run_time` 间隔从 500ms 改成 5ms 能降低延迟吗？

### 总结

**不能，且可能更糟。** 有三堵硬墙：① QMT 会给间隔设下限——你根本拿不到 5ms，低于约 100ms 时 QMT 会把 `adjust` 变成"尽快跑"的热循环（~2150 次/秒），**烧掉整整一个 CPU 核**；② 尾延迟的根源不是间隔，而是 QMT 持有 GIL，缩小间隔只能让排空在 GIL 释放后*更早*落一次，但打不穿 ~90ms 的地板；③ 你最可能关心的持仓/委托查询，延迟恒定 ~1s，与间隔完全无关（瓶颈是柜台查询本身）。

### 详细解释

`run_time("adjust", interval)` 设定 QMT 多久借一次线程给你排空。这个间隔只在"请求排队等被拾取"这段时间上加延迟，最多一个间隔。天真地想小间隔 = 低等待，但两堵硬墙挡住了。

**墙 1：QMT 给间隔设下限——你拿不到 5ms**

实测表（`BIG_QMT_REDIS_RPC.md:363-367`）：

| 请求的 interval | QMT 实际行为 | inline 尾延迟 (p90/max) | CPU |
|---|---|---|---|
| `500nMilliSecond` | ~2.4 次排空/秒 | ~490 / 510ms | 极低 |
| `200nMilliSecond` | 折中 | ~200ms 量级 | 中 |
| `100nMilliSecond` | ~2150 次/秒——QMT 翻成**"尽快跑"热循环** | ~92 / 108ms | **烧≈1 核** |

注意 `100ms` 处：QMT 不再把它当周期，而是把 `adjust` 变成忙等（~2150/秒，不是 ~10/秒）。你请求 `5nMilliSecond` 拿不到 5ms 节奏——拿到的是同一个被顶满的热循环，只是配置在骗你说它想干嘛。低于约 100ms 就已经是自旋循环，再往下改什么都不变，只是徒增意图。

**墙 2：尾延迟不是间隔，是 QMT 持有 GIL**

这才是真正原因。`gil_probe` 显示进程被 **QMT 自己的 C++ 主循环持有 GIL** 冻结 ~490ms（`:355-357`），且 `setswitchinterval` 抢不动。你的排空在那些窗口里跑不了，无论调度多频繁，因为运行需要 GIL 而 QMT 拿着。

这正是缩小间隔只带来*递减、受 GIL 封顶*的回报：500ms→~490ms 尾，100ms→~92ms 尾。你没消除尾延迟，只是安排更多排空尝试，好让某一次在 QMT 释放 GIL 后*更早*落地。5ms 打不过 ~90ms，因为 ~90ms 大致是 GIL 释放的粒度，不是调度产物。

**墙 3：你最可能关心的（持仓/委托）完全不受间隔影响**

延迟档交易查询（`get_positions` / `query_orders` / `query_trades`…）实测 **~1012ms p50，与间隔无关**（`:369-371`）。瓶颈是 `get_trade_detail_data` 自身的柜台查询开销，不是排空节奏。调间隔对它们毫无作用。

**具体建议**

- **5ms 不可达**——你会拿到跟请求 100ms 一样的 ~100ms 热循环，烧满一核，在 GIL 竞争的进程里还会*饿死其他线程*。
- **现实最佳收益**：inline 行情读取从 ~490ms 尾 → ~90ms 尾。真实，但代价是一个核，且这是地板不是零。
- **持仓/委托/成交查询毫无收益**（~1s，柜台绑定）。

**应该怎么做（按价值排序）**

1. **用 `200nMilliSecond`，别更低**——文档标注为"推荐平衡点"：尾延迟明显低于 500ms，又不顶满核。
2. **持仓/资金别实时查**——读 `position_sync` 已在写的**客户端 Redis 缓存**（`:370-371`）。把 ~1s 柜台查询变成亚毫秒缓存读。这才是真正的延迟收益，且与间隔正交。
3. **行情微延迟走 FormulaServer 直连**（README 里 0.07ms 路径）——绕开 QMT python 线程和 GIL，是真正逃出 ~90ms 地板的唯一办法。
4. **尾延迟的真正修复**是带独立 GIL 的 sidecar 进程（预留的 `shm_transport.py`），而非调间隔。

---

<a name="q5"></a>
## 5. RPC 如何响应客户端？同步/异步 QMT API 有何不同？QMT 回调如何送达客户端？

### 总结

**三套机制，各司其职：**

1. **同步 RPC 应答**：不走任何回调。`adjust` 排空跑完 QMT API 后，在同一线程上*直接* inline 把结果三路写回 Redis（`SETEX` key + `RPUSH` list + `PUBLISH` channel），客户端阻塞在 `BLPOP` 上被唤醒。按 `request_id` 关联。
2. **同步 vs 异步 API 的关键差异**：同步查询（`get_*`）*返回数据*，RPC 应答就带真实结果；异步 API（`passorder`/`cancel`）*不返回有用值*——它 fire-and-forget，`submit` 立即返回 `order_sys_id=None` 的"已提交"确认，真实成交结果稍后经机制 3 异步送达。
3. **QMT 自身回调（成交回报）→ 客户端**：QMT 在自己的线程上调 `order_callback`/`deal_callback`，策略把 `m_*` 字段归一化后 `xadd`+`PUBLISH` 到*账户级*频道，客户端订阅后按 `user_order_id` 匹配回原来的下单。

### 详细解释

**1. 同步 RPC 应答（请求/响应往返）**

应答**不**经任何回调——它在跑 QMT API 的同一个 `adjust` 线程上 inline 写回 Redis。`process_request`（`redis_rpc.py:1099`）里：

- `handlers.handle(method, params)` 同步跑 QMT API
- 返回值 JSON 化成 `response` dict
- `_publish_response` → `transport.send_response`（`redis_transport.py:233`）做**三路扇出**：`SETEX reply_key`、`RPUSH reply_list`、`PUBLISH reply_channel`

同时客户端阻塞在 `BLPOP reply_list`（`redis_transport.py:373`）。`RPUSH` 瞬间唤醒它；`SETEX` key 是 `BLPOP` 超时后 `GET` 的兜底。关联是按请求的：`reply_list`/`reply_key` 以 `request_id` 为键，客户端只读自己那条。纯请求/响应——回复频道名是客户端建请求时自己定的。

**2. 同步 vs 异步 QMT API——关键不对称**

**同步 API**（所有 `get_*` 查询——`get_ticks` / `get_market_data` / `get_trade_detail_data` / `get_asset`…）：它们*返回数据*。所以步骤 1 的 RPC 应答就带着真实结果。客户端一次往返即完成且有意义。

**异步 API**（`passorder` / `cancel`）：它们**不返回有用值**。看 `submit`（`order_bigqmt.py:93-111`）：调完 `passorder(...)` 立即返回 `OrderSubmitResult(status="SUBMITTED", order_sys_id=None, ...)`。QMT 的 `passorder` 是发射即忘——它*不*返回交易所委托号、成交价或状态，只接受意图。

所以对一笔委托，同步 RPC 应答只确认**"委托已提交"**，且故意带 `order_sys_id=None`，因为 `passorder` 返回时那个号还不存在。真实结果（接受/部成/全成/废单、真实 `order_sys_id`、成交价）*稍后带外*到达——这正是机制 3 的用途。`user_order_id`（你的 `remark`）是客户端埋下的关联键，好让稍后的异步事件匹配回这次下单。

**3. QMT 自身回调（成交）→ 客户端——独立推送频道**

QMT 在委托状态变化或成交时，*在它自己的线程上*调 `order_callback(ContextInfo, orderInfo)` 和 `deal_callback(ContextInfo, dealInfo)`。策略把它们接线（`strategy.py:705-710`）到：

- `normalize_order_event` / `normalize_trade_event` —— 把 QMT 的 `m_*` ThinkTrader 字段拍平成普通 dict（`exec_events.py:300, 331`）
- `publish_order_event` / `publish_trade_event` → `_publish`（`exec_events.py:363`），做 **`xadd`（封顶流，maxlen 2000）+ `PUBLISH`** 到*另一个、账户级*频道：`bigqmt:order_events:{account_id}` / `bigqmt:trade_events:{account_id}`

它与 RPC 应答的区别：

| | RPC 应答（机制 1） | 执行事件（机制 3） |
|---|---|---|
| 频道 | 按请求（`request_id`） | 按账户（`account_id`） |
| 触发 | `adjust` 排空跑完一个请求 | QMT 调 `order/deal_callback` |
| Redis 操作 | `SETEX`+`RPUSH`+`PUBLISH` | `xadd`+`PUBLISH` |
| 客户端侧 | `BLPOP`（阻塞、一次性） | 订阅频道 / 读流（持续） |
| 关联键 | `request_id` | `user_order_id`（即 `remark`） |

客户端订阅这两个账户频道，实时收到 MiniQMT 风格的 `on_stock_order` / `on_stock_trade` 回调，并按 `user_order_id` 把每个事件匹配回之前的 `submit_order`。`xadd` 流是封顶回放缓冲，让重连的客户端补回 `PUBLISH`（发射即忘）时错过的事件。

一个线程细节值得注意：`order_callback`/`deal_callback` 在 QMT 回调线程上触发，那里的 `_publish` 只是同步 Redis `xadd`+`publish`——便宜、不阻塞，所以在回调里 inline 做是安全的。（`_extract_direction` 的方向仲裁复杂度，是因为实盘 QMT 回调即使卖出也返回 `m_nDirection=48`——见 `exec_events.py:37-42`——所以要交叉核对 `m_nOffsetFlag`/`m_nOpType`。）

**串起来——一笔委托的生命周期**

1. 客户端 `RPUSH` 一个 `submit_order` 请求 → 阻塞在 `BLPOP reply_list`。
2. `adjust` 排空拾取，调异步 `passorder(...)`，什么也没拿回，写应答 `{status: SUBMITTED, order_sys_id: null}` → 客户端 `BLPOP` 唤醒。**往返完成，但委托尚未成交。**
3. 稍后 QMT 成交，在自己线程上调 `deal_callback` → 归一化 → `xadd`+`PUBLISH` 到 `bigqmt:trade_events:{account}`。
4. 客户端订阅了该频道，收到成交，按 `user_order_id` 匹配回步骤 1。

所以：**同步查询**在一次 RPC 往返里完成（机制 1）；**异步委托**经机制 1 拿到即时"已提交"确认、经独立事件推送频道拿到真实结果（机制 3）。两者分别按 `request_id` 和 `user_order_id` 关联。

---

<a name="q6"></a>
## 6. "进程被冻结 ~490ms" 的冻结间隔是多少？

### 总结

**~490ms 是冻结的*时长*，不是间隔；冻结*间隔*不是常量，也没被直接打印出来。** 它取决于当前处于哪种调度状态：在 `500nMilliSecond` 默认下，冻结是*稀疏、不规则*的（表现为 p90/max 尾延迟，间隔在秒级，进程大部分时间自由）；随着 `schedule_adjust_interval` 缩向热循环，间隔坍缩趋近于零、进程接近持续被 GIL 饿死。要拿到你机器上的真实数字，读一条 `[gil_probe]` 窗口日志，用 `(10000ms − total) / count` 算出冻结之间的空闲间隔。

### 详细解释

**首先——探针到底测什么，不测什么**

`_gil_probe_loop`（`strategy.py:513`）在紧循环里 sleep 5ms，记录每次醒来比 5ms 多花多久。`time.sleep()` 释放 GIL，所以醒晚了 X ms 意味着进程 X ms 内重拿不到 GIL——它被冻结了。探针每 10s 窗口报告：**>阈值的 stall 次数、max、p50、total 冻结总时长**。

关键：探针报告冻结的*时长和频次*——**它不直接报告冻结之间的间隔**。所以"冻结间隔"不是一个被打印的数，得从 `count` 和 `total` 推导。而这个推导完全取决于你处于哪种调度状态。

**间隔不固定——由"谁持有 GIL"决定，而这随 `schedule_adjust_interval` 变化**

冻结不是硬件时钟或固定的 QMT 心跳，而是"别人持有了 GIL ~490ms"。*谁*、*多久一次*取决于 adjust 节奏——所以冻结间隔是配置的函数，不是常量：

**状态 A —— `500nMilliSecond`（默认）。** `adjust` 约 2.4 次/秒，即约每 ~410ms 排空一次。此处主导的 GIL 持有者是 QMT 自己的 C++ 终端主循环，尾延迟 ~490ms。这个状态下冻结是*稀疏且周期性*的：进程大部分时间自由运行，然后一个 ~490ms 的 C++ 侧 stall 间歇性落下。从 bench 数字看（~490ms p90/max，但中位 inline 延迟亚毫秒），冻结是**尾事件，不是稳态**——多数 5ms sleep 按时返回，少数撞上 ~490ms 持有。所以冻结间隔*长而不规则*——大 stall 之间是秒级，这就是它们表现为 p90/max 尾而非 p50 的原因。

**状态 B —— `100nMilliSecond` 或更低。** QMT 不再把间隔当周期，而把 `adjust` 翻成**热循环（~2150–2500 次/秒）**（`redis_rpc_runtime.py:258-259`）。现在顶满 GIL 的是*你自己的 adjust 排空*，基本是持续的。此时不再有干净的"冻结间隔"——冻结占空比逼近 100%，这正是该配置"烧≈1 核"、会"饿死 zmq ROUTER 线程"的原因。stall 之间的空隙坍缩趋近于零。

**如何从探针行实际推导间隔**

既然没被直接打印，就从一条真实 `[gil_probe]` 窗口日志算。假设代码打印：

```
[gil_probe] over 10s: 18 stalls>50ms max=510ms p50=490ms total=8200ms
```

由此：
- **冻结时长**：~490ms 典型（p50），510ms 最坏（max）——就是文档引用的 "~490ms"。
- **冻结频次**：10s 内 18 次 stall。
- **冻结间隔（推导）**：10s 窗口含 ~8.2s *冻结*时间和 ~1.8s *运行*时间，分布在 18 次 stall 上。所以进程每次冻结之间自由运行 ≈1800ms/18 ≈ **~100ms**。占空比 ≈ 82% 冻结。

这个推导就是"冻结间隔是多少"的真正答案：**从探针行读 `total` 和 `count`，用 `(窗口 − total) / count` 算空闲间隔、用 `total / count` 算冻结时长。** 上面的数字是文档所述形态（~490ms 冻结、GIL 主导）的示意；实际值在你机器上实时打印，会随 `schedule_adjust_interval` 和 QMT 终端 C++ 循环的繁忙程度变化。

**为什么没有单一的"那个间隔"答案**

三个独立的 GIL 持有者交织，各有节奏：

1. **QMT 的 C++ 终端主循环** —— 文档记载的 ~490ms 持有；周期性但非干净时钟，且 `sys.setswitchinterval` 抢不动（`strategy.py:554`）。
2. **你的 `adjust` 排空** —— 节奏 = `schedule_adjust_interval`（或低于约 100ms 时的热循环）。
3. **后台传输线程**（ZMQ/MySQL）若启用。

冻结*时长*稳定（~490ms，是 C++ 循环的工作量子）。冻结*间隔*是这三者竞争的涌现结果，所以以经验分布（每 10s 的 count + total）而非固定周期报告——且你一改 adjust 间隔，它就剧烈变化。

**结论**：~490ms 是冻结*长度*；冻结*间隔*不是常量、也没直接打印——500ms 默认下它稀疏/不规则（冻结表现为 p90/max 尾、相隔数秒、进程大体自由），随着 adjust 间隔缩向热循环，空隙趋近零、进程近乎持续 GIL 饿死。要拿你机器的真实数，读一条 `[gil_probe]` 窗口，算 `(10000ms − total) / count` 得冻结之间的空闲间隔。
