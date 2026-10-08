# coding: utf-8
"""issue #174 follow-up: the strategy name WAS on the callback all along.

#174 was answered with "big QMT does not carry the strategy name on order or
deal objects" -- 120 attributes on ORDER, 47 on DEAL, `m_strStrategyName` in
neither -- so the only way back was the identity store the bridge writes at
submit time (redis, then the in-process journal).

That conclusion came from looking for one name and not finding it. The value
was sitting in the dump under a different one. @sumo225270's raw_fields for a
bridge-placed order (0.3.17, 国金) carry, on BOTH the order and the deal::

    m_strSource: str '大QMT桥接器'

which is the string this bridge passed to `passorder` as its 8th argument,
`strategyName`. The repo already knew that field is the strategy name and
nothing else -- it just never read it back:

* `docs/MiniQMT_2_BigQMT-Skill/api_mapping.md` maps
  ``order.strategy_name`` to ``o.m_strSource``.
* #154 measured it on a live terminal with the masked shape report:
  ``m_strSource`` was empty on 13 hand-placed rows and carried
  ``aaaaaa_aaaaa_aaaaaa`` on the 3 the bridge placed, while
  ``m_strStrategyName`` was empty on all 16. That is why
  `rpc_default_strategy_name` exists: the string shows up in QMT's 报单来源
  column, so the user gets to choose it.

So the name comes straight off the callback, with no redis, no journal, and no
remark-key matching -- which also covers the case none of those can: an order
submitted before the current process started.

The negative control matters as much as the positive one. Read-only on this
terminal (2026-09-04, 14 orders / 17 deals, all hand-placed), the shape report
answers::

    ORDER  m_strSource  14 rows  length 0, empty
    DEAL   m_strSource  17 rows  length 0, empty

Hand-placed orders leave it blank, so reading it cannot invent a strategy name
for an order this bridge never sent. What is NOT verified here: a live
bridge-placed order carrying it end to end on THIS terminal -- that needs a
real order, which the unattended inspection does not place. The positive
evidence is #154's measurement and @sumo225270's dump.

`_attr` returns the first non-None candidate, and "" is not None -- so a
terminal that carries `m_strStrategyName` as an empty string would stop there
and never reach `m_strSource`. The extraction has to take the first non-EMPTY
candidate instead.
"""

import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader import exec_events  # noqa: E402
from bigqmt_signal_trader.adapters.order_bigqmt import BigQmtOrderGateway  # noqa: E402
from bigqmt_signal_trader.adapters import order_bigqmt  # noqa: E402


class Row(object):
    def __init__(self, **fields):
        for key, value in fields.items():
            setattr(self, key, value)


# @sumo225270's dump, issue #174, trimmed to the identity-bearing fields.
# 601566.SH 九牧王, sell, 2026-09-04 10:22, sysid 9797.
ISSUE_174_REMARK = u"卖出: PriceType.PEER_PRICE_FIRST(上交所 / 深交所对手方最优价格委托)"
ISSUE_174_SOURCE = u"大QMT桥接器"


def _issue_174_order():
    return Row(
        m_strAccountID="39942929",
        m_strInstrumentID="601566",
        m_strInstrumentName=u"九牧王",
        m_strExchangeID="SH",
        m_strOrderSysID="9797",
        m_strRemark=ISSUE_174_REMARK,
        m_strSource=ISSUE_174_SOURCE,
        m_strOrderStrategyType=u"函数下单",
        m_nOrderStatus=56,
        m_nDirection=48,
        m_nOffsetFlag=49,
        m_dLimitPrice=10.04,
        m_nVolumeTotalOriginal=301400,
        m_nVolumeTraded=301400,
    )


def _issue_174_trade():
    return Row(
        m_strAccountID="39942929",
        m_strInstrumentID="601566",
        m_strInstrumentName=u"九牧王",
        m_strExchangeID="SH",
        m_strOrderSysID="9797",
        m_strTradeID="50026021",
        m_strRemark=ISSUE_174_REMARK,
        m_strSource=ISSUE_174_SOURCE,
        m_strOrderStrategyType=u"函数下单",
        m_nDirection=48,
        m_nOffsetFlag=49,
        m_dPrice=10.04,
        m_nVolume=3100,
        m_strTradeTime="102225",
    )


