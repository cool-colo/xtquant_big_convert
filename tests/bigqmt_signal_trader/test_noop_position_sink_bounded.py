# coding: utf-8
"""#441: the noop position sink must not grow without bound.

app.tick() publishes a snapshot on every adjust tick (~10/s). Non-redis
transports fall back to NoopPositionSyncSink, and a plain append kept every
snapshot forever -- measured as ~1MB/s of private memory on a reporter's
terminal until the pagefile filled the C drive. The fix keeps only the
latest snapshot; nothing in src reads the history.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.adapter_factory import NoopPositionSyncSink  # noqa: E402


class NoopSinkBoundedTest(unittest.TestCase):
    def test_publish_keeps_only_the_latest(self):
        sink = NoopPositionSyncSink()
        for index in range(100):
            sink.publish({"seq": index})
        self.assertEqual(len(sink.snapshots), 1)
        self.assertEqual(sink.snapshots[0]["seq"], 99)

    def test_outside_reference_sees_the_update(self):
        sink = NoopPositionSyncSink()
        held = sink.snapshots          # an existing reference must stay valid
        sink.publish({"seq": 1})
        sink.publish({"seq": 2})
        self.assertIs(held, sink.snapshots)
        self.assertEqual(held[-1]["seq"], 2)


if __name__ == "__main__":
    unittest.main()
