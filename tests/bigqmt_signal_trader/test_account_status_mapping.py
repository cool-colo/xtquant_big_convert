# coding: utf-8
"""query_account_status must come from the ACCOUNT row, not TASK (issue #272).

The placeholder used the TASK detail type -- 委托任务状态, empty whenever no
order task is running, which is exactly when you ask "is my account OK". The
nearest real source big QMT offers is the ACCOUNT row's m_Enable: enabled
account -> ACCOUNT_STATUS_OK(0), disabled -> ACCOUNT_STATUS_FAIL(3), no
ACCOUNT row -> empty list.
"""

import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.redis_rpc import BigQmtRpcHandlers

from test_redis_rpc import FakeMarketData, FakePositionProvider  # noqa: E402


class _Gateway(object):
    def __init__(self, rows, account_type="STOCK"):
        self.rows = rows
        self.account_type = account_type
        self.get_trade_detail_data = lambda *a, **k: rows


def _handlers(rows):
    return BigQmtRpcHandlers(
        account_id="acct", market_data=FakeMarketData(),
        position_provider=FakePositionProvider(),
        order_gateway=_Gateway(rows))


class AccountStatusMappingTest(unittest.TestCase):
    def test_enabled_account_reports_ok(self):
        rows = [{"m_strAccountID": "acct", "m_Enable": True}]
        out = _handlers(rows)._handle_query_account_status({})

        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["status"], 0)  # ACCOUNT_STATUS_OK
        self.assertEqual(out[0]["status_msg"], "")
        self.assertEqual(out[0]["account_type"], "STOCK")

    def test_disabled_account_reports_fail(self):
        rows = [{"m_strAccountID": "acct", "m_Enable": False}]
        out = _handlers(rows)._handle_query_account_status({})

        self.assertEqual(out[0]["status"], 3)  # ACCOUNT_STATUS_FAIL
        self.assertIn("disabled", out[0]["status_msg"])

    def test_no_account_row_means_empty_not_a_fake_status(self):
        out = _handlers([])._handle_query_account_status({})
        self.assertEqual(out, [])

    def test_task_is_never_touched(self):
        gateway = _Gateway([{"m_strAccountID": "acct", "m_Enable": True}])
        calls = []
        gateway.get_trade_detail_data = lambda *a, **k: calls.append((a, k)) or (lambda *x: gateway.rows)
        handlers = BigQmtRpcHandlers(
            account_id="acct", market_data=FakeMarketData(),
            position_provider=FakePositionProvider(), order_gateway=gateway)

        handlers._handle_query_account_status({})

        self.assertTrue(calls, "the ACCOUNT row must be queried")
        # _query_trade_detail calls the gateway's query helper, not TASK.
        self.assertNotIn("TASK", str(calls))


if __name__ == "__main__":
    unittest.main()
