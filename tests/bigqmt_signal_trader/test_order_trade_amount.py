# coding: utf-8
"""issue #173: ORDER 行没有透出成交金额 (m_dTradeAmount)。

@feel-think 在 ccxt 适配层 (QmtExchange) 要 order dict 的 ``cost``。ORDER 行
原生带 ``m_dTradeAmount``，但 ``OrderSnapshot`` 取了 ``price_type`` /
``traded_price``，唯独没取成交金额 —— 金额此前只在 DEAL 行以 ``amount`` 透出。

于是拿一笔委托的 cost 只有两条路，都不好：按 order_sysid 聚合 DEAL 行（多一次
RPC），或者调用方自己拿 traded_price * traded_volume 去算。

**issue 里"分笔成交时成交均价舍入会差几分"这条，在这台终端上没有复现。**
部署后实盘查了当日 14 笔委托：`trade_amount` 与按 order_sysid 聚合的 DEAL
金额 14/14 逐笔相等，而 `traded_price * traded_volume` 也一分不差 —— 因为
`m_dTradedPrice` 不是两位小数，它带完整精度（唯一一笔分价成交 55.14/55.13
报的是 55.13666666666666）。所以取这个字段的理由不是"修好了差几分"，而是
它是**柜台的原值**：不依赖某台终端的 m_dTradedPrice 精度，也不用多发一次
RPC。别的券商的 QMT 要是真把均价截成两位，估算就会开始漂。

**这个终端的 ORDER 行确实带这个字段。** 动手前先用只读的
``describe_trade_detail_fields`` 在实盘验过 (0.3.19, 账户当日 14 笔委托):
ORDER 行共 120 个属性, ``m_dTradeAmount`` 在其中; 掩码形状显示 13 笔是四到
五位数的金额, 唯一一笔 ``0.0`` 正是 ``m_nVolumeTraded`` 为 0 的未成交委托。
所以它不是一个"存在但恒为空"的字段 —— 那正是 #133 里 ``m_strShareholderID``
的下场 (属性表里根本没有), 也是这里必须先查一遍的原因。

取不到时留 0.0，**不拿 traded_price * traded_volume 兜底** —— 让估算值冒充
柜台金额，正好是这个 issue 要避开的那件事。
"""

import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.adapters.order_bigqmt import BigQmtOrderGateway
from bigqmt_signal_trader.exec_events import normalize_order_event
from bigqmt_signal_trader.xtquant_compat import BigQmtXtTrader, StockAccount


ACCOUNT = "8886800503"


class Row(object):
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _order_row(**overrides):
    base = dict(
        m_strOrderSysID="635030450",
        m_strRemark="",
        m_strInstrumentID="600722",
        m_strExchangeID="SH",
        m_strInstrumentName="金隅集团",
        m_nOffsetFlag=49,
        m_nVolumeTotalOriginal=100,
        m_nVolumeTraded=100,
        m_nOrderStatus=56,
        m_dLimitPrice=6.92,
        m_dTradedPrice=6.92,
        m_dTradeAmount=692.0,
        m_strStrategyName="alpha",
    )
    base.update(overrides)
    return Row(**base)


def _gateway(order_rows=()):
    def query(account_id, acct_type, detail_type, strategy_name=""):
        return list(order_rows) if detail_type == "ORDER" else []

    return BigQmtOrderGateway(
        context_info=None,
        account_id=ACCOUNT,
        get_trade_detail_data_func=query,
        account_type="STOCK",
    )


# ----------------------------------------------------------------- server


