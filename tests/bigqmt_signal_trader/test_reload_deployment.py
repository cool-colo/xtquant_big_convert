# coding: utf-8
"""Reload the deployed package without restarting the strategy.

QMT keeps modules in sys.modules across strategy re-runs, so a deploy does
nothing until the strategy is restarted -- five restarts in one afternoon of
fixing #133. importlib.reload is available on QMT's Python 3.6 and this repo
already uses it, but it cannot replace a restart on its own for two structural
reasons:

  * the existing reload site is inside _build_rpc_service, which only runs from
    init(), which only runs on a restart -- chicken and egg;
  * reload rebinds names in the reloaded module, while objects already
    constructed keep their old classes. The live handlers, order gateway and
    transport threads were all built from the old ones.

So the reload here purges rather than reloads (no dependency ordering to get
wrong -- order_bigqmt does `from ..models import OrderSnapshot` at import time,
so reloading models after it would leave the old class bound), re-points the
references THIS module bound at import time, and re-runs init() to rebuild the
object graph.

Two things it cannot do, stated rather than papered over:

  * bigqmt_signal_trader_strategy.py and the BIGQMT_REDIS_DRYRUN entry. QMT
    execs those; a module cannot reload the one it is running in.
  * happen inside the RPC handler. Performing it calls reset_app(), which stops
    the service answering the request, so the reply has to go out first. It is
    scheduled onto the adjust tick instead.
"""

import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.redis_rpc import (
    LISTENER_DEFERRED_METHODS,
    READ_METHODS,
    BigQmtRpcHandlers,
)

import bigqmt_signal_trader_strategy as strategy


class SchedulingTest(unittest.TestCase):
    def setUp(self):
        strategy._reload_request.update({"pending": False, "requested_at": 0.0,
                                         "by": ""})
        strategy._reload_result = {}

    def tearDown(self):
        strategy._reload_request.update({"pending": False, "requested_at": 0.0,
                                         "by": ""})
        strategy._reload_result = {}

    def test_requesting_marks_it_pending(self):
        strategy.request_reload("because")

        self.assertTrue(strategy._reload_request["pending"])
        self.assertEqual(strategy._reload_request["by"], "because")

    def test_it_reports_scheduled_not_done(self):
        """Answering "reloaded" before it happened is the lie to avoid."""
        answer = strategy.request_reload()

        self.assertTrue(answer["scheduled"])
        self.assertIn("next adjust tick", answer["note"])

    def test_it_reports_the_version_it_is_leaving(self):
        answer = strategy.request_reload()

        self.assertEqual(answer["version_before"], strategy._package_version())

    def test_status_says_pending_until_the_tick_runs(self):
        strategy.request_reload()

        self.assertTrue(strategy.reload_status()["pending"])

    def test_status_is_quiet_when_nothing_was_asked(self):
        self.assertFalse(strategy.reload_status()["pending"])


class RestoresImports(object):
    """Put sys.modules back exactly as it was.

    Anything that exercises the purge has to, and it is easy to miss which
    tests those are: _perform_reload purges in the middle, so stubbing out
    reset_app and init does NOT stop it. The first version of this file missed
    that, and the suite went from green to seven failures in test_transports.py
    and test_xtquant_compat.py -- modules that had already imported the package
    and held references to what these tests threw away. A test that clears a
    shared import cache breaks whatever runs after it.
    """

    def _snapshot_imports(self):
        self._imports = dict(
            (name, module) for name, module in sys.modules.items()
            if name == "bigqmt_signal_trader"
            or name.startswith("bigqmt_signal_trader."))

    def _restore_imports(self):
        for name in list(sys.modules):
            if name == "bigqmt_signal_trader" or name.startswith("bigqmt_signal_trader."):
                sys.modules.pop(name, None)
        sys.modules.update(self._imports)
        strategy._rebind_module_level_imports()


class PurgeTest(RestoresImports, unittest.TestCase):
    """Purging rather than reloading: there is no order to get wrong."""

    def setUp(self):
        self._snapshot_imports()

    def tearDown(self):
        self._restore_imports()

    def test_it_drops_the_package_and_its_submodules(self):
        import bigqmt_signal_trader.models          # noqa: F401
        import bigqmt_signal_trader.code_utils      # noqa: F401

        purged = strategy._purge_package_modules()

        self.assertIn("bigqmt_signal_trader.models", purged)
        self.assertIn("bigqmt_signal_trader.code_utils", purged)
        self.assertNotIn("bigqmt_signal_trader.models", sys.modules)

    def test_it_leaves_everything_else_alone(self):
        import json                                  # noqa: F401

        strategy._purge_package_modules()

        self.assertIn("json", sys.modules)
        self.assertIn("bigqmt_signal_trader_strategy", sys.modules)

    def test_the_strategy_module_itself_is_never_purged(self):
        """QMT execs it; a module cannot reload the one it is running in."""
        purged = strategy._purge_package_modules()

        self.assertNotIn("bigqmt_signal_trader_strategy", purged)

    def test_rebinding_restores_what_the_purge_broke(self):
        """Without this the reload looks like it worked and changes nothing."""
        strategy._purge_package_modules()

        strategy._rebind_module_level_imports()

        self.assertTrue(callable(strategy._default_build_app))
        self.assertTrue(callable(strategy.init_app))
        self.assertTrue(callable(strategy.tick_app))
        self.assertTrue(callable(strategy.forward_order_event))
        self.assertIsNotNone(strategy.BigQmtRuntimeAdapter)

    def test_rebinding_picks_up_the_fresh_modules(self):
        strategy._purge_package_modules()
        strategy._rebind_module_level_imports()

        self.assertIn("bigqmt_signal_trader.runner", sys.modules)
        self.assertIs(strategy.init_app,
                      sys.modules["bigqmt_signal_trader.runner"].init_app)


