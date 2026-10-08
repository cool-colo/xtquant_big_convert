# coding: utf-8
"""--dry-run must never place a real order.

Found live on 2026-09-02: `python qmt.py buy 601398.SH 100 --price 8.08
--dry-run` printed the dry-run preview AND THEN placed a real order
(xt1082201990, rejected 57/JUNK only because the account lacked the cash).
_ok() prints but does not exit, so execution fell through from the dry-run
branch into the real order_stock() call. Same shape in cmd_cancel.

These tests pin the contract: with dry_run=True the trader gateway must not
be touched at all.
"""

import argparse
import importlib.util
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

QMT_PY = os.path.join(ROOT, "qmt-trader", "scripts", "qmt.py")


def _load_qmt_cli():
    # Load as "qmt_cli": the plain name "qmt" collides with QMT's injected
    # strategy module name and any shim of it in sys.modules.
    spec = importlib.util.spec_from_file_location("qmt_cli", QMT_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class DryRunNeverOrdersTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # qmt.py's import-time path discovery appends the LIVE QMT python dir
        # to sys.path. Left in place, later tests in the same pytest process
        # then resolve bigqmt_signal_trader_local_config from the live install
        # (real account id, real zmq port) and test_runner's strategy init
        # dies on ZMQ_BIND_CONFLICT against the running QMT. Restore both
        # sys.path and any live-config modules when done.
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

    def _order_args(self, dry_run):
        return argparse.Namespace(
            account=None,
            code="601398.SH",
            volume=100,
            price=8.08,
            latest=False,
            strategy="issue152_test",
            remark="issue152-test",
            dry_run=dry_run,
        )

    def test_buy_dry_run_does_not_call_order_stock(self):
        trader = mock.Mock()
        with mock.patch.object(self.cli, "_init", return_value=(trader, None, None)), \
             mock.patch.object(self.cli, "_acc_or", return_value="ACC"):
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.cli._place_order(self._order_args(dry_run=True), "BUY")
        trader.order_stock.assert_not_called()
        out = json.loads(buf.getvalue())
        self.assertTrue(out["ok"])
        self.assertTrue(out["data"]["dry_run"])
        self.assertEqual(out["data"]["stock_code"], "601398.SH")

    def test_sell_dry_run_does_not_call_order_stock(self):
        trader = mock.Mock()
        with mock.patch.object(self.cli, "_init", return_value=(trader, None, None)), \
             mock.patch.object(self.cli, "_acc_or", return_value="ACC"):
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.cli._place_order(self._order_args(dry_run=True), "SELL")
        trader.order_stock.assert_not_called()

    def test_cancel_dry_run_does_not_call_cancel(self):
        trader = mock.Mock()
        args = argparse.Namespace(
            account=None, dry_run=True, order_id="xt123", market="SH",
        )
        with mock.patch.object(self.cli, "_init", return_value=(trader, None, None)), \
             mock.patch.object(self.cli, "_acc_or", return_value="ACC"):
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.cli.cmd_cancel(args)
        trader.cancel_order_stock_sysid.assert_not_called()
        out = json.loads(buf.getvalue())
        self.assertTrue(out["data"]["dry_run"])

    def test_real_order_still_reaches_order_stock(self):
        # The fix must not disable the normal path: dry_run=False still orders.
        trader = mock.Mock()
        trader.order_stock.return_value = 635093411
        trader.query_stock_orders.return_value = []
        with mock.patch.object(self.cli, "_init", return_value=(trader, None, None)), \
             mock.patch.object(self.cli, "_acc_or", return_value="ACC"):
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.cli._place_order(self._order_args(dry_run=False), "BUY")
        trader.order_stock.assert_called_once()
        call = trader.order_stock.call_args
        self.assertEqual(call[0][1], "601398.SH")
        self.assertEqual(call[0][3], 100)


if __name__ == "__main__":
    unittest.main()
