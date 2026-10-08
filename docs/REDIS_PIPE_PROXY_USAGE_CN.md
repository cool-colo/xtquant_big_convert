# Redis—命名管道代理使用指南

本文说明如何部署 `bigqmt-redis-pipe-proxy`，让远程 Linux/NautilusTrader
客户端继续通过 Redis 使用 BigQMT，同时保证 QMT 内嵌 Python 进程只访问本机
Windows 命名管道，不创建 Redis、ZMQ 或原生 `xtdata` 网络连接。

详细设计和安全语义见 [REDIS_PIPE_PROXY_DESIGN.md](REDIS_PIPE_PROXY_DESIGN.md)。

## 1. 适用场景与拓扑

适用于 QMT 环境禁止 socket、禁止安装第三方包，但允许 `ctypes` 调用 Windows
命名管道的场景。

```text
Linux / NautilusTrader
        |
        | Redis RPC 与执行事件
        v
Redis <- Windows 独立代理进程
             |              ^
             | RPC 管道     | 执行事件管道
             v              |
         QMT 内嵌策略进程
```

每个资金账号运行一个代理进程，对应两条本机管道：

- RPC：`\\.\pipe\bigqmt_rpc_<account_id>`
- 执行事件：`\\.\pipe\bigqmt_exec_<account_id>`

行情全推不经过代理。NautilusTrader 必须关闭 `use_quote_push`，通过 RPC 轮询
行情。订单、成交和错误事件则由 QMT 回调经事件管道实时转发；Nautilus 的
1 秒执行轮询仅用于对账和补漏，不是主事件通道。

## 2. 前置条件

- Windows 主机已安装并登录 QMT；
- Windows 主机能访问 Redis；
- Windows 的独立 Python 环境可安装本项目和 `redis` 可选依赖；
- QMT 内嵌 Python 不需要安装 `redis`、`pyzmq`；
- Linux 客户端与 Windows 代理使用相同的 Redis DB，默认是 DB 5；
- 账号 ID、RPC 管道名和事件管道名在 QMT 与代理两端完全一致。

建议先在模拟账号完成全部验收，再开放实盘下单方法。

## 3. 安装 Windows 代理

在 QMT 之外的普通 Windows Python 环境中，从项目目录安装：

```powershell
python -m pip install -e ".[redis]"
```

确认命令已经注册：

```powershell
bigqmt-redis-pipe-proxy --help
```

代理必须和 QMT 运行在同一台 Windows 主机上，建议使用同一个 Windows 用户，
避免命名管道 ACL 阻止连接。

## 4. 配置 QMT 端

在 QMT 使用的 `bigqmt_signal_trader_local_config.py` 中配置：

```python
BIGQMT_ACCOUNT_ID = "你的账号ID"
BIGQMT_ACCOUNT_TYPE = "STOCK"

BIGQMT_REDIS_CONFIG = {
    "transport": "pipe",
    "pipe": {
        "pipe_name": "bigqmt_rpc",
    },
    "rpc_background_threads": False,
    "schedule_adjust": True,
    "schedule_adjust_interval": "100nMilliSecond",

    # 只读验收通过前保持 False；模拟下单验证后再打开。
    "rpc_allow_order_methods": False,

    # QMT 内嵌进程禁止网络访问。
    "redis_enabled": False,
    "native_xtdata_enabled": False,
    "download_jobs_enabled": False,
    "full_tick_cache_enabled": False,
    "quote_push": {"enabled": False},

    # 原生订单/成交回调经独立管道发给代理。
    "exec_events_enabled": True,
    "exec_events_transport": "pipe",
    "exec_events_pipe_name": "bigqmt_exec",
    "exec_events_queue_capacity": 4096,
    "exec_events_connect_timeout_seconds": 0.25,
    "exec_events_ack_timeout_seconds": 2.0,
}
```

如果 QMT 沙箱禁止从磁盘加载项目包，生成单文件策略：

```text
python tools/build_pipe_single_file_flat.py
```

生成文件为 `src/BIGQMT_DRYRUN_PIPE_FLAT_ALL_IN_ONE.py`。它已内嵌事件管道
实现，并强制关闭 QMT 端 Redis、ZMQ 和行情全推。该文件使用 GBK 编码且被
Git 忽略，需要复制到 QMT 策略目录并设置正确账号。

若环境禁止写日志，先在 PowerShell 中设置用户环境变量，然后完全退出并重新
启动 QMT，使 QMT 进程继承该变量：

```powershell
setx BIGQMT_LOG_ENABLED 0
```

`BIGQMT_LOG_ENABLED` 是操作系统环境变量，不是
`bigqmt_signal_trader_local_config.py` 中的 Python 配置项。若只需对从当前
PowerShell 启动的 QMT 临时生效，可使用
`$env:BIGQMT_LOG_ENABLED = "0"` 后再从同一窗口启动 QMT。

