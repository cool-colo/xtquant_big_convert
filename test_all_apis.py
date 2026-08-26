# coding: utf-8
"""Systematically test all RPC APIs and MiniQMT alias mapping (end-to-end validation).

For each method: call it with sensible params, report ok/error, and VALIDATE the
result is actually correct (not just "call succeeded"). Catches silent failures
like:
  - get_positions returns {} when the account HAS positions
  - submit_order returns SUBMITTED but the order never entered the system
  - query_orders returns [] because strategy_name didn't match
  - client transport (redis) doesn't match server (zmq) → timeout

Config is read from bigqmt_signal_trader_local_config (gitignored) or env vars;
no credentials are hard-coded here. Run from a dir where that config module
resolves, e.g.:

    PYTHONPATH="src;D:\\国金证券QMT交易端\\python" python test_all_apis.py

or set BIGQMT_ACCOUNT_ID / BIGQMT_REDIS_HOST / BIGQMT_REDIS_PORT /
BIGQMT_REDIS_DB / BIGQMT_REDIS_PASSWORD env vars.
"""
import os
import sys
import time

# Add src to path so bigqmt_signal_trader resolves when run from repo root.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

import redis

from bigqmt_signal_trader.redis_rpc import call_redis_rpc


def _load_account_and_redis():
    cfg = {}
    try:
        import bigqmt_signal_trader_local_config as _c  # noqa
        cfg = getattr(_c, "BIGQMT_REDIS_CONFIG", {}) or {}
        account = getattr(_c, "BIGQMT_ACCOUNT_ID", None) or cfg.get("account_id")
    except Exception:
        account = None
    account = account or os.environ.get("BIGQMT_ACCOUNT_ID", "")
    redis_cfg = dict(
        host=cfg.get("host") or os.environ.get("BIGQMT_REDIS_HOST", "127.0.0.1"),
        port=int(cfg.get("port") or os.environ.get("BIGQMT_REDIS_PORT", 6379)),
        db=int(cfg.get("db") or os.environ.get("BIGQMT_REDIS_DB", 5)),
        password=cfg.get("password", os.environ.get("BIGQMT_REDIS_PASSWORD", "")),
        socket_timeout=15,
    )
    if not redis_cfg["password"]:
        redis_cfg.pop("password")
    return str(account), redis_cfg


ACCOUNT, REDIS = _load_account_and_redis()
# account_id placeholder filled in main() once ACCOUNT is confirmed.
_ACCT_PARAM = {"account_id": None}

# (method, params, label) — params chosen to be valid during/after market hours
TESTS = [
    # --- 行情快照 ---
    ("get_full_tick", {"codes": ["000001.SZ"]}, "tick"),
    ("get_ticks", {"codes": ["000001.SZ"]}, "ticks-alias"),
    # --- 合约/品种 ---
    ("get_instrument", {"code": "000001.SZ"}, "instrument"),
    ("get_instrument_detail", {"code": "000001.SZ"}, "instrument-alias"),
    ("get_instrumentdetail", {"code": "000001.SZ"}, "instrument-alias2"),
    ("get_instrument_type", {"code": "000001.SZ", "variety_list": ["stock", "fund"]}, "inst-type"),
    # --- K线/历史 ---
    ("get_market_data_ex", {"field_list": ["close"], "stock_list": ["000001.SZ"], "period": "1d", "count": 3}, "md-ex"),
    ("get_market_data", {"field_list": ["close"], "stock_list": ["000001.SZ"], "period": "1d", "count": 3}, "md"),
    ("get_local_data", {"field_list": ["close"], "stock_list": ["000001.SZ"], "period": "1d", "count": 3}, "local-data"),
    # --- 板块 ---
    ("get_sector_list", {}, "sector-list"),
    ("get_stock_list_in_sector", {"sector_name": "沪深A股"}, "sector-stocks"),
    # --- 交易日历 ---
    ("get_trading_dates", {"market": "SH", "count": 3}, "trade-dates"),
    ("get_holidays", {}, "holidays"),
    ("get_markets", {}, "markets"),
    ("get_market_last_trade_date", {"market": "SH"}, "last-trade-date"),
    # --- 账户 ---
    ("get_asset", {}, "asset"),
    ("get_positions", {}, "positions"),
    ("query_stock_asset", dict(_ACCT_PARAM), "asset-alias"),
    ("query_stock_positions", dict(_ACCT_PARAM), "positions-alias"),
    ("query_stock_position", dict(stock_code="000001.SZ", **_ACCT_PARAM), "position-single"),
]


