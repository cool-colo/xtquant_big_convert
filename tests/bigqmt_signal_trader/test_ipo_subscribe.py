"""IPO subscription is something you call, not something the bridge does (#96).

As submitted, PR #96 ran the subscription unconditionally from the adjust
timer: every deployment that upgraded would place real orders the next trading
morning without anyone asking, and it called passorder directly, so a user who
had deliberately set rpc_allow_order_methods=False still got orders placed.

It is an explicit method now, routed through order_stock -- which means it obeys
that flag and inherits the gateway's passorder settings, including quickTrade=2
(the submitted version hand-rolled quickTrade=1, which the API reference says is
wrong outside a bar context and can silently place nothing).
"""

import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader import xtquant_compat as compat
from bigqmt_signal_trader.xtquant_compat import ipo_market_of


class MarketClassificationTest(unittest.TestCase):
    """The predecessor ended in `return True` -- it guessed in favour of
    ordering, on a filter meant to keep cash-freezing BJ subscriptions out."""

    def test_shanghai_subscription_codes(self):
        for code in ("730001", "732123", "780456", "787888", "789001"):
            self.assertEqual(ipo_market_of(code), "SH", code)

    def test_shenzhen_subscription_codes(self):
        for code in ("001234", "002999", "300999"):
            self.assertEqual(ipo_market_of(code), "SZ", code)

    def test_beijing_codes_are_identified_not_guessed(self):
        for code in ("920001", "889001", "830799", "400001"):
            self.assertEqual(ipo_market_of(code), "BJ", code)

    def test_suffixes_win_over_prefixes(self):
        self.assertEqual(ipo_market_of("601398.SH"), "SH")
        self.assertEqual(ipo_market_of("000001.SZ"), "SZ")
        self.assertEqual(ipo_market_of("920001.BJ"), "BJ")

    def test_unrecognised_codes_return_none(self):
        for code in ("", None, "abc", "12", "XYZ123", "5"):
            self.assertIsNone(ipo_market_of(code), repr(code))


class _FakeTrader(object):
    """Stands in for the parts of BigQmtXtTrader ipo_subscribe_all touches."""

    def __init__(self, ipos, fail_on=()):
        self._ipos = ipos
        self._fail_on = set(fail_on)
        self.orders = []

    def query_ipo_data(self, account=None, stock_type=""):
        return self._ipos

    def ipo_subscribe(self, account, stock_code, volume, price,
                      strategy_name="ipo", order_remark="ipo_sub"):
        if stock_code in self._fail_on:
            raise RuntimeError("order rpc methods are disabled")
        self.orders.append((stock_code, volume, price, order_remark))
        return {"order_sys_id": "sys-%s" % stock_code}


def _run(ipos, fail_on=(), **kwargs):
    trader = _FakeTrader(ipos, fail_on)
    results = compat.BigQmtXtTrader.ipo_subscribe_all(trader, **kwargs)
    return trader, results


def _by_code(results):
    return dict((r["stock_code"], r) for r in results)


IPOS = {
    "730001": {"name": "沪主板新股", "issuePrice": 16.0, "maxPurchaseNum": 12000},
    "001234": {"name": "深主板新股", "issuePrice": 8.5, "maxPurchaseNum": 5000},
    "920001": {"name": "北交所新股", "issuePrice": 5.0, "maxPurchaseNum": 1000},
    "XYZ999": {"name": "认不出来", "issuePrice": 9.0, "maxPurchaseNum": 100},
}


class SubscribeAllTest(unittest.TestCase):
    def test_only_the_allowed_markets_are_ordered(self):
        trader, results = _run(IPOS)

        self.assertEqual(sorted(c for c, _v, _p, _r in trader.orders),
                         ["001234", "730001"])
        self.assertEqual(_by_code(results)["920001"]["action"], "skipped")

    def test_an_unidentifiable_code_is_skipped_not_guessed(self):
        _trader, results = _run(IPOS)
        entry = _by_code(results)["XYZ999"]

        self.assertEqual(entry["action"], "skipped")
        self.assertIn("not identified", entry["reason"])

    def test_beijing_can_be_opted_into(self):
        trader, _results = _run(IPOS, markets=("SH", "SZ", "BJ"))

        self.assertIn("920001", [c for c, _v, _p, _r in trader.orders])

    def test_dry_run_places_nothing(self):
        trader, results = _run(IPOS, dry_run=True)

        self.assertEqual(trader.orders, [])
        self.assertEqual(_by_code(results)["730001"]["action"], "planned")

    def test_price_and_volume_come_from_the_ipo_row(self):
        trader, _results = _run(IPOS)
        order = [o for o in trader.orders if o[0] == "730001"][0]

        self.assertEqual(order[1], 12000)
        self.assertEqual(order[2], 16.0)

    def test_a_row_missing_price_or_volume_is_skipped(self):
        _trader, results = _run({
            "730001": {"issuePrice": 0, "maxPurchaseNum": 12000},
            "730002": {"issuePrice": 16.0, "maxPurchaseNum": 0},
        })

        for entry in results:
            self.assertEqual(entry["action"], "skipped")
            self.assertIn("non-positive", entry["reason"])

    def test_one_rejected_order_does_not_stop_the_rest(self):
        """Order RPC may be disabled, or the broker may reject one code."""
        trader, results = _run(IPOS, fail_on=("730001",))

        self.assertEqual([c for c, _v, _p, _r in trader.orders], ["001234"])
        failed = _by_code(results)["730001"]
        self.assertEqual(failed["action"], "failed")
        self.assertIn("disabled", failed["reason"])

    def test_no_ipos_today_is_an_empty_result_not_an_error(self):
        _trader, results = _run({})

        self.assertEqual(results, [])

    def test_the_remark_identifies_the_subscription(self):
        trader, _results = _run(IPOS)

        self.assertTrue(all(remark.startswith("ipo:")
                            for _c, _v, _p, remark in trader.orders))


