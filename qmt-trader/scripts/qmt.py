#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
qmt.py - QMT 交易/行情统一 CLI 入口

设计目标：让大模型通过确定性命令调用 QMT 全部能力（行情、持仓、委托、下单、撤单），
避免每次现场写 Python。所有子命令默认输出 JSON，加 --table 可切换人类可读表格。

配置来源（按优先级）：
  1. 环境变量 BIGQMT_ACCOUNT_ID / BIGQMT_REDIS_HOST / ...
  2. bigqmt_signal_trader_client_config 模块（在 PYTHONPATH 中）
  3. bigqmt_signal_trader_local_config 模块
  4. 本脚本所在仓库的 src/ 自动加入 sys.path（开发模式）

用法示例：
  python qmt.py ping
  python qmt.py account
  python qmt.py positions
  python qmt.py orders --cancelable
  python qmt.py trades
  python qmt.py tick 600000.SH 000001.SZ
  python qmt.py kline 600000.SH --period 1d --count 60
  python qmt.py instrument 600000.SH
  python qmt.py sector "沪深A股"
  python qmt.py trading-dates --count 10
  python qmt.py north
  python qmt.py longhubang 600000.SH --count 5
  python qmt.py buy 600000.SH 100 --price 7.50 --strategy my_strat
  python qmt.py sell 600000.SH 100 --price 7.50
  python qmt.py cancel 12345 --market SH
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# 路径自动发现：把仓库 src/ 加到 sys.path，确保开发模式也能 import
# ---------------------------------------------------------------------------
def _ensure_src_on_path() -> None:
    here = Path(__file__).resolve().parent
    # skill/scripts/qmt.py  ->  上溯到仓库根
    for ancestor in [here, *here.parents]:
        candidate = ancestor / "src"
        if (candidate / "bigqmt_signal_trader" / "__init__.py").exists():
            src_str = str(candidate)
            # 不能只查 in sys.path：editable 安装会把 src 放在 site-packages
            # 之后，site-packages 里的真 xtquant 就会遮蔽本仓库的 shim
            # （实测：import xtquant 打印升级广告、污染 stdout 的 JSON 输出）。
            # 必须确保 src 在最前——已存在就挪到最前。
            if src_str in sys.path:
                if sys.path.index(src_str) == 0:
                    return
                sys.path.remove(src_str)
            sys.path.insert(0, src_str)
            return
    # 没找到仓库 src，假设用户已 pip install
    return


def _ensure_qmt_python_on_path() -> None:
    """把 QMT 的 python 目录加到 sys.path，让 local_config.py 能被发现。

    这样客户端能读到 QMT 端的 transport=zmq 配置（服务端用 zmq 时客户端也得用）。
    通过环境变量 BIGQMT_QMT_PYTHON_DIR 指定，或自动从常见路径/当前目录探测。
    """
    candidates = []
    env_dir = os.environ.get("BIGQMT_QMT_PYTHON_DIR")
    if env_dir:
        candidates.append(env_dir)
    # 常见 QMT 安装路径（国金证券、华泰等）
    for root in ("D:\\", "C:\\", "E:\\"):
        if not os.path.isdir(root):
            continue
        try:
            for entry in os.listdir(root):
                if "QMT" in entry.upper():
                    p = os.path.join(root, entry, "python")
                    if os.path.isdir(p):
                        candidates.append(p)
        except Exception:
            continue
    # 当前工作目录（如果就在 QMT python 目录里）
    cwd = os.getcwd()
    if cwd.endswith(r"\python") or cwd.endswith("/python"):
        candidates.append(cwd)

    for c in candidates:
        local_cfg = os.path.join(c, "bigqmt_signal_trader_local_config.py")
        if os.path.isfile(local_cfg):
            if c not in sys.path:
                # This path is needed to discover local_config.py, but must not
                # shadow the checked-out/PyPI client package selected above.
                # QMT commonly carries an older deployed package of the same
                # name; putting it at sys.path[0] made new CLI commands execute
                # against that stale copy.
                sys.path.append(c)
            return


_ensure_src_on_path()
_ensure_qmt_python_on_path()


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def _json_default(o):
    """JSON 序列化兜底：处理 pandas / numpy / CompatObject / Decimal 等。"""
    # pandas DataFrame / Series
    if hasattr(o, "to_dict"):
        try:
            if hasattr(o, "index") and hasattr(o, "columns"):
                # DataFrame -> list of dict records
                return o.reset_index().to_dict(orient="records")
            return o.to_dict()
        except Exception:
            pass
    # numpy
    if hasattr(o, "tolist"):
        return o.tolist()
    if hasattr(o, "isoformat"):
        return o.isoformat()
    # CompatObject（xtquant_compat 的属性包对象）
    if hasattr(o, "__dict__") and not isinstance(o, type):
        return {k: v for k, v in o.__dict__.items() if not k.startswith("_")}
    if isinstance(o, (set, tuple)):
        return list(o)
    if isinstance(o, bytes):
        return o.decode("utf-8", errors="replace")
    return str(o)


def _print_json(data, indent=2):
    print(json.dumps(data, ensure_ascii=False, indent=indent, default=_json_default))


def _print_table(rows, headers=None):
    """简易表格输出。"""
    if not rows:
        print("(empty)")
        return
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        headers = headers or list(rows[0].keys())
        lines = []
        widths = [max(len(str(h)), *(len(str(r.get(h, ""))) for r in rows)) for h in headers]
        lines.append("  ".join(str(h).ljust(w) for h, w in zip(headers, widths)))
        lines.append("  ".join("-" * w for w in widths))
        for r in rows:
            lines.append("  ".join(str(r.get(h, "")).ljust(w) for h, w in zip(headers, widths)))
        print("\n".join(lines))
    else:
        for r in rows:
            print(r)