def data_summary(data):
    """One-line summary of returned data for readability."""
    if data is None:
        return "None"
    if isinstance(data, dict):
        if not data:
            return "{}"
        if "__bigqmt_type__" in data:
            return "[%s cols=%s records=%d]" % (
                data.get("__bigqmt_type__"),
                data.get("columns"),
                len(data.get("records") or []),
            )
        keys = list(data.keys())[:3]
        return "{%s%s: ...}(%d keys)" % (keys, "" if len(keys) < 3 else ", ...", len(data))
    if isinstance(data, list):
        return "[list len=%d]" % len(data)
    return repr(data)[:60]


def _is_empty(data):
    return data is None or data == {} or data == [] or data == ""


def _call(r, method, params, timeout=12):
    """Call and return (response, latency_ms, error_str)."""
    t0 = time.time()
    try:
        resp = call_redis_rpc(r, ACCOUNT, method, params, timeout_seconds=timeout)
        return resp, (time.time() - t0) * 1000, None
    except Exception as e:
        return None, (time.time() - t0) * 1000, str(e)


def main():
    if not ACCOUNT:
        raise SystemExit("ACCOUNT is empty: set BIGQMT_ACCOUNT_ID or configure bigqmt_signal_trader_local_config")
    # Fill the account_id into the account-query test params now that we know it.
    for i, (method, params, label) in enumerate(TESTS):
        if "account_id" in params and params["account_id"] is None:
            params["account_id"] = ACCOUNT

    r = redis.Redis(**REDIS)

    print("=" * 90)
    print("全量 API 测试 (account=%s) — 端到端验证" % ACCOUNT)
    print("=" * 90)

    # === 端到端验证 0: 客户端/服务端 transport 一致性 ===
    print("\n--- 端到端验证: 客户端/服务端一致性 ---")
    # 检测客户端配置里的 transport
    client_transport = "redis"  # 默认
    try:
        import bigqmt_signal_trader_local_config as _c
        client_transport = str(getattr(_c, "BIGQMT_REDIS_CONFIG", {}).get("transport", "redis")).lower()
    except Exception:
        pass
    print("客户端配置 transport: %s" % client_transport)

    # 如果客户端是 zmq 但服务端不是, ping 会超时
    ping_resp, ping_ms, ping_err = _call(r, "ping", {}, timeout=8)
    if ping_err:
        print("❌ ping 失败: %s" % ping_err)
        if "timeout" in ping_err.lower():
            print("   可能原因: 客户端 transport 和服务端不匹配")
            print("   - 客户端配置 transport=%s" % client_transport)
            print("   - 如果服务端是 zmq, 客户端也要设 transport=zmq")
            print("   - 如果服务端是 redis, 客户端保持 redis 即可")
        return
    print("✅ ping OK (%.0fms) — 客户端/服务端连通" % ping_ms)

    # === 端到端验证 2: 账户有持仓时 get_positions 必须返回非空 ===
    print("\n--- 端到端验证: 持仓查询 ---")
    pos_resp, pos_ms, pos_err = _call(r, "get_positions", {}, timeout=12)
    if pos_err:
        print("❌ get_positions 失败: %s" % pos_err)
    elif not pos_resp.get("ok"):
        print("❌ get_positions 返回错误: %s" % pos_resp.get("error"))
    else:
        positions = pos_resp.get("data") or {}
        if len(positions) > 0:
            print("✅ get_positions OK (%.0fms) — 返回 %d 只持仓" % (pos_ms, len(positions)))
        else:
            print("⚠️  get_positions 返回空 — 账户可能真的没持仓, 或查询失败 (检查 QMT 上下文)")

    # === 端到端验证 3: query_orders 验证 (strategy_name 陷阱) ===
    print("\n--- 端到端验证: 委托查询 ---")
    ord_resp, ord_ms, ord_err = _call(r, "query_orders", {}, timeout=12)
    if ord_err:
        print("❌ query_orders 失败: %s" % ord_err)
    elif not ord_resp.get("ok"):
        print("❌ query_orders 返回错误: %s" % ord_resp.get("error"))
    else:
        orders = ord_resp.get("data") or []
        if len(orders) > 0:
            print("✅ query_orders OK (%.0fms) — 返回 %d 条委托" % (ord_ms, len(orders)))
        else:
            print("⚠️  query_orders 返回空 — 可能 strategy_name 不匹配 (默认应为 '' 返回全部)")

    # === 端到端验证 4: 买入/卖出后委托必须进系统 ===
    print("\n--- 端到端验证: 买入/卖出 (仅交易时段) ---")
    # 用极低价格买入 (确保不成交), 然后查委托确认进了系统
    # 先拿一只股票的现价
    tick_resp, _, tick_err = _call(r, "get_full_tick", {"codes": ["600654.SH"]}, timeout=12)
    if tick_err or not tick_resp.get("ok"):
        print("⚠️  跳过买入测试 (get_full_tick 失败: %s)" % (tick_err or tick_resp.get("error")))
    else:
        d = (tick_resp.get("data") or {}).get("600654.SH", {})
        last_close = float(d.get("lastClose") or d.get("lastPrice") or 3.0)
        buy_price = round(last_close * 0.8, 2)  # 跌停价, 确保不成交
        print("  用 600654.SH @%.2f 买入 100 股 (跌停价, 不成交)" % buy_price)

        # 下单前委托数
        ord_before, _, _ = _call(r, "query_orders", {}, timeout=12)
        before_count = len((ord_before or {}).get("data") or []) if ord_before else 0

        # 下单
        sub_resp, sub_ms, sub_err = _call(r, "submit_order", {
            "stock_code": "600654.SH", "action": "BUY", "volume": 100,
            "price": buy_price, "price_type": "LIMIT", "strategy_name": "rpc_test",
            "signal_id": "e2e-test-%d" % int(time.time()),
        }, timeout=15)
        if sub_err:
            print("❌ submit_order 失败: %s" % sub_err)
        elif not sub_resp.get("ok"):
            print("❌ submit_order 返回错误: %s" % sub_resp.get("error"))
        else:
            server_err = sub_resp.get("server_error") or ""
            print("✅ submit_order OK (%.0fms)" % sub_ms)
            if server_err:
                print("   ⚠️ server_error: %s" % server_err)

            # 等 1s 让 QMT 处理, 然后查委托确认进了系统
            time.sleep(1)
            ord_after, _, _ = _call(r, "query_orders", {}, timeout=12)
            after_orders = (ord_after or {}).get("data") or [] if ord_after else []
            found = any(
                str(o.get("stock_code") or "").upper() == "600654.SH"
                and str(o.get("action") or "").upper() == "BUY"
                and abs(float(o.get("price") or 0) - buy_price) < 0.01
                for o in after_orders
            )
            if found:
                print("✅ 委托已进系统 (query_orders 确认)")
                # 尝试撤单
                oid = None
                for o in after_orders:
                    if (str(o.get("stock_code") or "").upper() == "600654.SH"
                            and str(o.get("action") or "").upper() == "BUY"
                            and abs(float(o.get("price") or 0) - buy_price) < 0.01):
                        oid = str(o.get("order_sys_id") or "")
                        break
                if oid:
                    cancel_resp, cancel_ms, cancel_err = _call(r, "cancel_order", {
                        "order_sys_id": oid, "market": "SH"
                    }, timeout=15)
                    if cancel_err:
                        print("⚠️  cancel_order 失败: %s" % cancel_err)
                    elif cancel_resp and cancel_resp.get("ok"):
                        print("✅ cancel_order OK (%.0fms) — 已撤单" % cancel_ms)
                    else:
                        print("⚠️  cancel_order 返回: %s" % (cancel_resp or {}))
            else:
                print("❌ 委托没进系统 — submit_order 成功但 query_orders 找不到")
                print("   这是静默失败 (passorder 被 QMT 拒绝但没报错)")
                print("   检查: 1) 价格是否超出范围 2) 账户权限 3) QMT 风控")

    # === 全量 API 测试 ===
    print("\n" + "=" * 90)
    print("全量 API 测试")
    print("=" * 90)
    print("%-22s %-8s %-8s %s" % ("method", "ok", "ms", "data summary"))
    print("-" * 90)

    results = {"ok": [], "ok_empty": [], "fail": [], "timeout": []}
    for method, params, label in TESTS:
        resp, dt, err = _call(r, method, params, timeout=12)
        if err:
            is_timeout = "timeout" in err.lower()
            bucket = "timeout" if is_timeout else "fail"
            results[bucket].append((method, err[:60]))
            print("%-22s %-8s %6.0f   %s" % (method, "TIMEOUT" if is_timeout else "ERROR", dt, err[:50]))
            continue
        ok = resp.get("ok")
        data = resp.get("data")
        error = resp.get("error", "")
        server_err = resp.get("server_error", "")
        empty = _is_empty(data)
        if ok and not empty:
            results["ok"].append(method)
            status = "OK"
        elif ok and empty:
            results["ok_empty"].append(method)
            status = "EMPTY"
        else:
            results["fail"].append((method, error))
            status = "FAIL"
        summary = data_summary(data) if ok else error[:50]
        if server_err:
            summary += " [server_error: %s]" % server_err[:40]
        print("%-22s %-8s %6.0f   %s" % (method, status, dt, summary))

    print("-" * 90)
    print("\n=== 汇总 ===")
    print("有数据 (OK):     %d 个" % len(results["ok"]))
    print("成功但空 (EMPTY): %d 个 %s" % (len(results["ok_empty"]), results["ok_empty"]))
    print("失败 (FAIL):     %d 个 %s" % (len(results["fail"]), [m for m, _ in results["fail"]]))
    print("超时 (TIMEOUT):  %d 个 %s" % (len(results["timeout"]), [m for m, _ in results["timeout"]]))


if __name__ == "__main__":
    main()