## 5. 启动代理

不要把 Redis 密码直接写在命令行。PowerShell 示例：

```powershell
$env:BIGQMT_REDIS_PASSWORD = "你的Redis密码"

bigqmt-redis-pipe-proxy `
  --account-id "你的账号ID" `
  --redis-host "10.0.0.20" `
  --redis-port 6379 `
  --redis-db 5 `
  --redis-password-env BIGQMT_REDIS_PASSWORD `
  --pipe-name bigqmt_rpc `
  --event-pipe-name bigqmt_exec `
  --workers 8 `
  --max-pending 64
```

无认证的测试环境也可以使用不含凭据的 Redis URL：

```powershell
bigqmt-redis-pipe-proxy `
  --account-id "你的账号ID" `
  --redis-url "redis://10.0.0.20:6379/5"
```

不要在 `--redis-url` 中放入用户名或密码：命令行参数可能被进程查看工具或日志
记录。需要认证时使用前一示例中的 `--redis-password-env`；若同时需要 Redis
ACL 用户名，再加 `--redis-username`。

正常启动会显示类似：

```text
[bigqmt_proxy] ready account=123*** workers=8 max_pending=64
```

同一账号只能有一个代理。第二个进程会报
`another proxy owns account ...` 并退出。生产环境应让代理以前台进程运行，
交给 Windows 服务管理器或其他 supervisor 负责重启。

### 常用参数

| 参数 | 默认值 | 含义 |
|---|---:|---|
| `--workers` | 8 | 最大并发 RPC 管道往返数 |
| `--max-pending` | 64 | 已接收但未完成的请求上限 |
| `--pipe-connect-timeout` | 0.25 秒 | RPC 管道不存在时的连接等待 |
| `--safety-margin` | 0.10 秒 | 转发前保留的超时安全边界 |
| `--legacy-read-timeout` | 30 秒 | 无有效超时的旧读取和撤单请求上限 |
| `--event-max-bytes` | 1 MiB | 单个执行事件最大尺寸 |
| `--event-dedup-ttl` | 86400 秒 | 事件 ID 去重窗口 |
| `--shutdown-grace` | 10 秒 | 停机时等待已接收任务的时间 |

Nautilus 数据和执行客户端不共享缓存对象时，最多可能同时产生 8 个 RPC，
因此默认 8 个 worker。更多客户端或更高行情轮询扇出需要压测后提高
`workers`/`max_pending`。

## 6. 配置 NautilusTrader

Nautilus 仍然使用 Redis，不要配置成 `transport="pipe"`：

```python
BigQMTDataClientConfig(
    account_id=ACCOUNT_ID,
    redis_host=REDIS_HOST,
    redis_port=6379,
    redis_db=5,
    redis_password=REDIS_PASSWORD,
    transport="redis",
    rpc_timeout_secs=6.0,
    use_quote_push=False,
    poll_enabled=True,
    poll_interval_secs=MARKET_POLL_INTERVAL,
)

