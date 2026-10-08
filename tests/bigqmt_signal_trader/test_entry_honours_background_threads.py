# coding: utf-8
"""#188: the pure-zmq entries must not overwrite an explicit
rpc_background_threads.

#183 lets zmq/mysql opt into the adjust-thread drain by setting the key to
False, worth 4-6x on the live terminal (ping 405 -> 95ms, positions 607 ->
95ms). But BIGQMT_ZMQ_DRYRUN sets BIGQMT_FORCE_TRANSPORT = "zmq", and the
entry then assigned rpc_background_threads = True unconditionally -- so the
deployments that most want low latency were the ones that could not have it,
and a user who set False in their local config saw it silently ignored.

Entry files are exec'd by QMT with its own globals, so the suite pins them by
reading the source, the same way test_entry_encoding.py does. The behavioural
half -- that an explicit False survives to the resolver -- is covered by
test_transport_selection.py.
"""
import io
import os
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FILES = (
    os.path.join(ROOT, "src", "BIGQMT_REDIS_DRYRUN.py"),
    os.path.join(ROOT, "tools", "build_no_redis_single_file_flat.py"),
)


class EntryHonoursExplicitBackgroundThreadsTest(unittest.TestCase):
    def _source(self, path):
        return io.open(path, encoding="utf-8").read()

    def test_no_entry_assigns_background_threads_unconditionally(self):
        for path in FILES:
            source = self._source(path)
            bad = '["rpc_background_threads"] = True'
            self.assertNotIn(
                bad, source,
                "%s overwrites an explicit rpc_background_threads; use "
                "setdefault so a configured False still opts into the drain "
                "(#188)" % os.path.basename(path))

    def test_each_entry_still_fills_in_the_historical_default(self):
        """setdefault, not deletion: no key at all must still mean True."""
        for path in FILES:
            source = self._source(path)
            self.assertIn(
                'setdefault("rpc_background_threads", True)', source,
                "%s must still default rpc_background_threads to True when the "
                "local config never set it" % os.path.basename(path))

    def test_the_no_redis_banner_reports_the_value_it_actually_used(self):
        """The banner printed background_threads=True even when it was False.

        A log line that always says True is worse than no log line: it is the
        first thing anyone checks when the drain "did not work".
        """
        source = self._source(FILES[1])
        self.assertNotIn("no-redis mode: transport=zmq background_threads=True",
                         source)


if __name__ == "__main__":
    unittest.main()