def _table_rows(data, table_key=None):
    """The rows a --table render should show.

    A command's payload is shaped for JSON, and that shape is usually a
    wrapper: ``{"orders": [...], "count": 14}``. This used to hand the wrapper
    itself to _print_table as a single row, so every header lookup missed and
    the output was the header, the separator, and one line of spaces -- for
    any amount of data. Five commands rendered that way (positions, orders,
    trades, tick, kline).

    ``table_key`` names the tabular list inside the wrapper. Without one:
    a dict whose values are ALL dicts is keyed by something (tick is
    ``{code: {...}}``) and its rows are the values; anything else is a single
    flat record and stays one row -- account and instrument really are that,
    and were correct before.
    """
    if table_key and isinstance(data, dict) and table_key in data:
        data = data[table_key]
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and data and all(
            isinstance(value, dict) for value in data.values()):
        return list(data.values())
    return [data]


def _ok(data, table=False, headers=None, table_key=None):
    out = {"ok": True, "data": data, "ts": datetime.now().isoformat(timespec="seconds")}
    if table:
        _print_table(_table_rows(data, table_key), headers)
    else:
        _print_json(out)


def _err(msg, detail=None, code=None):
    out = {"ok": False, "error": msg, "ts": datetime.now().isoformat(timespec="seconds")}
    if detail:
        out["detail"] = detail
    if code:
        out["code"] = code
    _print_json(out)
    sys.exit(1)


def _position_to_dict(p):
    """持仓对象转 dict，补充计算字段。"""
    d = {}
    for attr in [
        "account_id", "stock_code", "stock_name", "volume", "can_use_volume",
        "available_amount", "enable_amount", "avg_price", "price", "open_price",
        "cost_price", "market_value", "frozen_volume", "yesterday_volume",
        "direction",
    ]:
        d[attr] = getattr(p, attr, None)
    vol = d.get("volume") or 0
    price = d.get("price") or 0
    d["market_value"] = d.get("market_value") or round(vol * price, 2)
    d["profit"] = None
    if d.get("avg_price") and vol:
        d["profit"] = round((price - d["avg_price"]) * vol, 2)
        d["profit_pct"] = round((price - d["avg_price"]) / d["avg_price"] * 100, 2) if d["avg_price"] else 0
    return d


def _order_to_dict(o):
    d = {}
    for attr in [
        "account_id", "stock_code", "order_type", "order_status",
        "order_volume", "traded_volume", "price", "order_sysid", "order_id",
        "strategy_name", "order_remark", "order_time",
        # 柜台成交金额 (#173)。旧部署不发它, getattr 兜底成 None ——
        # 这里给 None 而不是 0.0 是有意的: "服务端没告诉我" 和
        # "成交金额确实是 0" 是两回事, 前者该看得出来。
        "trade_amount",
    ]:
        d[attr] = getattr(o, attr, None)
    # 语义化
    d["order_type_name"] = {23: "BUY", 24: "SELL"}.get(d.get("order_type"), str(d.get("order_type", "")))
    status_map = {
        48: "UNREPORTED", 49: "WAIT_REPORTING", 50: "REPORTED",
        51: "REPORTED_CANCEL", 52: "PARTSUCC_CANCEL", 53: "PART_CANCEL",
        54: "CANCELED", 55: "PART_SUCC", 56: "SUCCEEDED", 57: "JUNK", 255: "UNKNOWN",
    }
    d["order_status_name"] = status_map.get(d.get("order_status"), str(d.get("order_status", "")))
    d["cancelable"] = d.get("order_status") in (49, 50, 55)
    return d


def _trade_to_dict(t):
    d = {}
    for attr in [
        "account_id", "stock_code", "order_type", "order_sysid", "order_id",
        "trade_id", "traded_volume", "traded_price", "traded_at", "order_remark",
        # 成交行的策略名 (#174)。服务端一直在发它 —— 实盘只读核对过成交
        # 应答带 strategy_name 这个键, 查询路径的 _attribute_to_strategies
        # 还会把本桥下的单补回名字 —— 只有这里没列, 委托的 _order_to_dict
        # 列了。#174 让人「回调拿不到就用查询兜一下」, 而照做的人用本 CLI
        # 查成交, 看到的却是查询路径也没有策略名。
        # 同 trade_amount: 取不到给 None 而不是 "", 「服务端没发」和
        # 「这笔单没有策略名(手工单)」是两回事。
        "strategy_name",
    ]:
        d[attr] = getattr(t, attr, None)
    d["order_type_name"] = {23: "BUY", 24: "SELL"}.get(d.get("order_type"), str(d.get("order_type", "")))
    return d


# ---------------------------------------------------------------------------
# 延迟初始化的兼容层对象
# ---------------------------------------------------------------------------
_xt_trader = None
_xtdata = None
_acc = None


def _init():
    """延迟初始化 xt_trader / xtdata / acc，避免 import 失败时整个 CLI 崩溃。"""
    global _xt_trader, _xtdata, _acc
    if _xt_trader is not None:
        return _xt_trader, _xtdata, _acc
    try:
        from bigqmt_signal_trader.xtquant_compat import (
            StockAccount, configure, xt_trader, xtdata,
        )
    except ImportError as e:
        _err(
            "无法导入 bigqmt_signal_trader。请确保：\n"
            "  1) 已 pip install xtquant-big-convert，或\n"
            "  2) 仓库 src/ 在 PYTHONPATH 中，或\n"
            "  3) 在仓库目录下运行",
            detail=str(e),
            code="IMPORT_FAIL",
        )
    try:
        configure()
    except Exception as e:
        _err(
            "configure() 失败。请检查配置：\n"
            "  - 环境变量 BIGQMT_ACCOUNT_ID / BIGQMT_REDIS_HOST 等\n"
            "  - 或 bigqmt_signal_trader_client_config.py 配置文件",
            detail=str(e),
            code="CONFIG_FAIL",
        )
    _xt_trader = xt_trader
    _xtdata = xtdata
    try:
        _acc = StockAccount(xt_trader.client.account_id, "STOCK")
    except Exception:
        _acc = None
    return _xt_trader, _xtdata, _acc


