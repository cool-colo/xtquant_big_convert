# coding: utf-8
"""The client must learn a backtest ended, not time out on it (#150).

Reported as:

    TimeoutError: backtest ZMQ request timed out: next_bar

raised at the END of a run -- "有没有办法知道回测已经结束了，让它停下来".

There is already a way: state carries ``done``, and BacktestStrategy.run()
breaks on it and then calls finish(). The signal never arrives because of the
order in the entry QMT actually calls:

    def stop(ContextInfo=None):
        _RUNTIME.on_qmt_stop()      # sets qmt_completed, wakes the waiter
        _RUNTIME.stop_server()      # ... and tears the socket down

The client is parked inside next_bar at that moment. on_qmt_stop wakes it, but
the reply still has to travel back over ZMQ, and stop_server is already closing
the socket. Even winning that race only moves the failure one call along:
run() calls finish() next, and by then the server is gone.

stop_server itself is not the mistake -- it is the fix for #109, where the port
stayed bound to a strategy that no longer existed and the next run could not
bind. So the shutdown has to do both: hand the client its ending, then release
the port. Same shape as reload_deployment waiting for the response queue to
drain before reset_app.

Why the existing coverage missed it: tests/bigqmt_backtest/test_zmq_bridge.py
drives ``session.on_qmt_stop()`` directly and never calls the module-level
stop(), so the teardown this issue is about was not in the test at all.

On which of these actually fail without the fix: only
test_next_bar_sent_after_the_run_ended_answers_done is deterministic, and it is
the one that reproduces the reported traceback (a client between calls when the
run ends sends into a socket that is already gone). The two where the client is
parked in next_bar are races -- in-process on loopback the reply usually beats
the teardown, so they pass either way and stand as non-regression guards.
"""

import os
import socket
import sys
import threading
import time
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_backtest import qmt_runtime
from bigqmt_backtest.client import BacktestZmqClient


def _free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class Context(object):
    """Minimal backtest ContextInfo: one symbol, one bar per barpos."""

    do_back_test = True
    stockcode = "600000"
    market = "SH"
    period = "1m"

    def __init__(self, barpos, close):
        self.barpos = barpos
        self.close_value = close

    def set_account(self, account_id):
        self.account_id = account_id

    def get_bar_timetag(self, barpos):
        return int((1704072600 + barpos * 60) * 1000)

    def get_history_data(self, count, period, field):
        values = {
            "open": self.close_value,
            "high": self.close_value + 0.1,
            "low": self.close_value - 0.1,
            "close": self.close_value,
            "volume": 10000,
            "amount": self.close_value * 10000,
            "preClose": self.close_value - 0.1,
        }
        return {"600000.SH": [values[field]]}


class _Runtime(unittest.TestCase):
    """Builds the runtime the way qmt_runtime.init does, on a free port."""

    def setUp(self):
        self.endpoint = "tcp://127.0.0.1:%d" % _free_port()
        qmt_runtime.reset_runtime()
        qmt_runtime._RUNTIME = qmt_runtime.QmtBacktestBridgeRuntime(
            config={
                "run_id": "stop-handshake",
                "account_id": "backtest-account",
                "bar_wait_timeout_seconds": 3,
                "bind_endpoint": self.endpoint,
            },
            qmt_api={},
        )
        self.runtime = qmt_runtime._RUNTIME
        self.runtime.start(Context(0, 10.0))
        self.addCleanup(qmt_runtime.reset_runtime)

    def _client(self, timeout=3.0):
        client = BacktestZmqClient(self.endpoint, run_id="", timeout_seconds=timeout)
        self.addCleanup(client.close)
        return client

    def _qmt_bar_thread(self, barpos=0, close=10.0):
        """One QMT bar, on its own thread.

        on_bar blocks inside QMT until the external strategy releases that
        index, exactly as it does in the terminal -- so this cannot be called
        inline, and the client must be the thing that lets it return.
        """
        self.errors = []

        def run():
            try:
                self.runtime.on_bar(Context(barpos, close))
            except Exception as exc:      # a stop while parked; see below
                self.errors.append("%s: %s" % (exc.__class__.__name__, exc))

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread

    def _client_on_first_bar(self):
        """A client attached and holding QMT's first bar, ready for next_bar."""
        bar = self._qmt_bar_thread()
        client = self._client()
        client.describe()
        state = client.start()
        self.assertEqual(state["frame_index"], 0)
        return client, bar


