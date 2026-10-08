# coding: utf-8
"""运行时 hollow 告警：换一家券商行为不一样时，要能被发现。

静态闸（test_trade_context_deferral_guard.py）保证**我们自己**不写漏 defer，
但它挡不住「这家券商的 QMT 行为和我们假设的不一样」。所以运行时再看一眼：
交易类响应回来「行数对、字段全空」时记一条 warning，把函数名和当前线程名
一起写出来。

只告警、不自动改路由 —— 两个原因：

  1. defer 实测只要 2.5-3.0ms（和 inline 的行情读 3.3ms 同量级），而猜错方向
     的代价是静默返回错数据，两边完全不对等。
  2. 「跑错线程」和「这个账户真没数据」从返回值上分不开。账户本来就没担保标的
     时两条路都返回空，据此自动切换只会把一次误判固化下来。

所以让「每家不一样」成为被观测到的事实，而不是被猜测后自动应对的。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.adapters.market_bigqmt import BigQmtMarketDataProvider
from bigqmt_signal_trader.adapters.order_dryrun import DryRunOrderGateway
from bigqmt_signal_trader import redis_rpc
from bigqmt_signal_trader.redis_rpc import BigQmtRpcHandlers


class _Ctx(object):
    def get_full_tick(self, codes):
        return {}


class _CapturingLogger(object):
    def __init__(self):
        self.warnings = []

    def warning(self, fmt, *args):
        self.warnings.append(fmt % args if args else fmt)

    def __getattr__(self, _name):
        return lambda *a, **k: None


class _LoggerPatch(object):
    """logging_setup.get_logger 是在函数体里 import 的，从模块上打桩。"""

    def __init__(self):
        self.logger = _CapturingLogger()

    def __enter__(self):
        from bigqmt_signal_trader import logging_setup
        self._module = logging_setup
        self._original = logging_setup.get_logger
        logging_setup.get_logger = lambda _name: self.logger
        return self.logger

    def __exit__(self, *exc):
        self._module.get_logger = self._original
        return False


def _handlers(qmt_api=None, gateway=None):
    return BigQmtRpcHandlers(
        account_id="acct",
        market_data=BigQmtMarketDataProvider(_Ctx()),
        position_provider=None,
        order_gateway=gateway if gateway is not None else DryRunOrderGateway(),
        qmt_api=qmt_api or {},
    )


HOLLOW = [{"m_strInstrumentID": None, "m_dAssureRatio": None}] * 3
REAL = [{"m_strInstrumentID": "600000", "m_dAssureRatio": 0.7}]


class HollowRuntimeWarningTest(unittest.TestCase):

    def test_hollow_rows_raise_a_warning_naming_the_function_and_thread(self):
        with _LoggerPatch() as log:
            _handlers({"get_assure_contract": lambda a: HOLLOW}).handle(
                "get_assure_contract", {})

        self.assertEqual(len(log.warnings), 1, log.warnings)
        message = log.warnings[0]
        self.assertIn("get_assure_contract", message)
        self.assertIn("every field empty", message)
        self.assertIn("thread", message)
        # 必须说清楚「调用没失败」，否则读日志的人会去查错方向
        self.assertIn("did NOT fail", message)
        self.assertIn("#204", message)

    def test_real_rows_are_silent(self):
        with _LoggerPatch() as log:
            _handlers({"get_assure_contract": lambda a: REAL}).handle(
                "get_assure_contract", {})
        self.assertEqual(log.warnings, [])

    def test_empty_result_is_silent(self):
        """真的没数据不是 hollow —— 不能对空账户天天报警。"""
        with _LoggerPatch() as log:
            _handlers({"get_assure_contract": lambda a: []}).handle(
                "get_assure_contract", {})
        self.assertEqual(log.warnings, [])

    def test_zero_valued_fields_are_silent(self):
        """没有负债就是 0，是真数据。"""
        with _LoggerPatch() as log:
            _handlers({"get_unclosed_compacts":
                       lambda a, t: [{"m_dRealCompactBalance": 0.0}]}).handle(
                "get_unclosed_compacts", {})
        self.assertEqual(log.warnings, [])

    def test_warning_is_throttled(self):
        """跑错线程时每一次查询都 hollow，不节流日志会被刷爆（#139 教训）。"""
        handlers = _handlers({"get_assure_contract": lambda a: HOLLOW})
        with _LoggerPatch() as log:
            for _ in range(20):
                handlers.handle("get_assure_contract", {})
        self.assertEqual(len(log.warnings), 1, len(log.warnings))

    def test_throttle_interval_is_a_minute_scale_not_a_millisecond_one(self):
        self.assertGreaterEqual(redis_rpc.HOLLOW_WARNING_INTERVAL_SECONDS, 30.0)

    def test_get_trade_detail_data_path_is_covered_too(self):
        """走 gateway 的那条路（get_asset / query_account_infos …）也要盯。"""
        class _Gw(DryRunOrderGateway):
            def __init__(self):
                super(_Gw, self).__init__()
                self.get_trade_detail_data = lambda a, t, d, s="": [
                    {"m_dBalance": None, "m_dAvailable": None}]

        with _LoggerPatch() as log:
            _handlers(gateway=_Gw()).handle("query_account_infos", {})

        self.assertEqual(len(log.warnings), 1, log.warnings)
        self.assertIn("get_trade_detail_data", log.warnings[0])
        self.assertIn("ACCOUNT", log.warnings[0])

    def test_a_broken_logger_never_breaks_the_rpc(self):
        """告警是诊断，不能让它把请求带崩。"""
        class _Exploding(object):
            def warning(self, *a, **k):
                raise RuntimeError("logger down")

        from bigqmt_signal_trader import logging_setup
        original = logging_setup.get_logger
        logging_setup.get_logger = lambda _n: _Exploding()
        try:
            rows = _handlers({"get_assure_contract": lambda a: HOLLOW}).handle(
                "get_assure_contract", {})
        finally:
            logging_setup.get_logger = original
        self.assertEqual(len(rows), 3)

    def test_the_rows_are_passed_through_unchanged(self):
        """只观测，不改数据 —— 调用方看到的还是 QMT 给的那份。"""
        out = _handlers({"get_assure_contract": lambda a: HOLLOW}).handle(
            "get_assure_contract", {})
        self.assertEqual(out, HOLLOW)


if __name__ == "__main__":
    unittest.main()
