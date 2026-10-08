# coding: utf-8
"""Behavioral regression tests for the qmt-trader CLI (qmt.py).

Covers the batch of issues found reading the file after the --dry-run
fall-through (2026-09-02):

1. cmd_cancel inverted the MiniQMT return contract: the gateway returns
   0 on success / -1 on failure (issue #113), and ``bool(0) is False``
   printed success:false for a cancel that worked. Now rc!=0 is an error
   exit, and a successful cancel is followed by a readback of the order
   row (写完必须回读).
2. Global flags were only accepted BEFORE the subcommand:
   ``qmt.py account --table`` died with "unrecognized arguments".
3. _ensure_src_on_path left the repo src BELOW site-packages when an
   editable install had already put it there, so ``import xtquant``
   resolved to the real site-packages package -- which prints an upgrade
   ad to stdout and corrupts the CLI's JSON output.
4. quote-subscribe: the first-frame snapshot can reach the callback
   before subscribe_whole_quote returns; ``sub_id`` was unbound at that
   point and the callback died with NameError once it tried to
   unsubscribe.
5. kline stats reported max/min of CLOSE prices as "high"/"low".
"""

import argparse
import importlib.util
import io
import json
import os
import sys
import types
import unittest
from contextlib import redirect_stdout
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

QMT_PY = os.path.join(ROOT, "qmt-trader", "scripts", "qmt.py")


