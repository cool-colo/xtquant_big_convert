# coding: utf-8
"""tools/credit_api_report.py —— 两融只读体检报告工具。

这个工具存在的理由是维护者没有两融账户：两融那一整片只能靠别人跑一次回报。
所以它自己必须是对的，否则收回来的报告会把人带偏。这组用例钉住三件事：

  1. **不能下单。** 清单里只能有查询方法。
  2. **默认不能泄露。** 报告是要贴到公开 issue 上的 —— 账号打码、金额不带原值、
     get_positions 的键（就是股票代码，等于持仓）不能进报告。
  3. **结论要能分辨「空」的几种成因。** 不是信用账户 / 桥不通 / 接口没绑 /
     被限流 / 真的没数据 —— 分不开的话，报告只是把「空列表」换个地方再说一遍。
"""
import io
import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "src"))

import credit_api_report as rep  # noqa: E402


ALL_CHECKS = (rep.CREDIT_ACCOUNT_CHECKS + rep.CREDIT_CONTRACT_CHECKS
              + rep.OTHER_TRADE_GLOBAL_CHECKS + rep.CONTROL_CHECKS)


class ReadOnlyTest(unittest.TestCase):
    """工具号称只读，这条要能被证伪才算数。"""

    WRITE_METHODS = (
        "submit_order", "submit_orders_batch", "cancel_order", "passorder",
        "order_stock", "order_stock_async", "smt_appointment",
        "create_sector", "add_sector", "remove_sector", "reset_sector",
    )

    def test_no_write_method_in_any_checklist(self):
        methods = [check[0] for check in ALL_CHECKS]
        for bad in self.WRITE_METHODS:
            self.assertNotIn(bad, methods)

    def test_every_check_is_a_query(self):
        for check in ALL_CHECKS:
            method = check[0]
            self.assertTrue(
                method.startswith(("query_", "get_")) or method == "ping",
                "非查询方法进了清单: %s" % method)

    def test_source_never_calls_an_order_entry_point(self):
        text = io.open(os.path.join(ROOT, "tools", "credit_api_report.py"),
                       encoding="utf-8").read()
        for bad in ("passorder", "order_stock", "submit_order", "cancel_order"):
            self.assertNotIn('"%s"' % bad, text)


class RedactionTest(unittest.TestCase):

    def test_account_id_is_masked(self):
        self.assertEqual(rep.mask_account("8886800503"), "88******03")
        self.assertNotIn("88680050", rep.mask_account("8886800503"))

    def test_amounts_are_bucketed_not_reported(self):
        self.assertEqual(rep.describe_value(120230.88, full=False), "non-zero")
        self.assertEqual(rep.describe_value(0.0, full=False), "zero")
        self.assertEqual(rep.describe_value(120230.88, full=True), 120230.88)

    def test_account_kind_fields_keep_their_real_value(self):
        """m_nBrokerType 打了码报告就没用了 —— 它是判断信用账户的唯一依据。"""
        out = rep.summarize_rows([{"m_nBrokerType": 3, "m_dBalance": 120230.88}],
                                 full=False)
        self.assertEqual(out["values"]["m_nBrokerType"], 3)
        self.assertEqual(out["values"]["m_dBalance"], "non-zero")

    def test_data_keyed_fields_are_redacted(self):
        """get_positions 的键是股票代码 —— 那就是持仓，不能进公开报告。"""
        row = {"600000.SH": {"volume": 100}, "000001.SZ": {"volume": 200}}
        out = rep.summarize_rows([row], full=False, keys_are_data=True)

        self.assertTrue(out["fields_redacted"])
        self.assertEqual(out["fields"], [])
        self.assertEqual(out["field_count"], 2)
        rendered = repr(out)
        self.assertNotIn("600000.SH", rendered)
        self.assertNotIn("000001.SZ", rendered)

    def test_full_mode_does_include_them(self):
        row = {"600000.SH": {"volume": 100}}
        out = rep.summarize_rows([row], full=True, keys_are_data=True)
        self.assertEqual(out["fields"], ["600000.SH"])

    def test_positions_check_is_marked_as_data_keyed(self):
        """清单本身要标出来，否则上面那条防护根本不会生效。"""
        by_method = dict((c[0], c) for c in rep.CONTROL_CHECKS)
        positions = by_method["get_positions"]
        self.assertGreater(len(positions), 4)
        self.assertTrue(positions[4])