class ClientLearnsTheRunEndedTest(_Runtime):
    def test_a_client_parked_in_next_bar_is_told_done_instead_of_timing_out(self):
        """The reported failure, end to end."""
        client, bar = self._client_on_first_bar()
        answers = {}

        def ask():
            try:
                answers["state"] = client.next_bar()
            except Exception as exc:
                answers["error"] = "%s: %s" % (exc.__class__.__name__, exc)

        asking = threading.Thread(target=ask, daemon=True)
        asking.start()
        bar.join(timeout=3.0)    # next_bar released index 0; QMT has no more
        time.sleep(0.2)          # the client is now parked waiting for index 1
        qmt_runtime.stop()       # what QMT calls when the backtest ends
        asking.join(timeout=8.0)

        self.assertNotIn("error", answers, answers.get("error"))
        self.assertTrue(answers["state"]["done"],
                        "state must say the run ended: %r" % (answers["state"],))

    def test_next_bar_sent_after_the_run_ended_answers_done(self):
        """The deterministic half of the report, and the one that names
        next_bar in the traceback.

        A client between calls when QMT ends the backtest sends its next_bar
        into a socket that is already gone -- no race to win, nothing answers,
        and the client raises `backtest ZMQ request timed out: next_bar`.
        With the handshake the server is still up and answers done immediately
        (next_bar returns state straight away once qmt_completed).
        """
        client, bar = self._client_on_first_bar()
        stopping = threading.Thread(target=qmt_runtime.stop, daemon=True)
        stopping.start()
        time.sleep(0.5)          # stop() has run; the client was not waiting

        state = client.next_bar()
        bar.join(timeout=3.0)

        self.assertTrue(state["done"], state)
        client.finish()
        stopping.join(timeout=8.0)

    def test_finish_still_answers_after_the_run_ended(self):
        """run() calls finish() right after it breaks on done, so the server
        must survive long enough to answer that too."""
        client, bar = self._client_on_first_bar()
        stopping = threading.Thread(target=qmt_runtime.stop, daemon=True)

        def stop_when_parked():
            time.sleep(0.5)
            stopping.start()

        threading.Thread(target=stop_when_parked, daemon=True).start()
        state = client.next_bar()
        bar.join(timeout=3.0)

        self.assertTrue(state["done"], state)
        result = client.finish()
        stopping.join(timeout=8.0)

        self.assertEqual(result["result_owner"], "QMT")
        self.assertTrue(result["qmt_completed"])

    def test_the_port_is_released_once_the_client_has_its_result(self):
        """#109's constraint: a finished run must not keep the port bound."""
        client, bar = self._client_on_first_bar()
        stopping = threading.Thread(target=qmt_runtime.stop, daemon=True)
        threading.Thread(
            target=lambda: (time.sleep(0.5), stopping.start()), daemon=True).start()

        client.next_bar()
        client.finish()
        bar.join(timeout=3.0)
        stopping.join(timeout=8.0)

        self.assertIsNone(self.runtime.server_thread)
        probe = socket.socket()
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", int(self.endpoint.rsplit(":", 1)[1])))
        finally:
            probe.close()

    def test_a_stop_with_no_client_attached_does_not_hang(self):
        """Someone hits QMT's stop button with nothing connected: the grace
        must not apply, or every manual stop costs the full timeout."""
        started = time.time()
        qmt_runtime.stop()

        self.assertLess(time.time() - started, 2.0)
        self.assertIsNone(self.runtime.server_thread)

    def test_a_client_that_never_collects_cannot_hold_the_port_forever(self):
        """The grace is bounded -- a dead client must not strand the port.

        The QMT bar thread is left parked in on_bar here (nothing released
        index 0), so it raises its own bar_wait timeout into self.errors. That
        is this fixture, not the behaviour under test: in the terminal QMT is
        not inside on_bar once the backtest has ended.
        """
        client, bar = self._client_on_first_bar()
        client.close()           # walks away without next_bar or finish

        self.runtime.stop_grace_seconds = 1.0
        started = time.time()
        qmt_runtime.stop()
        elapsed = time.time() - started

        self.assertGreaterEqual(elapsed, 0.5, "it should wait a little")
        self.assertLess(elapsed, 5.0, "but not forever: %.1fs" % elapsed)
        self.assertIsNone(self.runtime.server_thread)

    def test_the_grace_is_configurable(self):
        self.assertEqual(qmt_runtime._STOP_GRACE_SECONDS,
                         self.runtime.stop_grace_seconds)
        runtime = qmt_runtime.QmtBacktestBridgeRuntime(
            config={"run_id": "grace", "stop_grace_seconds": 2.5,
                    "bind_endpoint": "tcp://127.0.0.1:%d" % _free_port()},
            qmt_api={})

        self.assertEqual(runtime.stop_grace_seconds, 2.5)


if __name__ == "__main__":
    unittest.main()
