# coding: utf-8
"""The strategy name on an order is the user's to choose (#154).

@kingtsi asked about QMT's 报单来源 column: "这个报单来源的信息是qmt自动上报的吗？
能否处理掉呢，隐私信息太多了."

Measured on the live terminal rather than guessed, with a masked shape report
(describe_trade_detail_fields shape_fields=..., which never returns the value):

    m_strSource         13 rows empty, 3 rows  mask aaaaaa_aaaaa_aaaaaa
    m_strStrategyName   16 rows empty

Letters and underscores, no digits, no '-', no '|', no '{}'. So on this
terminal 报单来源 carries the strategy name and nothing else -- the bridge puts
no machine identifier anywhere (there is no uuid1/getnode/gethostname in src/),
and passorder takes exactly two caller strings: strategy_name and the remark.

Which makes the answerable half of the request "let the user choose the string".
Per call that already worked -- strategy_name= has always won. What was missing
is a default: "bigqmt_rpc" was hardcoded at three sites, so every unnamed order
announced the bridge on a screen the reporter reads.

The empty string has to survive as a real answer here: it is how you get the
column blank, the way a hand-placed order looks. A .get(key, default) anywhere
along the path would swallow it back into "bigqmt_rpc".
"""

import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.redis_rpc import (
    DEFAULT_ORDER_STRATEGY_NAME,
    BigQmtRpcHandlers,
)


def _handlers(**kwargs):
    return BigQmtRpcHandlers(
        account_id="acct", market_data=None, position_provider=None, **kwargs)


class DefaultTest(unittest.TestCase):
    def test_unset_keeps_the_name_existing_deployments_already_see(self):
        """Changing it silently would rename every order in the 委托 list."""
        self.assertEqual(_handlers().default_strategy_name,
                         DEFAULT_ORDER_STRATEGY_NAME)

    def test_a_chosen_name_is_used(self):
        self.assertEqual(
            _handlers(default_strategy_name="my_book").default_strategy_name,
            "my_book")

    def test_the_empty_string_survives(self):
        """The whole point of the request: leave 报单来源 blank."""
        self.assertEqual(
            _handlers(default_strategy_name="").default_strategy_name, "")

    def test_none_means_unset_not_blank(self):
        """None is 'the config said nothing', which is different from ''."""
        self.assertEqual(_handlers(default_strategy_name=None).default_strategy_name,
                         DEFAULT_ORDER_STRATEGY_NAME)


class NoHardcodedFallbackLeftTest(unittest.TestCase):
    """Three sites used to spell the default out; a missed one is invisible."""

    def _source(self):
        import inspect

        from bigqmt_signal_trader import redis_rpc

        return inspect.getsource(redis_rpc)

    def test_the_name_is_spelled_out_in_exactly_one_place(self):
        """The constant. Anywhere else is a site that ignores the config."""
        lines = [line for line in self._source().splitlines()
                 if '"bigqmt_rpc"' in line
                 and not line.lstrip().startswith("#")]

        self.assertEqual(
            [line.strip() for line in lines],
            ['DEFAULT_ORDER_STRATEGY_NAME = "bigqmt_rpc"'])

    def test_the_old_signal_trader_fallback_is_gone_too(self):
        self.assertNotIn('params.get("strategy_name", "bigqmt_signal_trader")',
                         self._source())

    def test_every_order_path_reads_the_attribute(self):
        source = self._source()

        self.assertGreaterEqual(source.count("self.default_strategy_name"), 3)


class RuntimeConfigTest(unittest.TestCase):
    """rpc_default_strategy_name has to survive the runtime's own plumbing."""

    def setUp(self):
        import bigqmt_signal_trader_redis_rpc_runtime as runtime

        self.runtime = runtime
        self._saved = runtime.RPC_DEFAULT_STRATEGY_NAME

    def tearDown(self):
        self.runtime.RPC_DEFAULT_STRATEGY_NAME = self._saved

    def test_the_config_key_reaches_the_module_global(self):
        """`global` had to name it: without that the assignment inside
        configure_runtime_redis binds a local and does nothing at all."""
        self.runtime.configure_runtime_redis(
            {"rpc_default_strategy_name": "my_book", "transport": "zmq"})

        self.assertEqual(self.runtime.RPC_DEFAULT_STRATEGY_NAME, "my_book")

    def test_an_empty_string_is_not_swallowed_by_a_default(self):
        self.runtime.configure_runtime_redis(
            {"rpc_default_strategy_name": "", "transport": "zmq"})

        self.assertEqual(self.runtime.RPC_DEFAULT_STRATEGY_NAME, "")

    def test_absent_leaves_it_unset(self):
        self.runtime.RPC_DEFAULT_STRATEGY_NAME = None
        self.runtime.configure_runtime_redis({"transport": "zmq"})

        self.assertIsNone(self.runtime.RPC_DEFAULT_STRATEGY_NAME)

    def test_the_rpc_block_forwards_it(self):
        import inspect

        source = inspect.getsource(self.runtime)

        self.assertIn('"default_strategy_name": RPC_DEFAULT_STRATEGY_NAME,',
                      source)

    def test_the_strategy_passes_it_without_a_default(self):
        """rpc_config.get(key) not .get(key, ...): "" must reach the handler."""
        import io

        path = os.path.join(ROOT, "src", "bigqmt_signal_trader_strategy.py")
        source = io.open(path, encoding="utf-8").read()

        self.assertIn(
            'default_strategy_name=rpc_config.get("default_strategy_name")',
            source)


class DocumentedWhereItIsReadTest(unittest.TestCase):
    def _example(self):
        import io

        path = os.path.join(ROOT, "src",
                            "bigqmt_signal_trader_local_config.example.py")
        return io.open(path, encoding="utf-8").read()

    def test_the_knob_is_in_the_example_config(self):
        self.assertIn("rpc_default_strategy_name", self._example())

    def test_the_example_says_where_the_string_shows_up(self):
        """Someone tuning this for privacy needs to know it is a visible
        column, not an internal tag."""
        self.assertIn("报单来源", self._example())

    def test_it_ships_commented_out(self):
        """Uncommenting is a decision; a live default would rename every
        order in every existing deployment."""
        for line in self._example().splitlines():
            if ("rpc_default_strategy_name" in line
                    and "#" not in line.split("rpc_default_strategy_name")[0]):
                self.fail("live in the example: %r" % line)


if __name__ == "__main__":
    unittest.main()
