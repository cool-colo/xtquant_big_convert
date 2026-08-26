# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/) 和 [语义化版本](https://semver.org/)。

## [0.2.9] - 2026-08-24

### 修复

- **redis-py 3.5.3 兼容（PR #67 回归，Issue #71）**：`build_redis_client` 无条件传 `protocol=` 在 QMT 自带的老 redis-py 上直接 TypeError——且 QMT 策略重跑后执行事件发布器重建客户端时崩掉，事件被静默全丢（发布器构建失败现在会记日志，不再无声）。改为按版本能力（inspect.signature）条件透传；客户端 `_redis()` 同步处理。
- **async_response 没有真实 order_id**（Issue #72）：委托号异步分配、RPC 应答时通常还没有，order_id 只能回落成 remark，按 order_id 管理委托的代码会解析失败。现在 response 触发前等屏障从暂存的委托事件里学到真实委托号（bounded 2s，学不到才回落 remark）。下单仍走 wait_settlement=False 快速应答（#50/#69 的吞吐不回退）。实盘验证：`async_response order_id=xt1090519419`（真实委托号）。
- **#69 发单间隔**：0.5s 检查已在 #44 改为结算停放（不阻塞提交）——见 issue 回复，无需改动。

## [0.2.8] - 2026-08-25

### 修复

- **期货持仓代码解析错误**（PR #68，@ReCodeLife）：裸期货合约（交易所字段为迅投简称 DF/SF/ZF）被错误送进股票归一化并抛 invalid stock code。改为**按交易所字段分类，不再猜代码形状**：迅投简称（IF/SF/DF/ZF/INE/GF）拼接后缀并**保留符号原始大小写**（`rb2401.SF` 小写 / `AP401.ZF` 大写，两者不可互换）；股票/港股通走归一化；`code_utils` 补 `.HGT` / `.SGT` 后缀识别。
- **一行无法解析的数据会搞垮整个查询**（PR #70）：上一条让 `_full_code` 遇到柜台式交易所 ID 时抛异常——信号本身是对的，但三个调用方的行循环都没有逐行保护，异常会一路抛出 `get_positions` / `query_orders` / `query_trades`。**一行异常 = 整个持仓查不到**；而 `query_orders` 外层 `except` 返回 `[]`，丢的是全部委托。对交易系统而言这比它要报告的问题更危险，也与本模块「降级而非崩溃」的既定风格矛盾（POSITION 查询外的 try/except 注释即为 *degrade to empty*）。现在跳过解析不了的那一行、其余照常返回，跳过按 `(kind, exchange)` 只记一次日志。

### 新增

- **`BIGQMT_ACCOUNT_TYPE` 配置**（PR #68）：账号类型独立可配（默认 `STOCK`，另有 `CREDIT` / `FUTURE` / `OPTION`），`redis_rpc_runtime` 向后兼容读取并归一化为大写字符串后传入 `configure()`。旧配置不写此项时行为不变。

### 已知限制

- **#58 的期货小写代码仍缺实盘样本**：`get_full_tick(['rb2708.SF'])` 返回空，无法据此判断大小写还原是否生效（空结果说明该合约无数据，而非映射失败）。本版持仓侧的大小写保留由单测覆盖。
- **单文件构建（#56）未进主干**：报告人提交的 `build_no_redis_single_file_flat.py` 依赖两个未附带的模块和 `bigqmt_no_redis/` 目录，尚不能独立运行；其惰性加载架构优于此前方案（PR #62 已关闭），待依赖补齐后合入。

---

## [Unreleased]

### 新增

