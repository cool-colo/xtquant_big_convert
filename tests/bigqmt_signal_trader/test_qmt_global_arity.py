# coding: utf-8
"""#207：机械校验每个 QMT 全局函数调用点的参数个数。

这一个 session 里，同一个形态的 bug 出现了六次：

  #96   get_ipo_data                  account_id 传到了 type 的位置
  #201  probe 探测                     单参数调两参数签名的 get_unclosed_compacts
  #205  get_hkt_exchange_rate         一个参数都没传（签名要两个）
  #207  get_value_by_order_id         只传 1 个（签名要 4 个）
  #207  get_last_order_id             只传 1 个（签名要 3-4 个）
  #207  get_history_trade_detail_data 传 4 个且错位（签名要 5 个）

每一次的表现都一样：`_call_qmt_global` 把 TypeError / boost::python 的
ArgumentError 吞掉返回空，从客户端看和「这台终端没这个功能」「这个账户没
数据」完全一样。**一个失败的调用和一个从没跑过的调用长得一模一样。**

映射是纯手写的，没有任何东西会发现它和官方签名对不上 —— 所以这里用 AST
把所有调用点扫出来，和下面这张按官方参考手抄的签名表比对。写错参数个数
从此是「测试红」，不是「线上静默返回空」。

签名表的出处一律是 docs/BIGQMT_INNER_PYTHON_API_REFERENCE.md，条目号写在
每一行后面，改之前先去核对文档。
"""
import ast
import io
import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

RPC_SOURCE = os.path.join(ROOT, "src", "bigqmt_signal_trader", "redis_rpc.py")

# QMT 全局函数 -> (最少参数, 最多参数)。None 表示不限。
# 出处：docs/BIGQMT_INNER_PYTHON_API_REFERENCE.md，条目号见注释。
OFFICIAL_ARITY = {
    "get_assure_contract": (1, 1),              # 6.12  (accId)
    "get_enable_short_contract": (1, 1),        # 6.12  (accId)
    "get_unclosed_compacts": (2, 2),            # 6.16  (accountID, accountType)
    "get_closed_compacts": (2, 2),              # 6.16  (accountID, accountType)
    "get_debt_contract": (1, 1),                # 6.17  (accId) 【已弃用】
    "get_ipo_data": (0, 1),                     # 6.10  (type="")
    "get_new_purchase_limit": (1, 1),           # 6.10  (accid)
    "get_option_subject_position": (1, 1),      # (accountID)
    "get_comb_option": (1, 1),                  # (accountID)
    "get_hkt_exchange_rate": (2, 2),            # 6.18  (accountID, accountType)
    "get_value_by_order_id": (4, 4),            # 6.11  (orderId, accountID,
                                                #        strAccountType, strDatatype)
    "get_last_order_id": (3, 4),                # 6.11  (accountID, strAccountType,
                                                #        strDatatype[, strategyName])
    "get_history_trade_detail_data": (5, 5),    # 6.9   (accountID, strAccountType,
                                                #        strDatatype, startDate, endDate)
}


def _call_sites():
    """(函数名, 实参个数, 行号) —— 所有 _call_qmt_global / _call_qmt_mapping 调用点。"""
    tree = ast.parse(io.open(RPC_SOURCE, encoding="utf-8").read())
    sites = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr not in ("_call_qmt_global", "_call_qmt_mapping",
                             "_call_qmt_scalar", "_call_qmt_object"):
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant):
            continue
        if any(isinstance(a, ast.Starred) for a in node.args):
            continue          # *args 展开的（get_last_order_id）单独用例覆盖
        sites.append((node.args[0].value, len(node.args) - 1, node.lineno))
    return sites