class SummaryTest(unittest.TestCase):

    def test_key_credit_fields_are_singled_out(self):
        out = rep.summarize_rows([{
            "m_dPerAssurescaleValue": 2.87,
            "m_dTotalDebt": 12345.0,
            "m_dSloMaxQuota": 0.0,
            "m_strAccountID": "acct",
        }], full=False)
        self.assertIn("m_dPerAssurescaleValue", out["key_credit_fields_present"])
        self.assertIn("m_dPerAssurescaleValue", out["key_credit_fields_non_zero"])
        self.assertNotIn("m_dSloMaxQuota", out["key_credit_fields_non_zero"])

    def test_both_spellings_of_total_debt_are_known(self):
        """柜台那份是 m_dTotalDebt，缓存那份是 m_dTotalDebit，只差一个字母。"""
        self.assertIn("m_dTotalDebt", rep.KEY_CREDIT_FIELDS)
        self.assertIn("m_dTotalDebit", rep.KEY_CREDIT_FIELDS)

    def test_all_zero_rows_are_flagged(self):
        """字段都在、值全是 0 —— 和「没数据」不是一回事，要分开报。"""
        out = rep.summarize_rows([{"m_dBalance": 0.0, "m_dTotalDebt": 0.0}],
                                 full=False)
        self.assertTrue(out["all_numeric_zero"])
        state, _detail = rep.verdict_for(dict(out, ok=True, method="x"))
        self.assertEqual(state, "有行但数值全为 0")

    def test_hollow_rows_are_not_reported_as_data(self):
        """行数对、值全 None —— 报告不能判成「有数据」（#204）。"""
        out = rep.summarize_rows(
            [{"m_dPerAssurescaleValue": None, "m_dTotalDebt": None}], full=False)

        self.assertTrue(out["hollow"])
        self.assertEqual(out["populated_fields"], 0)
        state, detail = rep.verdict_for(dict(out, ok=True, row_count=1))
        self.assertEqual(state, "有行但字段全空")
        self.assertIn("thread_routing", detail)

    def test_zero_is_data_not_hollow(self):
        """没有负债就是 0，不能当成没拿到。"""
        out = rep.summarize_rows(
            [{"m_dTotalDebt": 0.0, "m_dFinDebt": 0}], full=False)
        self.assertFalse(out["hollow"])
        self.assertEqual(out["populated_fields"], 2)

    def test_empty_mapping_is_no_data_not_a_hollow_row(self):
        """空 dict 是没数据。当成一行的话 hollow 判据会对它成立 —— 实跑第一次
        就把已经 defer 的 get_ipo_data 误报成跑错线程了。"""
        rows, _extra = rep.normalize_result({})
        self.assertEqual(rows, [])

        out = rep.summarize_rows(rows, full=False)
        self.assertEqual(out["row_count"], 0)
        self.assertFalse(out.get("hollow"))
        state, _ = rep.verdict_for(dict(out, ok=True))
        self.assertEqual(state, "空")

    def test_non_empty_mapping_is_still_one_row(self):
        rows, _extra = rep.normalize_result({"KCB": 3500, "SH": 3500})
        self.assertEqual(len(rows), 1)
        out = rep.summarize_rows(rows, full=False)
        self.assertFalse(out["hollow"])
        self.assertEqual(out["populated_fields"], 2)

    def test_conclusions_name_the_hollow_methods(self):
        report = {
            "account_shape": {"broker_type": 3},
            "checks": {
                "credit_account": [
                    {"method": "query_credit_detail", "ok": True,
                     "row_count": 1, "hollow": True}],
                "credit_contracts": [],
                "control": [{"method": "query_account_infos", "ok": True,
                             "row_count": 1}],
            },
        }
        joined = "\n".join(rep.build_conclusions(report))
        self.assertIn("字段全是 None", joined)
        self.assertIn("query_credit_detail", joined)
        self.assertIn("thread_routing", joined)

    def test_envelope_is_preserved_verbatim(self):
        """query_credit_account 的信封就是判断柜台通没通的依据，不能被归纳掉。"""
        rows, extra = rep.normalize_result({
            "rows": [], "query_issued": False, "callback_bound": False,
            "not_issued_reason": "query_credit_account is not bound",
        })
        self.assertEqual(rows, [])
        self.assertFalse(extra["callback_bound"])
        self.assertIn("not bound", extra["not_issued_reason"])


