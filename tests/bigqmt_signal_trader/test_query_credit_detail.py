# coding: utf-8
"""#201: query_credit_detail 返回空列表。

两融账户调 query_credit_detail 拿到 []。原实现把它路由到 get_debt_contract：

  - 语义就不对。get_debt_contract 是「负债合约明细」——一张张融资融券合约，
    不是信用账户对象。用户要的是维持担保比例 / 授信额度 / 合约金额那一组，
    即 CCreditAccountDetail（官方参考 3.14）。
  - 而且官方参考 6.17 把 get_debt_contract 标了【已弃用】，替代接口是
    get_unclosed_compacts / get_closed_compacts。
  - 于是没有未了结负债的两融账户必然拿到 []，有负债的也拿不到账户信息。

本仓自己的 docs/MiniQMT_2_BigQMT-Skill/api_mapping.md 一直写的是正确映射：
query_credit_detail(acc) -> get_trade_detail_data(account, 'CREDIT', 'account')。
代码和这份表对不上。

顺带：probe_capabilities 探测 get_unclosed_compacts 时只传了 account_id，
而它是两参数签名，实盘上撞 boost::python ArgumentError —— 把一个好用的接口
报成 ok:False。
"""
import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.adapters.market_bigqmt import BigQmtMarketDataProvider
from bigqmt_signal_trader.adapters.order_dryrun import DryRunOrderGateway
from bigqmt_signal_trader.redis_rpc import BigQmtRpcHandlers


CREDIT_ACCOUNT_ROW = {
    "m_strAccountID": "acct",
    "m_nBrokerType": 3,                    # 3 = 信用
    "m_dPerAssurescaleValue": 3.14,        # 个人维持担保比例
    "m_dFinMaxQuota": 1000000.0,           # 融资授信额度
    "m_dSloMaxQuota": 500000.0,            # 融券授信额度
    "m_dEnableBailBalance": 88888.0,       # 可用保证金
}

STOCK_ACCOUNT_ROW = {
    "m_strAccountID": "acct",
    "m_nBrokerType": 2,                    # 2 = 普通股票
    "m_dAssetBalance": 120230.88,
}


class _Ctx(object):
    def get_full_tick(self, codes):
        return {}


class RecordingGateway(DryRunOrderGateway):
    """记录 get_trade_detail_data 的每次调用，按 accountType 回不同的账户对象。"""

    account_type = "STOCK"        # 部署配置成普通账户，credit 查询仍须问 CREDIT

    def __init__(self):
        super(RecordingGateway, self).__init__()
        self.detail_calls = []
        self.get_trade_detail_data = self._get_trade_detail_data

    def _get_trade_detail_data(self, account_id, account_type, detail_type,
                               strategy_name=""):
        self.detail_calls.append((account_id, account_type, detail_type))
        if str(account_type).upper() == "CREDIT" and detail_type == "ACCOUNT":
            return [CREDIT_ACCOUNT_ROW]
        if detail_type == "ACCOUNT":
            return [STOCK_ACCOUNT_ROW]
        return []


def _handlers(gateway=None, qmt_api=None):
    return BigQmtRpcHandlers(
        account_id="acct",
        market_data=BigQmtMarketDataProvider(_Ctx()),
        position_provider=None,
        order_gateway=gateway if gateway is not None else RecordingGateway(),
        qmt_api=qmt_api or {},
    )