def _acc_or(acc_arg):
    """用传入的 account_id 或全局 _acc。"""
    from bigqmt_signal_trader.xtquant_compat import StockAccount
    if acc_arg:
        return StockAccount(acc_arg, "STOCK")
    _, _, acc = _init()
    if acc is None:
        _err("无法确定 account_id，请用 --account 显式指定")
    return acc


# ===========================================================================
# 子命令实现
# ===========================================================================

def cmd_ping(args):
    tr, _, _ = _init()
    t0 = time.time()
    try:
        result = tr.client.call("ping", {})
    except Exception as e:
        _err("ping 失败", detail=str(e), code="PING_FAIL")
    elapsed = round((time.time() - t0) * 1000, 1)
    _ok({"result": result, "latency_ms": elapsed})


def cmd_account(args):
    tr, _, _ = _init()
    acc = _acc_or(args.account)
    try:
        asset = tr.query_stock_asset(acc)
    except Exception as e:
        _err("查询资产失败", detail=str(e), code="QUERY_FAIL")
    if asset is None:
        _err("查询资产返回空")
    d = {
        "account_id": getattr(asset, "account_id", None),
        "cash": getattr(asset, "cash", None),
        "available_cash": getattr(asset, "available_cash", None),
        "frozen_cash": getattr(asset, "frozen_cash", 0),
        "total_asset": getattr(asset, "total_asset", None),
        "market_value": getattr(asset, "market_value", None),
    }
    _ok(d, table=args.table, headers=["account_id", "cash", "frozen_cash", "market_value", "total_asset"])


def cmd_positions(args):
    tr, _, _ = _init()
    acc = _acc_or(args.account)
    try:
        positions = tr.query_stock_positions(acc)
    except Exception as e:
        _err("查询持仓失败", detail=str(e), code="QUERY_FAIL")
    if args.code:
        positions = [p for p in (positions or []) if getattr(p, "stock_code", "") == args.code]
    rows = [_position_to_dict(p) for p in (positions or [])]
    # 汇总
    summary = {
        "count": len(rows),
        "total_market_value": round(sum(r.get("market_value") or 0 for r in rows), 2),
        "total_profit": round(sum(r.get("profit") or 0 for r in rows), 2),
    }
    _ok({"positions": rows, "summary": summary}, table=args.table, table_key="positions",
        headers=["stock_code", "stock_name", "volume", "can_use_volume", "avg_price", "price", "market_value", "profit", "profit_pct"])


def cmd_orders(args):
    tr, _, _ = _init()
    acc = _acc_or(args.account)
    try:
        orders = tr.query_stock_orders(
            acc,
            cancelable_only=args.cancelable,
            strategy_name=args.strategy or "",
        )
    except Exception as e:
        _err("查询委托失败", detail=str(e), code="QUERY_FAIL")
    rows = [_order_to_dict(o) for o in (orders or [])]
    _ok({"orders": rows, "count": len(rows)}, table=args.table, table_key="orders",
        headers=["stock_code", "order_type_name", "order_status_name", "order_volume", "traded_volume", "price", "trade_amount", "order_sysid", "cancelable"])


def cmd_trades(args):
    tr, _, _ = _init()
    acc = _acc_or(args.account)
    try:
        trades = tr.query_stock_trades(acc, strategy_name=args.strategy or "")
    except Exception as e:
        _err("查询成交失败", detail=str(e), code="QUERY_FAIL")
    rows = [_trade_to_dict(t) for t in (trades or [])]
    _ok({"trades": rows, "count": len(rows)}, table=args.table, table_key="trades",
        headers=["stock_code", "order_type_name", "traded_volume", "traded_price", "traded_at", "order_sysid"])


def cmd_tick(args):
    _, xtdata, _ = _init()
    codes = args.codes
    if not codes:
        _err("请指定股票代码，如: tick 600000.SH 000001.SZ")
    try:
        ticks = xtdata.get_full_tick(codes)
    except Exception as e:
        _err("查询行情失败", detail=str(e), code="QUERY_FAIL")
    # 精简输出
    result = {}
    for code, tick in (ticks or {}).items():
        t = dict(tick) if hasattr(tick, "items") else {}
        # 只保留关键字段
        compact = {
            "code": code,
            "lastPrice": t.get("lastPrice"),
            "open": t.get("open"),
            "high": t.get("high"),
            "low": t.get("low"),
            "lastClose": t.get("lastClose"),
            "volume": t.get("volume"),
            "amount": t.get("amount"),
            "bidPrice": (t.get("bidPrice") or [])[:5],
            "bidVol": (t.get("bidVol") or [])[:5],
            "askPrice": (t.get("askPrice") or [])[:5],
            "askVol": (t.get("askVol") or [])[:5],
            "time": t.get("time"),
            "stime": t.get("stime"),
        }
        # 涨跌幅
        if t.get("lastClose") and t.get("lastPrice"):
            compact["change_pct"] = round(
                (t["lastPrice"] - t["lastClose"]) / t["lastClose"] * 100, 2
            )
        result[code] = compact
    _ok(result, table=args.table,
        headers=["code", "lastPrice", "change_pct", "bidPrice", "askPrice", "volume"])


