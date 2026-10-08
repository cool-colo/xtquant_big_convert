# coding: utf-8
"""A finished reply must not wait for the router's receive timeout (#104).

Handlers for trade queries run on the adjust thread, because
get_trade_detail_data returns empty anywhere else. send_response therefore sees
a thread that is not the router thread and queues the reply -- and the queue is
only drained at the top of the router loop, after a recv that blocks up to
RCVTIMEO (1s).

Measured on the live bridge before this change:

    get_positions   wait 201ms | handle 1.0ms | return 1300ms | total 1500ms
    reply residency 1149ms average over 10 calls

So the handler cost 1ms and the reply then sat for more than a second. ping did
not show it at all -- ping is handled on the router thread itself and its reply
goes straight out -- which is why an earlier ping-only experiment made this look
refuted.

The fix signals an inproc wake pipe when a reply is queued, and the router loop
waits on the router socket and that pipe together. After it:

    get_positions   1500ms -> 605ms      reply residency 1149ms -> 117ms

The trade-off is real and was accepted deliberately: ping went 297ms -> 404ms,
because the loop now does poll + non-blocking recv instead of one blocking recv,
and that extra work lands on the thread ping is answered from.
"""

import os
import sys
import threading
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.transports import zmq_transport as zt


def _transport():
    return zt.ZmqTransport(account_id="8886800503",
                           bind_address="tcp://127.0.0.1:15999")


class SignalIsSafeTest(unittest.TestCase):
    """A missed nudge costs latency; a raised one costs the bridge."""

    def test_signalling_without_a_pipe_is_a_no_op(self):
        transport = _transport()
        transport._wake_recv = None

        transport._signal_wake()          # must not raise

    def test_a_broken_pipe_is_swallowed(self):
        transport = _transport()

        class Angry(object):
            def send(self, *a, **k):
                raise RuntimeError("socket is gone")

        transport._wake_recv = object()
        transport._wake_send = Angry()

        transport._signal_wake()          # must not raise

    def test_the_endpoint_is_per_instance(self):
        """Two bridges in one process must not fight over one inproc name."""
        a, b = _transport(), _transport()

        self.assertNotEqual(a._wake_endpoint, b._wake_endpoint)
        self.assertTrue(a._wake_endpoint.startswith("inproc://"))


class QueueingSignalsTest(unittest.TestCase):
    def test_queueing_a_reply_nudges_the_loop(self):
        """Without this the reply waits out RCVTIMEO -- the whole bug."""
        transport = _transport()
        transport._wake_recv = None
        nudges = []
        transport._signal_wake = lambda: nudges.append(1)

        transport._queue_response(b"peer", b"payload", reason="off-thread")

        self.assertEqual(len(nudges), 1)

    def test_the_reply_is_still_queued(self):
        transport = _transport()
        transport._wake_recv = None
        transport._signal_wake = lambda: None

        transport._queue_response(b"peer", b"payload", reason="off-thread")

        self.assertEqual(transport._response_queue.qsize(), 1)

    def test_the_queued_entry_carries_a_timestamp(self):
        """Residency is what made this measurable rather than arguable."""
        transport = _transport()
        transport._wake_recv = None
        transport._signal_wake = lambda: None

        transport._queue_response(b"peer", b"payload", reason="off-thread")
        identity, payload, queued_at = transport._response_queue.get_nowait()

        self.assertEqual(identity, b"peer")
        self.assertEqual(payload, b"payload")
        self.assertIsInstance(queued_at, float)


class ResidencyStatsTest(unittest.TestCase):
    def test_it_starts_empty(self):
        stats = _transport().reply_residency_stats()

        self.assertEqual(stats["count"], 0)
        self.assertEqual(stats["avg_ms"], 0.0)

    def test_it_averages_what_it_was_told(self):
        transport = _transport()

        transport._note_reply_residency(100.0)
        transport._note_reply_residency(300.0)
        stats = transport.reply_residency_stats()

        self.assertEqual(stats["count"], 2)
        self.assertEqual(stats["avg_ms"], 200.0)
        self.assertEqual(stats["max_ms"], 300.0)

    def test_draining_records_residency(self):
        transport = _transport()
        transport._wake_recv = None
        transport._signal_wake = lambda: None
        sent = []

        class Router(object):
            def send_multipart(self, frames):
                sent.append(frames)

        transport._router = Router()
        transport._queue_response(b"peer", b"payload", reason="off-thread")

        transport._drain_response_queue()

        self.assertEqual(len(sent), 1)
        self.assertEqual(transport.reply_residency_stats()["count"], 1)


class LoopStillWorksWithoutAPipeTest(unittest.TestCase):
    """If the inproc pipe cannot be opened the loop must still serve requests,
    just at the old latency -- degrading, never failing."""

    def test_open_wake_pipe_tolerates_failure(self):
        transport = _transport()

        def boom():
            raise RuntimeError("no inproc today")

        transport._ensure_zmq = boom
        try:
            transport._open_wake_pipe()
        except Exception as exc:
            self.fail("must not propagate: %r" % (exc,))

        self.assertIsNone(transport._wake_recv)


if __name__ == "__main__":
    unittest.main()
