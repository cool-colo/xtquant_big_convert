# coding: utf-8
"""fill_data must reach big QMT, not be dropped on the way (#167).

@zxm9999 pointed straight at it: _market_data_shapes builds big_kwargs without
fill_data, and big_kwargs is the FIRST shape _call_first_supported tries. It
succeeds, so the argument is silently discarded -- a caller asking not to fill
suspended days gets them filled anyway, and nothing raises.

Big QMT does accept it. Its own signature (API reference 5.x) is

    C.get_market_data_ex(fields=[], stock_code=[], period='follow',
                         start_time='', end_time='', count=-1,
                         dividend_type='follow', fill_data=True, subscribe=True)

and the terminal's bundled SDK agrees:

    get_market_data_ex(field_list, stock_list, period, start_time, end_time,
                       count, dividend_type, fill_data)

The fix carries fill_data in a shape of its own, tried ahead of the bare one,
rather than adding it to big_kwargs: _call_first_supported falls through only
on TypeError, so a terminal whose signature lacks fill_data still needs the
bare shape to be reachable.
"""

import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.adapters.market_bigqmt import BigQmtMarketDataProvider


class Recorder(object):
    """A ContextInfo that records how it was called.

    ``reject`` names kwargs this terminal's signature does not have; passing
    one raises TypeError, exactly as a real mismatch would.
    """

    def __init__(self, reject=()):
        self.reject = set(reject)
        self.calls = []

    def _record(self, name, args, kwargs):
        bad = self.reject.intersection(kwargs)
        self.calls.append({"method": name, "args": args, "kwargs": dict(kwargs),
                           "rejected": sorted(bad)})
        if bad:
            raise TypeError("unexpected keyword argument %r" % sorted(bad)[0])
        return {"600000.SH": {"close": [1.0]}}

    def get_market_data_ex(self, *args, **kwargs):
        return self._record("get_market_data_ex", args, kwargs)

    def get_market_data(self, *args, **kwargs):
        return self._record("get_market_data", args, kwargs)

    def get_local_data(self, *args, **kwargs):
        return self._record("get_local_data", args, kwargs)


def _provider(context):
    return BigQmtMarketDataProvider(context_info=context)


class ItReachesBigQmtTest(unittest.TestCase):
    def test_fill_data_false_is_sent(self):
        """The bug in one line: this used to arrive as no argument at all."""
        context = Recorder()
        _provider(context).get_market_data_ex(
            field_list=[], stock_list=["600000.SH"], period="1d",
            fill_data=False)

        first = context.calls[0]
        self.assertIn("fill_data", first["kwargs"])
        self.assertFalse(first["kwargs"]["fill_data"])

    def test_fill_data_true_is_sent_explicitly(self):
        context = Recorder()
        _provider(context).get_market_data_ex(
            field_list=[], stock_list=["600000.SH"], period="1d", fill_data=True)

        self.assertTrue(context.calls[0]["kwargs"]["fill_data"])

    def test_it_is_the_very_first_shape_tried(self):
        """Anything else means a shape without fill_data can win first."""
        context = Recorder()
        _provider(context).get_market_data_ex(
            field_list=[], stock_list=["600000.SH"], period="1d",
            fill_data=False)

        self.assertEqual(len(context.calls), 1, "should not need a fallback")
        self.assertIn("fill_data", context.calls[0]["kwargs"])

    def test_the_big_qmt_key_names_are_kept(self):
        """It must stay the big-QMT shape: fields/stock_code, not the mini
        spelling -- otherwise it is a different fallback that happens to work."""
        context = Recorder()
        _provider(context).get_market_data_ex(
            field_list=["close"], stock_list=["600000.SH"], period="1d",
            fill_data=False)

        kwargs = context.calls[0]["kwargs"]
        self.assertEqual(kwargs["fields"], ["close"])
        self.assertEqual(kwargs["stock_code"], ["600000.SH"])

    def test_get_market_data_carries_it_too(self):
        context = Recorder()
        _provider(context).get_market_data(
            field_list=[], stock_list=["600000.SH"], period="1d",
            fill_data=False)

        self.assertFalse(context.calls[0]["kwargs"]["fill_data"])

    def test_get_local_data_carries_it_too(self):
        context = Recorder()
        _provider(context).get_local_data(
            field_list=[], stock_list=["600000.SH"], period="1d",
            fill_data=False)

        self.assertFalse(context.calls[0]["kwargs"]["fill_data"])


class OlderTerminalStillWorksTest(unittest.TestCase):
    """A terminal whose signature lacks fill_data must not start failing."""

    def test_it_falls_back_to_the_bare_shape(self):
        context = Recorder(reject={"fill_data"})

        result = _provider(context).get_market_data_ex(
            field_list=[], stock_list=["600000.SH"], period="1d",
            fill_data=False)

        self.assertTrue(result, "the call must still succeed")
        self.assertEqual(context.calls[0]["rejected"], ["fill_data"])
        self.assertNotIn("fill_data", context.calls[1]["kwargs"])

    def test_the_fallback_keeps_every_other_argument(self):
        context = Recorder(reject={"fill_data"})

        _provider(context).get_market_data_ex(
            field_list=["close"], stock_list=["600000.SH"], period="1d",
            start_time="20260101", end_time="20260201", count=5,
            dividend_type="front", fill_data=False)

        kwargs = context.calls[1]["kwargs"]
        self.assertEqual(kwargs["fields"], ["close"])
        self.assertEqual(kwargs["period"], "1d")
        self.assertEqual(kwargs["start_time"], "20260101")
        self.assertEqual(kwargs["end_time"], "20260201")
        self.assertEqual(kwargs["count"], 5)
        self.assertEqual(kwargs["dividend_type"], "front")

    def test_a_terminal_rejecting_the_big_names_still_reaches_a_shape(self):
        """The whole point of the shape list -- do not narrow it."""
        context = Recorder(reject={"fields", "stock_code"})

        result = _provider(context).get_market_data_ex(
            field_list=[], stock_list=["600000.SH"], period="1d",
            fill_data=False)

        self.assertTrue(result)
        self.assertFalse(context.calls[-1]["rejected"])


class ShapeOrderTest(unittest.TestCase):
    def test_every_filled_shape_precedes_its_bare_twin(self):
        provider = _provider(Recorder())
        shapes = provider._market_data_shapes(
            "get_market_data_ex", field_list=[], stock_list=["600000.SH"],
            fill_data=False)

        kwarg_shapes = [k for _, args, k in shapes if not args]
        with_fill = [i for i, k in enumerate(kwarg_shapes) if "fill_data" in k]
        without = [i for i, k in enumerate(kwarg_shapes) if "fill_data" not in k]

        self.assertTrue(with_fill, "no shape carries fill_data")
        self.assertTrue(without, "the compatibility shape is gone")
        self.assertLess(min(with_fill), min(without))

    def test_the_default_is_still_true(self):
        """Callers that say nothing must keep QMT's own default."""
        provider = _provider(Recorder())
        shapes = provider._market_data_shapes(
            "get_market_data_ex", field_list=[], stock_list=["600000.SH"])

        self.assertTrue(shapes[0][2]["fill_data"])


if __name__ == "__main__":
    unittest.main()