def cmd_kline(args):
    _, xtdata, _ = _init()
    fields = args.fields.split(",") if args.fields else None
    try:
        result = xtdata.get_market_data_ex(
            field_list=fields,
            stock_list=[args.code],
            period=args.period,
            start_time=args.start or "",
            end_time=args.end or "",
            count=args.count,
            dividend_type=args.dividend,
            fill_data=not args.no_fill,
        )
    except Exception as e:
        _err("查询 K 线失败", detail=str(e), code="QUERY_FAIL")
    if not result or args.code not in result:
        _err("未获取到 K 线数据", code="NO_DATA")
    df = result[args.code]
    if df is None or len(df) == 0:
        _err("K 线数据为空")
    # 转为 list of dict
    records = df.reset_index().to_dict(orient="records")
    # 精简大数字字段
    for r in records:
        for k, v in list(r.items()):
            if hasattr(v, "item"):
                r[k] = v.item()
    # 统计
    closes = [r.get("close") for r in records if r.get("close") is not None]
    stats = {}
    if closes:
        stats["count"] = len(closes)
        stats["first_close"] = closes[0]
        stats["last_close"] = closes[-1]
        # high/low 要用 K 线的 high/low 字段，closes 的极值不是最高价/最低价
        highs = [r.get("high") for r in records if r.get("high") is not None]
        lows = [r.get("low") for r in records if r.get("low") is not None]
        stats["high"] = max(highs) if highs else max(closes)
        stats["low"] = min(lows) if lows else min(closes)
        stats["change_pct"] = round((closes[-1] - closes[0]) / closes[0] * 100, 2) if closes[0] else None
        # 简单均线
        if len(closes) >= 5:
            stats["ma5"] = round(sum(closes[-5:]) / 5, 3)
        if len(closes) >= 20:
            stats["ma20"] = round(sum(closes[-20:]) / 20, 3)
        if len(closes) >= 60:
            stats["ma60"] = round(sum(closes[-60:]) / 60, 3)
    _ok({"code": args.code, "period": args.period, "bars": records, "stats": stats},
        table=args.table, table_key="bars",
        headers=["time", "open", "high", "low", "close", "volume"])


def cmd_instrument(args):
    _, xtdata, _ = _init()
    try:
        detail = xtdata.get_instrument_detail(args.code)
    except Exception as e:
        _err("查询合约详情失败", detail=str(e), code="QUERY_FAIL")
    if detail is None:
        _err("未找到合约: %s" % args.code)
    _ok(detail, table=args.table)


def cmd_option_greeks(args):
    """Calculate one contract or a whole expiry locally from QMT market data."""
    _, xtdata, _ = _init()
    try:
        common = {
            "as_of": args.as_of,
            "risk_free_rate": args.risk_free,
            "dividend_yield": args.dividend,
            "price_period": args.period,
        }
        if args.expiry:
            result = xtdata.get_option_chain_analytics(
                args.code,
                args.expiry,
                opttype=args.option_type or "",
                isavailavle=args.available,
                underlying_price=args.underlying_price,
                **common
            )
        else:
            result = xtdata.get_option_analytics(
                args.code,
                option_price=args.option_price,
                underlying_price=args.underlying_price,
                include_native_iv=args.native_iv,
                **common
            )
    except Exception as e:
        _err("期权 IV/Greeks 计算失败", detail=str(e), code="QUERY_FAIL")
    _ok(result, table=args.table)


def cmd_sector(args):
    _, xtdata, _ = _init()
    if args.name:
        try:
            stocks = xtdata.get_stock_list_in_sector(args.name)
        except Exception as e:
            _err("查询板块成分股失败", detail=str(e), code="QUERY_FAIL")
        _ok({"sector": args.name, "count": len(stocks or []), "stocks": stocks or []})
    else:
        try:
            sectors = xtdata.get_sector_list()
        except Exception as e:
            _err("查询板块列表失败", detail=str(e), code="QUERY_FAIL")
        _ok({"sectors": sectors or []})


def cmd_trading_dates(args):
    _, xtdata, _ = _init()
    try:
        dates = xtdata.get_trading_dates(
            market=args.market,
            start_time=args.start or "",
            end_time=args.end or "",
            count=args.count if args.count > 0 else -1,
        )
    except Exception as e:
        _err("查询交易日历失败", detail=str(e), code="QUERY_FAIL")
    _ok({"dates": dates or []})


def cmd_north(args):
    _, xtdata, _ = _init()
    try:
        data = xtdata.get_north_finance_change(period=args.period or "1d")
    except Exception as e:
        _err("查询北向资金失败", detail=str(e), code="QUERY_FAIL")
    # data 可能是 dict[code -> DataFrame]
    if isinstance(data, dict):
        result = {}
        for k, v in data.items():
            if hasattr(v, "to_dict"):
                result[k] = v.reset_index().to_dict(orient="records")
            else:
                result[k] = v
    elif hasattr(data, "to_dict"):
        result = data.reset_index().to_dict(orient="records")
    else:
        result = data
    _ok({"north_finance": result})


def cmd_longhubang(args):
    _, xtdata, _ = _init()
    try:
        df = xtdata.get_longhubang(
            stock_list=[args.code],
            start_time=args.start or "",
            end_time=args.end or "",
            count=args.count if args.count > 0 else 5,
        )
    except Exception as e:
        _err("查询龙虎榜失败", detail=str(e), code="QUERY_FAIL")
    if df is None:
        _err("龙虎榜数据为空")
    if hasattr(df, "to_dict"):
        records = df.reset_index().to_dict(orient="records")
    else:
        records = df
    _ok({"longhubang": records})


def cmd_financial(args):
    _, xtdata, _ = _init()
    tables = args.tables.split(",") if args.tables else ["Capital.CAPITAL"]
    try:
        data = xtdata.get_financial_data(
            stock_list=args.codes,
            table_list=tables,
            start_time=args.start or "",
            end_time=args.end or "",
        )
    except Exception as e:
        _err("查询财务数据失败", detail=str(e), code="QUERY_FAIL")
    result = {}
    for code, df in (data or {}).items():
        if hasattr(df, "to_dict"):
            result[code] = df.reset_index().to_dict(orient="records")
        else:
            result[code] = df
    _ok({"financial": result})


def cmd_download(args):
    _, xtdata, _ = _init()
    try:
        result = xtdata.download_history_data2(
            stock_list=args.codes,
            period=args.period,
            start_time=args.start or "",
            end_time=args.end or "",
            dividend_type=args.dividend,
        )
    except Exception as e:
        _err("下载数据失败", detail=str(e), code="DOWNLOAD_FAIL")
    _ok({"download_result": result})