class RpcSurfaceTest(unittest.TestCase):
    def test_both_methods_are_callable_over_rpc(self):
        self.assertIn("reload_deployment", READ_METHODS)
        self.assertIn("reload_status", READ_METHODS)

    def test_the_reload_runs_on_the_main_thread(self):
        """It rebuilds the object graph the adjust thread is using."""
        self.assertIn("reload_deployment", LISTENER_DEFERRED_METHODS)

    def _handlers(self, **attributes):
        handlers = BigQmtRpcHandlers.__new__(BigQmtRpcHandlers)
        for name, value in attributes.items():
            setattr(handlers, name, value)
        return handlers

    def test_the_handler_forwards_the_reason(self):
        seen = []
        handlers = self._handlers(reload_hook=lambda reason: seen.append(reason))

        handlers._handle_reload_deployment({"reason": "issue-133 fix"})

        self.assertEqual(seen, ["issue-133 fix"])

    def test_an_older_deployment_says_restart_once(self):
        """The first reload always costs a restart; after that none do."""
        with self.assertRaises(RuntimeError) as caught:
            self._handlers()._handle_reload_deployment({})

        self.assertIn("restart the strategy once", str(caught.exception))

    def test_status_forwards_too(self):
        handlers = self._handlers(reload_status_hook=lambda: {"ok": True})

        self.assertEqual(handlers._handle_reload_status({}), {"ok": True})


class AdjustHookTest(unittest.TestCase):
    """The reload runs after the drain, and never on the scheduling tick.

    Getting this backwards is not a small bug. The drain is what flushes queued
    RPC responses, and reset_app() tears the transport down with anything still
    in it. Reloading first destroyed the reply to reload_deployment itself: the
    terminal logged "responded method=reload_deployment ok=True" and
    "[bigqmt_reload] ok purged=28 0.3.7 -> 0.3.7 in 0.99s" -- the reload
    genuinely worked -- while the client sat there until it timed out, with no
    way to tell that from a reload that had killed the bridge.
    """

    def setUp(self):
        import inspect

        self.source = inspect.getsource(strategy.adjust)

    def test_the_drain_comes_first(self):
        drain = self.source.index('_adjust_phase("drain"')
        reload_at = self.source.index('_adjust_phase("reload"')

        self.assertLess(drain, reload_at)

    def test_it_waits_for_the_grace_period(self):
        self.assertIn('time.time() >= _reload_request["not_before"]', self.source)

    def test_the_tick_returns_rather_than_running_on_rebuilt_state(self):
        after = self.source.split(
            '_adjust_phase("reload", _perform_reload, ContextInfo)', 1)[1]

        self.assertEqual(after.splitlines()[1].strip(), "return None")

    def test_scheduling_sets_a_deadline_in_the_future(self):
        import time as _time

        strategy._reload_request.update({"pending": False, "not_before": 0.0})
        try:
            before = _time.time()

            strategy.request_reload()

            self.assertGreater(strategy._reload_request["not_before"], before)
        finally:
            strategy._reload_request.update({"pending": False, "requested_at": 0.0,
                                             "not_before": 0.0, "by": ""})

    def test_the_grace_period_is_a_few_ticks_not_a_long_wait(self):
        """Ticks are ~100ms; this should be a handful of them."""
        self.assertGreaterEqual(strategy._RELOAD_GRACE_SECONDS, 0.2)
        self.assertLessEqual(strategy._RELOAD_GRACE_SECONDS, 3.0)


