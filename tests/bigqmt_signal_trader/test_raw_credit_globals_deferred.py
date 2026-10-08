# coding: utf-8
"""#204 / #205：真两融账户体检报告暴露的两个问题（#201 的后续）。

一位有两融账户的用户跑 tools/credit_api_report.py 回报，同一次运行里：

  query_stk_compacts      -> get_unclosed_compacts      52 行
  get_unclosed_compacts   -> get_unclosed_compacts       0 行   <-- 直连的空
  query_credit_subjects   -> get_assure_contract      71002 行
  get_assure_contract     -> get_assure_contract          0 行   <-- 直连的空
  query_credit_slo_code   -> get_enable_short_contract   50 行
  get_enable_short_contract -> 同一个函数                 0 行   <-- 直连的空

两边的 handler 逐字节相同、账号相同、QMT 函数相同，唯一的差别是
query_* 在 LISTENER_DEFERRED_METHODS 里（走 adjust 主线程），而直连的
get_* 不在（走后台 listener 线程）。这正是 get_ipo_data 早就踩过并已经
修掉的那个坑 —— 它的注释原文就是「交易类查询, 需主线程上下文 (后台线程
返回空)」。几个 get_* 孪生方法当时被漏掉了。

顺带：get_hkt_exchange_rate 的官方签名是 (accountID, accountType)，
handler 一个参数都没传，而 _call_qmt_global 会把 TypeError 吞掉返回 []
—— 又一次「静默返回空」，和 probe 少传一个参数把好接口报成坏的同类。
"""
import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.adapters.market_bigqmt import BigQmtMarketDataProvider
from bigqmt_signal_trader.adapters.order_dryrun import DryRunOrderGateway
from bigqmt_signal_trader.redis_rpc import (
    LISTENER_DEFERRED_METHODS,
    READ_METHODS,
    BigQmtRpcHandlers,
)


class _Ctx(object):
    def get_full_tick(self, codes):
        return {}


def _handlers(qmt_api=None):
    return BigQmtRpcHandlers(
        account_id="acct",
        market_data=BigQmtMarketDataProvider(_Ctx()),
        position_provider=None,
        order_gateway=DryRunOrderGateway(),
        qmt_api=qmt_api or {},
    )


class RawCreditGlobalsMustRunOnMainThreadTest(unittest.TestCase):
    """直连的 get_* 和它的 query_* 孪生方法必须待在同一条线程路径上。"""

    # (直连方法, 它的 query_* 孪生, 底层 QMT 全局函数)
    TWINS = (
        ("get_unclosed_compacts", "query_stk_compacts", "get_unclosed_compacts"),
        ("get_assure_contract", "query_credit_subjects", "get_assure_contract"),
        ("get_enable_short_contract", "query_credit_slo_code",
         "get_enable_short_contract"),
    )

    def test_each_raw_twin_is_deferred_like_its_query_twin(self):
        for raw, query, _global in self.TWINS:
            self.assertIn(query, LISTENER_DEFERRED_METHODS, query)
            self.assertIn(
                raw, LISTENER_DEFERRED_METHODS,
                "%s 和 %s 调同一个 QMT 函数，却跑在不同线程上 —— 实盘上直连那个"
                "返回空而 query_* 有数据，就是这么来的" % (raw, query))

    def test_the_other_trade_context_globals_are_deferred_too(self):
        """凡是要交易上下文的 QMT 全局函数，都不能留在后台线程上。"""
        for name in ("get_closed_compacts", "get_debt_contract",
                     "get_option_subject_position", "get_comb_option",
                     "get_new_purchase_limit", "get_hkt_exchange_rate"):
            self.assertIn(name, LISTENER_DEFERRED_METHODS, name)

    def test_market_data_reads_stay_inline(self):
        """别顺手把行情读也 defer 了 —— 那会白白搭上一个 adjust 间隔的延迟。"""
        for name in ("get_ticks", "get_market_data", "get_market_data_ex",
                     "get_option_list", "get_main_contract",
                     "get_contract_multiplier", "ping"):
            self.assertIn(name, READ_METHODS, name)
            self.assertNotIn(name, LISTENER_DEFERRED_METHODS, name)

    def test_raw_and_query_twins_call_the_global_identically(self):
        """两条路必须把同样的参数交给同一个函数，否则修了线程也还是不一致。"""
        for raw, query, global_name in self.TWINS:
            seen = []

            def _record(*args):
                seen.append(args)
                return [{"m_strInstrumentID": "600000"}]

            handlers = _handlers({global_name: _record})
            raw_rows = handlers.handle(raw, {})
            query_rows = handlers.handle(query, {})

            self.assertEqual(len(seen), 2, raw)
            self.assertEqual(seen[0], seen[1],
                             "%s 和 %s 传给 %s 的参数不一致: %r vs %r"
                             % (raw, query, global_name, seen[0], seen[1]))
            self.assertEqual(raw_rows, query_rows, raw)


