# coding: utf-8
"""The runtime forwards rpc.background_threads only when the local config
named rpc_background_threads explicitly.

Why it has to work this way: on non-redis transports the strategy side treats
an ABSENT key as "historical default" (background receiver threads on) and an
explicit False as the opt-in to the adjust-driven drain that drops the
cross-thread GIL handoffs from the round trip (#104). If the runtime emitted
its default False unconditionally, every zmq deployment would silently flip
into drain mode on restart -- the mode change must be opt-in.
"""
import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

import bigqmt_signal_trader_redis_rpc_runtime as runtime


class ForwardingTest(unittest.TestCase):
    def setUp(self):
        self._saved = {
            "RPC_BACKGROUND_THREADS": runtime.RPC_BACKGROUND_THREADS,
            "RPC_BACKGROUND_THREADS_EXPLICIT": runtime.RPC_BACKGROUND_THREADS_EXPLICIT,
            "configure": runtime.configure,
            "set_account_id": runtime.set_account_id,
        }
        self._captured = {}
        runtime.configure = lambda **kwargs: self._captured.update(kwargs)
        runtime.set_account_id = lambda account_id: None

    def tearDown(self):
        for key, value in self._saved.items():
            setattr(runtime, key, value)

    def _rpc_block(self):
        runtime._apply_config("TESTACCOUNT")
        return self._captured["rpc"]

    def test_absent_when_not_explicit(self):
        runtime.RPC_BACKGROUND_THREADS = False
        runtime.RPC_BACKGROUND_THREADS_EXPLICIT = False

        self.assertNotIn("background_threads", self._rpc_block())

    def test_absent_when_not_explicit_even_if_true(self):
        runtime.RPC_BACKGROUND_THREADS = True
        runtime.RPC_BACKGROUND_THREADS_EXPLICIT = False

        self.assertNotIn("background_threads", self._rpc_block())

    def test_forwarded_when_explicit(self):
        runtime.RPC_BACKGROUND_THREADS = False
        runtime.RPC_BACKGROUND_THREADS_EXPLICIT = True

        self.assertIs(self._rpc_block().get("background_threads"), False)

    def test_forwarded_true_when_explicit(self):
        runtime.RPC_BACKGROUND_THREADS = True
        runtime.RPC_BACKGROUND_THREADS_EXPLICIT = True

        self.assertIs(self._rpc_block().get("background_threads"), True)


class ExplicitFlagTest(unittest.TestCase):
    def setUp(self):
        self._saved = {
            "RPC_BACKGROUND_THREADS": runtime.RPC_BACKGROUND_THREADS,
            "RPC_BACKGROUND_THREADS_EXPLICIT": runtime.RPC_BACKGROUND_THREADS_EXPLICIT,
        }

    def tearDown(self):
        for key, value in self._saved.items():
            setattr(runtime, key, value)

    def test_key_present_marks_explicit(self):
        runtime.configure_runtime_redis({"rpc_background_threads": False})
        self.assertTrue(runtime.RPC_BACKGROUND_THREADS_EXPLICIT)
        self.assertFalse(runtime.RPC_BACKGROUND_THREADS)

    def test_key_absent_marks_not_explicit(self):
        runtime.configure_runtime_redis({"transport": "zmq"})
        self.assertFalse(runtime.RPC_BACKGROUND_THREADS_EXPLICIT)


if __name__ == "__main__":
    unittest.main()
