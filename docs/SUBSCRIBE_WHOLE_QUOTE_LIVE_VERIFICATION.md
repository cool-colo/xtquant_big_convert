# subscribe_whole_quote 全推行情 — 真机联调验证报告

> 日期:2026-08-10(周一,交易日)
> 环境:本地客户端 + Windows QMT 服务端(内网联调)
> 版本:`feat/impl_subscribe_whole_quote` 分支(commit 7e0d67d 及之后修复)
> 文档:`docs/SUBSCRIBE_WHOLE_QUOTE_PUSH.md`(设计),本文档为真实环境验证结果

---

## 1. 环境与部署

### 1.1 拓扑

```
本地客户端 (venv, Python 3.10)
  └─ bigqmt_signal_trader (editable 安装, 指向仓库 src/)
      ├─ BigQmtRpcClient ── redis (内网, db5) ──► 服务端 RPC
      └─ RedisQuotePushChannel (pub/sub bigqmt:quote_push:{acct}:{topic})
                                ▲
                                │ redis pub/sub
Windows 服务端 (QMT 交易端, Python 3.6)
  └─ <QMT python 目录>/  (策略 BIGQMT_REDIS_DRYRUN)
      ├─ bigqmt_signal_trader_strategy.py ── 启动 QuoteSubscriptionManager
      ├─ quote_subscription_manager.py     ── 引用计数订阅管理
      └─ quote_push_channel.py             ── 推送通道(服务端, json 兜底编码)
```

### 1.2 部署动作

| 步骤 | 内容 | 结果 |
|---|---|---|
| 1 | 修复本地开发 venv(uv 重建 Python 3.10) | ✅ |
| 2 | `uv pip install -e /path/xtquant_big_convert[redis,msgpack]` 部署到 venv | ✅ |
| 3 | 全量替换服务端 40 个 bigqmt 文件为本地当前版本(逐文件 MD5 校验一致,保留 `local_config.py` 生产配置) | ✅ |
| 4 | 服务端 `full_tick_cache_enabled: True`(既有配置,无需改动) | ✅ |

> 关键教训:初期误判"服务端文件已是最新"(大小写哈希比对看串),实际除 3 个新文件外其余 19 个均为旧版,导致订阅 RPC 报 `method is not allowed`。全量替换 + 程序化 MD5 校验后解决。

---

## 2. 验证过程与结果

### 2.1 阶段一:基础链路(09:00-09:06)

| # | 验证项 | 结果 | 证据 |
|---|---|---|---|
| 1 | RPC 链路存活 | ✅ | `get_full_tick` 2.1s 返回盘前快照 |
| 2 | `subscribe_whole_quote` RPC 允许 | ✅ | 282ms 返回 seq(修复前报 `method is not allowed`,因服务端旧版 `redis_rpc.py` 缺 `READ_METHODS |= QUOTE_SUBSCRIPTION_METHODS`) |
| 3 | 初始快照 prime | ✅ | 订阅后立即回调完整快照(lastPrice 11.19 昨收) |
| 4 | redis 推送通道 | ✅ | 模拟发布 → 客户端实时收到 |
| 5 | 竞价真实推送(09:15:27) | ✅ | stockStatus=12 集合竞价,盘口 397/23 |

**发现 Bug #1:msgpack/json 编码不对称**
- 现象:客户端推送线程 `msgpack.exceptions.ExtraData: unpack(b) received extra data` 崩溃
- 根因:服务端 QMT 内置 Python **无 msgpack**(json 兜底编码),客户端**有 msgpack**(按 msgpack 解码 json 文本 → 首字节 `{` 被当整数 + 尾随字节)
- 修复:`decode_push_payload` msgpack 失败时回退 json(测试驱动:红→绿)

### 2.2 阶段二:数据正确性(09:45-09:48,连续竞价)

**单标的 000001.SZ(60s)**:21 笔推送,间隔 min=2.21s / max=3.11s / avg=2.96s,>4s 的 0 个;time/volume/amount 单调性零违规。

**20 只活跃股(沪深300 成交额 top20, 120s)**:

