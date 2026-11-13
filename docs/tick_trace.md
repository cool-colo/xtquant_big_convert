# Tick 全链路时间戳

QMT 回调的每个原始 tick 字典新增 `_trace`，保留现有 `{code: tick}` 结构和行情字段。
时间戳均为 UNIX 纳秒，同一帧中的证券共享各环节的接收/发布时间。
桥接侧通过共享时钟兼容 QMT Python 3.6.8：优先使用 `time.time_ns()`，
不可用时使用 `int(time.time() * 1_000_000_000)`。后者保持相同时间基准和单位，
实际精度受浮点时钟及系统时钟分辨率限制，并非纳秒级测量精度。

| 字段 | 位置 |
| --- | --- |
| `qmt_callback_ns` | QMT 原生回调入口，归一化及加锁之前 |
| `pipe_publish_ns` | pipe 发布入口，编码及写锁等待之前 |
| `proxy_pipe_received_ns` | proxy 完整读取 pipe 帧之后，解码之前 |
| `redis_publish_ns` | proxy 工作线程进入 Redis 发布之后，编码之前 |
| `adapter_redis_received_ns` | Redis 订阅线程取得消息之后，解码之前 |
| `adapter_callback_ns` | Nautilus 回调入口，提交事件循环之前；仅诊断开启时采集 |

这些字段随对应帧一起替换、排队和转发。初始化快照和旧版消息不补造上游时间；
执行事件不打行情点。发布至接收的差值包含编码、锁等待、传输与接收线程调度，
并非网络或系统调用的纯耗时。直接 Redis 路径的 pipe 字段自然缺失。

## Nautilus → qapp

在 Nautilus 的 `BigQMTDataClientConfig` 中设置 `tick_trace_enabled=True`。
默认 `False`：不构建或发布诊断事件、不维护诊断缓存；原始 tick 仍携带桥接时间戳，
取时、复制及更大的消息体仍有少量开销。

开启后，adapter 在整批标准行情提交之后，通过本地消息总线发布 `BigQMTTickTrace`：

- Topic：`bigqmt.tick_trace.{instrument_id}`，支持 `bigqmt.tick_trace.*` 订阅。
- `instrument_id` 为字符串，`data_kind` 为 `quote` 或 `depth10`；同时订阅两者会得到两条事件。
- `ts_event`、`ts_init` 直接取自对应行情对象，保留原始事件时间和现有初始化时间。
- 上表六个字段均为 `int | None`；缺失或异常时间为 `None`，不转成零。
- 事件为不可变 dataclass，可通过 `dataclasses.asdict(event)` 转为日志字段。
- 不改变 QuoteTick/Depth10 类型、不落文件、不额外写 Redis；诊断异常不阻断行情处理。

qapp 的 BigQMT 启动入口已接入该开关和日志 Actor：在现有命令后加
`--bigqmt-tick-trace`，或设置 `BIGQMT_TICK_TRACE_ENABLED=1`；
`--no-bigqmt-tick-trace` 可以覆盖环境变量并关闭。默认关闭。
启用后自动订阅并通过后台线程输出 `live_tick_trace {JSON}` 到现有 INFO 日志，
无需手动添加策略回调。队列容量为 8192，满时丢弃诊断日志并累计计数。
推送链路需已启用（qapp 的 `BIGQMT_USE_QUOTE_PUSH=1`），诊断开关不切换行情来源。
使用细节见 qapp 的 `monitoring/tick_trace.md`。

需要自定义日志处理时，可参考以下异步队列示例（`tick_trace_queue` 由消费者创建）：

```python
from queue import Full

def on_start(self):
    self.msgbus.subscribe(topic="bigqmt.tick_trace.*", handler=self.on_tick_trace)

def on_tick_trace(self, event):
    try:
        self.tick_trace_queue.put_nowait((event, self.clock.timestamp_ns()))
    except Full:
        pass  # qapp 可计数；诊断过载不阻塞行情线程

def on_stop(self):
    self.msgbus.unsubscribe(topic="bigqmt.tick_trace.*", handler=self.on_tick_trace)
```

消息总线回调同步执行。回调中直接写日志、做统计或等待队列会增加后续行情处理耗时。
建议 qapp 只入队，由自己的日志线程落日志；订阅处理入口时间可记为 `qapp_received_ns`。
诊断事件在标准行情之后发布，这个时间表示诊断消费者收到事件，不等于策略行情回调时间。

## 分析

按 `instrument_id`、`data_kind` 分组，用同一组完整样本计算：

1. `qmt_callback_ns - ts_event`：交易所时间至 QMT 回调，包含上游传递及时间口径差异。
2. `pipe_publish_ns - qmt_callback_ns`：QMT 回调处理、归一化及订阅锁等待。
3. `proxy_pipe_received_ns - pipe_publish_ns`：pipe 编码、写锁/写入等待、传输及读取调度。
4. `redis_publish_ns - proxy_pipe_received_ns`：pipe 解码及 proxy 排队/调度。
5. `adapter_redis_received_ns - redis_publish_ns`：Redis 编码、发布、传输及订阅线程调度。
6. `adapter_callback_ns - adapter_redis_received_ns`：解码及订阅会话回调路由。
7. `ts_init - adapter_callback_ns`：adapter 回调处理及事件循环等待。

报告各段均值、P95，以及 `ts_init - ts_event` 最慢 5% 样本的分段构成。
各段 P95 不可直接相加。缺失字段的样本只参与可计算的分段统计，并报告样本数；
跨机时钟需要同步，负差值保留并标记异常，不能裁剪为零。

## 本地开销测量

在两仓库 checkout 上，使用能加载 Nautilus 编译扩展的 Python：

```bash
/path/to/nautilus_trader/.venv/bin/python tools/bench_tick_trace.py \
  --nautilus-repo /path/to/nautilus_trader --baseline-ref HEAD --samples 1000
```

`--baseline-ref` 应指定打点实现之前的版本。对每批 1/100 个证券，比较原版本、
仅时间戳、时间戳加事件三种状态的本地均值/P95、处理吞吐及消息字节数。
使用真实行情解析器和消息总线、空行情/诊断处理器；不包含 Windows pipe、Redis I/O、
事件循环等待或 qapp 日志成本，不能代替实盘延迟测量。