class QmtGlobalArityTest(unittest.TestCase):

    def test_every_call_site_matches_the_official_signature(self):
        wrong = []
        for name, count, line in _call_sites():
            bounds = OFFICIAL_ARITY.get(name)
            if bounds is None:
                continue
            low, high = bounds
            if not (low <= count <= high):
                wrong.append("redis_rpc.py:%d %s 传了 %d 个参数，官方签名要 %s"
                             % (line, name, count,
                                "%d" % low if low == high else "%d-%d" % (low, high)))
        self.assertEqual(wrong, [], "\n".join([""] + wrong))

    def test_the_signature_table_covers_every_global_we_call(self):
        """新接一个 QMT 全局函数就得往表里加一行，否则这道闸形同虚设。"""
        called = set(name for name, _count, _line in _call_sites())
        missing = sorted(called - set(OFFICIAL_ARITY))
        self.assertEqual(
            missing, [],
            "这些 QMT 全局函数被调用但没登记官方签名，去 "
            "docs/BIGQMT_INNER_PYTHON_API_REFERENCE.md 查了以后补进 "
            "OFFICIAL_ARITY: %s" % missing)

    def test_the_probe_calls_them_with_the_right_arity_too(self):
        """探测自己也踩过这个坑（#201）—— 它调的是同一批函数。"""
        from bigqmt_signal_trader.adapters.market_bigqmt import BigQmtMarketDataProvider
        from bigqmt_signal_trader.adapters.order_dryrun import DryRunOrderGateway
        from bigqmt_signal_trader.redis_rpc import BigQmtRpcHandlers

        class _Ctx(object):
            def get_full_tick(self, codes):
                return {}

        seen = {}

        def _recorder(name):
            def _fn(*args):
                seen[name] = len(args)
                return []
            return _fn

        probed = ("get_assure_contract", "get_enable_short_contract",
                  "get_unclosed_compacts", "get_debt_contract")
        BigQmtRpcHandlers(
            account_id="acct",
            market_data=BigQmtMarketDataProvider(_Ctx()),
            position_provider=None,
            order_gateway=DryRunOrderGateway(),
            qmt_api=dict((n, _recorder(n)) for n in probed),
        ).handle("probe_capabilities", {})

        for name in probed:
            low, high = OFFICIAL_ARITY[name]
            self.assertIn(name, seen, name)
            self.assertTrue(low <= seen[name] <= high,
                            "probe 用 %d 个参数调 %s，官方签名要 %d-%d"
                            % (seen[name], name, low, high))


class OrderIdQueryArgumentsTest(unittest.TestCase):
    """三个新修的：参数个数对不上只是表象，位置也得对。"""

    def _handlers(self, qmt_api):
        from bigqmt_signal_trader.adapters.market_bigqmt import BigQmtMarketDataProvider
        from bigqmt_signal_trader.adapters.order_dryrun import DryRunOrderGateway
        from bigqmt_signal_trader.redis_rpc import BigQmtRpcHandlers

        class _Ctx(object):
            def get_full_tick(self, codes):
                return {}

        return BigQmtRpcHandlers(
            account_id="acct",
            market_data=BigQmtMarketDataProvider(_Ctx()),
            position_provider=None,
            order_gateway=DryRunOrderGateway(),
            qmt_api=qmt_api,
        )

    def test_get_value_by_order_id_passes_all_four(self):
        seen = []

        def get_value_by_order_id(order_id, account_id, account_type, datatype):
            seen.append((order_id, account_id, account_type, datatype))
            return [{"m_strOrderSysID": "X1"}]

        order = self._handlers({"get_value_by_order_id": get_value_by_order_id}).handle(
            "get_value_by_order_id", {"order_id": "X1"})

        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], "X1")
        self.assertEqual(seen[0][1], "acct")
        self.assertTrue(seen[0][2])          # strAccountType
        self.assertEqual(seen[0][3], "ORDER")
        # 回的是一个委托对象，不是行列表 —— 官方 6.11 就是这么定义的
        self.assertEqual(order["m_strOrderSysID"], "X1")

    def test_get_value_by_order_id_can_ask_for_deals(self):
        seen = []
        self._handlers({"get_value_by_order_id":
                        lambda *a: (seen.append(a), [])[1]}).handle(
            "get_value_by_order_id", {"order_id": "X1", "detail_type": "deal"})
        self.assertEqual(seen[0][3], "DEAL")

    def test_get_last_order_id_passes_account_type_and_datatype(self):
        seen = []

        def get_last_order_id(*args):
            seen.append(args)
            return "12345"

        self._handlers({"get_last_order_id": get_last_order_id}).handle(
            "get_last_order_id", {})

        self.assertEqual(len(seen), 1)
        self.assertGreaterEqual(len(seen[0]), 3)
        self.assertLessEqual(len(seen[0]), 4)
        self.assertEqual(seen[0][0], "acct")
        self.assertEqual(seen[0][2], "ORDER")

    def test_get_history_trade_detail_data_puts_account_type_second(self):
        """漏掉 strAccountType 会让 detail_type 落到账户类型的位置（#96 同款）。"""
        seen = []

        def get_history_trade_detail_data(*args):
            seen.append(args)
            return []

        self._handlers({
            "get_history_trade_detail_data": get_history_trade_detail_data
        }).handle("get_history_trade_detail_data", {
            "detail_type": "DEAL", "start_date": "20260101", "end_date": "20260201"})

        self.assertEqual(len(seen[0]), 5)
        self.assertEqual(seen[0][0], "acct")
        self.assertNotEqual(seen[0][1], "DEAL")     # 这里必须是账户类型，不是数据类型
        self.assertEqual(seen[0][2], "DEAL")
        self.assertEqual(seen[0][3], "20260101")
        self.assertEqual(seen[0][4], "20260201")


