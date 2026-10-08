# coding: utf-8
"""A single-file build must carry the answers it was generated from (#153).

Reported by @simonfantasy: bigqmt-init was answered with the QMT machine's LAN
address, the generated bigqmt_signal_trader_local_config.py correctly said

    "zmq": {"bind_address": "tcp://0.0.0.0:15618"}

and the running FLAT build printed

    [bigqmt_rpc] zmq started bound=tcp://127.0.0.1:15618

They worked around it by editing DEFAULT_ZMQ_HOST in the generated file, and
their diagnosis was right: "QMT 实际上没有碰 local_config 这段代码".

A single-file deployment never reads local_config.py from disk -- it
synthesises that module from the config block embedded at the top of the build
(_load_local_config builds it out of _SHELL_CONFIG). So the embedded block IS
the config, and render_single_file_config_block was emitting neither key that
matters here:

  * no "zmq", so ZmqTransport fell back to DEFAULT_ZMQ_HOST = "127.0.0.1";
  * no "transport" either. The no-redis FLAT build forces zmq afterwards so it
    survived that, but the base64 single_file build does not -- answering
    transport=zmq there produced a server running redis while the client spoke
    zmq, which is precisely the "客户端 transport 和服务端不匹配" timeout that
    the end-to-end ping check prints.

The test that would have caught it is the structural one below: whatever the
server config emits, the single-file block has to emit too, because for that
deployment there is nowhere else for it to come from.
"""

import os
import re
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader import init_config


def answers(**overrides):
    base = dict(
        account_id="8886800558",
        account_type="STOCK",
        host="127.0.0.1",
        port=6379,
        db=5,
        username="",
        password="",
        allow_order_methods=False,
        transport="zmq",
        deployment="single_file_no_redis",
    )
    base.update(overrides)
    return base


def keys_of(block):
    """Top-level keys of the rendered BIGQMT_REDIS_CONFIG literal."""
    return set(re.findall(r'^\s{4}"([a-z_]+)"\s*:', block, re.M))


class BindAddressTest(unittest.TestCase):
    def test_a_remote_qmt_host_binds_all_interfaces(self):
        """The report, exactly: 0.0.0.0, not 127.0.0.1."""
        block = init_config.render_single_file_config_block(
            answers(host="192.168.1.50"))

        self.assertIn('"zmq": {"bind_address": "tcp://0.0.0.0:15618"}', block)

    def test_a_local_host_stays_on_loopback(self):
        """Binding all interfaces by default would expose an unauthenticated
        RPC port on every machine that only ever needed loopback."""
        block = init_config.render_single_file_config_block(answers())

        self.assertIn('"zmq": {"bind_address": "tcp://127.0.0.1:15618"}', block)

    def test_the_port_follows_the_account_id(self):
        block = init_config.render_single_file_config_block(
            answers(account_id="5556009168"))

        port = init_config._zmq_default_port("5556009168")
        self.assertIn('tcp://127.0.0.1:%d' % port, block)

    def test_localhost_and_empty_are_treated_as_local(self):
        for host in ("localhost", "", "  ", "127.0.0.1"):
            block = init_config.render_single_file_config_block(
                answers(host=host))

            self.assertIn("tcp://127.0.0.1:", block, repr(host))

    def test_redis_transport_emits_no_zmq_block(self):
        block = init_config.render_single_file_config_block(
            answers(transport="redis"))

        self.assertNotIn('"zmq"', block)


class TransportKeyTest(unittest.TestCase):
    """The second missing key, and the more dangerous one."""

    def test_the_answered_transport_is_written_down(self):
        for transport in ("zmq", "redis"):
            block = init_config.render_single_file_config_block(
                answers(transport=transport))

            self.assertIn('"transport": %r,' % transport, block, transport)

    def test_a_zmq_answer_never_produces_a_build_that_runs_redis(self):
        """The base64 single_file build does not force zmq the way the FLAT
        one does, so without this key the server ran redis while the client
        spoke zmq -- a ping timeout blamed on 'transport 不匹配'."""
        block = init_config.render_single_file_config_block(
            answers(transport="zmq", deployment="single_file"))

        self.assertNotIn('"transport": \'redis\'', block)
        self.assertIn('"transport": \'zmq\',', block)


class NothingIsLostVersusTheServerConfigTest(unittest.TestCase):
    """The structural check: a single-file build has no second source.

    render_server_config writes a file the package deployment reads at run
    time. A single-file build reads nothing, so every key the server config
    carries has to survive into the embedded block or it is gone silently.
    """

    KNOWN_ONLY_IN_SERVER_CONFIG = frozenset()

    def _blocks(self, **kw):
        a = answers(**kw)
        return (init_config.render_server_config(a),
                init_config.render_single_file_config_block(a))

    def test_the_single_file_block_carries_every_server_config_key(self):
        for transport in ("zmq", "redis"):
            server, single = self._blocks(transport=transport)

            missing = (keys_of(server) - keys_of(single)
                       - self.KNOWN_ONLY_IN_SERVER_CONFIG)
            self.assertEqual(missing, set(),
                             "lost from the %s single-file build: %s"
                             % (transport, sorted(missing)))

    def test_the_two_agree_on_the_bind_address(self):
        """They disagreed: the file said 0.0.0.0 and the build bound 127.0.0.1."""
        server, single = self._blocks(host="192.168.1.50")

        for text in (server, single):
            self.assertIn('"zmq": {"bind_address": "tcp://0.0.0.0:15618"},', text)

    def test_the_two_agree_on_the_transport(self):
        server, single = self._blocks(transport="zmq")

        for text in (server, single):
            self.assertIn('"transport": \'zmq\',', text)


class StillValidPythonTest(unittest.TestCase):
    """The block is spliced into a generated file, so it has to parse."""

    def test_the_block_compiles(self):
        import ast

        for transport in ("zmq", "redis"):
            block = init_config.render_single_file_config_block(
                answers(transport=transport))

            ast.parse(block)

    def test_it_evaluates_to_the_expected_config(self):
        namespace = {}
        exec(init_config.render_single_file_config_block(
            answers(host="192.168.1.50")), namespace)

        config = namespace["BIGQMT_REDIS_CONFIG"]
        self.assertEqual(config["transport"], "zmq")
        self.assertEqual(config["zmq"]["bind_address"], "tcp://0.0.0.0:15618")

    def test_the_injector_still_finds_and_replaces_the_block(self):
        """inject_single_file_config locates the block by its first line and
        its terminating brace; a new key inside must not break that."""
        source = (
            "# header\n"
            'BIGQMT_ACCOUNT_ID = "YOUR_ACCOUNT_ID"\n'
            "\n"
            'BIGQMT_ACCOUNT_TYPE = "STOCK"\n'
            "\n"
            "BIGQMT_REDIS_CONFIG = {\n"
            '    "host": "127.0.0.1",\n'
            "}\n"
            "# tail\n"
        )

        result = init_config.inject_single_file_config(source, answers())

        self.assertIn("# header", result)
        self.assertIn("# tail", result)
        self.assertIn('"zmq": {"bind_address"', result)
        self.assertNotIn("YOUR_ACCOUNT_ID", result)


if __name__ == "__main__":
    unittest.main()