- **Redis 5.x 兼容**（PR #67，@sunjian710）：`build_redis_client` 透传 `protocol`（默认 2/RESP2，可用 `protocol: 3` 或 `BIGQMT_REDIS_PROTOCOL` 覆盖）——redis-py 8.x 默认 RESP3 的 HELLO 握手在 Redis 5.0 上直接报错，现在开箱即用。客户端 `BigQmtRpcClient` 的 redis_config 同样透传。
- **持仓/资产对象原生字段别名**（PR #67，@sunjian710）：`query_stock_positions`/`query_stock_asset` 返回对象新增 `m_` 前缀原生字段（`m_strStockCode`/`m_nVolume`/`m_dCash`/`m_dTotalAsset` 等），读原生 xtquant 字段名的客户端代码（如 miniqmt_redis 风格）不再 AttributeError。

### 修复

- PR #67 合入修正：`_position_object` 的 m_ 别名引用了未定义局部变量（`stock_code`/`stock_name` 只内联在 kwargs 里），持仓查询全挂——提取为局部变量并补回归测试钉住全部别名。

## [0.2.6] - 2026-08-24

### 修复

- **回调对象字段命名不一致**（Issue #65）：`on_order_error`/`on_cancel_error`/async 回报的委托号字段是 `order_sys_id`，而 `on_stock_order`/`on_stock_trade` 用 MiniQMT 规范名 `order_sysid`。全部回调对象现在同时携带两个名字（同值），另补 `order_remark`/`status`/`strategy_name`。
- **on_order_error 缺 order_remark/status**（Issue #64）：服务端事件补上 `m_strRemark`→`order_remark`/`user_order_id` 与 `m_nOrderStatus`→`status`，撤单错误事件同步补齐。柜台的拒单理由此前已在 `error_msg`（m_strCancelInfo，#60）。
- **tick 分笔下载后立读为空**（Issue #66）：两个叠加问题——①QMT 下载全局提交即返回、数据异步落地，下载后立即读读到的是落地前的空结果；②FormulaServer 快速路径不服务 tick/L2 周期却静默返回空，把 RPC 桥的正确答案挡在门外。修复：download_history_data2 分批轮询直到批内每个代码出现真实数据行（`data_wait_seconds` 超时容忍停牌/退市）；FormulaServer 对 tick/l2 周期拒绝路由、回落 RPC。实盘验证：tick 下载 0820 → 立读 1434 行全部属当日。
- **拒单回报乱序**（Issue #51 屏障补全）：服务端在应答 RPC 之前就推送废单事件，order_error/cancel_error 此前不在屏障内，会比 async_response 先到（实盘实测倒挂）。屏障现在同时扣住 order/trade/order_error/cancel_error，实盘验证顺序：async_response → 已报(50) → 废单(57) → order_error。
- **call_async 异步 RPC**（Issue #63）：`client.call_async(method, params, callback=None)` 立即返回 Future，不阻塞调用方；有界在途（64）+ 8 线程池；可选 callback 在单 dispatcher 线程按完成顺序串行派发。注意：下单类 RPC 在服务端仍由 QMT 主线程串行处理，客户端异步叠加的是往返延迟，不会并行化报单到交易所的环节。

---

## [0.2.5] - 2026-08-21

### 修复

- **get_full_tick 返回 key 被全大写**（Issue #58）：代码 upper() 后传给 QMT，返回时未映射回调用方写法。期货交易所的合约代码是小写（`rb2708.SF`、`a2609.DF`、`nr2612`），导致 `code in result` 对每一个都失败、实时五档被误判缺失。现在只还原**大小写**，不动归一化——`600000` 仍补全为 `600000.SH`，补全后缀是调用方依赖的行为。QMT 主动返回而未被请求的 key 原样透传，不丢行情。
- **柜台拒单原因没有传出**（Issue #60）：`status_msg` 与 `error_msg` 在废单时均为空，形如 `[COUNTER] 资金可用余额不足，尚需[4789.630]` 的原因完全丢失。两处缺口：`OrderSnapshot` 没有 `status_msg` 字段；`normalize_order_error_event` 只读 `m_strErrorMsg`。柜台文本实际在 `m_strCancelInfo`（官方字段表标注为「废单原因」，状态 57 明确指向它）。已贯通 `OrderSnapshot` → `order_bigqmt` → 委托事件 → `_order_from_dict`，`order_error` 事件改为优先读该字段。