| 指标 | 结果 |
|---|---|
| 每只推送次数 | 41~42 次(120s / 3s ≈ 40,高度一致) |
| 最大间隔 | 3.1~3.3s(全部 < 4s) |
| 平均间隔 | 2.93~2.99s |
| gap>4s | 0(全部 20 只) |
| 数据单调性(vol/amt/time) | 0 违规(全部 20 只) |

结论:**每 3 秒一份推送、零丢失、零乱序、零数据回退**,覆盖主板/创业板/科创板。

### 2.3 阶段三:多标的规模(09:36-09:40)

| 标的数 | 订阅耗时 | 初始快照 | 60s 增量推送 | 覆盖 | 错误 |
|---|---|---|---|---|---|
| 20 只 | 1.1s | 4 次/20 只 | 61 次 | 20/20 | 0 |
| 50 只 | 0.8s | 7 次/50 只 | 140 次 | 50/50 | 0 |
| 100 只 | 2.1s | 19 次/100 只 | 336 次 | 100/100 | 0 |

结论:推送量随标的数线性增长,覆盖完整,订阅耗时稳定,零错误。

### 2.4 阶段四:心跳与超时回收(A 组,09:53-09:56)

| # | 验证项 | 结果 | 证据 |
|---|---|---|---|
| A1 | 正常心跳保活 | ✅ | 90s 31 次推送,间隔 2.90s |
| A2 | 心跳超时回收(kill 不发退订) | ✅ | 之后同 client_id 重连安全 |
| A4 | 同 client_id 重连恢复 | ✅ | 45s 16 次推送,推送恢复 |
| A5 | 正常退订 + 再订阅 | ✅ | 退订后 10s 0 次,再订阅 11 次恢复 |

### 2.5 阶段五:多 client 并发(B 组,09:57-10:00)

| # | 验证项 | 结果 | 证据 |
|---|---|---|---|
| B1 | 两 client 同组合 | ✅ | A=7 B=7 各自收推 |
| B2 | 组合去重共享订阅 | ✅ | 推送节奏一致(7=7),服务端只建 1 个订阅 |
| B3 | 一方退订对方持续 | ✅ | A 退订后 A=0 B=5 |
| B4 | 全退订拆订阅 | ✅ | 无推送 |
| B5 | 不同组合互不干扰 | ✅ | 000001 只有 A 收,000002 只有 B 收 |
| B6 | 同 client 多 sub_id | ✅(修复后) | 见 Bug #2 |
| B7 | 混合组合隔离 | ✅ | 000001 双方收,000002 只有 E 收 |

**发现 Bug #2:客户端订阅线程泄漏**
- 现象:B6 首测失败(退订 sub1 后 sub2 偶发收不到),深挖发现订阅/退订时 `_sync_subscriber_locked` 无脑新起线程、旧线程不停止(3 个 `bigqmt-quote-push-sub` 线程并存),多线程消费同一 pubsub 有竞态
- 修复:topic 集合 diff——不变则复用,变化则先 stop 旧线程再起新线程,变空则停(测试驱动)

### 2.6 阶段六:异常与边界(C 组,10:05-10:18)

| # | 验证项 | 结果 | 证据 |
|---|---|---|---|
| C1 | 重复订阅幂等 | ✅ | 15s 11 次推送 |
| C2 | 空代码列表 | ✅ | `ValueError: code_list is required` |
| C3 | 非法代码容错 | ✅ | 未崩,无推送 |
| C4 | 同 topic 多 sub_id 退订隔离 | ✅(修复后) | 见 Bug #3 |
| C5 | 拔线重连 | ✅ | A2/A4 覆盖 |

**发现 Bug #3:服务端引用计数粒度错误**
- 现象:C4 首测失败——同 client 两个 sub_id 订阅同一组合,退订一个后,另一个的推送停止(组合被整体拆掉)
- 根因:`_Combo.clients` 按 `client_id` 粒度,但订阅单元是 `(client_id, sub_id)`;退订一个 sub 时 `_remove_client_locked` 把整个 client 移出,组合错误拆解
- 修复:改为 `(client_id, sub_id)` 粒度(含 subscribe/unsubscribe/keepalive/reaper 四处)(测试驱动,新增单测覆盖)

