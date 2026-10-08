"""A market token must not fetch the whole exchange by default (issue #104).

"SH" asks QMT for every instrument the exchange lists: 26744 of them, of which
2315 (8.7%) are stocks -- the rest is bonds, repos and the like. Cost is linear
at ~0.29ms per instrument, so a whole-market snapshot took 7.4s where the stocks
alone take 0.9s. The reporter compared that against MiniQMT's ~1s, which is the
stocks-only figure.

Filtering the reply would not have helped: the per-instrument cost is paid
inside QMT before anything comes back. So the token is resolved to a sector
listing and only those codes are requested. The sector lookup is
FormulaServer-served (~13ms measured), so it costs nothing next to what it saves.

The default is now stocks; types=["all"] restores the full listing.
"""

import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.adapters import market_bigqmt


SECTORS = {
    market_bigqmt.STOCK_SECTOR_BY_MARKET["SH"]: ["600000.SH", "601398.SH", "688001.SH"],
    market_bigqmt.STOCK_SECTOR_BY_MARKET["SZ"]: ["000001.SZ", "300750.SZ"],
    market_bigqmt.SECTOR_BY_TYPE["stock"]: ["600000.SH", "601398.SH", "688001.SH",
                                            "000001.SZ", "300750.SZ"],
    market_bigqmt.SECTOR_BY_TYPE["etf"]: ["510300.SH", "159915.SZ"],
}


class FakeContext(object):
    def __init__(self):
        self.asked = []

    def get_full_tick(self, codes):
        self.asked.append(list(codes))
        return dict((c, {"lastPrice": 1.0}) for c in codes)


def _provider(sectors=None):
    provider = market_bigqmt.BigQmtMarketDataProvider.__new__(
        market_bigqmt.BigQmtMarketDataProvider)
    provider.context_info = FakeContext()
    listings = SECTORS if sectors is None else sectors
    provider.get_stock_list_in_sector = lambda name: list(listings.get(name, []))
    return provider


class DefaultNarrowsToStocksTest(unittest.TestCase):
    def test_a_market_token_becomes_that_market_s_stocks(self):
        provider = _provider()

        provider.get_ticks(["SH"])

        self.assertEqual(provider.context_info.asked[0],
                         ["600000.SH", "601398.SH", "688001.SH"])

    def test_the_token_itself_never_reaches_qmt(self):
        """If it did, QMT would list the whole exchange again."""
        provider = _provider()

        provider.get_ticks(["SH"])

        self.assertNotIn("SH", provider.context_info.asked[0])

    def test_each_market_resolves_separately(self):
        provider = _provider()

        provider.get_ticks(["SH", "SZ"])

        self.assertEqual(sorted(provider.context_info.asked[0]),
                         ["000001.SZ", "300750.SZ", "600000.SH",
                          "601398.SH", "688001.SH"])

    def test_explicit_codes_are_left_alone(self):
        provider = _provider()

        provider.get_ticks(["600000.SH", "000001.SZ"])

        self.assertEqual(provider.context_info.asked[0],
                         ["600000.SH", "000001.SZ"])

    def test_a_token_mixed_with_explicit_codes(self):
        provider = _provider()

        provider.get_ticks(["SH", "159915.SZ"])

        asked = provider.context_info.asked[0]
        self.assertIn("159915.SZ", asked)
        self.assertIn("600000.SH", asked)
        self.assertNotIn("SH", asked)


class ExplicitTypesTest(unittest.TestCase):
    def test_all_restores_the_whole_exchange(self):
        provider = _provider()

        provider.get_ticks(["SH"], types=["all"])

        self.assertEqual(provider.context_info.asked[0], ["SH"])

    def test_all_is_case_insensitive(self):
        provider = _provider()

        provider.get_ticks(["SH"], types=["ALL"])

        self.assertEqual(provider.context_info.asked[0], ["SH"])

    def test_types_combine(self):
        provider = _provider()

        provider.get_ticks(["SH"], types=["stock", "etf"])

        asked = provider.context_info.asked[0]
        self.assertIn("600000.SH", asked)   # stock
        self.assertIn("510300.SH", asked)   # etf
        self.assertNotIn("159915.SZ", asked)  # SZ etf, wrong market

    def test_a_combination_does_not_duplicate_codes(self):
        overlapping = dict(SECTORS)
        overlapping[market_bigqmt.SECTOR_BY_TYPE["etf"]] = ["600000.SH"]
        provider = _provider(overlapping)

        provider.get_ticks(["SH"], types=["stock", "etf"])

        asked = provider.context_info.asked[0]
        self.assertEqual(len(asked), len(set(asked)))


class FailsOpenTest(unittest.TestCase):
    """Narrowing is an optimisation. When it cannot be done the token goes
    through unchanged -- a slow answer beats an empty one, the same rule the
    key-mapping in get_ticks already follows."""

    def test_an_unknown_type_falls_back_to_the_token(self):
        provider = _provider()

        provider.get_ticks(["SH"], types=["bogus"])

        self.assertEqual(provider.context_info.asked[0], ["SH"])

    def test_an_empty_sector_falls_back_to_the_token(self):
        """A terminal that does not carry the sector, or a broker that names it
        differently."""
        provider = _provider(sectors={})

        provider.get_ticks(["SH"])

        self.assertEqual(provider.context_info.asked[0], ["SH"])

    def test_a_raising_sector_lookup_falls_back(self):
        provider = _provider()

        def boom(_name):
            raise RuntimeError("sector service down")

        provider.get_stock_list_in_sector = boom
        provider.get_ticks(["SH"])

        self.assertEqual(provider.context_info.asked[0], ["SH"])

    def test_a_market_with_no_stock_sector_falls_back(self):
        """HK has no A-share sector; it must still return quotes."""
        provider = _provider()

        provider.get_ticks(["HK"])

        self.assertEqual(provider.context_info.asked[0], ["HK"])