class OutcomeTest(RestoresImports, unittest.TestCase):
    """A failed reload must be loud: the bridge is then running on nothing."""

    def setUp(self):
        self._snapshot_imports()
        self.real_init = strategy.init
        self.real_reset = strategy.reset_app
        strategy._reload_result = {}
        strategy._reload_request.update({"pending": True, "requested_at": 0.0,
                                         "by": "test"})

    def tearDown(self):
        strategy.init = self.real_init
        strategy.reset_app = self.real_reset
        strategy._reload_result = {}
        strategy._reload_request.update({"pending": False, "requested_at": 0.0,
                                         "by": ""})
        self._restore_imports()

    def test_a_failure_is_recorded_rather_than_raised(self):
        """Raising here would propagate into adjust, and QMT stops a strategy
        whose callback raises."""
        strategy.reset_app = lambda: None
        strategy.init = lambda ctx: (_ for _ in ()).throw(RuntimeError("boom"))

        result = strategy._perform_reload(None)

        self.assertFalse(result["ok"])
        self.assertIn("boom", result["error"])

    def test_a_failure_clears_pending_so_it_does_not_loop_every_tick(self):
        strategy.reset_app = lambda: None
        strategy.init = lambda ctx: (_ for _ in ()).throw(RuntimeError("boom"))

        strategy._perform_reload(None)

        self.assertFalse(strategy._reload_request["pending"])

    def test_the_outcome_is_readable_afterwards(self):
        strategy.reset_app = lambda: None
        strategy.init = lambda ctx: None

        strategy._perform_reload(None)

        status = strategy.reload_status()
        self.assertTrue(status["ok"])
        self.assertFalse(status["pending"])
        self.assertGreaterEqual(status["seconds"], 0.0)

    def test_a_success_reports_both_versions(self):
        strategy.reset_app = lambda: None
        strategy.init = lambda ctx: None

        result = strategy._perform_reload(None)

        self.assertEqual(result["version_before"], result["version_after"])
        self.assertTrue(result["version_after"])


class FlushBeforeTeardownTest(unittest.TestCase):
    """The reply has to be on the wire before reset_app() takes the socket.

    Two live attempts both showed the terminal logging "responded
    method=reload_deployment ok=True" and "[bigqmt_reload] ok purged=28" while
    the client raised TransportTimeout. The reply was sitting on the ZMQ
    transport's response queue: the ROUTER thread sends it at the TOP of its
    next loop, and that loop blocks in recv_multipart for up to RCVTIMEO (1s)
    and needs the GIL the adjust thread is holding.

    From the client, "reloaded fine but the reply was lost" and "the reload
    killed the bridge" look identical. That is the failure to design out.
    """

    class Transport(object):
        def __init__(self, sizes):
            self._response_queue = self.Queue(sizes)

        class Queue(object):
            def __init__(self, sizes):
                self.sizes = list(sizes)

            def qsize(self):
                return self.sizes.pop(0) if self.sizes else 0

    class Service(object):
        def __init__(self, transport):
            self._transport = transport

    def setUp(self):
        self.real_service = strategy._rpc_service

    def tearDown(self):
        strategy._rpc_service = self.real_service

    def _with_queue(self, sizes):
        strategy._rpc_service = self.Service(self.Transport(sizes))

    def test_it_waits_until_the_queue_empties(self):
        self._with_queue([2, 1, 0])

        self.assertTrue(strategy._wait_for_responses_to_flush(timeout_seconds=3.0))

    def test_an_empty_queue_returns_at_once(self):
        self._with_queue([0])

        self.assertTrue(strategy._wait_for_responses_to_flush(timeout_seconds=3.0))

    def test_a_queue_that_never_drains_gives_up_rather_than_hanging(self):
        """Blocking the adjust thread forever would be worse than a lost
        reply."""
        strategy._rpc_service = self.Service(self.Transport([]))
        strategy._rpc_service._transport._response_queue.qsize = lambda: 3

        self.assertFalse(strategy._wait_for_responses_to_flush(timeout_seconds=0.3))

    def test_no_transport_is_not_an_error(self):
        """redis deployments have no response queue of their own."""
        strategy._rpc_service = None

        self.assertEqual(strategy._pending_response_count(), 0)
        self.assertTrue(strategy._wait_for_responses_to_flush(timeout_seconds=1.0))

    def test_a_transport_without_a_queue_is_not_an_error(self):
        strategy._rpc_service = self.Service(object())

        self.assertEqual(strategy._pending_response_count(), 0)

    def test_a_raising_qsize_is_not_an_error(self):
        strategy._rpc_service = self.Service(self.Transport([]))

        def angry():
            raise RuntimeError("closed")

        strategy._rpc_service._transport._response_queue.qsize = angry

        self.assertEqual(strategy._pending_response_count(), 0)

    def test_the_reload_flushes_before_it_resets(self):
        import inspect

        source = inspect.getsource(strategy._perform_reload)
        flush = source.index("_wait_for_responses_to_flush")
        reset = source.index("reset_app()")

        self.assertLess(flush, reset)

    def test_the_grace_period_outlasts_the_router_recv_timeout(self):
        """RCVTIMEO is 1s; a shorter grace can beat the thread to the send."""
        self.assertGreater(strategy._RELOAD_GRACE_SECONDS, 1.0)


if __name__ == "__main__":
    unittest.main()