class OrderEventSourceTest(unittest.TestCase):
    """The order callback names itself, without touching the identity store."""

    def test_the_reported_order_callback_carries_its_strategy_name(self):
        event = exec_events.normalize_order_event(_issue_174_order(), "39942929")
        self.assertEqual(event["strategy_name"], ISSUE_174_SOURCE)

    def test_a_bare_source_is_enough(self):
        event = exec_events.normalize_order_event(Row(m_strSource="my_book"), "a")
        self.assertEqual(event["strategy_name"], "my_book")

    def test_a_hand_placed_order_stays_unnamed(self):
        """The negative control: 报单来源 is blank on rows the bridge did not send.

        Measured read-only on this terminal -- 14 ORDER rows, all empty. If an
        empty source produced a name, every hand-placed order would be
        attributed to something.
        """
        event = exec_events.normalize_order_event(Row(m_strSource=""), "a")
        self.assertEqual(event["strategy_name"], "")

    def test_an_explicit_strategy_name_still_wins(self):
        event = exec_events.normalize_order_event(
            Row(m_strStrategyName="explicit", m_strSource="source"), "a")
        self.assertEqual(event["strategy_name"], "explicit")

    def test_an_empty_strategy_name_does_not_shadow_the_source(self):
        """`_attr` stops at the first non-None, and "" is not None.

        A terminal that carries m_strStrategyName as an empty string would
        otherwise answer "" while the name sits in m_strSource.
        """
        event = exec_events.normalize_order_event(
            Row(m_strStrategyName="", m_strSource="source"), "a")
        self.assertEqual(event["strategy_name"], "source")

    def test_no_identity_fields_at_all_is_empty_not_an_error(self):
        event = exec_events.normalize_order_event(Row(), "a")
        self.assertEqual(event["strategy_name"], "")


class TradeEventSourceTest(unittest.TestCase):
    """Same for the deal callback -- the dump shows m_strSource on both."""

    def test_the_reported_trade_callback_carries_its_strategy_name(self):
        event = exec_events.normalize_trade_event(_issue_174_trade(), "39942929")
        self.assertEqual(event["strategy_name"], ISSUE_174_SOURCE)

    def test_a_bare_source_is_enough(self):
        event = exec_events.normalize_trade_event(Row(m_strSource="my_book"), "a")
        self.assertEqual(event["strategy_name"], "my_book")

    def test_a_hand_placed_trade_stays_unnamed(self):
        event = exec_events.normalize_trade_event(Row(m_strSource=""), "a")
        self.assertEqual(event["strategy_name"], "")

    def test_an_empty_strategy_name_does_not_shadow_the_source(self):
        event = exec_events.normalize_trade_event(
            Row(m_strStrategyName="", m_strSource="source"), "a")
        self.assertEqual(event["strategy_name"], "source")


class RemarkIsUnaffectedTest(unittest.TestCase):
    """The remark keeps its own meaning -- it is the identity-store key.

    @sumo225270's remark round-tripped byte for byte (len 50, unchanged on both
    callbacks), which is what ruled out the truncation theory in #174. Reading
    the source must not disturb it.
    """

    def test_the_order_remark_survives_verbatim(self):
        event = exec_events.normalize_order_event(_issue_174_order(), "39942929")
        self.assertEqual(event["remark"], ISSUE_174_REMARK)
        self.assertEqual(event["user_order_id"], ISSUE_174_REMARK)
        self.assertEqual(len(event["remark"]), 50)

    def test_the_trade_remark_survives_verbatim(self):
        event = exec_events.normalize_trade_event(_issue_174_trade(), "39942929")
        self.assertEqual(event["remark"], ISSUE_174_REMARK)
        self.assertEqual(len(event["remark"]), 50)

    def test_the_source_is_not_mistaken_for_the_remark(self):
        event = exec_events.normalize_order_event(
            Row(m_strSource="a_book", m_strRemark="the_key"), "a")
        self.assertEqual(event["remark"], "the_key")
        self.assertEqual(event["user_order_id"], "the_key")
        self.assertEqual(event["strategy_name"], "a_book")


