# coding: utf-8
"""get_market_data must return the documented MiniQMT shape.

Documented for bar periods (1m/5m/1d): dict[field] -> pd.DataFrame with
index=stock_list, columns=time_list. Big QMT instead hands a bare long
DataFrame for one stock and dict[stock] -> long DataFrame for many; the
bridge passed both through, so client code written against the miniQMT docs
broke (reporter's printout, 2026-09-10).

The conversion runs client-side after the all-zero heal: the server keeps
the long shape, raw-RPC callers see no change, and no QMT deploy is needed.
"""

import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

import pandas as pd

from bigqmt_signal_trader.xtquant_compat import (
    BigQmtXtData,
    _to_documented_market_data_shape,
)


def _long_frame(rows):
    return pd.DataFrame(rows, columns=["index", "open", "close"])


STOCKS = ["510880.SH", "601398.SH"]
LONG = {
    "510880.SH": _long_frame([[20260901, 5.05, 5.06], [20260902, 5.07, 5.08]]),
    "601398.SH": _long_frame([[20260901, 8.10, 8.11], [20260902, 8.20, 8.21]]),
}


class PivotTest(unittest.TestCase):
    def test_multi_stock_pivots_to_field_keyed_wide_frames(self):
        out = _to_documented_market_data_shape(
            dict(LONG), ["open", "close"], STOCKS, "1d")

        self.assertEqual(set(out.keys()), {"open", "close"})
        frame = out["close"]
        self.assertEqual(list(frame.index), STOCKS)
        self.assertEqual(list(frame.columns), ['20260901', '20260902'])
        self.assertEqual(frame.loc["601398.SH", "20260902"], 8.21)
        self.assertEqual(frame.loc["510880.SH", "20260901"], 5.06)

    def test_single_stock_bare_frame_pivots(self):
        out = _to_documented_market_data_shape(
            LONG["510880.SH"], ["open", "close"], ["510880.SH"], "1d")

        self.assertEqual(set(out.keys()), {"open", "close"})
        self.assertEqual(list(out["close"].index), ["510880.SH"])
        self.assertEqual(out["open"].loc["510880.SH", "20260901"], 5.05)

    def test_already_documented_shape_passes_through(self):
        documented = {"close": pd.DataFrame(
            {20260901: [5.06], 20260902: [5.08]}, index=["510880.SH"])}
        out = _to_documented_market_data_shape(
            documented, ["close"], ["510880.SH"], "1d")
        self.assertIs(out, documented)

    def test_tick_period_passes_through(self):
        frame = LONG["510880.SH"]
        out = _to_documented_market_data_shape(frame, ["open"], STOCKS, "tick")
        self.assertIs(out, frame)

    def test_empty_and_garbage_pass_through(self):
        self.assertEqual(_to_documented_market_data_shape({}, [], STOCKS, "1d"), {})
        self.assertIsNone(_to_documented_market_data_shape(None, [], STOCKS, "1d"))
        weird = {"something": "not-a-frame"}
        self.assertIs(_to_documented_market_data_shape(weird, [], STOCKS, "1d"), weird)

    def test_field_list_limits_the_pivot(self):
        out = _to_documented_market_data_shape(dict(LONG), ["close"], STOCKS, "1d")
        self.assertEqual(set(out.keys()), {"close"})

    def test_frame_without_a_time_column_passes_through(self):
        frame = pd.DataFrame([[1, 2]], columns=["a", "b"])
        out = _to_documented_market_data_shape({"510880.SH": frame}, [], STOCKS, "1d")
        self.assertIsInstance(out, dict) and out["510880.SH"] is frame

    def test_columns_match_miniqmt_str_dtype(self):
        # miniQMT 实测 data['open'].columns dtype='str'（不是 int）——
        # 打印出来都不带引号所以容易误读，以 dtype 为准。
        frames = {"510880.SH": pd.DataFrame(
            [[20260901, 5.05, 5.06]], columns=["index", "open", "close"])}
        out = _to_documented_market_data_shape(frames, ["open", "close"], ["510880.SH"], "1d")
        self.assertEqual(list(out["close"].columns), ["20260901"])
        self.assertEqual(out["close"].loc["510880.SH", "20260901"], 5.06)


class ClientEndToEndTest(unittest.TestCase):
    def test_get_market_data_returns_documented_shape(self):
        data = BigQmtXtData.__new__(BigQmtXtData)
        data.client = type("C", (), {"call": lambda self, m, p: dict(LONG)})()
        data._heal_adjusted = lambda method, params, d, **kw: d

        out = data.get_market_data(
            field_list=["open", "close"], stock_list=STOCKS, period="1d")

        self.assertEqual(set(out.keys()), {"open", "close"})
        self.assertEqual(list(out["close"].index), STOCKS)
        self.assertEqual(out["close"].loc["510880.SH", "20260901"], 5.06)


if __name__ == "__main__":
    unittest.main()