def _place_order(args, action):
    """下单通用逻辑。action = 'BUY' 或 'SELL'。"""
    tr, _, _ = _init()
    acc = _acc_or(args.account)
    from bigqmt_signal_trader.xtquant_compat import (
        STOCK_BUY, STOCK_SELL, FIX_PRICE, LATEST_PRICE,
    )
    order_type = STOCK_BUY if action == "BUY" else STOCK_SELL
    if args.latest:
        price_type = LATEST_PRICE
        price = 0.0
    else:
        if args.price is None:
            _err("限价单必须指定 --price，或用 --latest 使用最新价")
        price_type = FIX_PRICE
        price = args.price
    strategy = args.strategy or "llm_agent"
    remark = args.remark or "llm_%s_%d" % (action.lower(), int(time.time()))
    # 干跑模式：_ok 只打印不退出，必须 return，否则会穿透到真实下单
    if args.dry_run:
        _ok({
            "dry_run": True,
            "action": action,
            "stock_code": args.code,
            "volume": args.volume,
            "price": price,
            "price_type": "LATEST" if args.latest else "LIMIT",
            "strategy_name": strategy,
            "order_remark": remark,
        })
        return
    # 真实下单
    try:
        order_id = tr.order_stock(
            acc, args.code, order_type, args.volume,
            price_type, price, strategy, remark,
        )
    except PermissionError as e:
        _err("下单被拒绝：服务端未开启下单权限（rpc_allow_order_methods=False）", detail=str(e), code="ORDER_DISABLED")
    except TimeoutError as e:
        _err(
            "下单超时——委托可能已提交但未收到响应。请先用 query_orders 确认，避免重复下单",
            detail=str(e), code="ORDER_TIMEOUT",
        )
    except Exception as e:
        _err("下单失败", detail=str(e), code="ORDER_FAIL")
    if order_id == -1:
        _err("下单返回 -1（失败），请检查：1) 账户权限 2) 价格范围 3) QMT 风控", code="ORDER_REJECTED")
    # 等 0.5 秒后查委托确认
    time.sleep(0.5)
    try:
        orders = tr.query_stock_orders(acc, strategy_name=strategy)
    except Exception:
        orders = None
    placed_order = None
    if orders:
        for o in orders:
            if getattr(o, "order_remark", "") == remark or getattr(o, "order_sysid", "") == str(order_id):
                placed_order = _order_to_dict(o)
                break
    _ok({
        "order_sys_id": str(order_id),
        "action": action,
        "stock_code": args.code,
        "volume": args.volume,
        "price": price,
        "strategy_name": strategy,
        "order_remark": remark,
        "confirmed_order": placed_order,
    })


def cmd_buy(args):
    _place_order(args, "BUY")


def cmd_sell(args):
    _place_order(args, "SELL")


def cmd_cancel(args):
    tr, _, _ = _init()
    acc = _acc_or(args.account)
    if args.dry_run:
        _ok({"dry_run": True, "order_sysid": args.order_id, "market": args.market or ""})
        return
    try:
        rc = tr.cancel_order_stock_sysid(acc, args.market or "", args.order_id)
    except Exception as e:
        _err("撤单失败", detail=str(e), code="CANCEL_FAIL")
    # MiniQMT 契约：0=成功，-1=失败（issue #113）。bool(rc) 会把含义颠倒过来。
    if rc != 0:
        _err(
            "撤单被拒绝（返回 %s）。委托可能已成/已撤/不存在——先用 orders 确认实际状态，"
            "注意 issue #151：撤不存在的委托也可能返回成功" % rc,
            code="CANCEL_REJECTED",
        )
    # 写完必须回读：撤单返回值只代表「请求发出去了」，不代表「撤成了」
    time.sleep(0.5)
    confirmed = None
    try:
        orders = tr.query_stock_orders(acc, strategy_name="")
        for o in orders or []:
            if str(getattr(o, "order_sysid", "")) == str(args.order_id):
                confirmed = _order_to_dict(o)
                break
    except Exception:
        pass
    _ok({
        "order_sysid": args.order_id,
        "market": args.market or "",
        "success": True,
        "confirmed_order": confirmed,
    })


def cmd_snapshot(args):
    """一键快照：资产+持仓+今日委托+今日成交，一次返回。"""
    tr, _, _ = _init()
    acc = _acc_or(args.account)
    result = {}
    # 资产
    try:
        asset = tr.query_stock_asset(acc)
        if asset:
            result["asset"] = {
                "account_id": getattr(asset, "account_id", None),
                "cash": getattr(asset, "cash", None),
                "frozen_cash": getattr(asset, "frozen_cash", 0),
                "total_asset": getattr(asset, "total_asset", None),
                "market_value": getattr(asset, "market_value", None),
            }
    except Exception as e:
        result["asset_error"] = str(e)
    # 持仓
    try:
        positions = tr.query_stock_positions(acc)
        result["positions"] = [_position_to_dict(p) for p in (positions or [])]
        result["position_count"] = len(result["positions"])
    except Exception as e:
        result["position_error"] = str(e)
    # 委托
    try:
        orders = tr.query_stock_orders(acc, strategy_name="")
        result["orders"] = [_order_to_dict(o) for o in (orders or [])]
        result["order_count"] = len(result["orders"])
    except Exception as e:
        result["order_error"] = str(e)
    # 成交
    try:
        trades = tr.query_stock_trades(acc, strategy_name="")
        result["trades"] = [_trade_to_dict(t) for t in (trades or [])]
        result["trade_count"] = len(result["trades"])
    except Exception as e:
        result["trade_error"] = str(e)
    _ok(result, table=args.table)