class QueryPathSourceTest(unittest.TestCase):
    """The query path had the same gap, so a row and its event disagreed.

    `_trade_to_dict` losing the field was the CLI half of this (#179). This is
    the other half: the builder never looked at 报单来源 either, so
    `query_stock_orders` / `query_stock_trades` answered "" for a bridge-placed
    order whenever the caller passed no strategy filter.
    """

    def _gateway(self, order_rows=(), deal_rows=()):
        def query(account_id, acct_type, detail_type, strategy_name=""):
            if detail_type == "ORDER":
                return list(order_rows)
            if detail_type in ("DEAL", "TRADE"):
                return list(deal_rows)
            return []

        return BigQmtOrderGateway(
            context_info=None,
            account_id="39942929",
            get_trade_detail_data_func=query,
            account_type="STOCK",
        )

    def _order_row(self, **overrides):
        base = dict(
            m_strOrderSysID="9797",
            m_strRemark=ISSUE_174_REMARK,
            m_strInstrumentID="601566",
            m_strExchangeID="SH",
            m_strInstrumentName=u"九牧王",
            m_nOffsetFlag=49,
            m_nVolumeTotalOriginal=301400,
            m_nVolumeTraded=301400,
            m_nOrderStatus=56,
            m_dLimitPrice=10.04,
            m_strSource=ISSUE_174_SOURCE,
        )
        base.update(overrides)
        return Row(**base)

    def _deal_row(self, **overrides):
        base = dict(
            m_strOrderSysID="9797",
            m_strTradeID="50026021",
            m_strRemark=ISSUE_174_REMARK,
            m_strInstrumentID="601566",
            m_strExchangeID="SH",
            m_strInstrumentName=u"九牧王",
            m_nOffsetFlag=49,
            m_nVolume=3100,
            m_dPrice=10.04,
            m_strTradeDate="20260904",
            m_strTradeTime="102225",
            m_strSource=ISSUE_174_SOURCE,
        )
        base.update(overrides)
        return Row(**base)

    def test_an_unfiltered_order_query_names_a_bridge_order(self):
        gateway = self._gateway(order_rows=[self._order_row()])
        row = gateway.query_orders_strict("39942929", "")[0]
        self.assertEqual(row.strategy_name, ISSUE_174_SOURCE)

    def test_an_unfiltered_trade_query_names_a_bridge_order(self):
        gateway = self._gateway(deal_rows=[self._deal_row()])
        row = gateway.query_trades_strict("39942929", "")[0]
        self.assertEqual(row.strategy_name, ISSUE_174_SOURCE)

    def test_a_hand_placed_row_stays_unnamed(self):
        gateway = self._gateway(order_rows=[self._order_row(m_strSource="")])
        row = gateway.query_orders_strict("39942929", "")[0]
        self.assertEqual(row.strategy_name, "")

    def test_an_empty_strategy_name_field_does_not_shadow_the_source(self):
        gateway = self._gateway(
            order_rows=[self._order_row(m_strStrategyName="")])
        row = gateway.query_orders_strict("39942929", "")[0]
        self.assertEqual(row.strategy_name, ISSUE_174_SOURCE)

    def test_the_row_wins_over_the_query_filter(self):
        """The filter fallback must not overwrite a name the row supplied."""
        gateway = self._gateway(order_rows=[self._order_row()])
        row = gateway.query_orders_strict("39942929", "some_filter")[0]
        self.assertEqual(row.strategy_name, ISSUE_174_SOURCE)

    def test_the_filter_still_backstops_an_unnamed_row(self):
        gateway = self._gateway(order_rows=[self._order_row(m_strSource="")])
        row = gateway.query_orders_strict("39942929", "some_filter")[0]
        self.assertEqual(row.strategy_name, "some_filter")


class CandidateTableTest(unittest.TestCase):
    """Both paths must read the SAME names, in the same order.

    They are two literal tuples in two modules on purpose: order_bigqmt does
    not import exec_events, because the single-file build execs every module
    inside a function body and the fewer cross-imports there are in that graph
    the better. Duplication is what that costs, and drift is what it risks --
    add a name to one table only and the same order answers one thing on the
    callback and another on the query, which is precisely what the comment
    above each table promises cannot happen. Pin them together instead.
    """

    def test_both_paths_read_the_same_candidate_names(self):
        self.assertEqual(
            order_bigqmt._STRATEGY_NAME_FIELDS,
            exec_events._STRATEGY_NAME_FIELDS,
        )

    def test_the_report_source_is_read_last(self):
        """A terminal that does fill a real strategy-name field still wins."""
        for table in (exec_events._STRATEGY_NAME_FIELDS,
                      order_bigqmt._STRATEGY_NAME_FIELDS):
            self.assertEqual(table[-1], "m_strSource")
            self.assertIn("m_strStrategyName", table)


if __name__ == "__main__":
    unittest.main()
