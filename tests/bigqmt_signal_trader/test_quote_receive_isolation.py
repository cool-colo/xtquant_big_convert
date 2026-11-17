import json
import os
import sys
import threading
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.quote_push_channel import RedisQuotePushChannel, encode_push_payload
from bigqmt_signal_trader.transports.pipe_push_channel import PipeQuotePushChannel


class FrameIsolationTest(unittest.TestCase):
    def test_redis_bad_frames_do_not_reconnect_or_block_later_data(self):
        stopped = threading.Event()
        frames = iter([b"invalid-json", encode_push_payload([]),
                       encode_push_payload({"data": []}),
                       encode_push_payload({"data": {"A": {"v": 1}}})])
        class PubSub:
            closed = False
            def subscribe(self, *channels):
                pass
            def get_message(self, timeout):
                return {"type": "message", "channel": b"bigqmt:quote_push:a:T", "data": next(frames)}
            def close(self):
                self.closed = True
        class Redis:
            connections = 0
            def pubsub(self, **kwargs):
                self.connections += 1
                return pubsub
        pubsub = PubSub()
        redis = Redis()
        channel = RedisQuotePushChannel(redis, "a")
        received = []
        def receive(topic, data):
            received.append((topic, data))
            stopped.set()
        channel._sub_loop(["T"], receive, stopped)
        self.assertEqual(received, [("T", {"A": {"v": 1}})])
        self.assertEqual(redis.connections, 1)
        self.assertTrue(pubsub.closed)
        self.assertEqual(channel._quote_diagnostics.counts["decode_frame"], 3)

    def test_pipe_bad_json_shape_and_callback_do_not_end_receiver(self):
        channel = PipeQuotePushChannel(connect_address="unused")
        stopped = threading.Event()
        frames = iter([b"invalid-json", b"[]", b'{"topic":"T","data":[]}',
                       json.dumps({"topic": "T", "data": {"v": 1}}).encode(),
                       json.dumps({"topic": "T", "data": {"v": 2}}).encode(), None])
        class Dll:
            def CloseHandle(self, handle):
                pass
        handles = iter([1, None])
        channel._dll = lambda: (Dll(), None)
        channel._connect_client = lambda event: next(handles)
        channel._read_one_message = lambda *args: next(frames)
        received = []
        def receive(topic, data):
            if data["v"] == 1:
                raise RuntimeError("callback failed")
            received.append(data)
        with patch("bigqmt_signal_trader.transports.pipe_push_channel.time.sleep"):
            channel._sub_loop(["T"], receive, stopped)
        self.assertEqual(received, [{"v": 2}])
        self.assertEqual(channel._quote_diagnostics.counts["decode_frame"], 3)
        self.assertEqual(channel._quote_diagnostics.counts["callback_failed"], 1)

    def test_pipe_unexpected_receiver_error_is_reported(self):
        channel = PipeQuotePushChannel(connect_address="unused")
        class Dll:
            def CloseHandle(self, handle):
                pass
        channel._dll = lambda: (Dll(), None)
        channel._connect_client = lambda event: 1
        def fail_read(*args):
            raise OSError("read failed")
        channel._read_one_message = fail_read
        channel._sub_loop([], lambda *args: None, threading.Event())
        self.assertEqual(channel._quote_diagnostics.counts["receiver_failed"], 1)


class InvalidEncodingTest(unittest.TestCase):
    def test_invalid_encoding_fails_at_construction_not_as_bad_stock(self):
        with self.assertRaisesRegex(ValueError, "unsupported quote push encoding"):
            RedisQuotePushChannel(None, wire_encoding="unsupported")