BigQMTExecClientConfig(
    account_id=ACCOUNT_ID,
    account_type="STOCK",
    redis_host=REDIS_HOST,
    redis_port=6379,
    redis_db=5,
    redis_password=REDIS_PASSWORD,
    transport="redis",
    rpc_timeout_secs=6.0,
    poll_enabled=True,
    poll_interval_secs=1.0,
)
```

`MARKET_POLL_INTERVAL` 必须根据订阅代码数量和实测 QMT 吞吐量确定。报价和
深度订阅会分别调用 `get_full_tick`，同一代码可能产生两次轮询。

验收期间设置 `BIGQMT_FORMULA_ENABLED=0`，避免 FormulaServer 快速路径绕过
Redis—代理—管道链路。

### Nautilus 撤单兼容问题

当前 Nautilus `BigQMTClient.cancel_order()` 对 `cancel_order_stock()` 返回值
直接调用 `bool()`，但兼容 API 使用 `0` 表示成功、`-1` 表示失败，结果会被
反转。实盘启用前必须在 Nautilus 适配器中改为“返回值等于 0 即成功”，不能
让代理篡改响应来掩盖这个问题。

## 7. 推荐启动与停止顺序

启动：

1. 启动 Redis；
2. 启动 Windows 代理，确认显示 `ready`；
3. 在 QMT 中启动命名管道策略；
4. 启动 NautilusTrader/Linux 客户端；
5. 先完成只读验收，再在模拟账号打开 `rpc_allow_order_methods=True`。

停止：

1. 停止 NautilusTrader/Linux 客户端；
2. 停止 QMT 策略；
3. 使用 `Ctrl+C` 或服务管理器正常停止代理；
4. 最后按需停止 Redis。

正常停止代理会先关闭入口，再等待已接收请求，随后释放账号锁。

## 8. 验收流程

### 8.1 只读验收

1. 保持 `rpc_allow_order_methods=False`；
2. 确认 `ping` 在 Nautilus 默认 6 秒超时内返回；
3. 验证 `probe_capabilities`、合约列表、合约详情、行情和账户查询；
4. 验证超过 1 MiB 的历史行情响应；
5. 分别重启 QMT、代理和 Redis，确认错误有界且恢复后可继续查询；
6. 代理停止期间压入的队列请求，重启后必须返回 `STALE_BACKLOG`，不能送入 QMT。

### 8.2 执行事件验收

1. 在模拟环境产生订单、成交、拒单和撤单错误事件；
2. 确认事件经过 `bigqmt_exec_<account_id>` 管道发布到以下 Redis 频道：
   - `bigqmt:order_events:<account_id>`
   - `bigqmt:trade_events:<account_id>`
   - `bigqmt:order_error_events:<account_id>`
   - `bigqmt:cancel_error_events:<account_id>`
3. 暂停 Nautilus 执行轮询时，回调事件仍应独立到达；
4. 恢复轮询后，同一订单状态和成交不能产生重复 Nautilus 事件；
5. 中断并恢复事件管道，确认事件使用同一 `event_id` 重试且 Redis 不重复发布。

### 8.3 模拟下单验收

1. 设置 `rpc_allow_order_methods=True`；
2. 测试下单、撤单、过载、超时、代理在管道写入后退出等情况；
3. 对不确定的下单结果调用 `get_request_outcome`；
4. 核对过期请求、启动前积压请求没有进入 QMT；
5. 部署 Nautilus 撤单返回值修复后，才允许进入实盘验收。

## 9. 重要安全语义

- 读取请求在管道超时后会收到 `PIPE_TIMEOUT` 等明确错误；
- 下单或撤单写入管道后，如果结果不确定，代理不会伪造失败响应；
- 此时 Redis 客户端会自然超时，下单方必须用原请求 ID 查询
  `get_request_outcome`，确认结果前禁止重复下单；
- 代理不会在不确定结果后自动重试交易 RPC；
- 执行事件可以用相同 `event_id` 重试，并在去重窗口内只发布一次；
- 去重窗口之外的超长中断必须人工复核，恢复实盘前重新对账。

## 10. 常见错误与排查

| 现象/错误 | 常见原因 | 处理 |
|---|---|---|
| `PIPE_UNAVAILABLE` | QMT 策略未启动，账号或管道名不一致 | 核对 QMT 账号、`pipe_name` 和策略状态 |
| `OVERLOADED` | 并发或轮询扇出超过代理容量 | 降低行情轮询量，或压测后提高 worker/pending |
| `STALE_BACKLOG` | 请求在代理启动前已留在 Redis 队列 | 不要重放；确认调用方收到明确拒绝 |
| `TIMEOUT_REQUIRED` | 下单请求没有正的 `timeout_seconds` | 升级客户端并保留超时字段 |
| `REQUEST_EXPIRED` | 请求在转发前已耗尽时限 | 检查负载、Redis 延迟和代理容量 |
| `PIPE_TIMEOUT`（读取） | QMT 未及时响应 | 检查 QMT adjust 调度和查询耗时 |
| 下单方仅看到 Redis 超时 | 管道写入后结果不确定 | 立即调用 `get_request_outcome`，禁止盲目重下 |
| 有对账轮询但无实时回调 | 事件管道未启用或名称不一致 | 检查 `exec_events_transport/pipe_name` 和代理日志 |
| `event queue full` | 代理/Redis 长时间不可用或事件峰值过大 | 停止实盘、恢复链路并完成账户对账 |
| `Redis Streams unavailable` | Redis 服务端低于 5.0 | 事件仍走 Pub/Sub，但没有事件流留存 |
| `another proxy owns account` | 同账号已有代理或旧锁尚未过期 | 查明原进程；不要同时启动两个代理 |

## 11. 当前限制

- 一个代理进程只服务一个账号；
- 当前多账号 QMT 管道服务不受支持；
- 行情全推未通过管道转发，必须轮询；
- 当前验证范围是现金 A 股 `STOCK` 账号；
- QMT 端下载任务、Redis 快照、身份持久化等功能保持关闭；
- Nautilus 配置目前没有 Redis username/TLS 字段；需要命名 ACL 用户或直连
  TLS 时，必须同步扩展 Nautilus 适配器；
- Windows/QMT 实机验收通过前，不应视为可用于实盘。

## 12. 生产运行建议

- Redis 仅开放给可信私网，配置认证、防火墙和必要的传输加密；
- 不在日志或命令行中记录 Redis 密码、完整账号、原始订单和完整响应；
- 监控代理退出码、账号锁、过载、管道超时、未知写入结果、事件重连和队列溢出；
- 代理异常退出应由 supervisor 重启，但 supervisor 不得重放交易请求；
- 每次升级先在只读和模拟环境执行第 8 节验收。
