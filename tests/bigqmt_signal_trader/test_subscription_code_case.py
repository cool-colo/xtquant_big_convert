# coding: utf-8
"""Whole-quote subscriptions must not uppercase futures symbols (#95).

The reporter's two runs say it exactly:

    CF701.ZF   pushes every 250ms, indefinitely
    cu2610.SF  one frame, then silence

and the single cu2610 frame arrives BEFORE his own `print("开始监听...")` --
it is the initial snapshot, which takes the case-preserving get_full_tick path.
The subscription behind it never produced anything.

It reads like an exchange difference and is not one. It is case:

    cu2610.SF   .upper() -> CU2610.SF    a contract QMT does not have
    CF701.ZF    .upper() -> CF701.ZF     unchanged, so it worked

The manager did its own `.upper()` on both the combo key and the code list
handed to big QMT, bypassing normalize_stock_code -- which since #58 keeps the
caller's symbol verbatim for the case-sensitive suffixes (.SF .DF .IF .ZF .INE
.GF) precisely because futures symbols are lowercase. Uppercasing is still
right for a bare exchange token, so the two cases are separated rather than the
call replaced.
"""

import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.quote_subscription_manager import (
    QuoteSubscriptionManager,
    combo_key,
    normalize_subscription_code,
)


class NormalizeTest(unittest.TestCase):
    def test_a_lowercase_futures_symbol_keeps_its_case(self):
        """The whole bug: this used to become CU2610.SF."""
        self.assertEqual(normalize_subscription_code("cu2610.SF"), "cu2610.SF")

    def test_an_uppercase_futures_symbol_is_left_alone(self):
        """CF701.ZF worked before only because .upper() was a no-op on it."""
        self.assertEqual(normalize_subscription_code("CF701.ZF"), "CF701.ZF")

    def test_the_suffix_is_still_normalized(self):
        """Only the symbol is the caller's; the exchange suffix is ours."""
        self.assertEqual(normalize_subscription_code("rb2610.sf"), "rb2610.SF")

    def test_exchange_tokens_are_still_uppercased(self):
        for token, expected in (("sh", "SH"), ("SH", "SH"), ("sz", "SZ"),
                                ("if", "IF"), ("sf", "SF")):
            self.assertEqual(normalize_subscription_code(token), expected, token)

    def test_stock_codes_are_unaffected(self):
        self.assertEqual(normalize_subscription_code("600000.SH"), "600000.SH")
        self.assertEqual(normalize_subscription_code("000001.sz"), "000001.SZ")

    def test_blank_input_is_empty_not_an_error(self):
        for value in ("", "   ", None):
            self.assertEqual(normalize_subscription_code(value), "")

    def test_something_unparseable_falls_back_rather_than_raising(self):
        """normalize_stock_code raises on junk; a subscription must not die
        because one code in a list was odd."""
        self.assertEqual(normalize_subscription_code("not.a.code"), "NOT.A.CODE")


class ComboKeyTest(unittest.TestCase):
    def test_exchange_tokens_still_share_one_subscription(self):
        """The dedupe this function exists for must keep working."""
        self.assertEqual(combo_key(["SH", "SZ"]), combo_key(["sz", "sh"]))
        self.assertEqual(combo_key(["SH", "SH", "SZ"]), "SH,SZ")

    def test_a_futures_code_keeps_its_case_in_the_key(self):
        self.assertEqual(combo_key(["cu2610.SF"]), "cu2610.SF")

    def test_two_cases_of_one_symbol_are_two_subscriptions(self):
        """Big QMT treats them as different -- only one of them exists -- so
        collapsing them would hand the wrong string to the exchange."""
        self.assertNotEqual(combo_key(["cu2610.SF"]), combo_key(["CU2610.SF"]))

    def test_it_is_still_order_independent(self):
        self.assertEqual(combo_key(["cu2610.SF", "CF701.ZF"]),
                         combo_key(["CF701.ZF", "cu2610.SF"]))

    def test_blanks_are_dropped(self):
        self.assertEqual(combo_key(["cu2610.SF", "", "   ", None]), "cu2610.SF")


class Source(object):
    """Records exactly what string reaches big QMT."""

    def __init__(self):
        self.subscribed = []

    def subscribe(self, codes, on_push):
        self.subscribed.append(list(codes))
        return len(self.subscribed)

    def unsubscribe(self, handle):
        pass


class WhatReachesBigQmtTest(unittest.TestCase):
    """The assertion that would have caught this: check the string sent out."""

    def _manager(self):
        source = Source()
        return QuoteSubscriptionManager(source), source

    def test_the_futures_symbol_goes_out_in_the_callers_case(self):
        manager, source = self._manager()

        manager.subscribe("client-1", "sub-1", ["cu2610.SF"])

        self.assertEqual(source.subscribed, [["cu2610.SF"]])

    def test_an_exchange_token_goes_out_uppercased(self):
        manager, source = self._manager()

        manager.subscribe("client-1", "sub-1", ["sh"])

        self.assertEqual(source.subscribed, [["SH"]])

    def test_a_mixed_list_gets_each_one_right(self):
        manager, source = self._manager()

        manager.subscribe("client-1", "sub-1", ["cu2610.SF", "sh", "CF701.ZF"])

        self.assertEqual(source.subscribed, [["CF701.ZF", "SH", "cu2610.SF"]])


if __name__ == "__main__":
    unittest.main()
