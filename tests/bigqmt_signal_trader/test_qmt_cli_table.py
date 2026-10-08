# coding: utf-8
"""--table printed headers and then one blank row, for any amount of data.

Found while confirming #173 through the CLI: `qmt.py orders --table` drew the
header row, the dashed separator, and a single line of spaces, while the same
query without --table returned count=14 with fully populated dicts. So the
data was fine and only the rendering was wrong.

Cause: a command's payload is shaped for JSON, and that shape is usually a
wrapper --

    _ok({"orders": rows, "count": 14}, table=..., headers=[...])

while _ok did::

    _print_table(data if isinstance(data, list) else [data], headers)

The wrapper is a dict, not a list, so it was wrapped again into ONE row, and
every header lookup (`stock_code`, ...) missed on it and rendered "". One
blank row, regardless of how many orders came back.

Measured before fixing, against the live bridge: positions, orders, trades,
tick and kline were all broken this way; account and instrument were correct,
because those really do pass a single flat record and one row is the right
answer. Both behaviours are pinned below -- the fix has to repair five
commands without turning the other two into something else.

tick is the odd shape: {code: {...}} keyed by code, so its rows are the
values rather than a named list.
"""

import argparse
import importlib.util
import io
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
    spec = importlib.util.spec_from_file_location("qmt_cli_table", QMT_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TableTestBase(unittest.TestCase):
    """Same isolation as test_qmt_cli_behavior: qmt.py reorders sys.path."""

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

    def _render(self, fn, args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            fn(args)
        return buf.getvalue().splitlines()

    def _body(self, lines):
        """Rows below the header and the dashed separator."""
        return [l for l in lines[2:] if l.strip()]


def _order(**kw):
    base = dict(
        account_id="ACC", stock_code="601398.SH", order_type=23,
        order_status=56, order_volume=100, traded_volume=100, price=8.08,
        order_sysid="xt123", order_id=1, strategy_name="", order_remark="",
        order_time=1788329111, trade_amount=808.0,
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


def _position(**kw):
    base = dict(
        account_id="ACC", stock_code="600000.SH", stock_name="浦发银行",
        volume=1000, can_use_volume=1000, avg_price=9.0, price=9.43,
        market_value=9430.0, open_price=9.0, cost_price=9.0,
        frozen_volume=0, yesterday_volume=1000, direction=48,
        available_amount=1000, enable_amount=1000,
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


def _trade(**kw):
    base = dict(
        account_id="ACC", stock_code="601398.SH", order_type=23,
        order_sysid="xt123", order_id=1, trade_id="t1", traded_volume=100,
        traded_price=8.08, traded_at="2026-09-04 10:00:00", order_remark="",
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


class OrdersTableTest(TableTestBase):
    def test_one_row_per_order_with_values(self):
        trader = mock.Mock()
        trader.query_stock_orders.return_value = [
            _order(stock_code="601398.SH"), _order(stock_code="600000.SH")]
        args = argparse.Namespace(account=None, table=True, cancelable=False,
                                  strategy=None)
        with mock.patch.object(self.cli, "_init", return_value=(trader, None, None)), \
             mock.patch.object(self.cli, "_acc_or", return_value="ACC"):
            lines = self._render(self.cli.cmd_orders, args)

        rows = self._body(lines)
        self.assertEqual(len(rows), 2)
        self.assertIn("601398.SH", rows[0])
        self.assertIn("600000.SH", rows[1])

    def test_the_row_is_not_blank(self):
        """The exact reported symptom: a row of nothing but spaces."""
        trader = mock.Mock()
        trader.query_stock_orders.return_value = [_order()]
        args = argparse.Namespace(account=None, table=True, cancelable=False,
                                  strategy=None)
        with mock.patch.object(self.cli, "_init", return_value=(trader, None, None)), \
             mock.patch.object(self.cli, "_acc_or", return_value="ACC"):
            lines = self._render(self.cli.cmd_orders, args)

        self.assertTrue(any(l.strip() for l in lines[2:]),
                        "every data row was blank: %r" % (lines[2:],))


class PositionsTableTest(TableTestBase):
    def test_one_row_per_position(self):
        trader = mock.Mock()
        trader.query_stock_positions.return_value = [
            _position(stock_code="600000.SH"), _position(stock_code="000001.SZ")]
        args = argparse.Namespace(account=None, table=True, code=None)
        with mock.patch.object(self.cli, "_init", return_value=(trader, None, None)), \
             mock.patch.object(self.cli, "_acc_or", return_value="ACC"):
            lines = self._render(self.cli.cmd_positions, args)

        rows = self._body(lines)
        self.assertEqual(len(rows), 2)
        self.assertIn("600000.SH", rows[0])

    def test_the_summary_is_not_rendered_as_a_row(self):
        """positions ships {"positions": [...], "summary": {...}} -- only the
        list is tabular; the summary must not become a phantom row."""
        trader = mock.Mock()
        trader.query_stock_positions.return_value = [_position()]
        args = argparse.Namespace(account=None, table=True, code=None)
        with mock.patch.object(self.cli, "_init", return_value=(trader, None, None)), \
             mock.patch.object(self.cli, "_acc_or", return_value="ACC"):
            lines = self._render(self.cli.cmd_positions, args)

        self.assertEqual(len(self._body(lines)), 1)


class TradesTableTest(TableTestBase):
    def test_one_row_per_trade(self):
        trader = mock.Mock()
        trader.query_stock_trades.return_value = [_trade(), _trade(trade_id="t2")]
        args = argparse.Namespace(account=None, table=True, strategy=None)
        with mock.patch.object(self.cli, "_init", return_value=(trader, None, None)), \
             mock.patch.object(self.cli, "_acc_or", return_value="ACC"):
            lines = self._render(self.cli.cmd_trades, args)

        self.assertEqual(len(self._body(lines)), 2)


class TickTableTest(TableTestBase):
    """{code: {...}} -- keyed by code, so the rows are the values."""

    def test_one_row_per_code(self):
        xtdata = mock.Mock()
        xtdata.get_full_tick.return_value = {
            "600000.SH": {"lastPrice": 9.43, "lastClose": 9.40, "volume": 100,
                          "bidPrice": [9.42], "askPrice": [9.44]},
            "000001.SZ": {"lastPrice": 11.89, "lastClose": 11.80, "volume": 200,
                          "bidPrice": [11.88], "askPrice": [11.90]},
        }
        args = argparse.Namespace(codes=["600000.SH", "000001.SZ"], table=True)
        with mock.patch.object(self.cli, "_init", return_value=(None, xtdata, None)):
            lines = self._render(self.cli.cmd_tick, args)

        rows = self._body(lines)
        self.assertEqual(len(rows), 2)
        self.assertTrue(any("600000.SH" in r for r in rows))
        self.assertTrue(any("000001.SZ" in r for r in rows))


class KlineTableTest(TableTestBase):
    def test_one_row_per_bar(self):
        import pandas

        df = pandas.DataFrame(
            {"open": [9.0, 9.1, 9.2], "high": [9.5, 9.6, 9.7],
             "low": [8.9, 9.0, 9.1], "close": [9.4, 9.5, 9.6],
             "volume": [100, 200, 300]},
            index=["20260902", "20260903", "20260904"])
        xtdata = mock.Mock()
        xtdata.get_market_data_ex.return_value = {"600000.SH": df}
        args = argparse.Namespace(
            code="600000.SH", table=True, fields=None, period="1d",
            start=None, end=None, count=3, dividend="none", no_fill=False)
        with mock.patch.object(self.cli, "_init", return_value=(None, xtdata, None)):
            lines = self._render(self.cli.cmd_kline, args)

        self.assertEqual(len(self._body(lines)), 3)


class SingleRecordCommandsStayOneRowTest(TableTestBase):
    """account and instrument really do return one flat record.

    These were CORRECT before the fix. Repairing the five broken commands
    must not turn these into an empty table or explode their fields into
    rows -- which is what a naive "always take the values" rule would do.
    """

    def test_account_is_one_row_with_values(self):
        trader = mock.Mock()
        trader.query_stock_asset.return_value = types.SimpleNamespace(
            account_id="ACC", cash=109904.38, available_cash=109904.38,
            frozen_cash=0.0, total_asset=120224.38, market_value=10320.0)
        args = argparse.Namespace(account=None, table=True)
        with mock.patch.object(self.cli, "_init", return_value=(trader, None, None)), \
             mock.patch.object(self.cli, "_acc_or", return_value="ACC"):
            lines = self._render(self.cli.cmd_account, args)

        rows = self._body(lines)
        self.assertEqual(len(rows), 1)
        self.assertIn("120224.38", rows[0])

    def test_instrument_is_one_row(self):
        xtdata = mock.Mock()
        xtdata.get_instrument_detail.return_value = {
            "ExchangeID": "SH", "InstrumentID": "600000",
            "InstrumentName": "浦发银行", "PriceTick": 0.01}
        args = argparse.Namespace(code="600000.SH", table=True)
        with mock.patch.object(self.cli, "_init", return_value=(None, xtdata, None)):
            lines = self._render(self.cli.cmd_instrument, args)

        rows = self._body(lines)
        self.assertEqual(len(rows), 1)
        self.assertIn("600000", rows[0])


class EmptyResultTest(TableTestBase):
    def test_no_orders_says_empty_rather_than_drawing_a_blank_row(self):
        trader = mock.Mock()
        trader.query_stock_orders.return_value = []
        args = argparse.Namespace(account=None, table=True, cancelable=False,
                                  strategy=None)
        with mock.patch.object(self.cli, "_init", return_value=(trader, None, None)), \
             mock.patch.object(self.cli, "_acc_or", return_value="ACC"):
            lines = self._render(self.cli.cmd_orders, args)

        self.assertEqual(lines, ["(empty)"])


if __name__ == "__main__":
    unittest.main()