def cmd_rpc(args):
    """通用 RPC 调用入口：任意白名单方法 + JSON 参数。

    用于调用没有专用子命令的 API（如 get_holidays、get_sector_info、
    get_hkt_statistics、bsm_price 等）。

    用法：
      python qmt.py rpc get_holidays
      python qmt.py rpc get_stock_name '{"stock":"600000.SH"}'
      python qmt.py rpc bsm_price '{"opt_type":"C","target_price":3.0,"strike_price":2.8,"risk_free":0.03,"sigma":0.3,"days":30}'
    """
    tr, _, _ = _init()
    params = {}
    if args.params:
        try:
            params = json.loads(args.params)
            if not isinstance(params, dict):
                _err("params 必须是 JSON 对象（如 '{\"key\":\"value\"}'）", code="PARAM_ERROR")
        except json.JSONDecodeError as e:
            _err("params JSON 解析失败", detail=str(e), code="PARAM_ERROR")
    try:
        result = tr.client.call(args.method, params)
    except Exception as e:
        _err("RPC 调用失败: %s" % args.method, detail=str(e), code="RPC_FAIL")
    _ok({"method": args.method, "result": result}, table=args.table)


def cmd_quote_subscribe(args):
    """订阅全推行情（打印前 N 条后退出）。"""
    _, xtdata, _ = _init()
    received = []
    sub_id = None  # 首帧快照可能在 subscribe 返回前就推给回调，此时 sub_id 还没绑上

    def on_quote(data):
        for code, tick in (data or {}).items():
            entry = {
                "code": code,
                "lastPrice": tick.get("lastPrice"),
                "volume": tick.get("volume"),
                "time": tick.get("time"),
            }
            received.append(entry)
            print(json.dumps(entry, ensure_ascii=False))
        if len(received) >= args.max and sub_id is not None:
            xtdata.unsubscribe_quote(sub_id)
            # 给一点时间让退订生效
            time.sleep(0.5)
            os._exit(0)

    try:
        sub_id = xtdata.subscribe_whole_quote(args.codes, callback=on_quote)
    except Exception as e:
        _err("订阅行情失败", detail=str(e), code="SUBSCRIBE_FAIL")
    print("# subscribed id=%s, waiting for quotes (max %d)..." % (sub_id, args.max), file=sys.stderr)
    # 等待
    timeout = args.timeout
    t0 = time.time()
    try:
        while time.time() - t0 < timeout:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    try:
        xtdata.unsubscribe_quote(sub_id)
    except Exception:
        pass
    _ok({"sub_id": sub_id, "received": received, "count": len(received)})


# ===========================================================================
# argparse 路由
# ===========================================================================

