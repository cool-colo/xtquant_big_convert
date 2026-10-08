"""Futures symbol case must survive normalisation (issue #95).

Each exchange writes its contract symbols its own way and the two are not
interchangeable: SHFE writes "rb2401", CZCE writes "AP401". To QMT those are
different strings, so uppercasing a lowercase futures symbol yields a code it
does not recognise -- and an unrecognised code comes back as an empty quote,
not an error.

normalize_stock_code opened with `.upper()`, so every caller got the uppercased
form. #68 fixed this for POSITION rows by bypassing the function entirely; the
order, quote, cache and risk-guard paths all still came through here.
"""

import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.code_utils import normalize_stock_code


# (symbol as the exchange writes it, suffix)
FUTURES = [
    ("rb2401", ".SF"),    # SHFE  螺纹钢 -- lower
    ("ag2412", ".SF"),    # SHFE  白银   -- lower
    ("m2609", ".DF"),     # DCE   豆粕   -- lower
    ("AP401", ".ZF"),     # CZCE  苹果   -- upper
    ("CF405", ".ZF"),     # CZCE  棉花   -- upper
    ("IC2609", ".IF"),    # CFFEX 中证500 -- upper
    ("IF2609", ".IF"),    # CFFEX 沪深300 -- upper
    ("sc2503", ".INE"),   # INE   原油   -- lower
    ("si2504", ".GF"),    # GFEX  工业硅 -- lower
]


class FuturesCasePreservedTest(unittest.TestCase):
    def test_symbol_case_is_returned_verbatim(self):
        for symbol, suffix in FUTURES:
            code = symbol + suffix
            self.assertEqual(normalize_stock_code(code), code,
                             "%s was rewritten" % code)

    def test_lowercase_symbols_are_not_uppercased(self):
        """The specific regression: rb2401.SF must not become RB2401.SF."""
        self.assertEqual(normalize_stock_code("rb2401.SF"), "rb2401.SF")
        self.assertEqual(normalize_stock_code("m2609.DF"), "m2609.DF")

    def test_uppercase_symbols_are_not_lowercased_either(self):
        self.assertEqual(normalize_stock_code("AP401.ZF"), "AP401.ZF")
        self.assertEqual(normalize_stock_code("IC2609.IF"), "IC2609.IF")

    def test_the_suffix_itself_is_normalised(self):
        """Only the symbol is case-sensitive; the market suffix is not."""
        self.assertEqual(normalize_stock_code("rb2401.sf"), "rb2401.SF")
        self.assertEqual(normalize_stock_code("AP401.zf"), "AP401.ZF")
        self.assertEqual(normalize_stock_code("sc2503.ine"), "sc2503.INE")

    def test_normalisation_is_idempotent(self):
        for symbol, suffix in FUTURES:
            once = normalize_stock_code(symbol + suffix)
            self.assertEqual(normalize_stock_code(once), once, once)

    def test_two_spellings_stay_distinct(self):
        """If both collapsed to one string the bug would be invisible."""
        self.assertNotEqual(normalize_stock_code("rb2401.SF"),
                            normalize_stock_code("RB2401.SF"))


class StockCodesUnaffectedTest(unittest.TestCase):
    """Stock symbols are digits, so this change must not move them."""

    def test_plain_and_suffixed_stock_codes(self):
        self.assertEqual(normalize_stock_code("600000"), "600000.SH")
        self.assertEqual(normalize_stock_code("000001"), "000001.SZ")
        self.assertEqual(normalize_stock_code("600000.SH"), "600000.SH")
        self.assertEqual(normalize_stock_code("sh600000"), "600000.SH")
        self.assertEqual(normalize_stock_code("SZ000001"), "000001.SZ")

    def test_other_suffixed_markets(self):
        self.assertEqual(normalize_stock_code("00700.HGT"), "00700.HGT")
        self.assertEqual(normalize_stock_code("00700.hgt"), "00700.HGT")
        self.assertEqual(normalize_stock_code("10004356.SHO"), "10004356.SHO")

    def test_invalid_codes_still_raise(self):
        for bad in ("", None):
            self.assertEqual(normalize_stock_code(bad), "")
        with self.assertRaises(ValueError):
            normalize_stock_code("not-a-code")


class CallerPathsTest(unittest.TestCase):
    """The function is only interesting because of what calls it. These are the
    paths that were sending an uppercased futures code to QMT."""

    def test_order_submission_keeps_the_symbol(self):
        from bigqmt_signal_trader.adapters.order_bigqmt import BigQmtOrderGateway
        from bigqmt_signal_trader.models import OrderRequest

        captured = {}

        def passorder(op_type, order_type, account, code, price_type, price,
                      volume, strategy, quick, remark, context=None, *a, **k):
            captured["code"] = code

        gateway = BigQmtOrderGateway(account_id="acct", passorder_func=passorder,
                                     context_info=object())
        gateway.submit(OrderRequest(
            signal_id="s1", account_id="acct", stock_code="rb2401.SF",
            action="BUY", volume=1, price=3500.0, price_type="LIMIT",
            strategy_name="s"))

        self.assertEqual(captured["code"], "rb2401.SF")

    def test_full_tick_cache_keys_keep_the_symbol(self):
        """This one uppercased the code BEFORE calling normalize_stock_code, so
        it undid the fix on the very path issue #95 reported."""
        from bigqmt_signal_trader.full_tick_cache import normalize_full_tick_codes

        self.assertEqual(normalize_full_tick_codes(["rb2401.SF"]), ["rb2401.SF"])
        self.assertEqual(normalize_full_tick_codes(["AP401.ZF"]), ["AP401.ZF"])

    def test_full_tick_cache_still_recognises_whole_market_tokens(self):
        from bigqmt_signal_trader.full_tick_cache import normalize_full_tick_codes

        self.assertEqual(normalize_full_tick_codes(["sh", "SZ", "hk"]),
                         ["HK", "SH", "SZ"])

    def test_position_rows_and_this_function_now_agree(self):
        """#68 worked around the uppercasing for POSITION rows. Both paths must
        produce the same string, or a position and its order look unrelated."""
        from bigqmt_signal_trader.adapters.position_bigqmt import _full_code

        for symbol, suffix in FUTURES:
            market = suffix.lstrip(".")
            self.assertEqual(_full_code(symbol, market),
                             normalize_stock_code(symbol + suffix),
                             "%s%s" % (symbol, suffix))


if __name__ == "__main__":
    unittest.main()