class VerdictTest(unittest.TestCase):
    """「空」有好几种成因，报告要说出是哪一种。"""

    def test_unbound_global(self):
        state, detail = rep.verdict_for({
            "ok": True, "row_count": 0,
            "envelope": {"callback_bound": False,
                         "not_issued_reason": "not bound in this deployment"},
        })
        self.assertEqual(state, "接口没绑上")
        self.assertIn("not bound", detail)

    def test_rate_limited(self):
        state, detail = rep.verdict_for({
            "ok": True, "row_count": 0,
            "envelope": {"callback_bound": True, "query_issued": False,
                         "not_issued_reason": "rate limited: 3.0s since..."},
        })
        self.assertEqual(state, "没发出查询")
        self.assertIn("rate limited", detail)

    def test_issued_but_no_callback(self):
        state, _ = rep.verdict_for({
            "ok": True, "row_count": 0,
            "envelope": {"callback_bound": True, "query_issued": True,
                         "fresh": False},
        })
        self.assertEqual(state, "发了但没等到回调")

    def test_plain_error(self):
        state, detail = rep.verdict_for({"ok": False, "error": "boom"})
        self.assertEqual(state, "报错")
        self.assertEqual(detail, "boom")

    def test_has_data(self):
        state, _ = rep.verdict_for({
            "ok": True, "row_count": 1, "field_count": 40,
            "key_credit_fields_non_zero": ["m_dPerAssurescaleValue"],
        })
        self.assertEqual(state, "有数据")


class ConclusionTest(unittest.TestCase):

    def _report(self, credit_account, contracts=None, control=None,
                broker_type=3):
        return {
            "account_shape": {"broker_type": broker_type},
            "checks": {
                "credit_account": credit_account,
                "credit_contracts": contracts or [],
                "control": control or [
                    {"method": "query_account_infos", "ok": True, "row_count": 1}],
            },
        }

    def test_non_credit_account_is_called_out_loudly(self):
        """普通账户跑出来的空报告说明不了任何事 —— 必须写清楚，不能当证据用。"""
        out = rep.build_conclusions(self._report(
            [{"method": "query_credit_detail", "ok": True, "row_count": 0},
             {"method": "query_credit_account", "ok": True, "row_count": 0}],
            broker_type=2))
        joined = "\n".join(out)
        self.assertIn("不是", joined)
        self.assertIn("说明不了", joined)

    def test_cached_empty_counter_ok_points_at_the_fallback(self):
        out = rep.build_conclusions(self._report([
            {"method": "query_credit_detail", "ok": True, "row_count": 0},
            {"method": "query_credit_account", "ok": True, "row_count": 1},
        ]))
        joined = "\n".join(out)
        self.assertIn("柜台那条通", joined)

    def test_both_paths_working_is_reported(self):
        out = rep.build_conclusions(self._report([
            {"method": "query_credit_detail", "ok": True, "row_count": 1},
            {"method": "query_credit_account", "ok": True, "row_count": 1},
        ]))
        self.assertIn("两条路都有数据", "\n".join(out))

    def test_unbound_global_never_claims_the_terminal_lacks_it(self):
        """global_namespace=False 不等于「终端没有这个函数」。

        #202 的教训：入口文件手抄的名单漏了 query_credit_account，桥拿不到它，
        报告据此断言「这台终端没有这个函数，重启也没用」—— 而用户在大 QMT 里
        直接调是好用的。一个桥的 bug 被报成了券商终端的能力缺失。错误但听起来
        很确定的结论，比不给结论更坏。
        """
        report = self._report([
            {"method": "query_credit_detail", "ok": True, "row_count": 0},
            {"method": "query_credit_account", "ok": True, "row_count": 0,
             "envelope": {"callback_bound": False}},
        ])
        report["probe"] = {"global_namespace": {"query_credit_account": False}}
        joined = "\n".join(rep.build_conclusions(report))
        # 两种可能都要列出来，并且要给出能分辨的办法
        self.assertIn("两种可能", joined)
        self.assertIn("capture_qmt_injected_funcs", joined)
        self.assertIn("重启", joined)
        self.assertIn("大 QMT 里直接调", joined)
        # 绝不能再断言终端的能力
        self.assertNotIn("这台终端没有 query_credit_account", joined)
        self.assertNotIn("重启策略也解决不了", joined)

    def test_bound_in_namespace_but_unbound_points_at_the_restart(self):
        report = self._report([
            {"method": "query_credit_detail", "ok": True, "row_count": 0},
            {"method": "query_credit_account", "ok": True, "row_count": 0,
             "envelope": {"callback_bound": False}},
        ])
        report["probe"] = {"global_namespace": {"query_credit_account": True}}
        joined = "\n".join(rep.build_conclusions(report))
        self.assertIn("重启策略", joined)
        self.assertNotIn("重启策略也解决不了", joined)

    def test_dead_bridge_is_diagnosed_before_blaming_credit(self):
        """对照组也空的话，问题不在两融接口 —— 别让人去查错的地方。"""
        out = rep.build_conclusions(self._report(
            [{"method": "query_credit_detail", "ok": True, "row_count": 0}],
            control=[{"method": "query_account_infos", "ok": True, "row_count": 0}]))
        self.assertIn("问题多半不在两融接口", "\n".join(out))