### 变更（PR #59，@cnwuwil）

- **回调/成交对象对齐 MiniQMT 原生契约**：`XtTrade` 补齐 `traded_id` / `traded_time` / `traded_amount` / `strategy_name`，成交金额优先取 `m_dTradeAmount` 真值（实测限价 9.14 成交 9.06 时估算值偏高约 0.9%）；撤单响应补 `cancel_result` / `error_msg`；业务回调异常落日志不再静默吞掉。
- **回调代码补全交易所后缀**：实盘 order/deal 回调的 `m_strInstrumentID` 只带 6 位裸代码，服务端事件构建时从 `m_strExchangeID` 补全，客户端分发层按 A 股代码段推断作兜底。
- **`order_volume` 语义修正**：由剩余量 `m_nVolumeTotal`（全部成交时为 0）改为原始委托量 `m_nVolumeTotalOriginal`，对齐 `XtOrder.order_volume`。**对依赖「剩余量」的调用方是行为变化**。
- **xtdata shim 签名对齐**：`get_instrument_detail` 接受并忽略第二参数 `is_detail`；`download_history_data(2)` 透传 `dividend_type`。
- 新增官方《内置Python》API 精简参考文档，作为字段映射的依据留存。

### 实盘验证（2026-08-21 收盘后，真实账户）

- **柜台拒单原因**（#60）：29 笔委托中 15 笔废单全部带回柜台原文，`m_strCancelInfo` 字段名经实盘证实。消息不止有原因，连参数也在：
  `[COUNTER][251005][证券可用数量不足][v_stock_code=000506,v_occur_amount=100.00,p_enable_amount=0.00,...]`
- **`order_volume` 语义**（#59）：13 笔已成（状态 56）委托的 `volume` 全部为真实委托量，无一为 0。旧代码读剩余量，这 13 笔会全是 0——这是该项修复的决定性证据。委托代码的交易所后缀也已确认带全。

### 已知限制

- **#58 的期货小写代码缺实盘样本**：`get_full_tick(['rb2708.SF'])` 返回空，无法据此判断大小写还原是否生效（空结果说明该合约无数据，而非映射失败）。股票代码路径确认未被破坏，小写映射逻辑由单测覆盖。
- Issue #56（单文件、无 Redis、仅 ZMQ 的 QMT 内嵌版本）未处理。

---

## [0.2.4] - 2026-08-20

### 修复

- **qmt_launcher mode=login 打不进密码**：SendMessage 把键消息发给顶层窗口，但 Qt 对话框只在窗口持前台焦点时才把按键路由给输入框——后台进程发送等于静默丢弃。改为物理输入（keybd_event/mouse_event）：Alt 键解锁前台保护 → 置顶 + 前置 → 物理点击字段 → 打字。账号预填时先 Ctrl+A 再覆盖（否则变成追加）；密码框点击位置避开右侧虚拟键盘图标；控件坐标按窗口尺寸比例定位。已是大窗（主界面=自动登录完成）时跳过整个输入流程，避免密码打进主窗口控件。
- **登录框就绪判定**：58600 端口在登录前就监听，launcher 的端口就绪不等于"已登录可用"——docstring 已注明，建议以 RPC ping 为真正就绪信号。

### 新增

- **normalize_stock_code 支持 QMT 期权/期货后缀**（PR #57）：`.BJ/.HK/.SHO/.SZO/.SF/.DF/.IF/.ZF/.INE/.GF` 等 ContextInfo 原生后缀直接透传，不再 ValueError——ETF 期权、商品期货/期权代码可正常走 RPC。原 6 位股票码逻辑不变。
- **get_full_tick 超时参数**（PR #57）：新增 `timeout_seconds=None`，调用方传值时优先（批量查 1256+ 期权代码时 120s 默认不够），不传时保留原自动检测（全市场 30s）。

---

