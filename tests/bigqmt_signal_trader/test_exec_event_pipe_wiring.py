# coding: utf-8
import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

import bigqmt_signal_trader_redis_rpc_runtime as runtime
import bigqmt_signal_trader_strategy as strategy


class RuntimeExecEventPipeForwardingTest(unittest.TestCase):
    def setUp(self):
        self.saved = {
            name: getattr(runtime, name)
            for name in (
                "EXEC_EVENTS_TRANSPORT", "EXEC_EVENTS_PIPE_NAME",
                "EXEC_EVENTS_QUEUE_CAPACITY", "EXEC_EVENTS_CONNECT_TIMEOUT_SECONDS",
                "EXEC_EVENTS_ACK_TIMEOUT_SECONDS", "EXEC_EVENTS_MAX_BYTES",
                "RPC_BACKGROUND_THREADS_EXPLICIT", "REDIS_ENABLED_EXPLICIT",
                "configure", "set_account_id",
            )
        }
        self.captured = {}
        runtime.configure = lambda **kwargs: self.captured.update(kwargs)
        runtime.set_account_id = lambda account_id: None

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(runtime, name, value)

    def test_flat_config_fields_are_forwarded_to_nested_exec_events(self):
        runtime.configure_runtime_redis({
            "exec_events_transport": "pipe",
            "exec_events_pipe_name": "custom_events",
            "exec_events_queue_capacity": 99,
            "exec_events_connect_timeout_seconds": 0.4,
            "exec_events_ack_timeout_seconds": 3.0,
            "exec_events_max_bytes": 12345,
        })

        block = self.captured["exec_events"]
        self.assertEqual(block["transport"], "pipe")
        self.assertEqual(block["pipe_name"], "custom_events")
        self.assertEqual(block["queue_capacity"], 99)
        self.assertEqual(block["connect_timeout_seconds"], 0.4)
        self.assertEqual(block["ack_timeout_seconds"], 3.0)
        self.assertEqual(block["max_event_bytes"], 12345)


class StrategySinkSelectionTest(unittest.TestCase):
    def test_pipe_transport_wins_over_redis_and_quote_push(self):
        sentinel = object()
        saved = strategy._exec_event_pipe_sink_instance
        strategy._exec_event_pipe_sink_instance = sentinel
        try:
            selected = strategy._exec_event_sink({
                "exec_events": {"enabled": True, "transport": "pipe"},
            })
        finally:
            strategy._exec_event_pipe_sink_instance = saved

        self.assertIs(selected, sentinel)


if __name__ == "__main__":
    unittest.main()