class SectorLookupTest(unittest.TestCase):
    def test_a_sector_is_looked_up_once_per_run(self):
        provider = _provider()
        calls = []
        listing = SECTORS[market_bigqmt.STOCK_SECTOR_BY_MARKET["SH"]]
        provider.get_stock_list_in_sector = lambda name: (calls.append(name)
                                                          or list(listing))

        for _ in range(5):
            provider.get_ticks(["SH"])

        self.assertEqual(len(calls), 1, "looked the sector up %d times" % len(calls))

    def test_sector_names_are_the_ones_the_terminal_uses(self):
        """Verified against a live terminal: the Beijing board is 京市A股, and
        北证A股 returns nothing."""
        self.assertEqual(market_bigqmt.STOCK_SECTOR_BY_MARKET["SH"], u"上证A股")
        self.assertEqual(market_bigqmt.STOCK_SECTOR_BY_MARKET["SZ"], u"深证A股")
        self.assertEqual(market_bigqmt.STOCK_SECTOR_BY_MARKET["BJ"], u"京市A股")
        self.assertEqual(market_bigqmt.SECTOR_BY_TYPE["stock"], u"沪深京A股")


class ProviderCompatibilityTest(unittest.TestCase):
    """A market-data provider whose get_ticks takes only `codes` must keep
    working -- three tests went red on exactly this while building the feature."""

    def test_the_handler_calls_a_one_argument_provider(self):
        from bigqmt_signal_trader.redis_rpc import BigQmtRpcHandlers

        seen = []

        class _Old(object):
            def get_ticks(self, codes):
                seen.append(list(codes))
                return {}

        handlers = BigQmtRpcHandlers.__new__(BigQmtRpcHandlers)
        handlers.market_data = _Old()
        handlers._handle_get_ticks({"codes": ["600000.SH"]})

        self.assertEqual(seen, [["600000.SH"]])

    def test_the_handler_forwards_types_when_asked(self):
        from bigqmt_signal_trader.redis_rpc import BigQmtRpcHandlers

        seen = {}

        class _New(object):
            def get_ticks(self, codes, types=None):
                seen["types"] = types
                return {}

        handlers = BigQmtRpcHandlers.__new__(BigQmtRpcHandlers)
        handlers.market_data = _New()
        handlers._handle_get_ticks({"codes": ["SH"], "types": ["all"]})

        self.assertEqual(seen["types"], ["all"])

    def test_a_string_type_is_accepted(self):
        from bigqmt_signal_trader.redis_rpc import BigQmtRpcHandlers

        seen = {}

        class _New(object):
            def get_ticks(self, codes, types=None):
                seen["types"] = types
                return {}

        handlers = BigQmtRpcHandlers.__new__(BigQmtRpcHandlers)
        handlers.market_data = _New()
        handlers._handle_get_ticks({"codes": ["SH"], "types": "all"})

        self.assertEqual(seen["types"], ["all"])


if __name__ == "__main__":
    unittest.main()


class WholeQuotePrimeScopeTest(unittest.TestCase):
    """The priming snapshot must cover what the push will cover.

    subscribe_whole_quote's push side is ContextInfo's own subscription, which
    this change does not narrow. If the primer narrowed, a subscriber to ["SH"]
    would get 2315 stocks once and then 26744 instruments on every push.
    """

    def _xtdata(self):
        from bigqmt_signal_trader import xtquant_compat as compat

        class _Session(object):
            def start(self):
                pass

            def subscribe_whole_quote(self, code_list, callback=None):
                return 1

            def has_subscription(self, sub_id):
                return True

            def unsubscribe_quote(self, sub_id):
                return 0

        class _Client(object):
            account_id = "acct"
            local_cache_config = {}
            full_tick_cache_config = {}
            transport_name = "redis"

            def __init__(self):
                self.params = []

            def call(self, method, params=None, **kwargs):
                self.params.append((method, dict(params or {})))
                return {}

        client = _Client()
        data = compat.BigQmtXtData(client)
        data._quote_session_factory = lambda: _Session()
        return data, client

    def test_the_primer_asks_for_everything(self):
        data, client = self._xtdata()

        data.subscribe_whole_quote(["SH"], callback=lambda d: None)

        tick_calls = [p for m, p in client.params if m == "get_full_tick"]
        self.assertTrue(tick_calls, "no priming snapshot was requested")
        self.assertEqual(tick_calls[0].get("types"), ["all"])

    def test_a_plain_get_full_tick_still_defaults_to_stocks(self):
        """The escape hatch above must not leak into ordinary calls."""
        data, client = self._xtdata()

        data.get_full_tick(["SH"])

        tick_calls = [p for m, p in client.params if m == "get_full_tick"]
        self.assertNotIn("types", tick_calls[0])