## [0.2.3] - 2026-08-19

### 修复

- **异步下单的 order/trade 事件先于 async_response 到达**（Issue #51）：两条回调走不同通道——`async_response` 在异步下单工作线程触发，`order`/`trade` 来自 Redis pub/sub 监听线程；服务端 `order_callback` 先推事件、后回 RPC，顺序颠倒是常态。客户端按 `order_remark` 设屏障：命中待响应委托的事件先暂存，response（或 error）触发后按到达顺序放行；10 秒超时兜底，提交失败的事件也不会被永久扣住（丢事件比顺序错乱更糟）。仅 `order_stock_async` 路径受影响，手工/同步/无 remark 委托直通。成交事件无 remark 时按委托事件学到的 `order_sys_id` 关联。已验证：9 个单测（含 4 个反向验证）+ 盘后真实 Redis 注入实测 + 盘中 3 轮真实买卖验证（async_response 均先于 order/trade 到达，买单 50→54 撤单成功，卖单无持仓 50→57 废单正确上报）。
- **重复 order_remark 导致暂存事件丢失**（Issue #51 后续）：`order_remark` 不强制唯一（网格类策略常复用同一 remark），同 remark 的第二笔下单会让 `_arm_order_barrier` 直接覆盖前一笔的屏障，暂存事件被静默丢弃；且前一笔的 response 会误放后一笔的屏障，使后一笔失去保序。改为接管旧屏障时先放行其暂存事件；`_release_order_barrier` 增加 seq 校验，只有 arm 时的那笔委托的 response 才能放行对应屏障。回归测试对任一半修复回退均失败。
- **download_history_data2 只下载当天数据**（Issue #54）：部分 QMT 版本只注入单股下载全局 `down_history_data`，而捕获列表只有 `download_history_data/2` → 下载 RPC 静默返回 False、什么都没下，读取只能看到当天数据。捕获列表补 `down_history_data`；`download_history_data2` 无批量全局时按代码循环调用单股全局（日期透传）；`_handle_download_history_data` 同步兜底。修正 DRYRUN 里恒为 False 的下载绑定诊断打印。
- **get_financial_data 表名不兼容**（Issue #52）：MiniQMT 传整表名（Balance/Income/…），大 QMT 要 `"BIGTABLE.field"` 点分字段列表。服务端适配层新增 MiniQMT→大QMT 表名映射（8 张表）+ 整表名展开为全字段点分列表；点分条目前缀重映射、未知表名透传。实盘验证：`Balance`（展开 54 字段）与 `Capital`（6 字段）均返回真实数据（首调服务端自动下载财务数据较慢，之后毫秒级）。
- **本地缓存丢时间轴导致日期窗口失效**（Issue #54 关联）：客户端规范化把 `stime` 列转成索引（MiniQMT 形态），而 local_cache 只在列里找时间轴 → `get_local_data` 的 start/end 完全不生效、按索引去重也失效。修复：缓存层识别索引形态时间轴（`write`/`read`/`covered`/count 截尾/占位行清理全链路保索引）；parquet 写盘带索引；老版写出的无时间轴缓存文件遇新版写入时自动废弃（行不可切片，留着只会污染窗口过滤）。新增 18 个读路径矩阵用例（形态 × 周期 × 窗口 × count × 复权隔离）。

## [0.2.2] - 2026-08-19

### 修复

