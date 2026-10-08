# coding: utf-8
"""The duplicate 已报 and empty instrument_name in order callbacks (#161).

@sumo225270's log: one manual order produced TWO identical 已报 events (the
first degenerate -- order_id 0, no sysid) and one 已成, and every event's
instrument_name was empty.

QMT fires order_callback once when the order row appears and again when
m_strOrderSysID lands (#152's window). The sysid-less event is now held for
exec_events_hold_presysid_seconds (default 0.8): the sysid-bearing twin
drops it, the adjust flush publishes it if no twin comes. And events get
instrument_name resolved from ContextInfo (cached) when QMT doesn't carry it.
"""

import os
import sys
import time
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

import bigqmt_signal_trader_strategy as strategy
from bigqmt_signal_trader import exec_events


def _order_obj(**overrides):
    attrs = dict(
        m_strInstrumentID="601398", m_strExchangeID="SH", m_nOffsetFlag=48,
        m_nVolumeTotalOriginal=100, m_nVolumeTotal=100, m_nVolumeTraded=0,
        m_strOrderSysID="", m_strRemark="sig-1", m_nOrderStatus=50,
        m_dLimitPrice=7.91, m_dTradedPrice=0.0, m_strAccountID="acct",
    )
    attrs.update(overrides)
    return type("FakeOrder", (), attrs)


class _Sink(object):
    def __init__(self):
        self.published = []


class _FakeExecEvents(object):
    """Real normalizers, fake transport: publish routes straight to the sink."""

    normalize_order_event = staticmethod(exec_events.normalize_order_event)
    normalize_trade_event = staticmethod(exec_events.normalize_trade_event)
    normalize_order_error_event = staticmethod(exec_events.normalize_order_error_event)
    enrich_order_identity = staticmethod(lambda redis_client, account_id, event: event)
    format_raw_snapshot = staticmethod(lambda *args: "")
    raw_field_snapshot = staticmethod(lambda *args: None)

    def publish_exec_event(self, sink, account_id, event):
        sink.published.append((account_id, event))


class _ContextInfo(object):
    def __init__(self):
        self.name_calls = 0

    def get_stock_name(self, code):
        self.name_calls += 1
        return {"601398.SH": "工商银行"}.get(code, "")


CFG = {"exec_events": {"enabled": True, "account_id": "acct",
                       "hold_presysid_order_seconds": 0.8}}


class _StrategyState(unittest.TestCase):
    def setUp(self):
        self._saved = {name: getattr(strategy, name)
                       for name in ("_build_config", "_exec_event_sink",
                                    "_exec_event_redis", "_exec_events")}
        self._sink = _Sink()
        strategy._build_config = lambda: CFG
        strategy._exec_event_sink = lambda config: self._sink
        strategy._exec_event_redis = lambda config: None
        strategy._exec_events = _FakeExecEvents()
        strategy._held_presysid_orders.clear()
        strategy._instrument_name_cache.clear()

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(strategy, name, value)
        strategy._held_presysid_orders.clear()
        strategy._instrument_name_cache.clear()

    def _publish(self, kind, obj, ctx=None):
        strategy._publish_exec_event(kind, obj, ctx)


class PresysidHoldTest(_StrategyState):
    def test_presysid_event_is_held_and_dropped_by_its_twin(self):
        self._publish("order", _order_obj())  # no sysid
        self.assertEqual(self._sink.published, [])
        self.assertEqual(len(strategy._held_presysid_orders), 1)

        self._publish("order", _order_obj(m_strOrderSysID="635076953"))

        self.assertEqual(len(self._sink.published), 1)
        event = self._sink.published[0][1]
        self.assertEqual(event["order_sys_id"], "635076953")
        self.assertEqual(strategy._held_presysid_orders, {})

    def test_held_event_flushes_at_timeout_when_no_twin_comes(self):
        cfg = {"exec_events": {"enabled": True, "account_id": "acct",
                               "hold_presysid_order_seconds": 0.05}}
        strategy._build_config = lambda: cfg
        self._publish("order", _order_obj())
        self.assertEqual(self._sink.published, [])

        time.sleep(0.1)
        strategy._flush_held_presysid_orders(cfg)

        self.assertEqual(len(self._sink.published), 1)
        self.assertEqual(self._sink.published[0][1]["user_order_id"], "sig-1")
        self.assertEqual(strategy._held_presysid_orders, {})

    def test_held_junk_event_still_owes_its_order_error_twin(self):
        cfg = {"exec_events": {"enabled": True, "account_id": "acct",
                               "hold_presysid_order_seconds": 0.05}}
        strategy._build_config = lambda: cfg
        self._publish("order", _order_obj(m_nOrderStatus=57))

        time.sleep(0.1)
        strategy._flush_held_presysid_orders(cfg)

        kinds = [e.get("event_type") for _acct, e in self._sink.published]
        self.assertEqual(kinds, ["order", "order_error"])

    def test_unkeyable_event_publishes_immediately(self):
        self._publish("order", _order_obj(m_strRemark="", m_strInstrumentID=""))
        self.assertEqual(len(self._sink.published), 1)
        self.assertEqual(strategy._held_presysid_orders, {})

    def test_hold_disabled_by_zero(self):
        cfg = {"exec_events": {"enabled": True, "account_id": "acct",
                               "hold_presysid_order_seconds": 0}}
        strategy._build_config = lambda: cfg
        self._publish("order", _order_obj())
        self.assertEqual(len(self._sink.published), 1)
        self.assertEqual(strategy._held_presysid_orders, {})

    def test_sysid_event_when_nothing_held_just_publishes(self):
        self._publish("order", _order_obj(m_strOrderSysID="635076953"))
        self.assertEqual(len(self._sink.published), 1)


class InstrumentNameTest(_StrategyState):
    def test_order_event_gets_the_name_and_caches_it(self):
        ctx = _ContextInfo()
        self._publish("order", _order_obj(m_strOrderSysID="S1"), ctx)
        self._publish("order", _order_obj(m_strOrderSysID="S1",
                                          m_nOrderStatus=56), ctx)

        names = [e["instrument_name"] for _a, e in self._sink.published]
        self.assertEqual(names, ["工商银行", "工商银行"])
        self.assertEqual(ctx.name_calls, 1, "the name lookup must be cached")

    def test_trade_event_gets_the_name(self):
        trade = type("FakeDeal", (), dict(
            m_strInstrumentID="601398", m_strExchangeID="SH", m_nOffsetFlag=48,
            m_nVolume=100, m_dPrice=7.91, m_strOrderSysID="S1",
            m_strTradeID="t-1", m_strRemark="sig-1", m_strAccountID="acct",
            m_nDirection=48,
        ))
        self._publish("trade", trade, _ContextInfo())
        self.assertEqual(self._sink.published[0][1]["instrument_name"], "工商银行")

    def test_no_context_info_leaves_empty_and_does_not_crash(self):
        self._publish("order", _order_obj(m_strOrderSysID="S1"), None)
        self.assertEqual(self._sink.published[0][1]["instrument_name"], "")

    def test_qmt_carried_name_wins_over_the_lookup(self):
        ctx = _ContextInfo()
        self._publish("order", _order_obj(m_strOrderSysID="S1",
                                          m_strInstrumentName="工行"), ctx)
        self.assertEqual(self._sink.published[0][1]["instrument_name"], "工行")
        self.assertEqual(ctx.name_calls, 0)


if __name__ == "__main__":
    unittest.main()