class ReturnShapeTest(unittest.TestCase):
    """参数对了还不够 —— 返回形状用错，数据照样被毁掉（#96 / #207 同款）。"""

    def _handlers(self, qmt_api):
        from bigqmt_signal_trader.adapters.market_bigqmt import BigQmtMarketDataProvider
        from bigqmt_signal_trader.adapters.order_dryrun import DryRunOrderGateway
        from bigqmt_signal_trader.redis_rpc import BigQmtRpcHandlers

        class _Ctx(object):
            def get_full_tick(self, codes):
                return {}

        return BigQmtRpcHandlers(
            account_id="acct",
            market_data=BigQmtMarketDataProvider(_Ctx()),
            position_provider=None,
            order_gateway=DryRunOrderGateway(),
            qmt_api=qmt_api,
        )

    def test_order_id_survives_instead_of_becoming_one_dict_per_character(self):
        """实盘实测：'635005110' 曾变成 9 个空 dict，一个字符一个。"""
        out = self._handlers(
            {"get_last_order_id": lambda *a: "635005110"}).handle(
            "get_last_order_id", {})
        self.assertEqual(out, "635005110")

    def test_qmt_not_found_marker_survives_too(self):
        """QMT 用 '-1' 表示没找到，原来变成 [{}, {}] —— 调用方无从分辨。"""
        out = self._handlers({"get_last_order_id": lambda *a: "-1"}).handle(
            "get_last_order_id", {})
        self.assertEqual(out, "-1")

    def test_an_int_order_id_is_not_swallowed(self):
        """int 在行归一化里直接抛 TypeError，被吞成 []。"""
        out = self._handlers({"get_last_order_id": lambda *a: 12345}).handle(
            "get_last_order_id", {})
        self.assertEqual(out, 12345)

    def test_unbound_scalar_global_answers_none_not_a_fake_row(self):
        out = self._handlers({}).handle("get_last_order_id", {})
        self.assertIsNone(out)

    def test_single_order_object_is_not_iterated_into_nothing(self):
        """get_value_by_order_id 回的是一个对象，不是列表。"""
        class _Order(object):
            m_strOrderSysID = "635005110"
            m_nOrderStatus = 56

        out = self._handlers(
            {"get_value_by_order_id": lambda *a: _Order()}).handle(
            "get_value_by_order_id", {"order_id": "635005110"})

        self.assertEqual(out["m_strOrderSysID"], "635005110")
        self.assertEqual(out["m_nOrderStatus"], 56)

    def test_a_list_of_one_object_also_works(self):
        """有的券商实现回一个单元素列表，两种都得吃得下。"""
        out = self._handlers(
            {"get_value_by_order_id": lambda *a: [{"m_strOrderSysID": "X1"}]}).handle(
            "get_value_by_order_id", {"order_id": "X1"})
        self.assertEqual(out["m_strOrderSysID"], "X1")

    def test_enum_constants_are_not_scraped_into_the_row(self):
        """QMT 委托对象上挂着 60 个数字串名的枚举常量，实盘实测把载荷翻了一倍。"""
        class _Order(object):
            m_strOrderSysID = "635005110"
            m_nOrderStatus = 56

        order = _Order()
        # 模拟 QMT 那些 '0' / '-1' / '101' 的常量属性
        for name, value in (("0", 48), ("-1", -1), ("101", 101)):
            setattr(order, name, value)

        out = self._handlers(
            {"get_value_by_order_id": lambda *a: order}).handle(
            "get_value_by_order_id", {"order_id": "635005110"})

        self.assertEqual(out["m_strOrderSysID"], "635005110")
        self.assertEqual(out["m_nOrderStatus"], 56)
        for junk in ("0", "-1", "101"):
            self.assertNotIn(junk, out)

    def test_missing_order_answers_empty_mapping(self):
        out = self._handlers(
            {"get_value_by_order_id": lambda *a: None}).handle(
            "get_value_by_order_id", {"order_id": "X1"})
        self.assertEqual(out, {})


if __name__ == "__main__":
    unittest.main()
