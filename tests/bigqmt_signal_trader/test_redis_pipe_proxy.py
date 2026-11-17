# coding: utf-8
import os
import sys
import threading
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
            # Use the full dedup key because event IDs themselves contain ':'.
            dedup_key = args[0]
            if dedup_key in self.event_ids:
                return 0
            self.event_ids.add(dedup_key)
            self.events.append((args[1], args[2]))
            return 1
        return 1

    def set(self, *args, **kwargs):
        return True

    def llen(self, _key):
        return 0

    def lpop(self, _key):
        return None

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
    def start_receiving(self, callback, background_threads=True):
        self.callback = callback

    def stop(self):
        pass


class FakeQuotePipe(object):
    def __init__(self):
        self.topics = None
        self.callback = None
        self.stopped = False

    def start_subscriber(self, topics, callback):
        self.topics = list(topics)
        self.callback = callback

    def stop(self):
        self.stopped = True


class FakeQuoteRedisChannel(object):
    def __init__(self):
        self.started = False
        self.published = []
        self.stopped = False

    def start_publisher(self):
        self.started = True

    def publish(self, topic, data):
        self.published.append((topic, data))

    def stop(self):
        self.stopped = True


class BlockingQuoteRedisChannel(FakeQuoteRedisChannel):
    def __init__(self):
        super(BlockingQuoteRedisChannel, self).__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def publish(self, topic, data):
        self.entered.set()
        self.release.wait(1.0)
        super(BlockingQuoteRedisChannel, self).publish(topic, data)