def _load_qmt_cli():
    spec = importlib.util.spec_from_file_location("qmt_cli", QMT_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class CliTestBase(unittest.TestCase):
    """Load qmt.py once per class without leaking its import-time path setup.

    qmt.py's module-level path discovery reorders sys.path (and appends the
    live QMT python dir). Snapshot and restore around it, same pattern as
    test_qmt_cli_dry_run.py, so later test files are unaffected.
    """

    @classmethod
    def setUpClass(cls):
        cls._saved_path = sys.path[:]
        cls._saved_modules = set(sys.modules)
        cls.cli = _load_qmt_cli()

    @classmethod
    def tearDownClass(cls):
        sys.path[:] = cls._saved_path
        for name in set(sys.modules) - cls._saved_modules:
            mod = sys.modules.get(name)
            f = getattr(mod, "__file__", None)
            if f and "QMT" in f.upper():
                sys.modules.pop(name, None)

    def _run(self, fn, args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            fn(args)
        return json.loads(buf.getvalue())


def _cancel_args(**kw):
    base = dict(account=None, dry_run=False, order_id="xt123", market="SH")
    base.update(kw)
    return argparse.Namespace(**base)


def _order_row(**kw):
    base = dict(
        account_id="ACC", stock_code="601398.SH", order_type=23,
        order_status=54, order_volume=100, traded_volume=0, price=8.08,
        order_sysid="xt123", order_id=878651294, strategy_name="",
        order_remark="r", order_time=1788329111,
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


class CancelContractTest(CliTestBase):
    def test_cancel_success_reports_success_true_and_readback(self):
        trader = mock.Mock()
        trader.cancel_order_stock_sysid.return_value = 0  # MiniQMT: 0 = success
        trader.query_stock_orders.return_value = [_order_row()]
        with mock.patch.object(self.cli, "_init", return_value=(trader, None, None)), \
             mock.patch.object(self.cli, "_acc_or", return_value="ACC"), \
             mock.patch.object(self.cli.time, "sleep", lambda *_: None):
            out = self._run(self.cli.cmd_cancel, _cancel_args())
        self.assertTrue(out["ok"])
        self.assertTrue(out["data"]["success"])
        # 回读到的委托行必须带回来，调用方据此确认最终状态
        self.assertIsNotNone(out["data"]["confirmed_order"])
        self.assertEqual(out["data"]["confirmed_order"]["order_status_name"], "CANCELED")

    def test_cancel_rejection_is_an_error_not_success(self):
        trader = mock.Mock()
        trader.cancel_order_stock_sysid.return_value = -1  # MiniQMT: -1 = failure
        with mock.patch.object(self.cli, "_init", return_value=(trader, None, None)), \
             mock.patch.object(self.cli, "_acc_or", return_value="ACC"):
            buf = io.StringIO()
            with redirect_stdout(buf), self.assertRaises(SystemExit) as cm:
                self.cli.cmd_cancel(_cancel_args())
        self.assertEqual(cm.exception.code, 1)
        out = json.loads(buf.getvalue())
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], "CANCEL_REJECTED")
        trader.query_stock_orders.assert_not_called()


class GlobalFlagsAfterSubcommandTest(CliTestBase):
    def test_table_after_subcommand(self):
        args = self.cli.build_parser().parse_args(["account", "--table"])
        self.assertTrue(args.table)

    def test_table_before_subcommand(self):
        args = self.cli.build_parser().parse_args(["--table", "account"])
        self.assertTrue(args.table)

    def test_defaults_intact(self):
        args = self.cli.build_parser().parse_args(["account"])
        self.assertFalse(args.table)
        self.assertIsNone(args.account)

    def test_account_after_subcommand(self):
        args = self.cli.build_parser().parse_args(["positions", "--account", "12345"])
        self.assertEqual(args.account, "12345")

    def test_buy_with_flags_after_positionals(self):
        args = self.cli.build_parser().parse_args(
            ["buy", "600000.SH", "100", "--price", "8.0", "--dry-run", "--table"]
        )
        self.assertTrue(args.dry_run)
        self.assertTrue(args.table)
        self.assertEqual(args.price, 8.0)


class SrcPathOrderTest(CliTestBase):
    def test_existing_src_is_moved_to_front(self):
        src = os.path.join(ROOT, "src")
        saved = sys.path[:]
        try:
            sys.path[:] = [p for p in sys.path if os.path.normpath(p) != os.path.normpath(src)]
            sys.path.append(src)  # editable 安装的效果：在 site-packages 之后
            self.cli._ensure_src_on_path()
            self.assertEqual(os.path.normpath(sys.path[0]), os.path.normpath(src))
            self.assertEqual(
                sum(1 for p in sys.path if os.path.normpath(p) == os.path.normpath(src)),
                1,
                "src must appear exactly once",
            )
        finally:
            sys.path[:] = saved


class QuoteSubscribeFirstFrameRaceTest(CliTestBase):
    def test_callback_before_subscribe_returns_does_not_crash(self):
        xtdata = mock.Mock()

        def fake_subscribe(codes, callback):
            # 首帧快照在 subscribe 返回之前就推给回调（sub_id 还没绑上）
            callback({"600000.SH": {"lastPrice": 8.0, "volume": 1, "time": 1}})
            return 42

        xtdata.subscribe_whole_quote.side_effect = fake_subscribe
        args = argparse.Namespace(codes=["SH"], max=1, timeout=0, table=False)
        with mock.patch.object(self.cli, "_init", return_value=(None, xtdata, None)):
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.cli.cmd_quote_subscribe(args)
        text = buf.getvalue()
        # stdout 上先有回调逐条打印的行情行，最后才是汇总 JSON
        out = json.loads(text[text.rfind("\n{") + 1:])
        self.assertTrue(out["ok"])
        self.assertEqual(out["data"]["count"], 1)
        self.assertEqual(out["data"]["sub_id"], 42)


class KlineStatsTest(CliTestBase):
    def test_high_low_come_from_high_low_fields_not_closes(self):
        pd = __import__("pandas")
        df = pd.DataFrame([
            {"time": 1, "open": 9.0, "high": 10.0, "low": 8.5, "close": 9.0, "volume": 100},
            {"time": 2, "open": 9.0, "high": 12.0, "low": 9.0, "close": 10.0, "volume": 100},
            {"time": 3, "open": 10.0, "high": 11.0, "low": 10.0, "close": 10.5, "volume": 100},
        ])
        xtdata = mock.Mock()
        xtdata.get_market_data_ex.return_value = {"600000.SH": df}
        args = argparse.Namespace(
            code="600000.SH", fields=None, period="1d", start=None, end=None,
            count=-1, dividend="none", no_fill=False, table=False,
        )
        with mock.patch.object(self.cli, "_init", return_value=(None, xtdata, None)):
            out = self._run(self.cli.cmd_kline, args)
        stats = out["data"]["stats"]
        self.assertEqual(stats["high"], 12.0)  # closes 的最大值是 10.5
        self.assertEqual(stats["low"], 8.5)    # closes 的最小值是 9.0


def _trade_row(**kw):
    """一行成交, 字段名照 xtquant_compat._trade_from_dict 构造的对象来。"""
    base = dict(
        account_id="ACC", stock_code="601398.SH", order_type=23,
        order_sysid="xt123", order_id=878651294, trade_id="t1",
        traded_volume=100, traded_price=8.08, traded_at="2026-09-04 10:01:02",
        order_remark="r", strategy_name="alpha_v2",
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


class TradeStrategyNameTest(CliTestBase):
    """成交行的 strategy_name 到了 CLI 就被丢掉了 (issue #174)。

    #174 给的绕行办法是「回调拿不到策略名时用查询兜一下」—— 查询路径
    确实补全了 (`_attribute_to_strategies`), 实盘只读核对过服务端发的
    成交行**带** strategy_name 这个键。但本仓库自己的 CLI 在
    `_trade_to_dict` 里没列这个字段, 委托的 `_order_to_dict` 列了。
    所以照着绕行办法用 `qmt.py trades` 去查的人, 看到的是「查询路径也
    没有策略名」—— 字段是在最后一步被丢掉的, 不是没送到。
    """

    def test_trades_row_carries_strategy_name(self):
        trader = mock.Mock()
        trader.query_stock_trades.return_value = [_trade_row()]
        args = argparse.Namespace(account=None, strategy=None, table=False)
        with mock.patch.object(self.cli, "_init", return_value=(trader, None, None)), \
             mock.patch.object(self.cli, "_acc_or", return_value="ACC"):
            out = self._run(self.cli.cmd_trades, args)
        row = out["data"]["trades"][0]
        self.assertIn("strategy_name", row)
        self.assertEqual(row["strategy_name"], "alpha_v2")

    def test_missing_strategy_name_is_none_not_empty_string(self):
        """旧部署不发这个字段时给 None, 和 trade_amount(#173) 一个道理。

        「服务端没告诉我」和「策略名确实是空」是两回事, 后者说明这笔单
        不是本桥下的 (手工单没有备注可查), 前者说明该升级服务端了。
        兜底成 "" 会把这两种情况抹平。
        """
        trader = mock.Mock()
        row_without = _trade_row()
        del row_without.strategy_name
        trader.query_stock_trades.return_value = [row_without]
        args = argparse.Namespace(account=None, strategy=None, table=False)
        with mock.patch.object(self.cli, "_init", return_value=(trader, None, None)), \
             mock.patch.object(self.cli, "_acc_or", return_value="ACC"):
            out = self._run(self.cli.cmd_trades, args)
        self.assertIsNone(out["data"]["trades"][0]["strategy_name"])


if __name__ == "__main__":
    unittest.main()
