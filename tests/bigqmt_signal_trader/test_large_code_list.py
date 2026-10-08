"""A long explicit code list must not lose everything (issue #104).

One RPC carries one timeout, so a list that does not fit does not degrade -- it
returns nothing at all. Measured here: 1000 codes answer in 0.42s, 10000 in
2.87s, 26744 time out. A reporter hit it at a lower threshold than that, which
is what a timeout does: where it lands depends on the machine.

So a long list that fails, or comes back short, is retried as a market-token
read filtered to the codes asked for. A token is one cheap argument that cannot
truncate.

Narrowed first. An exchange listing is mostly bonds -- "SH" is 26744
instruments of which 2315 are stocks -- so reading all of it costs 7.4s against
1.08s for the stocks, and widening to "all" only happens if the narrow read
came up short.
"""

import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader import xtquant_compat as compat
from bigqmt_signal_trader.xtquant_compat import LARGE_CODE_LIST, _markets_of


def _codes(count, market="SH"):
    return ["%06d.%s" % (600000 + i, market) for i in range(count)]


class FakeClient(object):
    """Answers get_full_tick; records how each request was made."""

    def __init__(self, direct=None, market_answers=None, raise_direct=False):
        self.account_id = "acct"
        self.local_cache_config = {}
        self.full_tick_cache_config = {}
        self.transport_name = "zmq"
        self.requests = []
        self._direct = direct
        self._market_answers = market_answers or {}
        self._raise_direct = raise_direct

    def call(self, method, params=None, timeout_seconds=None, **kwargs):
        params = params or {}
        codes = list(params.get("codes") or [])
        types = list(params.get("types") or [])
        self.requests.append((codes, types))
        if len(codes) == 1 and codes[0] in ("SH", "SZ", "BJ", "HK"):
            return dict(self._market_answers.get(
                (codes[0], tuple(types) or ("stock",)), {}))
        if self._raise_direct:
            raise TimeoutError("zmq rpc timeout: get_full_tick")
        return dict(self._direct or {})

    def _redis(self):
        raise AssertionError("redis not expected here")


def _xtdata(client):
    return compat.BigQmtXtData(client)


class MarketsOfTest(unittest.TestCase):
    def test_it_collects_the_suffixes(self):
        self.assertEqual(_markets_of(["600000.SH", "000001.SZ"]), {"SH", "SZ"})

    def test_an_unsuffixed_code_disables_the_fallback(self):
        """Filtering an exchange read cannot recover a code we cannot place."""
        self.assertEqual(_markets_of(["600000.SH", "600001"]), set())

    def test_a_futures_code_disables_it_too(self):
        self.assertEqual(_markets_of(["rb2610.SF"]), set())


class FallbackTriggersTest(unittest.TestCase):
    def test_a_long_list_that_times_out_is_recovered(self):
        codes = _codes(LARGE_CODE_LIST + 1)
        answers = {("SH", ("stock",)): dict((c, {"lastPrice": 1.0}) for c in codes)}
        client = FakeClient(raise_direct=True, market_answers=answers)

        result = _xtdata(client).get_full_tick(codes)

        self.assertEqual(len(result), len(codes))

    def test_a_short_answer_is_recovered(self):
        """The server dropped codes rather than raising."""
        codes = _codes(LARGE_CODE_LIST + 1)
        answers = {("SH", ("stock",)): dict((c, {"lastPrice": 1.0}) for c in codes)}
        client = FakeClient(direct={codes[0]: {"lastPrice": 1.0}},
                            market_answers=answers)

        result = _xtdata(client).get_full_tick(codes)

        self.assertEqual(len(result), len(codes))

    def test_only_the_requested_codes_come_back(self):
        codes = _codes(LARGE_CODE_LIST + 1)
        listing = dict((c, {"lastPrice": 1.0}) for c in codes)
        listing["999999.SH"] = {"lastPrice": 2.0}       # not asked for
        client = FakeClient(raise_direct=True,
                            market_answers={("SH", ("stock",)): listing})

        result = _xtdata(client).get_full_tick(codes)

        self.assertNotIn("999999.SH", result)

    def test_both_exchanges_are_read(self):
        codes = _codes(600, "SH") + _codes(600, "SZ")
        client = FakeClient(raise_direct=True, market_answers={
            ("SH", ("stock",)): dict((c, {}) for c in codes if c.endswith(".SH")),
            ("SZ", ("stock",)): dict((c, {}) for c in codes if c.endswith(".SZ")),
        })

        result = _xtdata(client).get_full_tick(codes)

        self.assertEqual(len(result), len(codes))