class FakeRedisTransport(object):
    request_queue = "queue"

    def __init__(self):
        self.responses = []

    def send_response(self, request, response):
        self.responses.append((request, response))

    def start_receiving(self, callback, background_threads=True):
        self.callback = callback

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
    def make_proxy(self, pipe=None, **kwargs):
        redis = FakeRedis()
        replies = FakeRedisTransport()
        proxy = RedisPipeProxy(
            redis, "acct", workers=1, max_pending=1,
            rpc_pipe=pipe or FakeRpcPipe(), event_pipe=FakeEventPipe(),
            redis_transport=replies, shutdown_grace_seconds=0,
            **kwargs
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

    def test_quote_push_relay_forwards_quotes_and_execution_fallback(self):
        quote_pipe = FakeQuotePipe()
        quote_redis = FakeQuoteRedisChannel()
        proxy, redis, _replies = self.make_proxy(
            quote_push_relay=True,
            quote_pipe=quote_pipe,
            quote_redis_channel=quote_redis,
        )
        proxy._start_quote_relay()

        proxy._handle_quote_push("000001.SZ", {"000001.SZ": {"lastPrice": 10.0}})
        proxy._handle_quote_push(
            "exec:trade",
            {"event_id": "fallback:1", "trade_id": "T1"},
        )
        proxy._handle_quote_push("000002.SZ", "not-a-dict")

        self.assertTrue(wait_for(lambda: len(quote_redis.published) == 1))
        self.assertTrue(wait_for(lambda: len(redis.events) == 1))
        self.assertEqual(
            quote_redis.published,
            [("000001.SZ", {"000001.SZ": {"lastPrice": 10.0}})],
        )
        self.assertIn("bigqmt:trade_events:acct", redis.events[0][0])

    def test_quote_pipe_reader_is_not_blocked_by_slow_redis_publish(self):
        quote_redis = BlockingQuoteRedisChannel()
        proxy, _redis, _replies = self.make_proxy(
            quote_push_relay=True,
            quote_pipe=FakeQuotePipe(),
            quote_redis_channel=quote_redis,
        )
        proxy._start_quote_relay()

        started = time.time()
        proxy._handle_quote_push("SH", {"600000.SH": {"lastPrice": 10.0}})
        elapsed = time.time() - started

        self.assertLess(elapsed, 0.1)
        self.assertTrue(quote_redis.entered.wait(1.0))
        quote_redis.release.set()

    def test_quote_overload_preserves_all_topics_and_latest_symbol(self):
        proxy, _redis, _replies = self.make_proxy(
            quote_push_relay=True,
            quote_queue_capacity=1,
            quote_pipe=FakeQuotePipe(),
            quote_redis_channel=FakeQuoteRedisChannel(),
        )

        proxy._handle_quote_push("SH", {"600000.SH": {"v": 1}})
        proxy._handle_quote_push("SH", {"600000.SH": {"v": 2}})
        proxy._handle_quote_push("SZ", {"000001.SZ": {"v": 3}})

        self.assertEqual(proxy._quote_pending, {
            "SH": {"600000.SH": {"v": 2}}, "SZ": {"000001.SZ": {"v": 3}}})
        self.assertEqual(proxy._quote_dropped, 0)
        proxy._start_quote_relay()
        self.assertTrue(wait_for(lambda: len(proxy._quote_redis_channel.published) == 2))
        self.assertEqual([topic for topic, _ in proxy._quote_redis_channel.published], ["SH", "SZ"])

    def test_replacing_pending_quote_keeps_only_that_frames_trace(self):
        quote_redis = FakeQuoteRedisChannel()
        proxy, _redis, _replies = self.make_proxy(
            quote_push_relay=True,
            quote_pipe=FakeQuotePipe(),
            quote_redis_channel=quote_redis,
        )
        older = {"600000.SH": {"lastPrice": 10, "_trace": {
            "qmt_callback_ns": 100, "proxy_pipe_received_ns": 110,
        }}}
        newer = {"600000.SH": {"lastPrice": 11, "_trace": {
            "qmt_callback_ns": 200, "proxy_pipe_received_ns": 210,
        }}}
        proxy._handle_quote_push("SH", older)
        proxy._handle_quote_push("SH", newer)
        proxy._start_quote_relay()
        self.assertTrue(wait_for(lambda: len(quote_redis.published) == 1))
        self.assertEqual(quote_redis.published, [("SH", newer)])
        self.assertEqual(older["600000.SH"]["_trace"]["qmt_callback_ns"], 100)

    def test_all_execution_fallback_topics_map_to_redis_event_types(self):
        proxy, _redis, _replies = self.make_proxy(
            quote_push_relay=True,
            quote_pipe=FakeQuotePipe(),
            quote_redis_channel=FakeQuoteRedisChannel(),
        )
        expected = {
            "exec:order": "order",
            "exec:trade": "trade",
            "exec:order_error": "order_error",
            "exec:cancel_error": "cancel_error",
        }

        for index, (topic, event_type) in enumerate(expected.items()):
            proxy._handle_quote_push(topic, {"event_id": "e:%d" % index})
            event = proxy._quote_exec_events.get_nowait()
            self.assertEqual(event["event_type"], event_type)
            self.assertEqual(event["account_id"], "acct")

    def test_default_proxy_relay_publisher_forces_json_and_surfaces_errors(self):
        proxy, _redis, _replies = self.make_proxy(
            quote_push_relay=True,
            quote_pipe=FakeQuotePipe(),
        )

        self.assertEqual(proxy._quote_redis_channel.wire_encoding, "json")
        self.assertTrue(proxy._quote_redis_channel.raise_publish_errors)

    def test_execution_fallback_retries_redis_without_dropping_event(self):
        class FlakyRedis(FakeRedis):
            def __init__(self):
                super(FlakyRedis, self).__init__()
                self.failures_left = 1

            def eval(self, script, key_count, *args):
                if self.failures_left:
                    self.failures_left -= 1
                    raise RuntimeError("redis unavailable")
                return super(FlakyRedis, self).eval(script, key_count, *args)

        redis = FlakyRedis()
        quote_redis = FakeQuoteRedisChannel()
        proxy = RedisPipeProxy(
            redis,
            "acct",
            workers=1,
            max_pending=1,
            rpc_pipe=FakeRpcPipe(),
            event_pipe=FakeEventPipe(),
            redis_transport=FakeRedisTransport(),
            shutdown_grace_seconds=0,
            quote_push_relay=True,
            quote_pipe=FakeQuotePipe(),
            quote_redis_channel=quote_redis,
        )
        self.addCleanup(proxy.stop)
        proxy._running = True
        proxy._accepting = True
        proxy._start_quote_relay()

        proxy._handle_quote_push(
            "exec:cancel_error",
            {"event_id": "fallback:2", "error_msg": "refused"},
        )

        self.assertTrue(wait_for(lambda: len(redis.events) == 1, timeout=2.0))
        self.assertEqual(proxy._quote_exec_failures, 1)
        self.assertIn("bigqmt:cancel_error_events:acct", redis.events[0][0])

    def test_late_primary_and_fallback_delivery_are_deduplicated(self):
        proxy, redis, _replies = self.make_proxy(
            quote_push_relay=True,
            quote_pipe=FakeQuotePipe(),
            quote_redis_channel=FakeQuoteRedisChannel(),
        )
        proxy._start_quote_relay()
        primary = request("publish_exec_event")
        primary["request_id"] = "shared:event:1"
        primary["params"] = {
            "event": {"event_type": "order", "order_id": "O1"},
        }

        self.assertTrue(proxy._handle_event(primary)["ok"])
        proxy._handle_quote_push(
            "exec:order",
            {"event_id": "shared:event:1", "order_id": "O1"},
        )

        self.assertTrue(wait_for(lambda: proxy._quote_exec_events.empty()))
        time.sleep(0.05)
        self.assertEqual(len(redis.events), 1)

    def test_quote_push_relay_lifecycle_starts_and_stops_both_channels(self):
        quote_pipe = FakeQuotePipe()
        quote_redis = FakeQuoteRedisChannel()
        proxy, _redis, _replies = self.make_proxy(
            quote_push_relay=True,
            quote_pipe=quote_pipe,
            quote_redis_channel=quote_redis,
        )
        proxy._running = False
        proxy._accepting = False

        proxy.start()
        self.assertTrue(quote_redis.started)
        self.assertEqual(quote_pipe.topics, [])
        self.assertIsNotNone(quote_pipe.callback)

        proxy.stop()
        self.assertTrue(quote_pipe.stopped)
        self.assertTrue(quote_redis.stopped)


if __name__ == "__main__":
    unittest.main()


class QuoteRecoveryTest(unittest.TestCase):
    make_proxy = RedisPipeProxyTest.make_proxy

    def test_incremental_symbols_merge_and_single_side_replaces_entire_tick(self):
        proxy, _, _ = self.make_proxy(quote_push_relay=True, quote_pipe=FakeQuotePipe(),
                                     quote_redis_channel=FakeQuoteRedisChannel())
        proxy._handle_quote_push("T", {"A": {"bidPrice": [10], "askPrice": [11], "_trace": {"n": 1}}})
        proxy._handle_quote_push("T", {"B": {"lastPrice": 20}})
        proxy._handle_quote_push("T", {"A": {"bidPrice": [10], "askPrice": [], "_trace": {"n": 2}}})
        proxy._handle_quote_push("T", {})
        proxy._handle_quote_push("T", {"A": {}, "bad": [1]})
        proxy._start_quote_relay()
        self.assertTrue(wait_for(lambda: len(proxy._quote_redis_channel.published) == 1))
        batch = proxy._quote_redis_channel.published[0][1]
        self.assertEqual(set(batch), {"A", "B"})
        self.assertEqual(batch["A"]["askPrice"], [])
        self.assertEqual(batch["A"]["_trace"], {"n": 2})
        self.assertEqual(proxy._quote_diagnostics.counts["coalesced_symbols"], 1)

    def test_publish_failure_retries_without_overwriting_newer_symbols(self):
        channel = FakeQuoteRedisChannel()
        proxy, _, _ = self.make_proxy(quote_push_relay=True, quote_pipe=FakeQuotePipe(),
                                     quote_redis_channel=channel)
        calls = []
        def publish(topic, data):
            calls.append(data)
            if len(calls) == 1:
                proxy._handle_quote_push(topic, {"A": {"v": 2}, "C": {"v": 3}})
                raise OSError("Redis unavailable")
            if len(calls) == 2:
                raise OSError("still unavailable")
            channel.published.append((topic, data))
        channel.publish = publish
        proxy._handle_quote_push("T", {"A": {"v": 1}, "B": {"v": 1}})
        proxy._start_quote_relay()
        self.assertTrue(wait_for(lambda: len(channel.published) == 1, timeout=2.0))
        self.assertEqual(channel.published[0][1], {"A": {"v": 2}, "B": {"v": 1}, "C": {"v": 3}})
        self.assertEqual(proxy._quote_publish_failures, 2)
        self.assertEqual(proxy._quote_pending, {})

    def test_failed_topic_does_not_starve_other_topics_with_capacity_one(self):
        channel = FakeQuoteRedisChannel()
        proxy, _, _ = self.make_proxy(quote_push_relay=True, quote_queue_capacity=1,
                                     quote_pipe=FakeQuotePipe(), quote_redis_channel=channel)
        failed = []
        def publish(topic, data):
            if topic == "A" and not failed:
                failed.append(topic)
                raise OSError("one failure")
            channel.published.append((topic, data))
        channel.publish = publish
        for topic in ["A", "B", "C"]:
            proxy._handle_quote_push(topic, {topic: {"v": 1}})
        proxy._start_quote_relay()
        self.assertTrue(wait_for(lambda: len(channel.published) == 3))
        self.assertEqual([topic for topic, _ in channel.published], ["B", "C", "A"])

    def test_unencodable_symbol_does_not_block_healthy_symbols(self):
        from bigqmt_signal_trader.quote_push_channel import RedisQuotePushChannel, decode_push_payload
        class PublishingRedis:
            def __init__(self): self.published = []
            def publish(self, channel, payload):
                self.published.append(decode_push_payload(payload))
        redis = PublishingRedis()
        channel = RedisQuotePushChannel(redis, "acct", wire_encoding="json", raise_publish_errors=True)
        proxy, _, _ = self.make_proxy(quote_push_relay=True, quote_pipe=FakeQuotePipe(),
                                     quote_redis_channel=channel)
        circular = {}; circular["self"] = circular
        proxy._handle_quote_push("T", {"bad": circular, "good": {"v": 1}})
        proxy._start_quote_relay()
        self.assertTrue(wait_for(lambda: len(redis.published) == 1))
        self.assertEqual(redis.published[0]["data"], {"good": {"v": 1}})
        self.assertEqual(channel._quote_diagnostics.counts["encode_symbol"], 1)

    def test_concurrent_batches_keep_every_symbols_latest_tick(self):
        proxy, _, _ = self.make_proxy(quote_push_relay=True, quote_pipe=FakeQuotePipe(),
                                     quote_redis_channel=FakeQuoteRedisChannel())
        def send(code):
            for n in range(50):
                proxy._handle_quote_push("T", {code: {"v": n}})
        workers = [threading.Thread(target=send, args=(str(i),)) for i in range(12)]
        for worker in workers: worker.start()
        for worker in workers: worker.join()
        self.assertEqual(proxy._quote_pending["T"], {str(i): {"v": 49} for i in range(12)})
        self.assertEqual(proxy._quote_topics.qsize(), 1)

    def test_only_final_subscription_removal_clears_pending_topic(self):
        proxy, _, _ = self.make_proxy(quote_push_relay=True, quote_pipe=FakeQuotePipe(),
                                     quote_redis_channel=FakeQuoteRedisChannel())
        def track(method, client):
            proxy._track_quote_response({"method": method, "params": {"client_id": client, "sub_id": 1}},
                                        {"ok": True, "data": {"topic": "T"}})
        track("subscribe_whole_quote", "a")
        track("subscribe_whole_quote", "b")
        proxy._handle_quote_push("T", {"A": {"v": 1}})
        track("unsubscribe_whole_quote", "a")
        self.assertIn("T", proxy._quote_pending)
        track("unsubscribe_whole_quote", "b")
        self.assertNotIn("T", proxy._quote_pending)
        proxy._handle_quote_push("T", {"A": {"v": 2}})
        self.assertNotIn("T", proxy._quote_pending)
        track("subscribe_whole_quote", "a")
        proxy._handle_quote_push("T", {"A": {"v": 3}})
        self.assertEqual(proxy._quote_pending["T"], {"A": {"v": 3}})

    def test_shutdown_reports_unsent_latest_quotes(self):
        logs = []
        proxy, _, _ = self.make_proxy(quote_push_relay=True, quote_pipe=FakeQuotePipe(),
                                     quote_redis_channel=FakeQuoteRedisChannel(), log=logs.append)
        proxy._handle_quote_push("T", {"A": {"v": 1}, "B": {"v": 2}})
        proxy.stop()
        self.assertTrue(any("shutdown_unsent" in message and "symbols=2" in message for message in logs))


class QuoteShutdownRaceTest(unittest.TestCase):
    make_proxy = RedisPipeProxyTest.make_proxy

    def test_publish_failure_during_stop_is_counted_without_requeueing(self):
        channel = FakeQuoteRedisChannel()
        entered, release = threading.Event(), threading.Event()
        def publish(topic, data):
            entered.set()
            release.wait(1.0)
            raise OSError("failed during stop")
        channel.publish = publish
        logs = []
        proxy, _, _ = self.make_proxy(quote_push_relay=True, quote_pipe=FakeQuotePipe(),
                                     quote_redis_channel=channel, log=logs.append)
        proxy._handle_quote_push("T", {"A": {"v": 1}})
        proxy._start_quote_relay()
        self.assertTrue(entered.wait(1.0))
        stopper = threading.Thread(target=proxy.stop)
        stopper.start()
        self.assertTrue(proxy._quote_relay_stop.wait(1.0))
        release.set()
        stopper.join(2.0)
        self.assertFalse(stopper.is_alive())
        self.assertEqual(proxy._quote_pending, {})
        self.assertTrue(any("shutdown_unsent" in message and "symbols=1" in message for message in logs))
        proxy._handle_quote_push("T", {"A": {"v": 2}})
        self.assertEqual(proxy._quote_pending, {})

    def test_retired_topic_tombstones_expire_instead_of_accumulating(self):
        proxy, _, _ = self.make_proxy(quote_push_relay=True, quote_pipe=FakeQuotePipe(),
                                     quote_redis_channel=FakeQuoteRedisChannel())
        proxy._quote_retired_topics["old"] = time.monotonic() - 1
        with proxy._quote_pending_lock:
            proxy._prune_retired_quote_topics_locked()
        self.assertEqual(proxy._quote_retired_topics, {})
