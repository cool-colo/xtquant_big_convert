# coding: utf-8
"""Redis and ZMQ must hand the caller identical values.

The two wrap the wire differently:

    redis   json.dumps(response)                 -> loads_rpc_response
    zmq     encode_rpc_request_payload(response) -> zmq_transport._loads
            (the same JSON, base64-obfuscated so patched QMT redis clients do
             not inspect stock-code text)

Same JSON either way, so the decoded objects must not differ. What makes that
worth pinning rather than assuming is the typed-payload flag: both sides scan
the response TEXT for the typed marker and record whether one was present, and
the client skips rebuilding when it was not (345.9ms -> 3.7ms on a
51285-instrument snapshot). Two independent scans of two different texts --
one of them base64-decoded first -- deciding whether to run a rebuild is
exactly the shape that produces "works on redis, subtly different on zmq".

Where the transports genuinely DO differ is in which features are wired, not
in what a working call returns. Those differences live in the strategy: see
test_strategy_name_attribution.py for the order-identity store, which only a
redis TRANSPORT used to build.
"""

import json
import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.redis_rpc import (
    TYPED_PAYLOAD_FLAG,
    encode_rpc_request_payload,
    loads_rpc_response,
    to_jsonable,
)
from bigqmt_signal_trader.transports import zmq_transport
from bigqmt_signal_trader.xtquant_compat import _restore_jsonable


def _envelope(data):
    """What the server sends back, after to_jsonable, for both transports."""
    return {"ok": True, "request_id": "r-1", "data": to_jsonable(data)}


def _client_tail(response):
    """The last few lines of BigQmtRpcClient.call, shared by both paths."""
    if response.pop(TYPED_PAYLOAD_FLAG, None) is False:
        return response.get("data")
    return _restore_jsonable(response.get("data"))


def _over_redis(data):
    raw = json.dumps(_envelope(data), ensure_ascii=False)
    return _client_tail(loads_rpc_response(raw))


def _over_zmq(data):
    raw = encode_rpc_request_payload(_envelope(data)).encode("utf-8")
    return _client_tail(zmq_transport._loads(raw))


class ParityTest(unittest.TestCase):
    def _same(self, data, expected=None):
        over_redis, over_zmq = _over_redis(data), _over_zmq(data)

        self.assertEqual(over_redis, over_zmq,
                         "redis gave %r, zmq gave %r" % (over_redis, over_zmq))
        if expected is not None:
            self.assertEqual(over_redis, expected)
        return over_redis

    def test_a_plain_dict(self):
        self._same({"account_id": "8886800503", "cash": 1234.56})

    def test_a_list_of_rows(self):
        self._same([{"stock_code": "600000.SH", "volume": 100},
                    {"stock_code": "000001.SZ", "volume": 200}])

    def test_chinese_text(self):
        """ensure_ascii=False on one side, base64 of utf-8 on the other."""
        self._same({"instrument_name": "金 螳 螂", "sector": "沪深A股"},
                   {"instrument_name": "金 螳 螂", "sector": "沪深A股"})

    def test_floats_keep_their_precision(self):
        self._same({"commission": 1.2275900000000002, "price": 17.03})

    def test_nan_and_inf_are_already_none_before_either_transport(self):
        """to_jsonable does that server-side, so neither wire format has to."""
        self._same({"a": float("nan"), "b": float("inf")}, {"a": None, "b": None})

    def test_none_and_empty_containers(self):
        self._same({"nothing": None, "rows": [], "map": {}})

    def test_a_deeply_nested_structure(self):
        self._same({"a": [{"b": [{"c": {"d": [1, 2, 3]}}]}]})

    def test_the_data_key_being_absent(self):
        bare = {"ok": True}

        over_redis = _client_tail(loads_rpc_response(json.dumps(bare)))
        over_zmq = _client_tail(zmq_transport._loads(
            encode_rpc_request_payload(bare).encode("utf-8")))

        self.assertEqual(over_redis, over_zmq)
        self.assertIsNone(over_redis)


class TypedPayloadFlagTest(unittest.TestCase):
    """The flag decides whether the client rebuilds. Both must decide alike."""

    def _flags(self, data):
        envelope = _envelope(data)
        redis_flag = loads_rpc_response(
            json.dumps(envelope, ensure_ascii=False))[TYPED_PAYLOAD_FLAG]
        zmq_flag = zmq_transport._loads(
            encode_rpc_request_payload(envelope).encode("utf-8"))[TYPED_PAYLOAD_FLAG]
        return redis_flag, zmq_flag

    def test_plain_data_is_flagged_plain_on_both(self):
        redis_flag, zmq_flag = self._flags({"cash": 1.0})

        self.assertEqual((redis_flag, zmq_flag), (False, False))

    def test_the_zmq_scan_looks_at_the_DECODED_text(self):
        """It is base64 on the wire; scanning that would never see the marker
        and every typed payload would silently skip its rebuild."""
        try:
            import pandas as pd
        except ImportError:
            self.skipTest("pandas not installed")

        _redis_flag, zmq_flag = self._flags(pd.DataFrame({"close": [1.0, 2.0]}))

        self.assertTrue(zmq_flag)

    def test_both_agree_a_typed_payload_is_typed(self):
        try:
            import pandas as pd
        except ImportError:
            self.skipTest("pandas not installed")

        redis_flag, zmq_flag = self._flags(pd.DataFrame({"close": [1.0, 2.0]}))

        self.assertEqual(redis_flag, zmq_flag)


class TypedPayloadParityTest(unittest.TestCase):
    def setUp(self):
        try:
            import pandas as pd
        except ImportError:
            self.skipTest("pandas not installed")
        self.pd = pd

    def test_a_dataframe_survives_both_identically(self):
        frame = self.pd.DataFrame({"close": [1.0, 2.0], "volume": [100, 200]},
                                  index=["20260901", "20260902"])

        over_redis, over_zmq = _over_redis(frame), _over_zmq(frame)

        self.pd.testing.assert_frame_equal(over_redis, over_zmq)

    def test_a_dict_of_frames_survives_both_identically(self):
        frames = {"600000.SH": self.pd.DataFrame({"close": [1.0]}),
                  "000001.SZ": self.pd.DataFrame({"close": [2.0]})}

        over_redis, over_zmq = _over_redis(frames), _over_zmq(frames)

        self.assertEqual(sorted(over_redis), sorted(over_zmq))
        for code in over_redis:
            self.pd.testing.assert_frame_equal(over_redis[code], over_zmq[code])

    def test_a_series_survives_both_identically(self):
        series = self.pd.Series([1.0, 2.0], index=["a", "b"])

        over_redis, over_zmq = _over_redis(series), _over_zmq(series)

        self.assertEqual(type(over_redis), type(over_zmq))
        self.pd.testing.assert_series_equal(over_redis, over_zmq)


class TheObfuscationIsTheOnlyWireDifferenceTest(unittest.TestCase):
    def test_the_zmq_wire_is_not_plain_json(self):
        raw = encode_rpc_request_payload(_envelope({"cash": 1.0}))

        self.assertNotIn('"cash"', raw)

    def test_and_it_decodes_back_to_exactly_the_redis_wire(self):
        envelope = _envelope({"cash": 1.0, "name": "金 螳 螂"})

        decoded = zmq_transport.decode_rpc_request_payload(
            encode_rpc_request_payload(envelope))

        self.assertEqual(json.loads(decoded),
                         json.loads(json.dumps(envelope, ensure_ascii=False)))


if __name__ == "__main__":
    unittest.main()