class HktExchangeRateArityTest(unittest.TestCase):
    """get_hkt_exchange_rate(accountID, accountType) —— 两个参数都得传。"""

    def test_passes_account_id_and_account_type(self):
        seen = []

        def get_hkt_exchange_rate(account_id, account_type):
            seen.append((account_id, account_type))
            return {"bidReferenceRate": 0.91, "askReferenceRate": 0.92}

        result = _handlers({"get_hkt_exchange_rate": get_hkt_exchange_rate}).handle(
            "get_hkt_exchange_rate", {})

        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], "acct")
        self.assertEqual(seen[0][1], "HUGANGTONG")
        self.assertEqual(result["bidReferenceRate"], 0.91)

    def test_account_type_is_overridable_for_shenzhen(self):
        seen = []

        def get_hkt_exchange_rate(account_id, account_type):
            seen.append(account_type)
            return {}

        _handlers({"get_hkt_exchange_rate": get_hkt_exchange_rate}).handle(
            "get_hkt_exchange_rate", {"account_type": "SHENGANGTONG"})

        self.assertEqual(seen, ["SHENGANGTONG"])

    def test_zero_arg_call_would_have_been_swallowed_as_empty(self):
        """钉住这个 bug 的形状：参数不对时 _call_qmt_global 静默返回空。"""
        def get_hkt_exchange_rate(account_id, account_type):
            return {"bidReferenceRate": 0.91}

        # 传对了就有数据；这条用例的价值在于它在修复前必然拿到 {}
        result = _handlers({"get_hkt_exchange_rate": get_hkt_exchange_rate}).handle(
            "get_hkt_exchange_rate", {})
        self.assertTrue(result)