def build_parser():
    p = argparse.ArgumentParser(
        prog="qmt.py",
        description="QMT 交易/行情统一 CLI（给大模型用）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--account", default=None, help="指定账号 ID（覆盖配置）")
    p.add_argument("--table", action="store_true", help="输出表格而非 JSON")
    sub = p.add_subparsers(dest="command", required=True)

    # ping
    sub.add_parser("ping", help="连通性检测").set_defaults(func=cmd_ping)

    # account
    sub.add_parser("account", help="查询账户资产").set_defaults(func=cmd_account)

    # positions
    sp = sub.add_parser("positions", help="查询持仓")
    sp.add_argument("code", nargs="?", default=None, help="可选：只查指定股票")
    sp.set_defaults(func=cmd_positions)

    # orders
    sp = sub.add_parser("orders", help="查询今日委托")
    sp.add_argument("--cancelable", action="store_true", help="只查可撤委托")
    sp.add_argument("--strategy", default=None, help="按策略名过滤（空=全部）")
    sp.set_defaults(func=cmd_orders)

    # trades
    sp = sub.add_parser("trades", help="查询今日成交")
    sp.add_argument("--strategy", default=None, help="按策略名过滤")
    sp.set_defaults(func=cmd_trades)

    # tick
    sp = sub.add_parser("tick", help="实时五档盘口")
    sp.add_argument("codes", nargs="+", help="股票代码，如 600000.SH 000001.SZ")
    sp.set_defaults(func=cmd_tick)

    # kline
    sp = sub.add_parser("kline", help="K线/历史行情")
    sp.add_argument("code", help="股票代码")
    sp.add_argument("--period", default="1d", help="周期: 1d/1m/5m/15m/30m/60m/tick")
    sp.add_argument("--count", type=int, default=-1, help="获取根数（-1=全部）")
    sp.add_argument("--start", default=None, help="开始日期 YYYYMMDD")
    sp.add_argument("--end", default=None, help="结束日期 YYYYMMDD")
    sp.add_argument("--fields", default=None, help="字段逗号分隔，如 close,open,volume")
    sp.add_argument("--dividend", default="none", help="复权: none/front/back")
    sp.add_argument("--no-fill", action="store_true", help="不填充缺失数据")
    sp.set_defaults(func=cmd_kline)

    # instrument
    sp = sub.add_parser("instrument", help="合约详情")
    sp.add_argument("code", help="股票代码")
    sp.set_defaults(func=cmd_instrument)

    # option-greeks: one option code, or underlying + --expiry for a chain.
    sp = sub.add_parser("option-greeks", help="本地计算期权 IV 和标准 Greeks")
    sp.add_argument("code", help="期权代码；使用 --expiry 时传标的代码")
    sp.add_argument("--expiry", default=None, help="到期月份/日期；提供后计算整条期权链")
    sp.add_argument("--option-type", default="", help="期权链筛选 C/P")
    sp.add_argument("--option-price", type=float, default=None, help="单合约期权价格（默认最新 close）")
    sp.add_argument("--underlying-price", type=float, default=None, help="标的价格（默认最新 close）")
    sp.add_argument("--risk-free", type=float, default=None, help="无风险利率小数（默认合约元数据）")
    sp.add_argument("--dividend", type=float, default=0.0, help="连续分红率小数")
    sp.add_argument("--as-of", default=None, help="估值时点 YYYY-MM-DD HH:MM:SS")
    sp.add_argument("--period", default="1m", help="缺省价格所用 K 线周期")
    sp.add_argument("--available", action="store_true", help="期权链只取可用合约")
    sp.add_argument("--native-iv", action="store_true", help="单合约同时返回 QMT 原生 IV 作对照")
    sp.set_defaults(func=cmd_option_greeks)

    # sector
    sp = sub.add_parser("sector", help="板块查询")
    sp.add_argument("name", nargs="?", default=None, help="板块名（不填则列板块）")
    sp.set_defaults(func=cmd_sector)

    # trading-dates
    sp = sub.add_parser("trading-dates", help="交易日历")
    sp.add_argument("--market", default="SH", help="市场 SH/SZ")
    sp.add_argument("--count", type=int, default=10, help="获取天数")
    sp.add_argument("--start", default=None)
    sp.add_argument("--end", default=None)
    sp.set_defaults(func=cmd_trading_dates)

    # north
    sp = sub.add_parser("north", help="北向资金")
    sp.add_argument("--period", default="1d")
    sp.set_defaults(func=cmd_north)

    # longhubang
    sp = sub.add_parser("longhubang", help="龙虎榜")
    sp.add_argument("code", help="股票代码")
    sp.add_argument("--count", type=int, default=5)
    sp.add_argument("--start", default=None)
    sp.add_argument("--end", default=None)
    sp.set_defaults(func=cmd_longhubang)

    # financial
    sp = sub.add_parser("financial", help="财务数据")
    sp.add_argument("codes", nargs="+", help="股票代码")
    sp.add_argument("--tables", default=None, help="表名逗号分隔，如 Capital.CAPITAL,Performance.EXPRESS")
    sp.add_argument("--start", default=None)
    sp.add_argument("--end", default=None)
    sp.set_defaults(func=cmd_financial)

    # download
    sp = sub.add_parser("download", help="下载历史数据到服务端")
    sp.add_argument("codes", nargs="+", help="股票代码")
    sp.add_argument("--period", default="1d")
    sp.add_argument("--start", default=None)
    sp.add_argument("--end", default=None)
    sp.add_argument("--dividend", default="none")
    sp.set_defaults(func=cmd_download)

    # buy
    sp = sub.add_parser("buy", help="买入下单")
    sp.add_argument("code", help="股票代码")
    sp.add_argument("volume", type=int, help="委托数量（股）")
    sp.add_argument("--price", type=float, default=None, help="限价单价格")
    sp.add_argument("--latest", action="store_true", help="用最新价下单")
    sp.add_argument("--strategy", default=None, help="策略名")
    sp.add_argument("--remark", default=None, help="委托备注/user_order_id")
    sp.add_argument("--dry-run", action="store_true", help="只打印不下单")
    sp.set_defaults(func=cmd_buy)

    # sell
    sp = sub.add_parser("sell", help="卖出下单")
    sp.add_argument("code", help="股票代码")
    sp.add_argument("volume", type=int, help="委托数量（股）")
    sp.add_argument("--price", type=float, default=None, help="限价单价格")
    sp.add_argument("--latest", action="store_true", help="用最新价下单")
    sp.add_argument("--strategy", default=None, help="策略名")
    sp.add_argument("--remark", default=None, help="委托备注/user_order_id")
    sp.add_argument("--dry-run", action="store_true", help="只打印不下单")
    sp.set_defaults(func=cmd_sell)

    # cancel
    sp = sub.add_parser("cancel", help="撤单")
    sp.add_argument("order_id", help="委托号 order_sysid")
    sp.add_argument("--market", default=None, help="市场 SH/SZ")
    sp.add_argument("--dry-run", action="store_true", help="只打印不撤单")
    sp.set_defaults(func=cmd_cancel)

    # snapshot
    sub.add_parser("snapshot", help="一键快照：资产+持仓+委托+成交").set_defaults(func=cmd_snapshot)

    # rpc — 通用 RPC 调用（兜底所有白名单方法）
    sp = sub.add_parser("rpc", help="通用 RPC 调用（任意白名单方法 + JSON 参数）")
    sp.add_argument("method", help="方法名，如 get_holidays / get_stock_name / bsm_price")
    sp.add_argument("params", nargs="?", default=None, help='JSON 参数，如 \'{"stock":"600000.SH"}\'')
    sp.set_defaults(func=cmd_rpc)

    # ---- 高频快捷命令（转发到 xtdata 对应方法） ----
    def _quick(name, help_text, method, params_builder):
        def make(args):
            tr, xtdata, _ = _init()
            params = params_builder(args)
            try:
                fn = getattr(xtdata, method)
                result = fn(**params) if isinstance(params, dict) else fn(*params)
            except AttributeError:
                # 方法不存在时回退到 RPC 调用
                result = tr.client.call(method, params if isinstance(params, dict) else {})
            except Exception as e:
                _err("%s 失败" % name, detail=str(e), code="QUERY_FAIL")
            _ok({"result": result}, table=getattr(args, "table", False))
        sp2 = sub.add_parser(name, help=help_text)
        sp2.add_argument("args", nargs="*", help="位置参数（按方法签名顺序）")
        sp2.set_defaults(func=make)
        return sp2

    # 节假日
    _quick("holiday", "节假日列表", "get_holidays", lambda a: {})
    # 股票名称
    _quick("stock-name", "股票名称", "get_stock_name", lambda a: {"stock": a.args[0] if a.args else _err("需传股票代码")})
    # 品种类型
    _quick("instrument-type", "品种类型(stock/fund/etf/bond/index)", "get_instrument_type", lambda a: {"stock_code": a.args[0] if a.args else _err("需传代码")})
    # 除权除息因子
    _quick("divid-factors", "除权除息因子", "get_divid_factors", lambda a: {"stock_code": a.args[0] if a.args else _err("需传代码"), "start_time": a.args[1] if len(a.args) > 1 else "", "end_time": a.args[2] if len(a.args) > 2 else ""})
    # 交易时段
    _quick("market-times", "日内交易时段", "get_trade_times", lambda a: {"stockcode": a.args[0] if a.args else "SH"})
    # 交易日历（含时段）
    _quick("trading-calendar", "交易日历(含时段)", "get_trading_calendar", lambda a: {"market": a.args[0] if a.args else "SH", "start_time": a.args[1] if len(a.args) > 1 else "", "end_time": a.args[2] if len(a.args) > 2 else ""})
    # 期权列表
    _quick("option-list", "期权列表", "get_option_list", lambda a: {"undl_code": a.args[0] if a.args else _err("需传标的代码"), "dedate": a.args[1] if len(a.args) > 1 else ""})
    # BSM 期权定价
    _quick("bsm-price", "BSM 期权定价", "bsm_price", lambda a: {"opt_type": a.args[0] if a.args else "C", "target_price": float(a.args[1]) if len(a.args) > 1 else 3.0, "strike_price": float(a.args[2]) if len(a.args) > 2 else 2.8, "risk_free": float(a.args[3]) if len(a.args) > 3 else 0.03, "sigma": float(a.args[4]) if len(a.args) > 4 else 0.3, "days": int(a.args[5]) if len(a.args) > 5 else 30})
    # BSM 隐含波动率
    _quick("bsm-iv", "BSM 隐含波动率", "bsm_iv", lambda a: {"opt_type": a.args[0] if a.args else "C", "target_price": float(a.args[1]) if len(a.args) > 1 else 3.0, "strike_price": float(a.args[2]) if len(a.args) > 2 else 2.8, "option_price": float(a.args[3]) if len(a.args) > 3 else 0.25, "risk_free": float(a.args[4]) if len(a.args) > 4 else 0.03, "days": int(a.args[5]) if len(a.args) > 5 else 30})
    # 港股通统计
    _quick("hkt-stats", "港股通统计", "get_hkt_statistics", lambda a: {"stock_code": a.args[0] if a.args else _err("需传代码")})
    # 港股通明细
    _quick("hkt-details", "港股通明细", "get_hkt_details", lambda a: {"stock_code": a.args[0] if a.args else _err("需传代码")})
    # 港股通汇率
    _quick("hkt-rate", "港股通汇率", "get_hkt_exchange_rate", lambda a: {})
    # 十大股东
    _quick("top10-holder", "十大股东", "get_top10_share_holder", lambda a: {"stock_list": [a.args[0] if a.args else _err("需传代码")], "data_name": "holder", "start_time": a.args[1] if len(a.args) > 1 else "", "end_time": a.args[2] if len(a.args) > 2 else ""})
    # 股东户数
    _quick("holder-num", "股东户数", "get_holder_num", lambda a: {"stock_list": [a.args[0] if a.args else _err("需传代码")]})
    # 新股数据
    _quick("ipo", "新股数据", "get_ipo_data", lambda a: {})
    # 新股申购额度
    _quick("ipo-limit", "新股申购额度", "get_new_purchase_limit", lambda a: {})
    # 融资融券担保品
    _quick("credit-assure", "融资融券担保品合约", "get_assure_contract", lambda a: {})
    # 融资融券融券标的
    _quick("credit-short", "融券标的合约", "get_enable_short_contract", lambda a: {})
    # 负债合约
    _quick("credit-debt", "负债合约", "get_debt_contract", lambda a: {})
    # 历史 ST
    _quick("his-st", "历史 ST 数据", "get_his_st_data", lambda a: {"stock_code": a.args[0] if a.args else _err("需传代码")})
    # 指数权重
    _quick("index-weight", "指数权重", "get_index_weight", lambda a: {"index_code": a.args[0] if a.args else _err("需传指数代码")})
    # 行业
    _quick("industry", "行业成分", "get_industry", lambda a: {"industry_name": a.args[0] if a.args else _err("需传行业名")})
    # 板块信息
    _quick("sector-info", "板块详情", "get_sector_info", lambda a: {"sector_name": a.args[0] if a.args else ""})
    # 时间转换
    _quick("timetag2dt", "毫秒时间戳转日期", "timetag_to_datetime", lambda a: {"timetag": int(a.args[0]) if a.args else _err("需传毫秒时间戳"), "format": a.args[1] if len(a.args) > 1 else "%Y%m%d %H:%M:%S"})
    _quick("dt2timetag", "日期转毫秒时间戳", "datetime_to_timetag", lambda a: {"datetime_str": a.args[0] if a.args else _err("需传日期字符串"), "format": a.args[1] if len(a.args) > 1 else "%Y%m%d%H%M%S"})
    # 本地数据（缓存）
    _quick("local-data", "本地缓存数据", "get_local_data", lambda a: {"field_list": ["close"], "stock_list": [a.args[0] if a.args else _err("需传代码")], "period": a.args[1] if len(a.args) > 1 else "1d", "count": -1})

    # quote-subscribe
    sp = sub.add_parser("quote-subscribe", help="订阅全推行情（实时推送）")
    sp.add_argument("codes", nargs="+", help="代码或市场，如 SH SZ 600000.SH")
    sp.add_argument("--max", type=int, default=10, help="收到 N 条后退出")
    sp.add_argument("--timeout", type=int, default=30, help="超时秒数")
    sp.set_defaults(func=cmd_quote_subscribe)

    # argparse 只认子命令之前的全局 flag，但 `qmt.py account --table` 才是
    # 自然写法。给每个子命令也挂上这两个 flag：default=SUPPRESS 保证未提供时
    # 不覆盖顶层解析到的值。
    for subp in sub.choices.values():
        subp.add_argument("--account", default=argparse.SUPPRESS,
                          help="指定账号 ID（覆盖配置）")
        subp.add_argument("--table", action="store_true", default=argparse.SUPPRESS,
                          help="输出表格而非 JSON")

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(1)
    try:
        args.func(args)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("\n(interrupted)", file=sys.stderr)
        sys.exit(130)
    except Exception as e:
        _err("未预期错误", detail=traceback.format_exc(), code="UNEXPECTED")


if __name__ == "__main__":
    main()