class OrderTradeAmountTest(unittest.TestCase):
    def test_it_comes_off_the_row(self):
        order = _gateway([_order_row()]).query_orders_strict(ACCOUNT, "")[0]

        self.assertEqual(order.trade_amount, 692.0)

    def test_the_counter_amount_wins_over_price_times_volume(self):
        """Read the field; never recompute it from price and volume.

        This terminal reports m_dTradedPrice at full precision, so the two
        agree here (verified live: 14/14). A terminal that truncates 成交均价
        to two decimals would not -- 800 shares averaging 3.414 reported as
        3.41 gives 2728.00 against the counter's 2731.20. This pins that the
        answer comes off the row either way.
        """
        row = _order_row(m_nVolumeTraded=800, m_dTradedPrice=3.41,
                         m_dTradeAmount=2731.2)

        order = _gateway([row]).query_orders_strict(ACCOUNT, "")[0]

        self.assertEqual(order.trade_amount, 2731.2)
        self.assertNotEqual(order.trade_amount,
                            order.traded_price * order.traded_volume)

    def test_an_unfilled_order_is_zero(self):
        row = _order_row(m_nVolumeTraded=0, m_dTradedPrice=0.0,
                         m_dTradeAmount=0.0)

        order = _gateway([row]).query_orders_strict(ACCOUNT, "")[0]

        self.assertEqual(order.trade_amount, 0.0)

    def test_a_row_without_the_field_is_zero_never_an_estimate(self):
        """A broker whose QMT omits it must not get a fabricated amount."""
        row = _order_row()
        del row.m_dTradeAmount

        order = _gateway([row]).query_orders_strict(ACCOUNT, "")[0]

        self.assertEqual(order.trade_amount, 0.0)

    def test_the_other_order_fields_are_unchanged(self):
        order = _gateway([_order_row()]).query_orders_strict(ACCOUNT, "")[0]

        self.assertEqual(order.traded_price, 6.92)
        self.assertEqual(order.traded_volume, 100)
        self.assertEqual(order.instrument_name, "金隅集团")


class OrderEventTradeAmountTest(unittest.TestCase):
    """The push path has to carry it too, or callback users still lack cost."""

    def test_the_order_event_carries_it(self):
        event = normalize_order_event(_order_row(), ACCOUNT)

        self.assertEqual(event["trade_amount"], 692.0)

    def test_a_callback_object_without_it_still_builds(self):
        row = _order_row()
        del row.m_dTradeAmount

        event = normalize_order_event(row, ACCOUNT)

        self.assertIn("trade_amount", event)
        self.assertIsNone(event["trade_amount"])


# ----------------------------------------------------------------- client


class FakeClient(object):
    def __init__(self, orders=()):
        self.account_id = ACCOUNT
        self.local_cache_config = {}
        self.full_tick_cache_config = {}
        self.orders = list(orders)

    def call(self, method, params=None, account_id=None, timeout_seconds=None):
        if method == "ping":
            return {"pong": True, "account_id": ACCOUNT, "account_type": "STOCK"}
        if method == "query_stock_orders":
            return self.orders
        return {}

    def _redis(self):
        raise AssertionError("redis not expected here")


def _trader(orders=()):
    trader = BigQmtXtTrader(account_id=ACCOUNT)
    trader.client = FakeClient(orders)
    trader.connect()
    return trader


ORDER_PAYLOAD = {
    "order_sys_id": "635030450", "stock_code": "600722.SH", "action": "BUY",
    "volume": 100, "traded_volume": 100, "status": "56", "price": 6.92,
    "traded_price": 6.92, "trade_amount": 692.0,
}


class ClientOrderTradeAmountTest(unittest.TestCase):
    def test_query_stock_orders_passes_it_through(self):
        order = _trader([ORDER_PAYLOAD]).query_stock_orders(
            StockAccount(ACCOUNT))[0]

        self.assertEqual(order.trade_amount, 692.0)

    def test_an_older_deployment_leaves_it_zero_without_attributeerror(self):
        """Client upgrades first; the QMT side is not synced and restarted yet.

        The key is simply absent then. The attribute must still exist -- that
        is what #133 was, and trading one AttributeError for another is not a
        fix.
        """
        bare = dict(ORDER_PAYLOAD)
        del bare["trade_amount"]

        order = _trader([bare]).query_stock_orders(StockAccount(ACCOUNT))[0]

        self.assertEqual(order.trade_amount, 0.0)

    def test_it_is_not_backfilled_from_price_times_volume(self):
        """0.0 means "the counter did not tell us", and must stay legible as
        that. Filling in 692.0 here would make an estimate indistinguishable
        from the real amount, which is the bug this issue is avoiding."""
        bare = dict(ORDER_PAYLOAD)
        del bare["trade_amount"]

        order = _trader([bare]).query_stock_orders(StockAccount(ACCOUNT))[0]

        self.assertEqual(order.traded_price * order.traded_volume, 692.0)
        self.assertEqual(order.trade_amount, 0.0)


if __name__ == "__main__":
    unittest.main()
