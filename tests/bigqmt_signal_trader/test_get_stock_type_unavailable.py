# coding: utf-8
"""get_stock_type must not hand back the server's meaningless 0 (#130 follow-up).

PR #132 added six wrappers for methods the RPC whitelist already carried. Five
of them fail loudly on Big QMT ("needs native xtdata SDK quote service"), which
is fine -- the caller learns something. `get_stock_type` was the exception: it
returned successfully. Live against the deployed bridge:

    600000.SH     (stock)  -> 0
    589820.SH     (ETF)    -> 0
    186511.SH     (bond)   -> 0
    10011096.SHO  (option) -> 0
    600000 / SH600000 / 600000.SSE   -> 0

Server-side it is `ContextInfo.get_stock_type(stock)`, and a *missing* stub
would raise NotImplementedError -- so the stub is there and QMT itself answers
0 for everything. A constant dressed as a classification is worse than the
AttributeError #130 reported: the error is visible, the wrong answer is not.

So the wrapper refuses, and points at `get_instrument_type`, which was already
there and which does discriminate (verified live in the same run).
"""

import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.redis_rpc import MARKET_DATA_METHODS
from bigqmt_signal_trader.xtquant_compat import BigQmtXtData


class _Recorder(BigQmtXtData):
    def __init__(self):
        self.calls = []

    def _call(self, method, **params):
        self.calls.append((method, params))
        return 0                      # what the live bridge actually answers


class RefusesTest(unittest.TestCase):
    def setUp(self):
        self.data = _Recorder()

    def test_it_raises_instead_of_returning_the_constant(self):
        with self.assertRaises(NotImplementedError):
            self.data.get_stock_type("600000.SH")

    def test_it_does_not_even_spend_the_round_trip(self):
        """No point paying an RPC for an answer we know is meaningless."""
        try:
            self.data.get_stock_type("600000.SH")
        except NotImplementedError:
            pass

        self.assertEqual(self.data.calls, [])

    def test_the_message_names_the_working_alternative(self):
        try:
            self.data.get_stock_type("600000.SH")
        except NotImplementedError as exc:
            self.assertIn("get_instrument_type", str(exc))
        else:
            self.fail("expected NotImplementedError")

    def test_the_message_says_why_rather_than_just_no(self):
        try:
            self.data.get_stock_type("600000.SH")
        except NotImplementedError as exc:
            text = str(exc)
            self.assertIn("returns 0 for every code", text)
            self.assertIn("ContextInfo.get_stock_type", text)
        else:
            self.fail("expected NotImplementedError")

    def test_the_variety_list_parameter_survives_for_signature_parity(self):
        with self.assertRaises(NotImplementedError):
            self.data.get_stock_type("600000.SH", variety_list=["stock"])


class StillExposedTest(unittest.TestCase):
    """Refusing is not the same as deleting.

    The coverage invariant in test_compat_method_coverage.py asks that every
    whitelisted method be wrapped or declared call-method-only. A wrapper that
    raises still satisfies it -- and should, because the raise is the useful
    part.
    """

    def test_it_is_still_an_attribute(self):
        self.assertTrue(hasattr(BigQmtXtData, "get_stock_type"))

    def test_it_is_still_whitelisted_server_side(self):
        self.assertIn("get_stock_type", MARKET_DATA_METHODS)


class TheAlternativeStillWorksTest(unittest.TestCase):
    """Regression guard: don't break the method we are redirecting people to."""

    def test_get_instrument_type_still_forwards(self):
        data = _Recorder()

        data.get_instrument_type("600000.SH")

        self.assertEqual(data.calls,
                         [("get_instrument_type",
                           {"code": "600000.SH", "variety_list": None})])


class ShimAgreesTest(unittest.TestCase):
    """The top-level xtquant.xtdata shim must not route around the refusal."""

    def test_the_shim_forwards_to_the_wrapper_not_call_method(self):
        import io

        path = os.path.join(ROOT, "src", "xtquant", "xtdata.py")
        with io.open(path, encoding="utf-8") as handle:
            source = handle.read()

        self.assertIn("return _compat.xtdata.get_stock_type(stock)", source)
        self.assertNotIn('call_method("get_stock_type"', source)


if __name__ == "__main__":
    unittest.main()
