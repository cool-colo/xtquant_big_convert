# coding: utf-8
"""#436 gaps 4 & 5: a late drain reply must not cross to a recycled
connection, and stop() must not leave the accept loop or stray instances
behind.

Gap 4's mechanism: drain mode tags a request with the RAW handle value; a
deferred answer writes back to that integer later. The client that asked may
have timed out and disconnected by then -- the handle is closed, the kernel
recycles the VALUE onto a new connection, and the stale reply lands on the
new client, which (pre-fix) never checked request_id.
"""
import os
import sys
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.transports.base import TransportError  # noqa: E402
from bigqmt_signal_trader.transports import pipe_transport  # noqa: E402
from bigqmt_signal_trader.transports.pipe_transport import (  # noqa: E402
    NamedPipeTransport,
)

WINDOWS_ONLY = unittest.skipUnless(os.name == "nt", "命名管道是 Windows 专有")


def _transport(name="drain_reply_t"):
    return NamedPipeTransport(account_id="t", pipe_name=name)


class StaleDrainReplyDroppedTest(unittest.TestCase):
    """send_response must not write to a recycled handle value."""

    def _drain_transport(self):
        t = _transport("stale_drain_t")
        t._background_threads = False
        return t

    def test_reply_dropped_when_the_token_is_gone(self):
        t = self._drain_transport()
        with t._server_lock:
            t._drain_handles.append(100)
            t._drain_tokens[100] = object()
        with t._server_lock:
            token = t._drain_tokens[100]
        request = {"request_id": "r1", "method": "get_positions",
                   "_pipe_handle": 100, "_pipe_token": token}
        # The connection dies before the deferred answer.
        t._forget_drain_handle(100)
        # ... and the VALUE is recycled onto a brand-new connection.
        with t._server_lock:
            t._drain_handles.append(100)
            t._drain_tokens[100] = object()
        with mock.patch.object(t, "_write") as write:
            t.send_response(request, {"request_id": "r1", "ok": True})
            write.assert_not_called()      # stale token: dropped, not delivered

    def test_live_token_still_writes(self):
        t = self._drain_transport()
        with t._server_lock:
            t._drain_handles.append(101)
            t._drain_tokens[101] = object()
        with t._server_lock:
            token = t._drain_tokens[101]
        request = {"request_id": "r2", "_pipe_handle": 101, "_pipe_token": token}
        with mock.patch.object(t, "_write") as write:
            t.send_response(request, {"request_id": "r2", "ok": True})
            self.assertEqual(write.call_count, 1)


class ClientRequestIdCheckTest(unittest.TestCase):
    """send_request must reject a reply that belongs to another request."""

    def _call(self, response_payload):
        t = _transport("client_rid_t")
        raw = pipe_transport.json.dumps(response_payload).encode("utf-8")
        with mock.patch.object(t, "_client_handle", return_value=999):
            with mock.patch.object(t, "_write"):
                with mock.patch.object(t, "_read_with_watchdog", return_value=raw):
                    with mock.patch.object(t, "_drop_client_handle") as drop:
                        t.send_request(
                            {"request_id": "mine", "method": "ping"}, 1.0)
                        drop.assert_called()

    def test_crossed_reply_raises_and_drops_the_handle(self):
        with self.assertRaises(TransportError) as caught:
            self._call({"request_id": "someone-elses", "ok": True})
        self.assertIn("someone-elses", str(caught.exception))

    def test_matching_reply_passes_through(self):
        t = _transport("client_rid_t")
        raw = pipe_transport.json.dumps(
            {"request_id": "mine", "ok": True}).encode("utf-8")
        with mock.patch.object(t, "_client_handle", return_value=999):
            with mock.patch.object(t, "_write"):
                with mock.patch.object(t, "_read_with_watchdog", return_value=raw):
                    out = t.send_request({"request_id": "mine"}, 1.0)
        self.assertEqual(out["request_id"], "mine")

    def test_reply_without_request_id_passes_through(self):
        t = _transport("client_rid_t")
        raw = pipe_transport.json.dumps({"ok": True}).encode("utf-8")
        with mock.patch.object(t, "_client_handle", return_value=999):
            with mock.patch.object(t, "_write"):
                with mock.patch.object(t, "_read_with_watchdog", return_value=raw):
                    out = t.send_request({"request_id": "mine"}, 1.0)
        self.assertTrue(out["ok"])


@WINDOWS_ONLY
class StopJoinsAcceptLoopTest(unittest.TestCase):
    def test_stop_waits_for_the_accept_thread(self):
        t = _transport("stop_join_t")
        t.start_receiving(lambda request: None)
        time.sleep(0.3)
        listener = t._listener
        self.assertTrue(listener.is_alive())
        t.stop()
        self.assertFalse(listener.is_alive())

    def test_no_instance_outlives_stop(self):
        t = _transport("stop_sweep_t")
        t.start_receiving(lambda request: None)
        time.sleep(0.3)
        t.stop()
        with t._server_lock:
            self.assertEqual(t._server_handles, [])
        self.assertEqual(t._drain_handles, [])
        self.assertEqual(t._drain_tokens, {})

    def test_rerun_stops_the_previous_server_on_the_same_path(self):
        first = _transport("rerun_same_t")
        first.start_receiving(lambda request: None)
        time.sleep(0.3)
        second = NamedPipeTransport(account_id="t", pipe_name="rerun_same_t")
        second.start_receiving(lambda request: None)
        time.sleep(0.3)
        try:
            self.assertFalse(first._running)
            self.assertFalse(first._listener.is_alive())
            self.assertTrue(second._running)
            with pipe_transport._SERVERS_LOCK:
                self.assertIs(
                    pipe_transport._SERVERS_BY_PATH.get(second.path), second)
        finally:
            second.stop()


if __name__ == "__main__":
    unittest.main()