- **server_error 污染后续查询**（Issue #43）：`_last_server_error` 是实例状态，但每个成功响应都会读取它，而只有下单路径会重置。一次静默拒绝的委托会把错误盖到之后**所有** ping 和查询上，直到下一次下单。改为在 `handle()` 中每请求清空，且清空发生在方法校验之前，因此被拒绝的方法也不会携带上一次的诊断。
- **order_remark 匹配的模糊兜底**（Issue #41）：那段 `stock_code + action` 的兜底并非用于*识别*委托，而是**告警闸门**——问题比报告描述的更严重。`order_tag` 是我们生成的唯一 id，匹配不上即真未进系统；模糊兜底唯一的作用是**压制真实告警**：账户中若有一笔无关的同股票同方向委托（手动下的或上一笔未成交的），会导致 `order_sys_id` 未回填、`server_error` 为空，客户端看到一次干净的成功，而该委托从未进入系统。已移除。
- **order_stock_async 阻塞 QMT 主线程**（Issue #44）：`_handle_submit_order` 中的 `sleep(0.5)` 在 adjust 主线程执行（下单方法不在 `listener_methods` 中，走 deferred 路径），使其余请求串行等待，吞吐上限约 2 单/秒。改为**推迟响应而非推迟工作**：提交后登记 `OrderSettlement` 并停放响应，由每次 adjust drain 重试查询，委托号就绪即在同一 tick 内回复（零 sleep）。后台线程方案不可行——`get_trade_detail_data` 在非主策略线程返回空，会把每笔都误判为静默拒绝。
- **order_stock_async 未立即返回**（Issue #50）：客户端内部同步调用 `order_stock`，阻塞整个 RPC 往返，加上 #44 后的结算等待，每笔 0.5~1 秒。服务端新增 `wait_settlement` 参数（false 时 passorder 一返回即回复，委托号由 `order_callback` 推送）；客户端提交移至工作线程，`order_stock_async` 不碰网络直接返回 seq。`on_order_error` 现在也携带 `seq`，此前无法判断是哪一笔异步委托失败。
- **未复权下载实为空跑**（Issue #47，亦是 #39 的真正原因）：服务端下载此前只在请求复权时执行，未复权路径仅调用 `get_market_data_ex` 读取已有数据，却照常通过 callback 报告 `{finished: N}`——为一件没发生的事显示进度。而 1d/tick 默认即 `dividend_type="none"`。现在所有 `dividend_type` 都执行下载。实盘验证：601398.SH 本地日线从 0 根变为有数据。
- **query_stock_orders 缺少 order_time**（Issue #48）：大 QMT 的 ORDER 行提供报单日期与时间，但三层均未读取。已贯通 `OrderSnapshot` → `order_bigqmt` → `_order_from_dict`，按 MiniQMT `XtOrder.order_time` 语义输出 Unix 秒。实盘验证：11 笔真实委托全部有值。

### 新增

- **qmt_launcher**（Issue #45）：`open` / `close` / `restart` / `status` 四个命令管理 QMT 终端。按 `bin.x64` 路径隔离（同机多实例并存时不会误关其他账户）、以 FormulaServer 端口可连接为就绪判据而非固定 sleep、窗口标题前缀匹配（不再写死版本号）、先优雅终止 20 秒后才强杀。登录路径用 `SendMessage` 投递窗口句柄，不依赖窗口置于前台。
- **get_market_data_ex 分批**（Issue #47 评论）：宽 `stock_list` 此前共用一个 RPC 超时，要么装得下要么整批丢失。改为按 100 个代码一批，单批失败只损失自身代码，全部失败才抛异常。`chunk_size=0` 恢复原行为。
- **bar driver 观测埋点**：`adjust()` 按触发来源分别计数、`tick_app` 全量耗时直方图、init 报告策略品种/周期/订阅能力。用于定位 RPC 读延迟的来源。

### 变更

- `AssetSnapshot` 补齐 `frozen_cash` / `market_value`，对齐 MiniQMT `XtAsset`；`market_value` 优先取 `m_dInstrumentValue`，仅在服务端未上报时才推导（推导会扣除冻结金额，此前未扣导致市值虚高）。
- ZMQ 传输改为精确绑定配置端口，冲突时报错而非向上扫描——端口静默漂移会让客户端连不上。

### 已知限制

