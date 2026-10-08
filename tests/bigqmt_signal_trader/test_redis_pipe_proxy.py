# coding: utf-8
import os
import sys
import time
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.redis_pipe_proxy import RedisPipeProxy
from bigqmt_signal_trader.transports.base import TransportTimeout


class FakeRedis(object):
    def __init__(self):
        self.events = []
        self.event_ids = set()
        self.closed = False

    def eval(self, script, key_count, *args):
        if "XADD" in script:
            event_id = args[0].rsplit(":", 1)[-1]
            # Use the full dedup key because event IDs themselves contain ':'.
            dedup_key = args[0]
            if dedup_key in self.event_ids:
                return 0
            self.event_ids.add(dedup_key)
            self.events.append((args[1], args[2]))
            return 1
        return 1

    def close(self):
        self.closed = True


class FakeRpcPipe(object):
    def __init__(self, result=None):
        self.result = result
        self.requests = []

    def send_request(self, request, timeout_seconds):
        self.requests.append((request, timeout_seconds))
        if isinstance(self.result, Exception):
            raise self.result
        if self.result is not None:
            return self.result
        return {
            "request_id": request["request_id"],
            "account_id": request["account_id"],
            "method": request["method"],
            "ok": True,
            "data": {"pong": True},
        }

    def stop(self):
        pass


class FakeEventPipe(object):
    def stop(self):
        pass


class FakeRedisTransport(object):
    request_queue = "queue"

    def __init__(self):
        self.responses = []

    def send_response(self, request, response):
        self.responses.append((request, response))

    def stop(self):
        pass


def request(method="ping", timeout=6.0):
    return {
        "schema_version": 1,
        "request_id": "request-1",
        "account_id": "acct",
        "method": method,
        "params": {},
        "timeout_seconds": timeout,
    }


def wait_for(predicate, timeout=1.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class RedisPipeProxyTest(unittest.TestCase):
    def make_proxy(self, pipe=None):
        redis = FakeRedis()
        replies = FakeRedisTransport()
        proxy = RedisPipeProxy(
            redis, "acct", workers=1, max_pending=1,
            rpc_pipe=pipe or FakeRpcPipe(), event_pipe=FakeEventPipe(),
            redis_transport=replies, shutdown_grace_seconds=0,
        )
        self.addCleanup(proxy.stop)
        proxy._running = True
        proxy._accepting = True
        return proxy, redis, replies

    def test_successful_read_is_returned_through_original_redis_route(self):
        pipe = FakeRpcPipe()
        proxy, _redis, replies = self.make_proxy(pipe)
        incoming = request()
        incoming["_received_at"] = 1.0

        self.assertIsNone(proxy._ingress(incoming))
        self.assertTrue(wait_for(lambda: len(replies.responses) == 1))
        self.assertTrue(replies.responses[0][1]["ok"])
        self.assertGreater(pipe.requests[0][0]["_received_at"], 1.0)
        self.assertEqual(incoming["_received_at"], 1.0)

    def test_timeoutless_submit_is_rejected_before_pipe_dispatch(self):
        pipe = FakeRpcPipe()
        proxy, _redis, _replies = self.make_proxy(pipe)
        result = proxy._ingress(request("order_stock", timeout=None))

        self.assertEqual(result["proxy_error"], "TIMEOUT_REQUIRED")
        self.assertEqual(pipe.requests, [])

    def test_unknown_submit_timeout_has_no_synthesized_redis_response(self):
        proxy, _redis, replies = self.make_proxy(
            FakeRpcPipe(TransportTimeout("lost after write")),
        )

        self.assertIsNone(proxy._ingress(request("order_stock")))
        self.assertTrue(wait_for(lambda: not proxy._futures))
        self.assertEqual(replies.responses, [])

    def test_read_timeout_returns_pipe_timeout(self):
        proxy, _redis, replies = self.make_proxy(
            FakeRpcPipe(TransportTimeout("read timed out")),
        )

        self.assertIsNone(proxy._ingress(request("ping")))
        self.assertTrue(wait_for(lambda: len(replies.responses) == 1))
        self.assertEqual(replies.responses[0][1]["proxy_error"], "PIPE_TIMEOUT")

    def test_late_cancel_uses_operational_timeout_instead_of_expiring(self):
        pipe = FakeRpcPipe()
        proxy, _redis, replies = self.make_proxy(pipe)
        late = request("cancel_order_stock", timeout=0.001)

        self.assertIsNone(proxy._ingress(late))
        self.assertTrue(wait_for(lambda: len(replies.responses) == 1))
        self.assertEqual(pipe.requests[0][1], proxy.legacy_read_timeout_seconds)

    def test_event_publish_is_deduplicated_by_event_id(self):
        proxy, redis, _replies = self.make_proxy()
        event_request = request("publish_exec_event")
        event_request["request_id"] = "instance:7"
        event_request["params"] = {"event": {"event_type": "trade", "trade_id": "T1"}}

        self.assertTrue(proxy._handle_event(event_request)["ok"])
        self.assertTrue(proxy._handle_event(event_request)["ok"])
        self.assertEqual(len(redis.events), 1)
        self.assertIn("bigqmt:trade_events:acct", redis.events[0][0])

    def test_inner_event_account_cannot_cross_the_proxy_account(self):
        proxy, redis, _replies = self.make_proxy()
        event_request = request("publish_exec_event")
        event_request["request_id"] = "instance:8"
        event_request["params"] = {
            "event": {"event_type": "order", "account_id": "other"},
        }

        response = proxy._handle_event(event_request)

        self.assertFalse(response["ok"])
        self.assertEqual(response["event_error"], "ACCOUNT_MISMATCH")
        self.assertEqual(redis.events, [])


if __name__ == "__main__":
    unittest.main()