class QueryCreditDetailTest(unittest.TestCase):

    def test_asks_for_the_credit_account_object_not_debt_contracts(self):
        gateway = RecordingGateway()
        rows = _handlers(gateway).handle("query_credit_detail", {})

        self.assertEqual(gateway.detail_calls, [("acct", "CREDIT", "ACCOUNT")])
        self.assertEqual(len(rows), 1)
        # 信用账户特有字段必须在，这才是「两融账户信息」
        self.assertEqual(rows[0]["m_dPerAssurescaleValue"], 3.14)
        self.assertEqual(rows[0]["m_dFinMaxQuota"], 1000000.0)
        self.assertEqual(rows[0]["m_nBrokerType"], 3)

    def test_credit_type_is_forced_even_when_deployment_trades_as_stock(self):
        """部署配置成 STOCK，query_credit_detail 仍然问 CREDIT。

        账户类型是问哪本账，不是这台部署下单用哪本账；跟着配置走的话，一台
        按 STOCK 配置的部署永远查不到自己的信用账户。
        """
        gateway = RecordingGateway()
        gateway.account_type = "STOCK"
        rows = _handlers(gateway).handle("query_credit_detail", {})

        self.assertEqual(gateway.detail_calls[0][1], "CREDIT")
        self.assertEqual(rows[0]["m_nBrokerType"], 3)

    def test_deprecated_get_debt_contract_is_no_longer_the_source(self):
        """即使 get_debt_contract 绑着且有数据，也不该从它取账户信息。"""
        calls = []

        def get_debt_contract(account_id):
            calls.append(account_id)
            return [{"m_strCompactId": "C1"}]

        gateway = RecordingGateway()
        rows = _handlers(gateway, {"get_debt_contract": get_debt_contract}).handle(
            "query_credit_detail", {})

        self.assertEqual(calls, [])
        self.assertNotIn("m_strCompactId", rows[0])

    def test_account_infos_still_follows_the_configured_type(self):
        """query_account_infos 不受影响，仍按部署配置的账户类型查。"""
        gateway = RecordingGateway()
        rows = _handlers(gateway).handle("query_account_infos", {})

        self.assertEqual(gateway.detail_calls, [("acct", "STOCK", "ACCOUNT")])
        self.assertEqual(rows[0]["m_nBrokerType"], 2)

    def test_missing_get_trade_detail_data_degrades_to_empty(self):
        gateway = RecordingGateway()
        gateway.get_trade_detail_data = None
        self.assertEqual(_handlers(gateway).handle("query_credit_detail", {}), [])


class ProbeUnclosedCompactsArityTest(unittest.TestCase):
    """get_unclosed_compacts(accountID, accountType) — 探测必须传两个参数。"""

    def test_probe_passes_account_type_to_unclosed_compacts(self):
        calls = []

        def get_unclosed_compacts(account_id, account_type):
            calls.append((account_id, account_type))
            return [{"m_strCompactId": "C1"}]

        info = _handlers(None, {"get_unclosed_compacts": get_unclosed_compacts}).handle(
            "probe_capabilities", {})

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "acct")
        self.assertTrue(calls[0][1])
        probe = info["credit_probe"]["get_unclosed_compacts"]
        self.assertTrue(probe["ok"], probe.get("error"))
        self.assertEqual(probe["rows"], 1)

    def test_probe_reports_the_credit_account_object(self):
        """空列表要能分辨「不是信用账户」和「读不到」。"""
        gateway = RecordingGateway()
        info = _handlers(gateway).handle("probe_capabilities", {})

        probe = info["credit_probe"]["get_trade_detail_data(CREDIT,ACCOUNT)"]
        self.assertTrue(probe["ok"], probe.get("error"))
        self.assertEqual(probe["rows"], 1)
        self.assertEqual(probe["broker_type"], 3)
        self.assertTrue(probe["has_credit_fields"])

    def test_probe_credit_object_survives_a_missing_gateway_function(self):
        gateway = RecordingGateway()
        gateway.get_trade_detail_data = None
        info = _handlers(gateway).handle("probe_capabilities", {})
        self.assertFalse(
            info["credit_probe"]["get_trade_detail_data(CREDIT,ACCOUNT)"]["available"])

    def test_single_arg_globals_are_still_called_with_one_arg(self):
        calls = []

        def get_assure_contract(account_id):
            calls.append(account_id)
            return [{"a": 1}]

        info = _handlers(None, {"get_assure_contract": get_assure_contract}).handle(
            "probe_capabilities", {})

        self.assertEqual(calls, ["acct"])
        self.assertTrue(info["credit_probe"]["get_assure_contract"]["ok"])


if __name__ == "__main__":
    unittest.main()
