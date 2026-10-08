"""xtquant shim 层签名兼容测试。

原生 xtquant.xtdata 的调用方依赖:
- get_instrument_detail(stock_code, is_detail) 双参调用
- download_history_data / download_history_data2 携带 dividend_type,
  保证下载与 get_local_data 读取使用同一复权类型 (否则 front 读取落空)。
"""

import os
import sys
import unittest
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from xtquant import xtdata as shim
import bigqmt_signal_trader.xtquant_compat as _compat


class _FakeXtData:
    def __init__(self):
        self.calls = []

    def get_instrument_detail(self, stock_code):
        self.calls.append(("get_instrument_detail", (stock_code,)))
        return {"InstrumentName": "浦发银行"}

    def download_history_data(self, stock_code, period, start_time="", end_time="",
                              incrementally=None, dividend_type="none"):
        self.calls.append((
            "download_history_data",
            (stock_code, period, start_time, end_time, incrementally, dividend_type),
        ))
        return True

    def download_history_data2(self, stock_list, period, start_time="", end_time="",
                               callback=None, incrementally=None, dividend_type="none",
                               chunk_size=None, download_timeout_seconds=180.0):
        self.calls.append((
            "download_history_data2",
            (tuple(stock_list), period, start_time, end_time, incrementally, dividend_type),
        ))
        return True

    def get_stock_type(self, stock_code, variety_list=None):
        # 真实包装会抛 NotImplementedError；这里只验证 shim 转发到哪
        self.calls.append(("get_stock_type", (stock_code,)))
        return "ETF"

    def call_method(self, method, **params):
        self.calls.append(("call_method", (method, params)))
        return "ETF"


class XtdataShimSignatureTest(unittest.TestCase):
    def setUp(self):
        self.fake = _FakeXtData()
        self._patcher = mock.patch.object(_compat, "xtdata", self.fake)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

    def test_get_instrument_detail_single_arg(self):
        detail = shim.get_instrument_detail("600000.SH")
        self.assertEqual(detail["InstrumentName"], "浦发银行")
        self.assertEqual(self.fake.calls, [("get_instrument_detail", ("600000.SH",))])

    def test_get_instrument_detail_two_args_native_compatible(self):
        # 原生 xtquant 调用形态: (stock_code, False)
        detail = shim.get_instrument_detail("600000.SH", False)
        self.assertEqual(detail["InstrumentName"], "浦发银行")
        self.assertEqual(self.fake.calls, [("get_instrument_detail", ("600000.SH",))])

    def test_download_history_data_passes_dividend_type(self):
        shim.download_history_data(
            "600000.SH", "1d", start_time="20260801", end_time="20260820",
            incrementally=True, dividend_type="front",
        )
        name, args = self.fake.calls[-1]
        self.assertEqual(name, "download_history_data")
        self.assertEqual(args[-1], "front")

    def test_download_history_data_default_dividend_type_is_none(self):
        shim.download_history_data("600000.SH", "1d")
        _, args = self.fake.calls[-1]
        self.assertEqual(args[-1], "none")

    def test_download_history_data2_passes_dividend_type(self):
        shim.download_history_data2(
            ["600000.SH", "000001.SZ"], "1d",
            start_time="20260801", end_time="20260820",
            incrementally=True, dividend_type="front",
        )
        name, args = self.fake.calls[-1]
        self.assertEqual(name, "download_history_data2")
        self.assertEqual(args[-1], "front")

    def test_get_stock_type_forwards_to_the_wrapper(self):
        """顶层 shim 只转发，不复制证券分类逻辑。

        以前走 call_method("get_stock_type")，绕开了包装。实盘验证后包装改成
        拒绝（服务端 ContextInfo stub 对任何代码都返回 0），两条路径必须一致，
        否则顶层 xtdata 还会把那个 0 递给调用方。详见
        test_get_stock_type_unavailable.py。
        """
        self.assertEqual(shim.get_stock_type("510300.SH"), "ETF")
        self.assertEqual(self.fake.calls, [("get_stock_type", ("510300.SH",))])


if __name__ == "__main__":
    unittest.main()