class HollowRowProbeTest(unittest.TestCase):
    """行数不等于有数据 —— probe 自己踩过这个坑，这组用例把它钉死。

    QMT 交易类查询跑在主策略线程之外时，返回的不是空列表，而是**行数对、
    字段全空**的对象（本机实测：get_asset 移出 defer 名单后照样回 5 个键，
    cash / total_asset / frozen_cash / market_value 全是 None）。原来 probe
    只报 len(rows)，于是在一份真两融账户的报告里给出 ok:true rows:71002，
    而同一次运行走 handler 的同一个函数是 0 行 —— 探测把人带偏了。
    """

    def test_hollow_rows_are_flagged_not_reported_as_success(self):
        def get_assure_contract(account_id):
            # 71002 行，每行字段名齐全、值全是 None —— 跑错线程的签名
            return [{"m_strInstrumentID": None, "m_dAssureRatio": None}] * 3

        info = _handlers({"get_assure_contract": get_assure_contract}).handle(
            "probe_capabilities", {})
        probe = info["credit_probe"]["get_assure_contract"]

        self.assertEqual(probe["rows"], 3)
        self.assertTrue(probe["hollow"])
        self.assertEqual(probe["populated_fields"], 0)
        self.assertIn("字段全空", probe["note"])

    def test_real_rows_are_not_flagged_hollow(self):
        def get_assure_contract(account_id):
            return [{"m_strInstrumentID": "600000", "m_dAssureRatio": 0.7}]

        probe = _handlers({"get_assure_contract": get_assure_contract}).handle(
            "probe_capabilities", {})["credit_probe"]["get_assure_contract"]

        self.assertEqual(probe["rows"], 1)
        self.assertNotIn("hollow", probe)
        self.assertEqual(probe["populated_fields"], 2)

    def test_zero_is_a_real_value_not_an_empty_one(self):
        """没有负债就是 0 —— 不能把 0 当成「没拿到」。"""
        def get_unclosed_compacts(account_id, account_type):
            return [{"m_dRealCompactBalance": 0.0, "m_nRealCompactVol": 0}]

        probe = _handlers({"get_unclosed_compacts": get_unclosed_compacts}).handle(
            "probe_capabilities", {})["credit_probe"]["get_unclosed_compacts"]

        self.assertNotIn("hollow", probe)
        self.assertEqual(probe["populated_fields"], 2)

    def test_a_row_with_no_fields_is_not_hollow(self):
        """零字段是「没数据」，不是「空行」—— 判错会把返回 {} 的接口报成跑错线程。"""
        probe = _handlers({"get_debt_contract": lambda a: [{}]}).handle(
            "probe_capabilities", {})["credit_probe"]["get_debt_contract"]

        self.assertEqual(probe["rows"], 1)
        self.assertNotIn("hollow", probe)

    def test_empty_result_is_not_hollow(self):
        """真的没数据和拿到一堆空行是两回事。"""
        probe = _handlers({"get_debt_contract": lambda a: []}).handle(
            "probe_capabilities", {})["credit_probe"]["get_debt_contract"]

        self.assertEqual(probe["rows"], 0)
        self.assertNotIn("hollow", probe)

    def test_credit_account_object_checks_values_not_key_presence(self):
        """键在不等于值在 —— has_credit_fields 原来只看键名。"""
        class _Gw(DryRunOrderGateway):
            def __init__(self, value):
                super(_Gw, self).__init__()
                self.get_trade_detail_data = lambda a, t, d, s="": [{
                    "m_nBrokerType": 3,
                    "m_dPerAssurescaleValue": value,
                    "m_dFinMaxQuota": value,
                    "m_dSloMaxQuota": value,
                }]

        def _probe(gateway):
            return BigQmtRpcHandlers(
                account_id="acct",
                market_data=BigQmtMarketDataProvider(_Ctx()),
                position_provider=None,
                order_gateway=gateway,
                qmt_api={},
            ).handle("probe_capabilities", {})[
                "credit_probe"]["get_trade_detail_data(CREDIT,ACCOUNT)"]

        # 信用字段全 None、但 m_nBrokerType 有值：整行不算全空（所以不标
        # hollow，那是对的），可是 has_credit_fields 必须是 False —— 调用方
        # 要的那几个字段一个都没拿到。
        partial = _probe(_Gw(None))
        self.assertFalse(partial["has_credit_fields"])
        self.assertEqual(partial["populated_fields"], 1)
        self.assertNotIn("hollow", partial)

        real = _probe(_Gw(2.87))
        self.assertTrue(real["has_credit_fields"])
        self.assertEqual(real["populated_fields"], 4)
        self.assertNotIn("hollow", real)


class ThreadRoutingProbeTest(unittest.TestCase):
    """probe_capabilities 要报出线程路由 —— #204 就是被这个盲区拖住的。"""

    class _FakeService(object):
        process_in_listener = True

        def __init__(self, listener):
            self.listener_methods = set(listener)

        def _should_process_in_listener(self, payload):
            return str((payload or {}).get("method") or "") in self.listener_methods

    def test_reports_which_thread_each_sample_method_runs_on(self):
        handlers = _handlers()
        handlers.rpc_service = self._FakeService({"ping", "get_ticks"})

        routing = handlers.handle("probe_capabilities", {})["thread_routing"]

        self.assertTrue(routing["available"])
        self.assertTrue(routing["process_in_listener"])
        self.assertEqual(routing["listener_method_count"], 2)
        self.assertEqual(routing["sample"]["ping"], "listener_thread")
        self.assertEqual(routing["sample"]["get_ticks"], "listener_thread")
        # 交易类查询必须落在主线程上，这正是 #204 修的
        for name in ("query_credit_detail", "get_unclosed_compacts",
                     "get_assure_contract", "get_enable_short_contract",
                     "get_ipo_data", "get_asset", "get_positions"):
            self.assertEqual(routing["sample"][name], "adjust_thread", name)

    def test_degrades_when_there_is_no_service_backref(self):
        handlers = _handlers()
        routing = handlers.handle("probe_capabilities", {})["thread_routing"]
        self.assertFalse(routing["available"])


if __name__ == "__main__":
    unittest.main()