class NoAutomaticSubscriptionTest(unittest.TestCase):
    """The strategy must not place IPO orders on its own."""

    def _strategy_source(self):
        path = os.path.join(ROOT, "src", "bigqmt_signal_trader_strategy.py")
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def test_adjust_does_not_run_a_daily_subscription(self):
        source = self._strategy_source()

        self.assertNotIn("_maybe_run_daily_ipo", source)
        self.assertNotIn("_run_daily_ipo", source)

    def test_the_strategy_forwards_the_qmt_global_but_never_calls_it(self):
        """get_ipo_data must stay in the forwarded-globals list -- that is how
        the RPC handler reaches it -- but the strategy itself must not invoke
        it, which is what drove the automatic subscription."""
        source = self._strategy_source()

        self.assertIn('"get_ipo_data",', source)      # still forwarded over RPC
        self.assertNotIn("get_ipo_data(", source)     # never called here
        self.assertNotIn("passorder(23, 1101", source)


class EmptyResultShapeTest(unittest.TestCase):
    def test_query_ipo_data_returns_a_dict_when_there_is_nothing(self):
        """get_ipo_data answers with a dict. Returning [] on the empty path --
        which is most days -- breaks a caller doing .items()."""
        class _Client(object):
            account_id = "acct"

            def call(self, method, params=None, **kwargs):
                return None

        data = compat.BigQmtXtTrader.query_ipo_data(
            type("T", (), {"client": _Client()})(), None)

        self.assertEqual(data, {})
        self.assertEqual(list(data.items()), [])


if __name__ == "__main__":
    unittest.main()


class MappingShapeTest(unittest.TestCase):
    """get_ipo_data answers with a dict keyed by subscription code, not with
    detail rows. It was being sent through _normalize_detail_rows, which
    iterates a dict by KEY and attribute-scrapes each code string -- so two real
    IPOs arrived as [{}, {}]. Live check on 2026-08-28: the server returned
    [{}] for type="STOCK" and [] for "BOND", i.e. QMT had handed over a
    non-empty dict and the bridge had emptied it.
    """

    def test_the_row_normaliser_destroys_a_mapping(self):
        from bigqmt_signal_trader.redis_rpc import _normalize_detail_rows

        ipos = {"730001": {"issuePrice": 16.0, "maxPurchaseNum": 12000},
                "001234": {"issuePrice": 8.5, "maxPurchaseNum": 5000}}
        wrecked = _normalize_detail_rows(ipos)

        self.assertEqual(wrecked, [{}, {}])   # this is why it needed its own path

    def test_the_mapping_path_keeps_codes_and_values(self):
        from bigqmt_signal_trader.redis_rpc import _normalize_mapping_value

        ipos = {"730001": {"name": "x", "issuePrice": 16.0, "maxPurchaseNum": 12000}}
        kept = dict((k, _normalize_mapping_value(v)) for k, v in ipos.items())

        self.assertIn("730001", kept)
        self.assertEqual(kept["730001"]["issuePrice"], 16.0)
        self.assertEqual(kept["730001"]["maxPurchaseNum"], 12000)

    def test_nested_values_survive(self):
        from bigqmt_signal_trader.redis_rpc import _normalize_mapping_value

        value = _normalize_mapping_value({"a": [1, 2, {"b": "c"}], "d": None})

        self.assertEqual(value, {"a": [1, 2, {"b": "c"}], "d": None})

    def test_a_wrong_shaped_response_is_reported_not_swallowed(self):
        """Coercing [{}, {}] to {} reads as "no IPOs today" -- the exact silence
        that let this survive."""
        import logging

        class _Client(object):
            account_id = "acct"

            def call(self, method, params=None, **kwargs):
                return [{}, {}]

        trader = type("T", (), {"client": _Client()})()
        with self.assertLogs("bigqmt.xtquant_compat", level=logging.WARNING) as caught:
            data = compat.BigQmtXtTrader.query_ipo_data(trader, None)

        self.assertEqual(data, {})
        self.assertIn("too old", "".join(caught.output))

    def test_an_empty_response_logs_nothing(self):
        class _Client(object):
            account_id = "acct"

            def call(self, method, params=None, **kwargs):
                return []

        trader = type("T", (), {"client": _Client()})()
        self.assertEqual(compat.BigQmtXtTrader.query_ipo_data(trader, None), {})