class CoverageTest(unittest.TestCase):
    """清单要盖全 —— 漏一个接口，别人跑一趟也照样查不出来。"""

    def test_every_credit_rpc_method_is_covered(self):
        from bigqmt_signal_trader.redis_rpc import READ_METHODS

        credit_methods = set(m for m in READ_METHODS
                             if "credit" in m or "compact" in m
                             or "assure" in m or "short_contract" in m
                             or m == "get_debt_contract")
        covered = set(check[0] for check in ALL_CHECKS)
        self.assertEqual(sorted(credit_methods - covered), [])

    def test_every_method_the_fixes_touched_is_exercised(self):
        """报告是拿回验证的唯一手段 —— 漏一个方法，那个修复就永远没人验。

        #204 把 9 个交易类全局函数推回主线程，#205 修了 get_hkt_exchange_rate
        的参数。这些必须都在清单里，否则用户跑一趟也测不到。
        """
        covered = set(check[0] for check in ALL_CHECKS)
        for name in ("get_assure_contract", "get_enable_short_contract",
                     "get_unclosed_compacts", "get_closed_compacts",
                     "get_debt_contract", "get_option_subject_position",
                     "get_comb_option", "get_new_purchase_limit",
                     "get_hkt_exchange_rate"):
            self.assertIn(name, covered, "#204 推回主线程的 %s 没进报告清单" % name)

    def test_deferred_method_list_and_report_checklist_agree(self):
        """defer 名单里的交易类方法，报告清单要跟着走 —— 两边别再漂。"""
        from bigqmt_signal_trader.redis_rpc import LISTENER_DEFERRED_METHODS

        covered = set(check[0] for check in ALL_CHECKS)
        credit_ish = set(m for m in LISTENER_DEFERRED_METHODS
                         if "credit" in m or "compact" in m or "assure" in m
                         or "short_contract" in m or "debt" in m)
        self.assertEqual(sorted(credit_ish - covered), [])

    def test_both_account_detail_paths_are_checked(self):
        methods = set(c[0] for c in rep.CREDIT_ACCOUNT_CHECKS)
        self.assertEqual(methods, {"query_credit_detail", "query_credit_account"})

    def test_credit_order_type_constants_match_xtconstant(self):
        """PR #88 把 33/34 当成了信用融资类型，其实那是股票期权。这条钉住。"""
        from xtquant import xtconstant

        for name, expected, _meaning in rep.CREDIT_ORDER_TYPES:
            self.assertEqual(getattr(xtconstant, name), expected, name)

    def test_report_renders_without_a_live_bridge(self):
        report = {
            "meta": {"generated_at": "2026-09-06 00:00:00",
                     "account_masked": "88******03", "client_version": "0.0.0",
                     "bridge_version": "0.0.0", "full_values": False},
            "account_shape": {"broker_type": 3, "configured_account_type": "CREDIT",
                              "verdict": "信用账户"},
            "checks": {"credit_account": [
                {"method": "query_credit_detail", "label": "x", "qmt_backend": "y",
                 "ok": True, "row_count": 0, "elapsed_ms": 1.0}],
                "credit_contracts": [], "control": []},
            "probe": {"credit_probe": {}},
            "order_types": [],
            "conclusions": ["ok"],
        }
        text = rep.render_text(report)
        self.assertIn("两融", text)
        self.assertIn("88******03", text)
        self.assertIn("没有下过任何单", text)


if __name__ == "__main__":
    unittest.main()