### 2.7 阶段七:服务端重启恢复(C6,10:27-10:31)

| 验证轮次 | 客户端行为 | 结果 |
|---|---|---|
| C6v1(修复前) | 无自动重放 | ❌ 重启后推送永久中断(140s 静默) |
| C6v2(keepalive 失败重放) | 只靠 keepalive 失败检测 | ❌ 未触发——重启窗口内 keepalive 被 redis 队列兜住"成功",检测不到 |
| C6v3(静默检测重放) | 推送静默超阈值自动重放 | ✅ 中断 42s 后自动恢复,后续 27 次推送正常 |

**发现 Bug #4:服务端重启后订阅丢失,客户端无自动恢复**
- 根因:文档承诺的"client 检测断连→自动重放"**从未实现**(`replay_subscriptions` 无生产调用点);且仅靠 keepalive 失败检测不可靠(redis 请求队列在重启窗口内缓冲,keepalive 不抛异常)
- 修复:心跳循环增加**推送静默检测**——订阅期间超过 N 个心跳周期(默认 10,≈30s)无推送到达,自动重放订阅(测试驱动,新增 2 个单测)

### 2.8 阶段八:配置验证(D 组,10:32)

| # | 验证项 | 结果 |
|---|---|---|
| D1 | `BIGQMT_QUOTE_HEARTBEAT_SECONDS` env 生效 | ✅ 1s 心跳,30s 10 次推送 |
| D2 | `heartbeat_timeout_seconds` 配置生效 | 单测覆盖(修改需重启策略,跳过) |

---

## 3. 发现并修复的 Bug 汇总(4 个)

| # | Bug | 位置 | 根因 | 修复 |
|---|---|---|---|---|
| 1 | 推送解码崩溃 `ExtraData` | `quote_push_channel.py` | 服务端无 msgpack(json 兜底)与客户端 msgpack 解码不对称 | msgpack 失败回退 json |
| 2 | 订阅线程泄漏/竞态 | `whole_quote_session.py` | `_sync_subscriber_locked` 每次新起线程不停止旧的 | topic 集合 diff,复用/停止 |
| 3 | 同 client 多 sub 退订误拆组合 | `quote_subscription_manager.py` | 引用计数按 client 粒度而非 (client, sub) 粒度 | 改 (client_id, sub_id) 粒度 |
| 4 | 服务端重启后订阅丢失 | `whole_quote_session.py` | 自动重放从未接线;keepalive 被 redis 队列兜住检测不到重启 | 推送静默检测自动重放 |

全部按 TDD 修复(红→绿),同步部署到服务端。

---

## 4. 测试基线

- 全量:`266 passed, 3 skipped, 1 failed`
- 唯一失败:`test_transports.py::MysqlTransportTest::test_round_trip`(`No module named 'dbutils'`,本地 venv 未装 mysql extra)——**预先存在,与本次改动无关**
- 新增测试:
  - `test_quote_push_channel.py`:json 解码回退 2 个
  - `test_whole_quote_client.py`:线程复用/重启 4 个 + 自动重放 2 个
  - `test_quote_subscription_manager.py`:(client, sub) 粒度退订 1 个

---

## 5. 结论

1. **全链路功能正确**:订阅 RPC、初始快照 prime、redis 推送通道、增量推送(3s 节奏)、多标的(20/50/100)、多 client 共享订阅、退订隔离、心跳保活/超时回收、服务端重启自动恢复,全部通过。
2. **数据正确性**:单标的 21/60s、20 只活跃股 41-42/120s,间隔 2.93-3.0s 稳定,零丢失、零乱序、零回退。
3. **发现并修复 4 个真实 bug**,均经 TDD 验证,全量测试无回归。
4. **遗留**:D2(超时配置生效)仅单测覆盖;mysql 测试环境缺 DBUtils(预先存在);`docs/SUBSCRIBE_WHOLE_QUOTE_PUSH.md` 中"自动重放"设计描述现已实现,可保持同步。