- Issue #44 / #50 的实盘下单验证尚未完成（单测已量化非阻塞行为：drain < 0.2s、20 单 < 1.0s）。
- Issue #47 评论所述的 `get_market_data_ex` 超时未能复现；三组压测（300 只 × count=3、300 只 × 全历史、50 只 × 1m 全天）最慢 718ms，远在默认 6s 超时内。分批目前是防御性改动。
- RPC 读延迟受 QMT 主线程 GIL 制约，延迟 ≈ 基础 + N × `schedule_adjust_interval`。实测该间隔 200ms → 100ms 可使 p50 从 374ms 降至 172ms，代价是 CPU 占用上升。

---

## [0.2.1] - 2026-08-17

### 修复

- **正常下单误报 on_order_error(-1)**（Issue #38）：passorder 提交成功但委托号异步分配，客户端把「暂无 order_sys_id」误判为失败。服务端 `_handle_submit_order` 按唯一 `user_order_id`(remark) 匹配并回填 `order_sys_id`；顺带修掉校验代码对无 `.get()` 方法的 `OrderSnapshot` 调 `.get()` 的死代码（server_error 之前从未生效）。客户端 `call()` 不再丢弃 `server_error`，委托未进系统时转成异常，`order_stock_async` 携带真实原因回调 `on_order_error`。实盘验证：async 下单回调带真实委托号、提交阶段零误报（302 个测试通过，新增 5 个）。
- **query_stock_orders 查不到委托**（strategy_name 陷阱）：客户端别名默认 `"bigqmt_signal_trader"` 与服务端默认 `""` 不一致，改用其他策略名下单后别名查询返回空。默认改 `""`（返回全部）并对齐测试。

### 新增

- **qmt-trader skill 首次部署引导**：客户端装包、QMT 端文件同步、私有配置模板、入口启动验证、部署排错速查，零上下文也能从零跑通。
- **PyPI 发布**：`BIGQMT_REDIS_DRYRUN` 入口模块补进 py-modules，`pip install xtquant-big-convert` 即可获得完整包（wheel/sdist 均通过 twine check）。

### 变更

- README 头部加 PyPI / Python 版本 / License 徽章，新增「AI 助手 Skill：qmt-trader」专节（启用方式、命令概览、安全设计）。

---

## [0.2.0] - 2026-08-15

### 新增（Features）

- **qmt-trader skill**：统一 CLI 驱动全部 QMT API（`qmt-trader/scripts/qmt.py`），46 个子命令覆盖行情/持仓/委托/下单/撤单/财务/期权/两融/北向/龙虎榜等，含通用 `rpc` 兜底命令 + 25 个高频快捷命令。
- **异步回报回调**：`XtQuantTraderCallback` 全链路（`on_account_status` / `on_order_stock_async_response` / `on_stock_order` / `on_stock_trade` / `on_order_error` / `on_cancel_error` / `on_cancel_order_stock_async_response`），对齐 MiniQMT 原生语义，实盘验证。
- **全推行情订阅**（`subscribe_whole_quote` 真推送）：服务端引用计数管理 + PUB/SUB 数据面通道（redis/zmq）+ 客户端心跳 + 推送静默检测 + 服务端重启恢复。
- **完整 xtconstant 枚举**：91 个常量全量覆盖（账号类型/委托类型-股票期货信用期权/报价类型/委托状态/账号状态/`ORDER_TYPE_SET`），值对齐原生 MiniQMT。
- **文件日志系统**（`logging_setup.py`）：TimedRotatingFileHandler 按天轮转、保留 7 天（`BIGQMT_LOG_RETENTION_DAYS` 可配），双输出（文件 + QMT 面板），线程安全。
- **启动自动诊断**：`init()` 打印服务状态、关键函数绑定、行情链路，方便排错。
- **server_error 字段**：`submit_order` 校验委托是否进系统，静默失败时返回原因给客户端。
- **统一测试入口**：`run_all_tests.py` 分组跑全部测试（signal_trader 274 + backtest 16）。
- **端到端测试**：`test_all_apis.py` 验证真实 QMT 返回（transport 一致性/持仓空/委托空/下单未进系统/server_error）。
- **生产失败场景单元测试**：7 个测试覆盖返回空/全 0/拒绝的 QMT 边界（非 happy-path）。
- **官方交易查询函数**：`get_value_by_order_id` / `get_last_order_id` / `get_ipo_data` / `get_new_purchase_limit` / `get_history_trade_detail_data` / 融资融券 5 个 / 期权持仓 2 个 / 港股通汇率。
- **无 redis 版本**（`bigqmt_no_redis/`）：自包含 ZMQ transport + 无 redis DRYRUN，解决 QMT 沙箱 `import redis` 报错。
- **多账号使用文档**：README 加「多账号使用」章节（多策略实例 + 多 client）。
- **MiniQMT→BigQMT 转换 skill**：docs + scripts + templates（PR #37）。

