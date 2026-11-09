import ctypes
import os
import sys
import threading
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.transports.pipe_push_channel import PipeQuotePushChannel  # noqa: E402


class _WinTypes(object):
    DWORD = ctypes.c_ulong


class _FailingDll(object):
    def WriteFile(self, _handle, _payload, _length, _written, _overlapped):
        return 0

    def DisconnectNamedPipe(self, _handle):
        return 1

    def CloseHandle(self, _handle):
        return 1


class PipePushDeliveryTest(unittest.TestCase):
    def test_exec_frame_fails_when_publisher_is_stopped(self):
        channel = PipeQuotePushChannel(bind_address="unused")

        with self.assertRaisesRegex(RuntimeError, "not running"):
            channel.publish("exec:order", {"order_sys_id": "S1"})

    def test_exec_frame_fails_when_no_reader_is_connected(self):
        channel = PipeQuotePushChannel(bind_address="unused")
        channel._running = True

        with self.assertRaisesRegex(RuntimeError, "no connected readers"):
            channel.publish("exec:order", {"order_sys_id": "S1"})

    def test_exec_frame_fails_when_every_reader_write_fails(self):
        channel = PipeQuotePushChannel(bind_address="unused")
        channel._running = True
        channel._sub_handles = {1: threading.Lock(), 2: threading.Lock()}
        channel._dll = lambda: (_FailingDll(), _WinTypes)

        with self.assertRaisesRegex(RuntimeError, "every reader"):
            channel.publish("exec:trade", {"trade_id": "T1"})

        self.assertEqual(channel.publisher_subscriber_count(), 0)

    def test_quote_frame_remains_best_effort_without_readers(self):
        channel = PipeQuotePushChannel(bind_address="unused")
        channel._running = True

        self.assertEqual(channel.publish("SH,SZ", {"000001.SZ": {}}), 0)


if __name__ == "__main__":
    unittest.main()
