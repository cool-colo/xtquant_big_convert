# coding: utf-8
import os
import sys
import time
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.exec_event_pipe import ExecEventQueueFull
from bigqmt_signal_trader.exec_event_pipe import NamedPipeExecEventSink


class FakeTransport(object):
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.requests = []
        self.stopped = False

    def send_request(self, request, timeout_seconds):
        self.requests.append((request, timeout_seconds))
        response = self.responses.pop(0) if self.responses else True
        if isinstance(response, Exception):
            raise response
        return {
            "request_id": request["request_id"],
            "account_id": request["account_id"],
            "method": request["method"],
            "ok": bool(response),
            "event_error": "" if response else "EVENT_REDIS_ERROR",
        }

    def stop(self):
        self.stopped = True


def wait_for(predicate, timeout=1.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class ExecEventPipeSinkTest(unittest.TestCase):
    def test_publish_is_queued_and_preserves_event_identity_on_retry(self):
        transport = FakeTransport([RuntimeError("lost ack"), True])
        sink = NamedPipeExecEventSink(
            "acct", transport=transport, retry_interval_seconds=0.01,
            instance_id="instance",
        ).start()
        self.addCleanup(sink.stop)

        self.assertEqual(sink.publish("exec:order", {"event_type": "order"}), 1)
        self.assertTrue(wait_for(lambda: sink.status()["published"] == 1))

        self.assertEqual(len(transport.requests), 2)
        first = transport.requests[0][0]
        second = transport.requests[1][0]
        self.assertEqual(first["request_id"], "instance:1")
        self.assertEqual(second["request_id"], first["request_id"])
        self.assertEqual(first["params"]["event"]["event_id"], first["request_id"])

    def test_full_queue_rejects_newest_without_blocking(self):
        sink = NamedPipeExecEventSink("acct", transport=FakeTransport(), queue_capacity=1)
        sink.publish("exec:order", {"event_type": "order", "value": 1})

        with self.assertRaises(ExecEventQueueFull):
            sink.publish("exec:trade", {"event_type": "trade", "value": 2})

        self.assertEqual(sink.status()["overflow"], 1)

    def test_oversized_event_is_rejected_before_enqueue(self):
        sink = NamedPipeExecEventSink(
            "acct", transport=FakeTransport(), max_event_bytes=100,
        )
        with self.assertRaises(ValueError):
            sink.publish("exec:order", {"event_type": "order", "text": "x" * 200})


if __name__ == "__main__":
    unittest.main()