### 修复（Bug Fixes）

- **QMT 自动退出**：`ZmqQuotePushChannel.stop()` 跨线程关 SUB socket 触发 Windows signaler abort → 进程崩溃。改为订阅线程自己关 socket。
- **QMT 自动退出（系列）**：`_adjust_phase` 无 except（redis 故障崩策略）、`_publish_response` 逃出、deal_callback/forward_order_event/forward_trade_event/sync_positions_app 无防护、pending 队列满（queue.Full）、init() 无防护、socket_timeout=None 永久阻塞主线程、reset_app 不清理 quote-push/whole-quote（重启泄漏）、exec 事件每次回调新建 redis client（连接池泄漏）。
- **download_history_data 下载不了**（Issue #32）：`download_history_data` 是 QMT 全局函数不是 ContextInfo 方法，改走 `qmt_api` 注入。
- **复权数据返回全 0**（front/back）：服务端需先下载原始数据 + 除权因子。下载类（`download_history_data2`）自动预下载；读取类（`get_market_data_ex`/`get_market_data`）自愈（检测全 0 → 服务端下载 → 重试）。
- **卖出方向误判**（exec_events）：QMT 回调 `m_nDirection` 恒为 48，改仲裁链（offset_flag > direction > op_type）。
- **query_orders/query_trades 返回空**：`strategy_name` 过滤不匹配，默认改 `""` 返回全部。
- **get_financial_data 返回 None**：参数顺序错误（stock_list/table_list 反了）。
- **position_events 内存无限增长**（Issue #21）：xadd 无 maxlen，加 maxlen=2000。
- **异步回调签名错误**：`on_order_stock_async_response`/`on_cancel_order_stock_async_response` 原生签名 1 参数（response 带 seq），之前传 2 参数导致 TypeError 被吞。
- **order_stock 返回 -1**：`order_stock_async` 调 `result.get()` 崩，改为触发 `on_order_error`。
- **客户端 transport 不匹配**（Issue #24）：`query_stock_asset` 返回 None 的根因是客户端 redis / 服务端 zmq 不匹配。
- **DRYRUN 硬编码路径**：`_known_qmt_python_dir` 改 sys.path 扫描（paste-run 模式）。
- **ZMQ bind 冲突提示**：加端口占用检测 + 解决步骤提示。

### 变更（Changed）

- 包发布：`pip install xtquant-big-convert`（pyproject.toml 完善元数据 + LICENSE）。
- README 重写：依赖安装分客户端/服务端、API 总览、传输层对比、FormulaServer 直连、异步回调、无 redis 版本、日志排错、多账号、复权陷阱等章节。

---

## [0.1.0] - 2026-07-02

初始版本：Big QMT Redis RPC 桥接 + MiniQMT 兼容层。

### 新增

- Redis RPC 服务（rpush/blpop/brpop）+ 可插拔传输层（redis/zmq/mysql/shm）。
- 客户端兼容层（`xtquant_compat`）：`xt_trader` / `xtdata` 方法名映射。
- 行情/持仓/委托/下单基础 RPC 接口。
- `BIGQMT_REDIS_DRYRUN.py` QMT 编辑器入口。
