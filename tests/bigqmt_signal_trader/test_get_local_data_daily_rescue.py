# coding: utf-8
"""#226/#307 family, the 1d case: a forming daily bar the local answer
dropped is appended from the live daily read.

Measured live (国金 2.1.33, 2026-10-26 mid-session): the live daily read
carries today's partial bar, while a client-cache frame written before the
session (download_history_data2 never refreshes it) -- and on some terminals
a local-store read right after an incremental download -- stop at the
previous trading day. The user-visible symptom: download a batch, read
get_local_data, the last bar is Friday, not today.
"""
import datetime as dt
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader import xtquant_compat
from bigqmt_signal_trader.xtquant_compat import BigQmtXtData

# A Monday mid-session; the stale frame's last bar is the previous Friday.
TODAY = dt.datetime(2026, 10, 26, 13, 30)
TODAY_LABEL = "20261026"
FRIDAY_LABEL = "20261023"

FIELDS = ["time", "open", "close", "low", "high", "volume"]


def _frame(labels, columns):
    import pandas as pd
    return pd.DataFrame(columns, index=labels)


def _stale_frame():
    """Completed history ending at the previous trading day."""
    return _frame(
        ["20261021", FRIDAY_LABEL],
        {"time": [1792598400000, 1792684800000],
         "open": [2.55, 2.50], "close": [2.46, 2.43],
         "low": [2.45, 2.25], "high": [2.63, 2.51],
         "volume": [174829, 335427]},
    )


def _live_frame(volume=145555):
    """The live daily read: today's forming bar."""
    return _frame(
        [TODAY_LABEL],
        {"open": [2.41], "high": [2.41], "low": [2.29],
         "close": [2.36], "volume": [volume], "amount": [34006824.0]},
    )


class _XtData(BigQmtXtData):
    """Just enough wiring: a stubbed inner RPC read and live daily read."""

    def __init__(self, stale, live=None, live_error=False):
        self._stale = stale
        self._live = live
        self._live_error = live_error
        self.live_calls = []

    def _local_cache(self):
        return None

    def _call(self, method, **kwargs):
        return self._stale

    def get_market_data_ex(self, **kwargs):
        self.live_calls.append(kwargs)
        if self._live_error:
            raise RuntimeError("live read down")
        return {"000001.SZ": self._live}


class RescueAppendsTest(unittest.TestCase):
    def _rescue(self, live=None, live_error=False, data=None):
        xt = _XtData(None, live=live, live_error=live_error)
        payload = data if data is not None else {"000001.SZ": _stale_frame()}
        with mock.patch.object(xtquant_compat, "_in_trading_session", lambda now: True):
            with mock.patch.object(xtquant_compat, "_now", lambda: TODAY):
                out = xt._rescue_ongoing_daily_bar(
                    payload, period="1d", end_time="", dividend_type="front_ratio")
        return out, xt

    def test_appends_today_from_live_read(self):
        out, xt = self._rescue(live=_live_frame())
        frame = out["000001.SZ"]
        self.assertEqual(list(frame.index)[-1], TODAY_LABEL)
        self.assertEqual(len(frame), 3)          # two stale rows + today
        self.assertAlmostEqual(float(frame["close"].iloc[-1]), 2.36)
        self.assertAlmostEqual(float(frame["volume"].iloc[-1]), 145555)
        self.assertAlmostEqual(float(frame["close"].iloc[1]), 2.43)  # untouched
        self.assertEqual(len(xt.live_calls), 1)
        kwargs = xt.live_calls[0]
        self.assertEqual(kwargs["period"], "1d")
        self.assertFalse(kwargs["use_formula"])  # forming bar: live RPC, not a snapshot

    def test_history_window_never_appends(self):
        xt = _XtData(None, live=_live_frame())
        with mock.patch.object(xtquant_compat, "_in_trading_session", lambda now: True):
            with mock.patch.object(xtquant_compat, "_now", lambda: TODAY):
                out = xt._rescue_ongoing_daily_bar(
                    {"000001.SZ": _stale_frame()}, period="1d",
                    end_time="20261023", dividend_type="front_ratio")
        self.assertEqual(len(out["000001.SZ"]), 2)
        self.assertEqual(xt.live_calls, [])      # end before today: no live call

    def test_out_of_session_never_appends(self):
        xt = _XtData(None, live=_live_frame())
        with mock.patch.object(xtquant_compat, "_in_trading_session", lambda now: False):
            with mock.patch.object(xtquant_compat, "_now", lambda: TODAY):
                out = xt._rescue_ongoing_daily_bar(
                    {"000001.SZ": _stale_frame()}, period="1d",
                    end_time="", dividend_type="front_ratio")
        self.assertEqual(len(out["000001.SZ"]), 2)
        self.assertEqual(xt.live_calls, [])

    def test_already_current_never_appends(self):
        current = _frame(
            [FRIDAY_LABEL, TODAY_LABEL],
            {"time": [1792684800000, 1792944000000],
             "open": [2.50, 2.41], "close": [2.43, 2.36],
             "low": [2.25, 2.29], "high": [2.51, 2.41],
             "volume": [335427, 90000]},
        )
        out, xt = self._rescue(live=_live_frame(), data={"000001.SZ": current})
        self.assertEqual(len(out["000001.SZ"]), 2)   # untouched, no live call
        self.assertEqual(xt.live_calls, [])

    def test_live_failure_keeps_local_answer(self):
        out, xt = self._rescue(live_error=True)
        self.assertEqual(len(out["000001.SZ"]), 2)
        self.assertEqual(len(xt.live_calls), 1)

    def test_live_empty_keeps_local_answer(self):
        out, xt = self._rescue(live=_frame([], {}))
        self.assertEqual(len(out["000001.SZ"]), 2)

    def test_non_1d_period_untouched(self):
        xt = _XtData(None, live=_live_frame())
        out = xt._rescue_ongoing_daily_bar(
            {"000001.SZ": _stale_frame()}, period="1w",
            end_time="", dividend_type="front_ratio")
        self.assertEqual(len(out["000001.SZ"]), 2)
        self.assertEqual(xt.live_calls, [])


class GetLocalDataWiringTest(unittest.TestCase):
    """get_local_data runs the rescue on both the RPC branch (cache off)."""

    def test_rpc_branch_appends_today(self):
        xt = _XtData(_stale_frame(), live=_live_frame())
        with mock.patch.object(xtquant_compat, "_in_trading_session", lambda now: True):
            with mock.patch.object(xtquant_compat, "_now", lambda: TODAY):
                out = xt.get_local_data(
                    field_list=FIELDS, stock_list=["000001.SZ"],
                    period="1d", count=-1, dividend_type="front_ratio",
                    fill_data=True)
        frame = out["000001.SZ"] if isinstance(out, dict) else out
        self.assertEqual(list(frame.index)[-1], TODAY_LABEL)

    def test_rpc_branch_opt_out(self):
        xt = _XtData(_stale_frame(), live=_live_frame())
        with mock.patch.object(xtquant_compat, "_in_trading_session", lambda now: True):
            with mock.patch.object(xtquant_compat, "_now", lambda: TODAY):
                out = xt.get_local_data(
                    field_list=FIELDS, stock_list=["000001.SZ"],
                    period="1d", count=-1, dividend_type="front_ratio",
                    fill_data=True, resynth_ongoing_multiday=False)
        frame = out["000001.SZ"] if isinstance(out, dict) else out
        self.assertEqual(list(frame.index)[-1], FRIDAY_LABEL)
        self.assertEqual(xt.live_calls, [])


if __name__ == "__main__":
    unittest.main()
