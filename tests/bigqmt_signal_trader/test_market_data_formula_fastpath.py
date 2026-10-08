# coding: utf-8
"""get_market_data rides the FormulaServer fast path too (user report,
2026-09-10: 500+ codes through the QMT-thread RPC kept timing out while
get_market_data_ex answered the same bars via FormulaServer in no time).

Same FormulaServer getMarketData call, same staleness guard as
get_market_data_ex; the client pivots the answer into the documented
dict[field]->wide-frame shape afterwards.
"""

import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader import xtquant_compat
from bigqmt_signal_trader.formula_server import (
    FormulaServerRouter,
    Unroutable,
    _market_data_params,
    _market_data_result,
)


class RoutingTest(unittest.TestCase):
    def test_get_market_data_is_supported(self):
        router = FormulaServerRouter(client=object())
        self.assertTrue(router.supports("get_market_data"))

    def test_params_translate_like_md_ex(self):
        out = _market_data_params({
            "field_list": ["close"], "stock_list": ["510880.SH"],
            "period": "1d", "start_time": "20260901", "end_time": "20260910",
        })
        self.assertEqual(out["stockCodes"], ["510880.SH"])
        self.assertEqual(out["period"], "1d")
        self.assertEqual(out["dividendType"], "none")

    def test_adjusted_reads_are_refused_to_rpc(self):
        with self.assertRaises(ValueError):
            _market_data_params({
                "field_list": ["close"], "stock_list": ["510880.SH"],
                "period": "1d", "dividend_type": "back",
            })

    def test_result_adapts_the_flat_wire(self):
        raw = {"result": ["510880.SH",
                          [20260901, ["close", 5.06], 20260902, ["close", 5.08]]]}
        out = _market_data_result(raw, {"field_list": ["close"],
                                        "stock_list": ["510880.SH"]})
        frame = out["510880.SH"]
        self.assertEqual(frame["__bigqmt_type__"], "DataFrame")
        self.assertEqual(frame["records"][0]["close"], 5.06)


class ClientEndToEndTest(unittest.TestCase):
    """Real client.call fast-path + BigQmtXtData.get_market_data pivot."""

    def _client(self, router):
        from bigqmt_signal_trader.xtquant_compat import BigQmtRpcClient

        class _Transport(object):
            def __init__(self):
                self.calls = 0

            def send_request(self, request, timeout_seconds):
                self.calls += 1
                return {"ok": True, "data": {"via": "transport"}}

        transport = _Transport()
        client = BigQmtRpcClient(account_id="acct",
                                 redis_config={"host": "127.0.0.1"})
        client.transport_name = "zmq"
        client._transport_instance = transport
        client._formula_router_instance = router
        return client, transport

    def test_formula_answer_comes_out_in_the_documented_shape(self):
        class _Router(object):
            def supports(self, method):
                return True

            def call(self, method, params):
                return _market_data_result(
                    {"result": ["510880.SH",
                                [20260901, ["close", 5.06, "open", 5.05]]]},
                    params)

        client, transport = self._client(_Router())
        from bigqmt_signal_trader.xtquant_compat import BigQmtXtData

        out = BigQmtXtData(client).get_market_data(
            field_list=["close", "open"], stock_list=["510880.SH"], period="1d")

        self.assertEqual(set(out.keys()), {"close", "open"})
        self.assertEqual(list(out["close"].index), ["510880.SH"])
        self.assertEqual(out["close"].loc["510880.SH", "20260901"], 5.06)
        self.assertEqual(transport.calls, 0, "the transport must not be touched")

    def test_stale_answer_fails_over_like_md_ex(self):
        import datetime as _dt

        import pandas as pd

        old_bar = (_dt.datetime.now() - _dt.timedelta(hours=3)).strftime("%Y%m%d%H%M%S")

        class _StaleRouter(object):
            def supports(self, method):
                return True

            def call(self, method, params):
                return {"600000.SH": pd.DataFrame(
                    {"stime": [old_bar], "close": [1.0]})}

        xtquant_compat._formula_stale_until["ts"] = 0.0
        try:
            client, transport = self._client(_StaleRouter())
            result = client.call("get_market_data", {"period": "1m",
                                                     "field_list": ["close"],
                                                     "stock_list": ["600000.SH"]})
        finally:
            xtquant_compat._formula_stale_until["ts"] = 0.0
        self.assertEqual(result, {"via": "transport"},
                         "a stale formula answer must fail over to the bridge")
        self.assertEqual(transport.calls, 1)


if __name__ == "__main__":
    unittest.main()