class FallbackDoesNotTriggerTest(unittest.TestCase):
    def test_a_short_list_failure_is_raised(self):
        """Below the threshold a failure is a real failure, not a size problem
        -- swallowing it would hide a broken bridge."""
        client = FakeClient(raise_direct=True)

        with self.assertRaises(TimeoutError):
            _xtdata(client).get_full_tick(_codes(10))

    def test_a_short_list_that_answers_partially_is_left_alone(self):
        """Missing instruments are normal: suspended, delisted, not subscribed."""
        codes = _codes(10)
        client = FakeClient(direct={codes[0]: {"lastPrice": 1.0}})

        result = _xtdata(client).get_full_tick(codes)

        self.assertEqual(len(result), 1)
        self.assertEqual(len(client.requests), 1)   # no second read

    def test_a_market_token_request_is_not_re_read(self):
        client = FakeClient(market_answers={("SH", ("stock",)): {"600000.SH": {}}})

        _xtdata(client).get_full_tick(["SH"])

        self.assertEqual(len(client.requests), 1)

    def test_unsuffixed_codes_still_raise(self):
        codes = ["%06d" % (600000 + i) for i in range(LARGE_CODE_LIST + 1)]
        client = FakeClient(raise_direct=True)

        with self.assertRaises(TimeoutError):
            _xtdata(client).get_full_tick(codes)


class NarrowsBeforeWideningTest(unittest.TestCase):
    """Reading a whole exchange is 7.4s against 1.08s for its stocks."""

    def test_it_asks_for_stocks_first(self):
        codes = _codes(LARGE_CODE_LIST + 1)
        client = FakeClient(raise_direct=True, market_answers={
            ("SH", ("stock",)): dict((c, {}) for c in codes)})

        _xtdata(client).get_full_tick(codes)

        market_requests = [t for c, t in client.requests if c == ["SH"]]
        self.assertEqual(market_requests[0], ["stock"])

    def test_it_does_not_widen_when_the_narrow_read_was_enough(self):
        codes = _codes(LARGE_CODE_LIST + 1)
        client = FakeClient(raise_direct=True, market_answers={
            ("SH", ("stock",)): dict((c, {}) for c in codes)})

        _xtdata(client).get_full_tick(codes)

        self.assertNotIn(["all"], [t for c, t in client.requests if c == ["SH"]])

    def test_it_widens_to_all_when_the_narrow_read_falls_short(self):
        """The codes were not stocks -- bonds, say."""
        codes = _codes(LARGE_CODE_LIST + 1)
        client = FakeClient(raise_direct=True, market_answers={
            ("SH", ("stock",)): {},
            ("SH", ("all",)): dict((c, {}) for c in codes)})

        result = _xtdata(client).get_full_tick(codes)

        self.assertEqual(len(result), len(codes))
        self.assertIn(["all"], [t for c, t in client.requests if c == ["SH"]])

    def test_a_caller_supplied_type_is_used_for_the_re_read(self):
        codes = _codes(LARGE_CODE_LIST + 1)
        client = FakeClient(raise_direct=True, market_answers={
            ("SH", ("etf",)): dict((c, {}) for c in codes)})

        _xtdata(client).get_full_tick(codes, types=["etf"])

        market_requests = [t for c, t in client.requests if c == ["SH"]]
        self.assertEqual(market_requests[0], ["etf"])


if __name__ == "__main__":
    unittest.main()
