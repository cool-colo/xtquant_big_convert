# coding: utf-8
"""Big QMT redis-bridge CLI client (Jianghai Securities deployment).

External-python entry point for querying assets/positions/orders/trades and
market data through the xtquant_big_convert RPC bridge, plus order submit and
cancel. Usage examples:

    python qmt_cli.py discover
    python qmt_cli.py ping
    python qmt_cli.py asset
    python qmt_cli.py positions
    python qmt_cli.py orders
    python qmt_cli.py trades
    python qmt_cli.py tick 000001.SZ 600519.SH
    python qmt_cli.py kline 000001.SZ --period 1d --count 5
    python qmt_cli.py buy 000001.SZ 100 10.50
    python qmt_cli.py sell 000001.SZ 100 11.00
    python qmt_cli.py cancel 1234
    python qmt_cli.py watch

The account id is read from bigqmt_signal_trader_client_config.BIGQMT_ACCOUNT_ID,
or from --account / BIGQMT_ACCOUNT_ID env var. `discover` scans Redis for the
account id the QMT-side strategy registered.
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import bigqmt_signal_trader_client_config as cfg  # noqa: E402
except ImportError:  # config not written yet (e.g. pre-deploy discover)
    cfg = None

from bigqmt_signal_trader.xtquant_compat import (  # noqa: E402
    BigQmtRpcClient,
    BigQmtXtTrader,
    BigQmtXtData,
)
from xtquant import xtconstant  # noqa: E402
from xtquant.xttype import StockAccount  # noqa: E402
import redis  # noqa: E402


def account_id(args):
    value = (
        getattr(args, "account", "")
        or os.environ.get("BIGQMT_ACCOUNT_ID", "")
        or getattr(cfg, "BIGQMT_ACCOUNT_ID", "")
    )
    value = str(value or "").strip()
    if not value:
        sys.exit(
            "account id is empty: set BIGQMT_ACCOUNT_ID in "
            "bigqmt_signal_trader_client_config.py, or pass --account, "
            "or export BIGQMT_ACCOUNT_ID"
        )
    return value


def redis_kwargs():
    if cfg is None:
        sys.exit(
            "bigqmt_signal_trader_client_config.py not found next to this "
            "script -- run deploy_qmt_bridge.ps1 first, or create it with "
            "BIGQMT_REDIS_CONFIG (see deploy/README.md)"
        )
    return dict(cfg.BIGQMT_REDIS_CONFIG)


def raw_redis():
    kw = redis_kwargs()
    kw.pop("transport", None)
    kw.pop("zmq", None)
    username = kw.get("username") or None
    kw["username"] = username
    return redis.Redis(**kw)


def make_trader(acc_id):
    return BigQmtXtTrader(
        account_id=acc_id,
        redis_config=redis_kwargs(),
        timeout_seconds=getattr(cfg, "BIGQMT_RPC_TIMEOUT_SECONDS", 30.0),
    )


def make_data(acc_id):
    client = BigQmtRpcClient(
        account_id=acc_id,
        redis_config=redis_kwargs(),
        timeout_seconds=getattr(cfg, "BIGQMT_RPC_TIMEOUT_SECONDS", 30.0),
    )
    return BigQmtXtData(client)


def as_stock_account(acc_id):
    account_type = str(getattr(cfg, "BIGQMT_ACCOUNT_TYPE", "STOCK") or "STOCK")
    return StockAccount(acc_id, account_type)


def fmt_row(obj, fields):
    out = {}
    for name in fields:
        value = getattr(obj, name, None)
        if isinstance(value, float):
            value = round(value, 4)
        out[name] = value
    return out


def cmd_discover(args):
    r = raw_redis()
    keys = [k.decode() if isinstance(k, bytes) else k for k in r.keys("bigqmt:*")]
    if not keys:
        print("no bigqmt:* keys in redis db=%s -- is the QMT strategy running?" % r.connection_pool.connection_kwargs.get("db"))
        return
    accounts = set()
    for key in keys:
        for prefix in ("bigqmt:positions:", "bigqmt:rpc:req:", "bigqmt:order_events:", "bigqmt:trade_events:"):
            if key.startswith(prefix):
                accounts.add(key[len(prefix):])
    print("redis keys:")
    for key in sorted(keys):
        print("  ", key)
    print("discovered accounts:", sorted(accounts) or "(none yet)")


def cmd_ping(args):
    acc_id = account_id(args)
    trader = make_trader(acc_id)
    started = time.time()
    pong = trader.client.call("ping")
    elapsed_ms = (time.time() - started) * 1000.0
    print("ping OK in %.1f ms" % elapsed_ms)
    print(json.dumps(pong, ensure_ascii=False, indent=2, default=str))


def cmd_asset(args):
    acc_id = account_id(args)
    trader = make_trader(acc_id)
    asset = trader.query_stock_asset(as_stock_account(acc_id))
    if asset is None:
        print("query_stock_asset returned None")
        return
    print(json.dumps(fmt_row(asset, [
        "account_id", "account_type", "cash", "frozen_cash",
        "market_value", "total_asset",
    ]), ensure_ascii=False, indent=2, default=str))


def cmd_positions(args):
    acc_id = account_id(args)
    trader = make_trader(acc_id)
    positions = trader.query_stock_positions(as_stock_account(acc_id)) or []
    print("positions: %d" % len(positions))
    for pos in positions:
        print(json.dumps(fmt_row(pos, [
            "stock_code", "volume", "can_use_volume", "open_price",
            "market_value", "on_road_volume", "yesterday_volume",
        ]), ensure_ascii=False, default=str))


def cmd_orders(args):
    acc_id = account_id(args)
    trader = make_trader(acc_id)
    orders = trader.query_stock_orders(
        as_stock_account(acc_id), cancelable_only=args.cancelable
    ) or []
    print("orders: %d" % len(orders))
    for order in orders:
        print(json.dumps(fmt_row(order, [
            "order_id", "stock_code", "order_type", "order_volume",
            "price", "traded_volume", "order_status", "strategy_name",
            "order_time", "order_remark",
        ]), ensure_ascii=False, default=str))


def cmd_trades(args):
    acc_id = account_id(args)
    trader = make_trader(acc_id)
    trades = trader.query_stock_trades(as_stock_account(acc_id)) or []
    print("trades: %d" % len(trades))
    for trade in trades:
        print(json.dumps(fmt_row(trade, [
            "stock_code", "order_type", "traded_volume", "traded_price",
            "traded_amount", "traded_time", "order_id",
        ]), ensure_ascii=False, default=str))


def cmd_tick(args):
    acc_id = account_id(args)
    data = make_data(acc_id)
    ticks = data.get_full_tick(args.codes) or {}
    for code in args.codes:
        tick = ticks.get(code)
        if not tick:
            print("%s: no data" % code)
            continue
        print(json.dumps({
            "code": code,
            "lastPrice": tick.get("lastPrice"),
            "time": tick.get("time"),
            "bidPrice": tick.get("bidPrice"),
            "askPrice": tick.get("askPrice"),
            "pvolume": tick.get("pvolume"),
        }, ensure_ascii=False, default=str))


def cmd_kline(args):
    acc_id = account_id(args)
    data = make_data(acc_id)
    result = data.get_market_data_ex(
        ["open", "high", "low", "close", "volume", "amount"],
        [args.code], period=args.period, count=args.count,
        dividend_type="none", fill_data=False,
    ) or {}
    df = result.get(args.code)
    if df is None or len(df) == 0:
        print("no kline for %s (not downloaded in QMT local data?)" % args.code)
        return
    print(df.to_string())


def cmd_order(args, order_type):
    acc_id = account_id(args)
    trader = make_trader(acc_id)
    account = as_stock_account(acc_id)
    if trader.connect() != 0:
        sys.exit("connect failed")
    price_type = xtconstant.FIX_PRICE if args.price > 0 else xtconstant.LATEST_PRICE
    order_id = trader.order_stock(
        account, args.code, order_type, args.volume, price_type,
        args.price, "bigqmt_cli", args.remark or "cli-order",
    )
    if isinstance(order_id, int) and order_id == -1:
        sys.exit("order rejected (order_stock returned -1)")
    print("order submitted, order_id=%s" % order_id)


def cmd_cancel(args):
    acc_id = account_id(args)
    trader = make_trader(acc_id)
    account = as_stock_account(acc_id)
    result = trader.cancel_order_stock(account, args.order_id)
    print("cancel_order_stock -> %s (0 = success, -1 = failure)" % result)


class _Printer:
    def on_disconnected(self):
        print("[event] disconnected")

    def on_stock_order(self, order):
        print("[order] %s" % json.dumps(fmt_row(order, [
            "order_id", "stock_code", "order_type", "order_volume",
            "price", "traded_volume", "order_status",
        ]), ensure_ascii=False, default=str))

    def on_stock_trade(self, trade):
        print("[trade] %s" % json.dumps(fmt_row(trade, [
            "stock_code", "order_type", "traded_volume", "traded_price",
        ]), ensure_ascii=False, default=str))

    def on_order_error(self, error):
        print("[order_error] %s" % json.dumps(fmt_row(error, [
            "order_id", "error_id", "error_msg",
        ]), ensure_ascii=False, default=str))

    def on_cancel_error(self, error):
        print("[cancel_error] %s" % json.dumps(fmt_row(error, [
            "order_id", "error_id", "error_msg",
        ]), ensure_ascii=False, default=str))

    def on_order_stock_async_response(self, response):
        print("[async_response] %s" % json.dumps(fmt_row(response, [
            "seq", "order_id",
        ]), ensure_ascii=False, default=str))

    def on_account_status(self, status):
        print("[account_status] %s" % json.dumps(fmt_row(status, [
            "account_id", "account_type", "status",
        ]), ensure_ascii=False, default=str))


def cmd_watch(args):
    acc_id = account_id(args)
    trader = make_trader(acc_id)
    account = as_stock_account(acc_id)
    trader.register_callback(_Printer())
    trader.start()
    if trader.connect() != 0:
        sys.exit("connect failed")
    trader.subscribe(account)
    print("watching exec events for %s ... Ctrl+C to stop" % acc_id)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nstopped.")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Big QMT redis-bridge CLI")
    parser.add_argument("--account", default="", help="override account id")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("discover", help="scan redis for registered accounts")
    sub.add_parser("ping", help="rpc ping")
    sub.add_parser("asset", help="query account asset")
    sub.add_parser("positions", help="query positions")
    p_orders = sub.add_parser("orders", help="query orders")
    p_orders.add_argument("--cancelable", action="store_true")
    sub.add_parser("trades", help="query trades")
    p_tick = sub.add_parser("tick", help="full tick snapshot")
    p_tick.add_argument("codes", nargs="+")
    p_kline = sub.add_parser("kline", help="kline from QMT local data")
    p_kline.add_argument("code")
    p_kline.add_argument("--period", default="1d")
    p_kline.add_argument("--count", type=int, default=10)
    for name, order_type in (("buy", xtconstant.STOCK_BUY), ("sell", xtconstant.STOCK_SELL)):
        p = sub.add_parser(name, help="submit a %s order" % name)
        p.add_argument("code")
        p.add_argument("volume", type=int)
        p.add_argument("price", type=float, help="0 = latest-price (market) order")
        p.add_argument("--remark", default="")
        p.set_defaults(order_type=order_type)
    p_cancel = sub.add_parser("cancel", help="cancel an order")
    p_cancel.add_argument("order_id")
    sub.add_parser("watch", help="watch order/trade callbacks")

    args = parser.parse_args(argv)
    handlers = {
        "discover": cmd_discover,
        "ping": cmd_ping,
        "asset": cmd_asset,
        "positions": cmd_positions,
        "orders": cmd_orders,
        "trades": cmd_trades,
        "tick": cmd_tick,
        "kline": cmd_kline,
        "buy": lambda a: cmd_order(a, a.order_type),
        "sell": lambda a: cmd_order(a, a.order_type),
        "cancel": cmd_cancel,
        "watch": cmd_watch,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
